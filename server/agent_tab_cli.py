"""Start tabs and manage review links from an agent's own Tether pane."""
import argparse
import http.client
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import auth
from ask_bridge import install_settings

PATH = '/mobile/api/agent-tab'
FINISHED = 0
ERROR = 1
TIMEOUT = 3
GONE = 4
NEEDS_INPUT = 5
NO_TURN = 6
POLL = 2


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def arguments(argv):
    parser = Parser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    start = actions.add_parser('start')
    start.add_argument('--agent', choices=('claude', 'codex'))
    start.add_argument('--model')
    start.add_argument('--effort')
    review = start.add_mutually_exclusive_group()
    review.add_argument('--review-it', action='store_true')
    review.add_argument('--reviews-me', action='store_true')
    start.add_argument('--context', action='store_true')
    start.add_argument('--name')
    start.add_argument('--role', choices=('advisor', 'second-advisor'), help='name the tab after this tab and mark it with the role icon')
    start.add_argument('--cwd')
    start.add_argument('--worktree', metavar='BRANCH')
    start.add_argument('--stack', '--group', dest='group', metavar='NAME', help='put the new tab in this stack of the session, made if missing (Unstacked or Main = no stack); --group is the old name')
    start.add_argument('task', nargs='?', default='')
    stop = actions.add_parser('stop')
    stop.add_argument('--worker', required=True)
    stop.add_argument('--reviewer')
    link = actions.add_parser('link')
    link.add_argument('--worker', required=True)
    link.add_argument('--reviewer', required=True)
    actions.add_parser('list')
    send = actions.add_parser('send')
    send.add_argument('--pane', required=True)
    send.add_argument('text')
    close = actions.add_parser('close')
    close.add_argument('--pane', required=True)
    wait = actions.add_parser('wait')
    wait.add_argument('--pane', required=True)
    wait.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    body = {'action': args.action}
    if args.action == 'start':
        message = sys.stdin.read() if args.task == '-' else args.task
        if not args.reviews_me and not message.strip():
            parser.error('a task is required unless --reviews-me is set')
        body.update(message=message, context=args.context,
                    review='new' if args.reviews_me else 'source' if args.review_it else False)
        for key in ('agent', 'model', 'effort', 'name', 'role', 'cwd', 'worktree', 'group'):
            value = getattr(args, key)
            if value is not None:
                body[key] = os.path.abspath(value) if key == 'cwd' else value
    elif args.action in ('stop', 'link'):
        body['worker'] = args.worker
        if args.reviewer is not None:
            body['reviewer'] = args.reviewer
    elif args.action == 'send':
        message = sys.stdin.read() if args.text == '-' else args.text
        if not message.strip():
            parser.error('a message is required')
        body.update(pane=args.pane, message=message)
    elif args.action == 'close':
        body['pane'] = args.pane
    elif args.action == 'wait':
        if not 1 <= args.timeout <= 600:
            parser.error('timeout must be 1 to 600 seconds')
        body.update(pane=args.pane, timeout=args.timeout)
    return body


def http_error(exc):
    error = str(exc.code)
    try:
        reply = json.loads(exc.read(262144))
        if isinstance(reply, dict) and isinstance(reply.get('error'), str):
            error = reply['error']
    except (ValueError, OSError):
        pass
    return error


def main(argv=None):
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        print('Run this inside a Tether tab.', file=sys.stderr)
        return 1
    try:
        body = arguments(argv)
    except ValueError as exc:
        print(f'Tether tab error: {exc}', file=sys.stderr)
        return 1
    body['session'] = pane
    try:
        port = int(os.environ.get('MOBILE_TMUX_PORT') or install_settings()['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Invalid port.')
        secret = auth.TOKEN_FILE.read_text().strip()
        if not secret:
            raise ValueError('Local hook authentication required.')
        credential = auth.session_value(secret)
        url = f'http://127.0.0.1:{port}'
        waiting = body['action'] == 'wait'
        seconds = body.pop('timeout') if waiting else None
        request = Request(url + PATH, json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Origin': url, 'X-Codex-Mobile': '1',
            'X-Tether-Hook': credential, 'Cookie': auth.COOKIE + '=' + credential})
        opener = build_opener(ProxyHandler({}))
        if not waiting:
            with opener.open(request, timeout=150 if 'worktree' in body else 60 if body['action'] == 'send' else 30) as response:
                result = json.loads(response.read(262144))
            print(json.dumps(result))
            return FINISHED
        deadline = time.monotonic() + seconds
        state, error = None, None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = {'status': 'timeout', 'pane': body['pane'], 'state': state, 'timeout': seconds}
                if error is not None:
                    result['error'] = error
                print(json.dumps(result))
                return TIMEOUT
            try:
                with opener.open(request, timeout=min(10, max(1, remaining))) as response:
                    result = json.loads(response.read(2 * 1024 * 1024))
                error = None
                state = result.get('state', state)
                if result['status'] != 'running':
                    code = {'finished': FINISHED, 'gone': GONE, 'needs_input': NEEDS_INPUT, 'no_turn': NO_TURN}.get(result['status'])
                    if code is None:
                        raise ValueError('Invalid helper status.')
                    print(json.dumps(result))
                    return code
            except HTTPError as exc:
                if exc.code != 503:
                    raise
                error = http_error(exc)
            except (OSError, URLError, http.client.HTTPException) as exc:
                error = str(exc)
            time.sleep(min(POLL, max(0, deadline - time.monotonic())))
    except HTTPError as exc:
        print(f'Tether tab error: {http_error(exc)}', file=sys.stderr)
    except (OSError, URLError, ValueError, KeyError):
        print('Tether tab error: Could not contact the local Tether server.', file=sys.stderr)
    return ERROR


if __name__ == '__main__':
    sys.exit(main())
