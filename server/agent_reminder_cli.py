"""Set and manage reminders for Mak from an agent's tab. Tether delivers each one at its time (push, urgent ping,
Waiting on you). It works on the reminder file directly: a Codex sandbox blocks the Tether port, not that folder.

Times: "2026-11-19 10:00" (Asia/Kolkata unless it has an offset), "10:00" (next), "tomorrow 10:00", "in 90m", "in 2h".
Add --every day|weekday|week|month|year to repeat a reminder when you mark it Done.
Use --interval N to repeat every N units (default 1). Use --last-day with --every month.
"""
import argparse
import os
import sys
from calendar import month_abbr
from datetime import datetime, timezone

import agent_backlog_cli
import reminders
from projects import ProjectError


def shown(record, now=None):
    zone = datetime.fromtimestamp(record['due'], reminders.ZONE).strftime('%Y-%m-%d %H:%M %Z')
    utc = datetime.fromtimestamp(record['due'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    state = record['state'] + (' (late)' if record.get('late') and record['state'] == 'due' else '')
    rule = record.get('repeat')
    if rule:
        every, interval = rule['every'], rule.get('interval', 1)
        state += '  every ' + (every if interval == 1 else f'{interval} {every}s')
        if every == 'week':
            weekday = datetime.fromtimestamp(rule['anchor'], reminders.repeat_zone(rule)).strftime('%a')
            state += f' ({weekday})'
        elif every == 'month':
            label = 'last day' if rule['day'] == -1 else f'day {rule["day"]}'
            state += f' ({label})'
        elif every == 'year':
            month = month_abbr[rule['month']]
            label = f'last day of {month}' if rule['day'] == -1 else f'{rule["day"]} {month}'
            state += f' ({label})'
    link = ' · '.join(f'{key} {value}' for key, value in (record.get('link') or {}).items() if key != 'thread')
    return f'{record["id"]}  {zone} ({utc})  {state}  {record["text"]}' + (f'  [{link}]' if link else '')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add', help='set a reminder for Mak')
    add.add_argument('text', help=f'what to remind Mak about, {reminders.TEXT_LIMIT} characters at most')
    add.add_argument('--at', required=True, help='when: "2026-11-19 10:00", "tomorrow 10:00", "in 2h"')
    add.add_argument('--every', choices=reminders.RULES, help='repeat each time the reminder is marked done')
    add.add_argument('--interval', type=int, help='repeat every N units (default: 1)')
    add.add_argument('--last-day', action='store_true', help='repeat on the last day of the month')
    add.add_argument('--project', help="link a project (its repository folder name); this tab's project when left out")
    add.add_argument('--no-project', action='store_true', help='link no project')
    add.add_argument('--pane', help='link a tab, like %%12; use "$TMUX_PANE" for this tab')
    add.add_argument('--item', help='link a backlog item, like B12')
    listed = commands.add_parser('list', help='upcoming and due reminders; --all adds done ones')
    listed.add_argument('--all', action='store_true')
    for name, text in (('done', 'mark a reminder done'), ('delete', 'delete a reminder')):
        sub = commands.add_parser(name, help=text)
        sub.add_argument('id', help='the reminder ID, like R3')
    snooze = commands.add_parser('snooze', help='move a reminder to a later time')
    snooze.add_argument('id')
    snooze.add_argument('--until', required=True, help='when: "in 1h", "tomorrow 10:00", "2026-11-19 10:00"')
    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            repeat = None
            if args.every:
                repeat = {'every': args.every, 'last_day': args.last_day}
                if args.interval is not None:
                    repeat['interval'] = args.interval
            elif args.last_day:
                raise reminders.ReminderError('Use --last-day only with --every month.')
            elif args.interval is not None:
                raise reminders.ReminderError('Use --interval with --every.')
            link = {'item': args.item.upper() if args.item else None,
                    'project': None if args.no_project else (args.project if args.project is not None else agent_backlog_cli.project_name())}
            if args.pane:
                link['pane'] = args.pane
                if args.pane == os.environ.get('TMUX_PANE'):
                    agent = agent_backlog_cli.guess_agent()
                    link['thread'] = os.environ.get(agent_backlog_cli.CHAT_ENV[agent]) or None
            who = agent_backlog_cli.source(agent_backlog_cli.guess_agent())
            record = reminders.add(args.text, reminders.parse_when(args.at), who, link, repeat=repeat)
            print('Added: ' + shown(record))
        elif args.command == 'list':
            items = reminders.listing()['reminders']
            items = [r for r in items if args.all or r['state'] != 'done']
            print('\n'.join(shown(r) for r in sorted(items, key=lambda r: r['due'])) or 'No reminders.')
        elif args.command == 'snooze':
            record = reminders.change(args.id, 'snooze', {'until': reminders.parse_when(args.until)}, by_id=True)
            print('Snoozed: ' + shown(record))
        elif args.command == 'done':
            record = reminders.change(args.id, 'done', by_id=True)
            print('Done: ' + shown(record))
            if record['next_id']:
                upcoming = next((item for item in reminders.listing()['reminders'] if item['id'] == record['next_id']), None)
                if upcoming:
                    print('Next: ' + shown(upcoming))
        else:
            reminders.change(args.id, 'delete')
            print(f'Deleted {args.id.upper()}.')
    except (reminders.ReminderError, ProjectError, OSError) as exc:
        print(f'Reminder error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
