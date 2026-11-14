"""Triage ledger for team Chat bug reports, kept as work board items (`tether triage`).

  tether triage --project P --view FILE                 Chat checked-up-to mark + every open item (start here)
  ... open                                              open items, one line each (latest line per item)
  ... last [N]                                          newest N lines (default 15)
  ... show ID                                           every line for one item, e.g. T-003
  ... new NAME --msgs IDS [--window W] [--note TEXT]    open an item; prints its id
  ... add ID STATUS TEXT [--ref REF] [--option O ...]   record a step; REF = where the detail lives
  ... seen TIME                                         Chat feed checked up to TIME (UTC ISO, from the message)
  ... sync                                              name every item's tab after its latest status
  ... import                                            one time: bring the ledger file's open items onto the board

STATUS: handed triaging needs-mak ticket fixing fixed closed duplicate wontfix.
fixing moves the item to Building, fixed to Review, closed, duplicate and wontfix to Done. needs-mak asks Mak in
Needs you with TEXT as the question (answers: Go ahead, Hold, or your own with --option); the answer comes back to
this tab. The next step after it returns the item to where it was.
`add` names the item's triage tab "<name> · <status>". A Done item's window is then closed, unless you run `add`
from inside that window. FILE is a view: Tether writes it again after each command. Never edit it."""
import argparse
import sys

import agent_backlog_cli
import agent_item_cli
import work_items
import work_triage
from projects import ProjectError
from work_triage import TriageError, row_line

COMMANDS = ('open', 'last', 'show', 'new', 'add', 'seen', 'sync', 'import', 'help')


def arguments(argv):
    parser = argparse.ArgumentParser(prog='tether triage', add_help=False)
    parser.add_argument('--project', required=True)
    parser.add_argument('--view', required=True, help='the ledger view file')
    parser.add_argument('cmd', nargs='?')
    parser.add_argument('args', nargs='*')
    parser.add_argument('--msgs')
    parser.add_argument('--window')
    parser.add_argument('--note')
    parser.add_argument('--ref', default='')
    parser.add_argument('--option', action='append')
    parser.add_argument('--session', default='', help='import: the tmux session of the triage tabs')
    parser.add_argument('-h', '--help', action='store_true')
    return parser.parse_args(argv)


def run(args, out):
    if args.help or args.cmd in ('help', '-h'):
        return __doc__
    if args.cmd is not None and args.cmd not in COMMANDS:
        raise TriageError(f'Unknown command {args.cmd}.\n\n{__doc__}')
    ledger = work_triage.Ledger(args.project, args.view)
    state = ledger.state()
    if args.cmd != 'import' and state != 'ready':
        first = (f'{ledger.view.name} is still the old ledger.' if state == 'old' else
                 f'The import of {ledger.view.name} stopped part way.')
        raise TriageError(f'{first} Run the import: tether triage --project {args.project} --view {args.view} import')
    who = agent_backlog_cli.source(agent_backlog_cli.guess_agent())
    pane, chat = agent_item_cli.here()
    if args.cmd is None:
        rows = ledger.rows()
        shown = ledger.open_rows(rows)
        return '\n'.join([f'Chat checked up to: {ledger.seen_mark(rows)}', f'Open items: {len(shown)}',
                          '\n'.join(row_line(r) for r in shown) or '(none)'])
    if args.cmd == 'open':
        return '\n'.join(row_line(r) for r in ledger.open_rows()) or '(none)'
    if args.cmd == 'last':
        try:
            count = int(args.args[0]) if args.args else 15
        except ValueError:
            raise TriageError('last takes a number.') from None
        rows = ledger.rows()
        return '\n'.join(row_line(r) for r in (rows[-count:] if count > 0 else []))
    if args.cmd == 'show':
        if not args.args:
            raise TriageError('usage: show T-NNN')
        wanted = args.args[0].strip().upper()
        return '\n'.join(row_line(r) for r in ledger.rows() if r[1] == wanted) or 'no such item'
    if args.cmd == 'new':
        if not args.args or not args.msgs:
            raise TriageError('usage: new NAME --msgs IDS [--window W] [--note TEXT]')
        return ledger.new(args.args[0], args.msgs, args.window or '', args.note or '')
    if args.cmd == 'add':
        if len(args.args) < 3:
            raise TriageError('usage: add T-NNN STATUS TEXT [--ref REF]; STATUS one of '
                              + ' '.join(work_triage.STATUSES))
        status = args.args[1]
        item = ledger.add(args.args[0], status, ' '.join(args.args[2:]), args.ref, args.option, pane, chat, who)
        if status == 'needs-mak' and pane and not any(t['pane'] == pane for t in item['tabs']):
            item = work_items.link(item['id'], pane, 'triage', who=who, chat=chat, **agent_item_cli.tab(pane))
        rows = ledger.item_rows(item['id'])
        out.append(row_line(next(r for r in reversed(rows) if r[2] == status)))
        moved = work_triage.show_status(item, status, pane)
        return moved
    if args.cmd == 'seen':
        if not args.args:
            return ledger.seen_mark()
        return row_line(ledger.seen(' '.join(args.args)))
    if args.cmd == 'sync':
        lines = []
        for item in ledger.items().values():
            rows = [r for r in ledger.item_rows(item['id']) if r[2] in work_triage.STATUSES]
            if rows:
                lines.append(work_triage.show_status(item, rows[-1][2], pane))
        return '\n'.join(line for line in lines if line)
    if args.cmd == 'import':
        report = ledger.import_ledger()
        linked = []
        if args.session:
            for item_id in report['made']:
                item = ledger.items()[item_id]
                target = work_triage.link_window(item, args.session)
                if target:
                    work_items.link(item_id, target, 'triage', by='import', **agent_item_cli.tab(target))
                    linked.append(f'{item_id} {target}')
        return (f'Read {report["rows"]} lines. New items: {", ".join(report["made"]) or "none"}. '
                f'Lines added to items already on the board: {report["added"]}.'
                + (f' Late lines from the view: {report["late"]}.' if report['late'] else '')
                + (f'\nLinked tabs: {", ".join(linked)}.' if linked else ''))
    raise TriageError('Unknown command.')


def main(argv=None):
    args = arguments(sys.argv[1:] if argv is None else argv)
    out = []
    code = 0
    try:
        shown = run(args, out)
    except (TriageError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        code, shown = 1, ''
    except PermissionError:
        print(agent_item_cli.RESTART, file=sys.stderr)
        return 1
    for line in out + ([shown] if shown else []):
        print(line)
    if not args.help and args.cmd not in ('help', '-h'):
        try:
            problem = work_triage.Ledger(args.project, args.view).write_view()
        except (ProjectError, OSError) as exc:
            problem = f'Tether could not write the view ({exc}).'
        if problem:
            print(problem, file=sys.stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())
