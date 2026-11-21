#!/usr/bin/env python3
"""Plan, apply, or undo the one-time Tether task-tree sort."""
import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import task_tree_sort as sort


def main(argv=None, *, run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    plan = commands.add_parser('plan', help='write a move list and undo records without changing tmux')
    plan.add_argument('--out', required=True, type=Path)
    commands.add_parser('apply', help='apply a saved plan').add_argument('folder', type=Path)
    commands.add_parser('undo', help='restore the original records').add_argument('folder', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'plan':
            count = sort.save_plan(args.out, run=run)
            print(f'{count} records to update. Review {args.out / "move-list.md"}.')
        else:
            count = sort.replay(args.folder, undo=args.action == 'undo', run=run)
            verb = 'Restored' if args.action == 'undo' else 'Updated'
            print(f'{verb} {count} records.')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
