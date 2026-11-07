"""Read and change the shared backlog from an agent's tab. An added item records which tab and chat added it.
It works on the file directly: Codex's sandbox blocks the Tether port and the tmux socket, but not the backlog folder."""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import backlog
import paths
from projects import ProjectError

CHAT_ENV = {'claude': 'CLAUDE_CODE_SESSION_ID', 'codex': 'CODEX_THREAD_ID'}
CHAT = re.compile(r'[A-Za-z0-9_-]{1,80}')
TMUX_ENV = re.compile(r'.*,[0-9]+,([0-9]+)')
WORKTREE = re.compile(r'gitdir:\s*(.+?)/\.git/worktrees/')


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


def _clean(name):
    return re.sub(r'[^\w.\- ]+', '-', name).strip(' -')[:60]


def project_name(start=None):
    """The repository's folder name, the same from a subfolder or a worktree. No group for the home folder."""
    try:
        here = Path(start or os.getcwd()).resolve()
    except OSError:
        return ''
    home = Path(paths.HOME).resolve()
    if here == home:
        return ''
    for place in (here, *here.parents):
        if place == home:
            break
        marker = place / '.git'
        if marker.is_dir():
            return _clean(place.name)
        if marker.is_file():
            try:
                found = WORKTREE.match(marker.read_text(errors='replace'))
            except OSError:
                found = None
            return _clean(Path(found[1]).name if found else place.name)
    return _clean(here.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help="the inbox and this project's open items")
    add = commands.add_parser('add', help='add an open item to this project')
    add.add_argument('--agent', required=True, choices=sorted(CHAT_ENV))
    add.add_argument('text')
    detail = commands.add_parser('detail', help='add a line under an open item')
    detail.add_argument('item', help='part of the item text')
    detail.add_argument('text')
    done = commands.add_parser('done', help='tick an open item')
    done.add_argument('item', help='part of the item text')
    args = parser.parse_args(argv)
    project = project_name()
    try:
        if args.command == 'list':
            print(backlog.list_open(project))
        elif args.command == 'add':
            print(f'Added: {backlog.add(args.text, source(args.agent), project)}')
        elif args.command == 'detail':
            backlog.add_detail(args.item, args.text, project)
            print('Added the detail.')
        else:
            print(f'Done: {backlog.mark_done(args.item, project)}')
    except (ProjectError, OSError) as exc:
        print(f'Backlog error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
