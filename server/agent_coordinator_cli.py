"""Coordinator commands for the agent tab that routes the user's tasks to the other tabs (coordinator.py)."""
import argparse
import http.client
import json
import os
import sys
from urllib.error import HTTPError, URLError

import agent_tab_cli
import coordinator


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def arguments(argv):
    parser = Parser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    claim = actions.add_parser('claim', help='make this tab the coordinator of its session; prints the rules and the tabs')
    claim.add_argument('--events', choices=coordinator.MODES)
    claim.add_argument('--take', action='store_true', help='take the role from another tab; only when the user asks')
    actions.add_parser('release', help='end the coordinator role of this tab')
    events = actions.add_parser('events', help='which tab updates Tether sends to this tab')
    events.add_argument('mode', choices=coordinator.MODES)
    actions.add_parser('tabs', help='the agent tabs of this session: state, what each waits on, start of the last reply')
    reply = actions.add_parser('reply', help='the full last reply of one tab')
    reply.add_argument('--pane', required=True)
    send = actions.add_parser('send', help='send a message to a tab of this session')
    send.add_argument('--pane', required=True)
    send.add_argument('text')
    args = parser.parse_args(argv)
    body = {'action': args.action}
    if args.action == 'claim':
        if args.events:
            body['events'] = args.events
        if args.take:
            body['take'] = True
    elif args.action == 'events':
        body['events'] = args.mode
    elif args.action == 'reply':
        body['pane'] = args.pane
    elif args.action == 'send':
        message = sys.stdin.read() if args.text == '-' else args.text
        if not message.strip():
            parser.error('a message is required')
        body.update(pane=args.pane, message=message)
    return body


def show(result):
    """The rules as plain text (the agent reads them), the rest as JSON."""
    rules = result.pop('rules', None) if isinstance(result, dict) else None
    if rules:
        print(rules + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=1))


def main(argv=None):
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        print('Run this inside a Tether tab.', file=sys.stderr)
        return 1
    try:
        body = arguments(argv)
    except ValueError as exc:
        print(f'Tether coordinator error: {exc}', file=sys.stderr)
        return 1
    body['session'] = pane
    try:
        opener, request = agent_tab_cli.connection(body, coordinator.PATH)
        with opener.open(request, timeout=60 if body['action'] == 'send' else 30) as response:
            show(json.loads(response.read(2 * 1024 * 1024)))
        return 0
    except HTTPError as exc:
        print(f'Tether coordinator error: {agent_tab_cli.http_error(exc)}', file=sys.stderr)
    except (OSError, URLError, ValueError, KeyError, http.client.HTTPException):
        print('Tether coordinator error: Could not contact the local Tether server.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
