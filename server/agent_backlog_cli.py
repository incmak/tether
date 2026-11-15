"""Read and change the shared backlog from an agent's tab. An added item records which tab and chat added it.
It works on the file directly: Codex's sandbox blocks the Tether port and the tmux socket, but not the backlog folder."""
import argparse
import os
import re
import subprocess
import sys

import backlog
import paths
from projects import ProjectError

CHAT_ENV = {'claude': 'CLAUDE_CODE_SESSION_ID', 'codex': 'CODEX_THREAD_ID'}
CHAT = re.compile(r'[A-Za-z0-9_-]{1,80}')
TMUX_ENV = re.compile(r'.*,[0-9]+,([0-9]+)')
TITLE = 100  # an item's line is its title: the Backlog sheet shows it in full. The why and how go in --detail lines.


def tmux_session(pane):
    """The session the pane is in now (a merge can move it). In a sandbox, the session the pane started in."""
    try:
        shown = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'], capture_output=True, text=True, timeout=3)
        if shown.returncode == 0 and re.fullmatch(r'\$[0-9]+', shown.stdout.strip()):
            return shown.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    found = TMUX_ENV.fullmatch(os.environ.get('TMUX', ''))
    return f'${found[1]}' if found else ''


def source(agent):
    """Who added the item: "codex · tmux $3 · pane %12 · chat 019a". A value that is missing or looks wrong stays out.
    The agent names itself: a Codex started from a Claude tab also has Claude's chat variable."""
    parts = [agent]
    pane = os.environ.get('TMUX_PANE', '')
    if re.fullmatch(r'%[0-9]+', pane):
        session = tmux_session(pane)
        if session:
            parts.append(f'tmux {session}')
        parts.append(f'pane {pane}')
    chat = os.environ.get(CHAT_ENV[agent], '')
    if CHAT.fullmatch(chat):
        parts.append(f'chat {chat}')
    return ' · '.join(parts)


def project_name(start=None):
    return backlog.project_name(start)


def guess_agent():
    """No command needs --agent: a Codex tab has its thread variable. A Claude tab never does."""
    return 'codex' if CHAT.fullmatch(os.environ.get('CODEX_THREAD_ID', '')) else 'claude'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    shown = commands.add_parser('list', help="the inbox and this project's open items")
    add = commands.add_parser('add', help='add an open item to this project')
    add.add_argument('--agent', choices=sorted(CHAT_ENV), help='who adds it; found from the tab when left out')
    add.add_argument('--detail', action='append', default=[], help='a line under the item: the why, the files, the case; repeat for more')
    add.add_argument('text', help=f'a short title, {TITLE} characters at most')
    reword = commands.add_parser('reword', help="change an item's title; its ID stays")
    reword.add_argument('item', help='the item ID, like B12, or part of the item text')
    reword.add_argument('text', help=f'a short title, {TITLE} characters at most')
    move = commands.add_parser('move', help='move an item and its details to another project')
    move.add_argument('item', help='the item ID, like B12, or part of the item text')
    move.add_argument('to', help='the project: its repository folder name, like tether; "" for the inbox')
    detail = commands.add_parser('detail', help='add a line under an item')
    detail.add_argument('item', help='the item ID, like B12, or part of the item text')
    detail.add_argument('text')
    begin = commands.add_parser('start', help='mark an open item as in progress by this tab')
    begin.add_argument('item', help='the item ID, like B12, or part of the item text')
    begin.add_argument('--agent', choices=sorted(CHAT_ENV))
    archive = commands.add_parser('archive', help='move every finished item to backlog/DONE.md now')
    archive.add_argument('--dry-run', action='store_true', help='only count what would move')
    done = commands.add_parser('done', help='tick an open item, with what was done')
    done.add_argument('item', help='the item ID, like B12, or part of the item text')
    done.add_argument('result', nargs='?', default='', help='what was done: the commit, PR, or outcome')
    done.add_argument('--agent', choices=sorted(CHAT_ENV))
    for command in (shown, add):
        command.add_argument('--project', help="the project, by its repository folder name; this tab's project when left out")
    args = parser.parse_args(argv)
    project = project_name() if getattr(args, 'project', None) is None else backlog._clean(args.project)
    if args.command in ('add', 'reword') and len(' '.join(args.text.split())) > TITLE:
        print(f'Backlog error: the title is over {TITLE} characters. Write a short title; put the rest in '
              + ('--detail "..."' if args.command == 'add' else 'detail B12 "..."') + '.', file=sys.stderr)
        return 1
    try:
        if args.command == 'list':
            print(backlog.list_open(project))
        elif args.command == 'add':
            print(f'Added: {backlog.add(args.text, source(args.agent or guess_agent()), project, args.detail)}')
        elif args.command == 'reword':
            print(f'Reworded: {backlog.reword(args.item, args.text, project)}')
        elif args.command == 'move':
            print(f'Moved to {args.to or "the inbox"}: {backlog.move(args.item, args.to, project)}')
        elif args.command == 'detail':
            backlog.add_detail(args.item, args.text, project)
            print('Added the detail.')
        elif args.command == 'start':
            print(f'Started: {backlog.start(args.item, source(args.agent or guess_agent()), project)}')
        elif args.command == 'archive':
            count, before, after = backlog.archive_done(args.dry_run)
            verb = 'Would move' if args.dry_run else 'Moved'
            print(f'{verb} {count} finished items to DONE.md. The backlog: {before:,} -> {after:,} characters.')
        else:
            who = source(args.agent or guess_agent())
            print(f'Done: {backlog.mark_done(args.item, project, args.result, who)}')
    except (ProjectError, OSError) as exc:
        print(f'Backlog error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
