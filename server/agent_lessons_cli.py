"""Record feedback hints from an agent's own project tab."""
import argparse
import json
import os
import re
import subprocess
import sys

import codex_level
import lessons
import paths


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def arguments(argv):
    parser = Parser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    add = actions.add_parser('add')
    kind = add.add_mutually_exclusive_group(required=True)
    kind.add_argument('--avoid', dest='kind', action='store_const', const='avoid')
    kind.add_argument('--keep', dest='kind', action='store_const', const='keep')
    add.add_argument('--source', choices=('user', 'reviewer'), default='user')
    add.add_argument('--new', dest='force_new', action='store_true')
    add.add_argument('text')
    for action in ('reinforce', 'propose-global'):
        actions.add_parser(action).add_argument('id')
    actions.add_parser('list')
    return parser.parse_args(argv)


def context():
    pane = os.environ.get('TMUX_PANE', '')
    if not re.fullmatch(r'%[0-9]+', pane):
        raise ValueError('Run this inside a Tether project tab.')
    def shown(format):
        done = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, format],
                              capture_output=True, text=True, timeout=5)
        if done.returncode:
            raise ValueError('Could not read this tab.')
        return done.stdout.strip()
    if shown('#{@tether_helper}'):
        raise ValueError('Helper tabs cannot record lessons.')
    pid = shown('#{@project}')
    lessons.path(pid)
    command = shown('#{pane_current_command}').lower()
    agent = command if command in ('claude', 'codex') else ''
    return pid, agent


def main(argv=None):
    try:
        args = arguments(argv)
        pid, agent = context()
        if not lessons.enabled():
            print(json.dumps({'status': 'off'}))
            return 8
        if args.action == 'list':
            result = {'lessons': lessons.entries(pid)}
        elif args.action == 'add':
            text = sys.stdin.read() if args.text == '-' else args.text
            result = lessons.add(pid, text, args.kind, args.source, agent, args.force_new)
        else:
            if not re.fullmatch(r'[0-9a-f]{8}', args.id):
                raise ValueError('Choose a lesson ID.')
            row = (lessons.reinforce(pid, args.id) if args.action == 'reinforce'
                   else lessons.propose_global(pid, args.id))
            if row is None:
                raise ValueError('That lesson is gone.')
            result = {'status': 'reinforced' if args.action == 'reinforce' else 'proposed', 'lesson': row}
        print(json.dumps(result))
        return 9 if result.get('status') == 'similar' else 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
