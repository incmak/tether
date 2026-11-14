"""Work items on the Tether work board, from an agent's tab (`tether item ...`).

An item moves by stage: triage, needs (Needs you), ready, building, review, done, hold. Ask Mak with `ask`: the item
moves to Needs you, and Mak's answer comes back to this tab as a message. It works on the files directly: Codex's
sandbox blocks the Tether port and the tmux socket, but not the items folder."""
import argparse
import json
import os
import subprocess
import sys

import agent_backlog_cli
import groups
import paths
import work_items
from projects import ProjectError

RESTART = ('Tether could not write the items folder. A Codex tab started before the work board cannot write there: '
           'restart this tab, then try again.')


def _tmux(*args):
    try:
        shown = subprocess.run([paths.TMUX, *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ''
    return shown.stdout.rstrip('\n') if shown.returncode == 0 else ''


def tab(pane):
    """{'name', 'session', 'stack'} of a pane, as the sidebar shows it. Empty values when tmux is out of reach."""
    fields = _tmux('display-message', '-p', '-t', pane, '#{@chat_name}\t#{session_name}\t#{@group}\t#{@groups}').split('\t', 3)
    if len(fields) != 4:
        return {'name': '', 'session': '', 'stack': ''}
    name, session, label, raw = fields
    stack = next((g.get('name', '') for g in groups.parse(raw) if g['id'] == label), '') if label else ''
    return {'name': name, 'session': session, 'stack': stack}


def here():
    """This tab: (pane, chat). Empty when the command runs outside tmux."""
    pane = os.environ.get('TMUX_PANE', '')
    pane = pane if work_items.PANE.fullmatch(pane) else ''
    agent = agent_backlog_cli.guess_agent()
    chat = os.environ.get(agent_backlog_cli.CHAT_ENV[agent], '')
    return pane, chat if work_items.CHAT.fullmatch(chat) else ''


def show(item, verbose=False):
    lines = [f'{item["id"]} [{work_items.STAGE_LABELS[item["stage"]]}] {item["title"]} (v{item["version"]})']
    if item.get('ask'):
        lines.append(f'  Asks: {item["ask"]["q"]} [{" / ".join(item["ask"]["options"])}]')
    if verbose:
        if item['refs']:
            lines.append('  Links: ' + ', '.join(item['refs']))
        if item['qa']:
            lines.append('  Needs QA testing')
        for linked in item['tabs']:
            lines.append(f'  Tab {linked["pane"]} ({linked["role"]}) {linked.get("name", "")}')
        for entry in item['history'][-12:]:
            lines.append(f'  - {entry["text"]}' + (f' ({entry["ref"]})' if entry.get('ref') else ''))
    return '\n'.join(lines)


def arguments(argv):
    parser = argparse.ArgumentParser(prog='tether item', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    shown = commands.add_parser('list', help='open items, all projects or one')
    shown.add_argument('--project')
    shown.add_argument('--stage', choices=work_items.STAGES)
    shown.add_argument('--all', action='store_true', help='include Done and On hold')
    one = commands.add_parser('show', help='one item with its tabs and history')
    one.add_argument('item')

    made = commands.add_parser('new', help='create an item in Triage')
    made.add_argument('title')
    made.add_argument('--project', required=True)
    made.add_argument('--ref', action='append', default=[], help='a link: Jira key, PR, Teams message (repeat)')
    made.add_argument('--source', default='', help='where it came from, like "QA report, Teams"')
    made.add_argument('--qa', action='store_true', help='needs QA testing when Done')
    made.add_argument('--link', action='store_true', help='link this tab as the triage tab')
    made.add_argument('--id', help='keep an existing id, for an import')
    made.add_argument('--stage', default='triage', choices=[s for s in work_items.STAGES if s != 'needs'])

    asked = commands.add_parser('ask', help='ask Mak; the item moves to Needs you and the answer comes to this tab')
    asked.add_argument('item')
    asked.add_argument('question')
    asked.add_argument('--option', action='append', required=True, help='an answer button, like Build or Hold (repeat)')

    moved = commands.add_parser('stage', help='move an item to another stage')
    moved.add_argument('item')
    moved.add_argument('stage', choices=[s for s in work_items.STAGES if s != 'needs'])
    moved.add_argument('--text', default='', help='why, in one line')
    moved.add_argument('--ref', default='')

    linked = commands.add_parser('link', help='link a tab (this one by default) to the item')
    linked.add_argument('item')
    linked.add_argument('--pane')
    linked.add_argument('--role', default='other', choices=work_items.ROLES)
    gone = commands.add_parser('unlink', help='unlink a tab (this one by default)')
    gone.add_argument('item')
    gone.add_argument('--pane')

    noted = commands.add_parser('note', help='add a line to the history')
    noted.add_argument('item')
    noted.add_argument('text')
    noted.add_argument('--ref', default='')
    refs = commands.add_parser('ref', help='add links to the item')
    refs.add_argument('item')
    refs.add_argument('refs', nargs='+')
    qa = commands.add_parser('qa', help='set the Needs QA testing flag')
    qa.add_argument('item')
    qa.add_argument('value', choices=('on', 'off'))

    projects = commands.add_parser('project', help='list or add work projects')
    projects.add_argument('action', choices=('list', 'add'))
    for field in ('name', 'prefix', 'folder', 'triage', 'work', 'jira', 'qa-status', 'jira-user'):
        projects.add_argument('--' + field, default='')
    return parser.parse_args(argv)


def run(args):
    who = agent_backlog_cli.source(agent_backlog_cli.guess_agent())
    pane, chat = here()
    if args.command == 'list':
        items = work_items.board()['items']
        items = [i for i in items if (not args.project or i['project'] == args.project)
                 and (args.stage and i['stage'] == args.stage
                      or not args.stage and (args.all or i['stage'] not in ('done', 'hold')))]
        return '\n'.join(show(i) for i in items) or 'No items.'
    if args.command == 'show':
        return show(work_items.find(args.item)[1], verbose=True)
    if args.command == 'project':
        if args.action == 'list':
            found = work_items.registry()['projects']
            return '\n'.join(f'{p["name"]} ({p["prefix"]}-) {p["folder"]} · triage {p["triage"]} · work {p["work"]}'
                             + (f' · Jira {p["jira"]}' if p['jira'] else '') for p in found) or 'No projects.'
        data = {'name': args.name, 'prefix': args.prefix, 'folder': args.folder, 'triage': args.triage, 'work': args.work,
                'jira': args.jira, 'qaStatus': args.qa_status, 'jiraUser': args.jira_user}
        work_items.save_project(data, work_items.registry()['revision'], create=True)
        return f'Added project {args.name}.'
    if args.command == 'new':
        item = work_items.new(args.project, args.title, 'agent', who, args.ref, args.source, args.id, args.stage, args.qa)
        if args.link and pane:
            item = work_items.link(item['id'], pane, 'triage', who=who, chat=chat, **tab(pane))
        return show(item)
    if args.command == 'ask':
        if not pane:
            raise work_items.invalid('Run ask from a Tether tab: the answer comes back to it.')
        item = work_items.ask(args.item, args.question, args.option, pane, chat, who=who)
        if not any(t['pane'] == pane for t in item['tabs']):
            work_items.link(item['id'], pane, 'triage' if item.get('askFrom') == 'triage' else 'other', who=who, chat=chat, **tab(pane))
        if not chat:
            return show(item) + ('\nMak sees it in Needs you. Tether cannot tell which conversation runs in this tab, '
                                 'so the answer stays on the board: check it with `tether item show`.')
        return show(item) + '\nMak sees it in Needs you. The answer comes to this tab as a message.'
    if args.command == 'stage':
        return show(work_items.stage(args.item, args.stage, 'agent', who, args.text, ref=args.ref))
    if args.command in ('link', 'unlink'):
        target = args.pane or pane
        if not target:
            raise work_items.invalid('Give --pane, or run it from a Tether tab.')
        if args.command == 'unlink':
            return show(work_items.unlink(args.item, target, who=who))
        return show(work_items.link(args.item, target, args.role, who=who, chat=chat if target == pane else '', **tab(target)))
    if args.command == 'note':
        return show(work_items.note(args.item, args.text, 'agent', who, args.ref))
    if args.command == 'ref':
        return show(work_items.add_refs(args.item, args.refs, 'agent', who))
    if args.command == 'qa':
        return show(work_items.set_qa(args.item, args.value == 'on', 'agent', who))
    raise work_items.invalid('Unknown command.')


def main(argv=None):
    args = arguments(sys.argv[1:] if argv is None else argv)
    try:
        print(run(args))
    except ProjectError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except PermissionError:
        print(RESTART, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'{RESTART} ({exc.strerror or exc})', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
