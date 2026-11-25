#!/usr/bin/env python3
"""Agent access to Tether's shared browser, using only the standard library."""
import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


def note():
    command = shlex.join([sys.executable, str(Path(__file__).resolve())]) + ' ask "<what you need>"'
    return f'''## Tether shared browser
Tether runs one shared browser that the user can watch from desktop or phone.
- Playwright MCP already uses it. Do not pass --isolated or launch another browser.
- In scripts, connect instead of launching: Node `chromium.connectOverCDP(process.env.TETHER_CDP_URL)`, Python `p.chromium.connect_over_cdp(os.environ["TETHER_CDP_URL"])`. Use `browser.contexts[0]`. You get a private context; the user's logins are not in it.
- Limit: 3 tabs. Close tabs you no longer need.
- ask-signed "<why>" [--minutes N] [--url URL] gives you one tab with Mak's sign-ins: Mak's own Chrome when it is connected, else the server signed-in browser. It prints JSON with a CDP URL. When Mak allows agents in Settings, it answers at once; otherwise Mak approves on the phone.
- Use signed-in access only for a task Mak asked for.
- Before a risky or destructive step, stop and ask Mak in chat, then wait for a yes: send, post or reply as Mak; pay or buy; delete; change account, security or sharing settings; accept terms. You decide what else is risky. When unsure, ask.
- "reason":"busy" means other chats hold all signed-in tabs. Tell Mak, or ask again later.
- If the signed CDP URL returns 410 or its socket closes with "Access ended": when the time ran out, ask again if the task still needs it. When Mak ended it, tell Mak and do not ask again unless Mak says so.
- If you need the user in the browser (2FA code, captcha, a password you must not see), run `{command}`, then wait for the user to reply in chat.
- Sign-ins: the test browser is headless. Ask for signed-in access when the task needs Mak's account; Mak signs in through the panel.'''


def endpoint(cdp, action):
    url = urlsplit(cdp)
    if (url.scheme not in ('ws', 'wss', 'http', 'https') or not url.hostname
            or not url.port or url.username or url.password or url.query or url.fragment
            or not re.fullmatch(r'/cdp/[A-Za-z0-9_-]+', url.path)):
        raise ValueError('Invalid shared browser endpoint.')
    scheme = 'https' if url.scheme in ('wss', 'https') else 'http'
    return urlunsplit((scheme, url.netloc, url.path.replace('/cdp/', f'/{action}/', 1), '', ''))


def main(args=None):
    args = sys.argv[1:] if args is None else args
    if args and args[0] == 'ask-signed':
        return ask_signed(args[1:])
    if args == ['hook']:
        try:
            sys.stdin.read()
            if os.environ.get('TETHER_CDP_URL'):
                print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': note()}}))
        except Exception:
            pass  # A SessionStart hook must never print an error or stop the agent.
        return 0
    cdp = os.environ.get('TETHER_CDP_URL')
    if not cdp:
        print('Tether shared browser is not available in this shell.')
        return 1
    status = args == ['status']
    if not status and (len(args) != 2 or args[0] != 'ask' or not args[1].strip()):
        print('Usage: tether-browser ask "<text>" | status | hook')
        return 1
    try:
        data = None
        headers = {}
        if not status:
            body = {'text': args[1]}
            pane = os.environ.get('TMUX_PANE', '')
            if re.fullmatch(r'%[0-9]+', pane):
                body['pane'] = pane
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(endpoint(cdp, 'health' if status else 'ask'), data=data, headers=headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            if not status or exc.code != 503:
                raise
            response = exc
        with response:
            result = json.load(response)
        if status:
            if result.get('ok') is True:
                print('on')
                return 0
            reason = ' '.join(str(result.get('reason') or 'unavailable').split())
            print('off: ' + reason)
            return 1
        if not isinstance(result.get('id'), str) or not result['id']:
            raise ValueError('Invalid shared browser response.')
        print('Asked the user. Wait for their reply in chat.')
        return 0
    except Exception as exc:
        # Do not expose the endpoint's bearer token in exception messages.
        reason = f'HTTP {exc.code}' if isinstance(exc, HTTPError) else 'shared browser request failed'
        print(('off: ' if status else 'Tether browser error: ') + reason)
        return 1


def ask_signed(args):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)
    try:
        parser = Parser(add_help=False)
        parser.add_argument('text')
        parser.add_argument('--minutes', type=int, default=15)
        parser.add_argument('--url')
        parser.add_argument('--browser', type=lambda value: 'chrome' if value == 'mac' else value,
                            choices=('auto', 'chrome', 'server'), default='auto')
        values = parser.parse_args(args)
        if not values.text.strip() or not 1 <= values.minutes <= 60:
            raise ValueError('Invalid signed browser request.')
        body = {'text': values.text, 'minutes': values.minutes}
        body['browser'] = values.browser
        if values.url is not None:
            body['url'] = values.url
        pane = os.environ.get('TMUX_PANE', '')
        if re.fullmatch(r'%[0-9]+', pane):
            body['pane'] = pane
        request = Request(endpoint(os.environ.get('TETHER_CDP_URL', ''), 'ask-signed'),
                          data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=125) as response:
            result = json.load(response)
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise ValueError('Invalid signed browser response.')
        if result['ok']:
            if (not isinstance(result.get('cdp'), str) or not isinstance(result.get('tab'), str)
                    or type(result.get('expires')) not in (int, float)):
                raise ValueError('Invalid signed browser grant.')
        elif (result.get('reason') not in ('denied', 'timeout', 'busy', 'failed')
              or 'error' in result and not isinstance(result['error'], str)):
            raise ValueError('Invalid signed browser response.')
        print(json.dumps(result, separators=(',', ':')))
        return 0 if result['ok'] else 1
    except TimeoutError:
        result = {'ok': False, 'reason': 'timeout'}
    except Exception:
        result = {'ok': False, 'reason': 'request failed'}
    print(json.dumps(result, separators=(',', ':')))
    return 1


if __name__ == '__main__':
    sys.exit(main())
