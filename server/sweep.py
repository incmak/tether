"""A sweep over the agent tabs that work in a git worktree (B210): every 30 minutes, one line per finding.

A tab is swept when its folder is a linked git worktree (not the main checkout) and it is a Claude, Codex or
Antigravity tab that is not a reviewer or an advisor. Three findings:

- behind: the branch lacks commits of the repository's main branch. The main branch is the one origin/HEAD names,
          else main, else master. The local branch and origin's copy are both read, and the larger count wins: a main
          checkout can hold commits that are not pushed yet. Nothing is fetched.
- checks: GitHub check runs of the branch's head commit that failed, read with `gh`. No gh, no sign-in, no GitHub
          remote or a commit that is not pushed: no finding (checks are "not read", never "passing").
- idle:   the tab does not work and its transcript did not change for IDLE seconds.

The sweep only reads. It never rebases, merges, fetches or writes to a repository (GIT_OPTIONAL_LOCKS=0).
The lines go to the Factory view (factory.payload) and, for findings that are new since the last sweep, to one
low-priority notification. Notifications > Low is Off by default, so the notice shows only when the person chose it.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import paths

PERIOD = 30 * 60
IDLE = 2 * 3600
GIT_TIMEOUT = 5
GH_TIMEOUT = 15
TABS = 40            # worktree tabs read in one sweep
KNOWN = 86400        # a finding seen in this time is not announced again (a check that gh could not read comes back)
NOTICE_LINES = 3
AGENTS = ('claude', 'codex', 'antigravity')
FAILED = ('failure', 'timed_out', 'startup_failure')
KINDS = ('behind', 'checks', 'idle')


def path():
    return paths.CACHE / 'worktree-sweep.json'


def _git(run, folder, *args):
    try:
        done = run(['git', '-C', folder, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def worktree(folder, run=subprocess.run):
    """{'top', 'branch', 'head'} when the folder is in a linked worktree, else None. A detached HEAD has branch ''."""
    shown = _git(run, folder, 'rev-parse', '--path-format=absolute', '--git-dir', '--git-common-dir', '--show-toplevel')
    lines = shown.splitlines() if shown else []
    if len(lines) != 3 or lines[0] == lines[1]:
        return None
    head = _git(run, folder, 'rev-parse', 'HEAD')
    if not head:
        return None
    branch = _git(run, folder, 'symbolic-ref', '--short', '-q', 'HEAD') or ''
    return {'top': lines[2], 'branch': branch, 'head': head}


def main_branch(folder, run=subprocess.run):
    """The repository's main branch name: what origin/HEAD names, else main, else master. None when there is none."""
    named = _git(run, folder, 'symbolic-ref', '--short', '-q', 'refs/remotes/origin/HEAD')
    names = ([named.split('/', 1)[1]] if named and '/' in named else []) + ['main', 'master']
    for name in names:
        for ref in (f'refs/heads/{name}', f'refs/remotes/origin/{name}'):
            if _git(run, folder, 'rev-parse', '--verify', '-q', ref) is not None:
                return name
    return None


def behind(folder, name, run=subprocess.run):
    """How many commits of the main branch the folder's HEAD lacks: the larger of the local and the origin count."""
    counts = []
    for ref in (f'refs/heads/{name}', f'refs/remotes/origin/{name}'):
        shown = _git(run, folder, 'rev-list', '--count', f'HEAD..{ref}')
        if shown and shown.isdigit():
            counts.append(int(shown))
    return max(counts, default=0)


def failed_checks(folder, head, run=subprocess.run):
    """The names of the failed GitHub check runs of one commit, or None when they cannot be read."""
    env = {**os.environ, 'GH_PROMPT_DISABLED': '1', 'GH_NO_UPDATE_NOTIFIER': '1', 'NO_COLOR': '1'}
    try:
        done = run(['gh', 'api', '--method', 'GET', f'repos/{{owner}}/{{repo}}/commits/{head}/check-runs?per_page=100'],
                   capture_output=True, text=True, timeout=GH_TIMEOUT, env=env, cwd=folder)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode:
        return None
    try:
        runs = json.loads(done.stdout or 'null')['check_runs']
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(runs, list):
        return None
    names = [str(item.get('name') or 'check')[:60] for item in runs
             if isinstance(item, dict) and item.get('status') == 'completed' and item.get('conclusion') in FAILED]
    return sorted(set(names))


def _hours(seconds):
    hours = int(seconds // 3600)
    return f'{hours} h' if hours < 48 else f'{hours // 24} days'


def _name(entry):
    return entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or entry['id']


def read_tab(entry, folder, working, run=subprocess.run, now=None, checks=failed_checks, tree=None):
    """The findings of one tab: [{'pane', 'name', 'branch', 'kind', 'text', 'key'}]. key names the finding, so the
    same finding in the next sweep is not announced again. tree: the folder's worktree() when the caller read it."""
    now = time.time() if now is None else now
    tree = tree or worktree(folder, run)
    if tree is None:
        return []
    base = {'pane': entry['id'], 'name': _name(entry)[:60], 'branch': tree['branch'][:200]}
    found = []
    name = main_branch(folder, run)
    if name and tree['branch'] != name:
        count = behind(folder, name, run)
        if count:
            found.append(dict(base, kind='behind', key=f'{entry["id"]}:behind',
                              text=f'{count} {"commit" if count == 1 else "commits"} behind {name}. Rebase by hand.'))
    failed = checks(tree['top'], tree['head'], run)
    if failed:
        shown = ', '.join(failed[:3]) + (f' and {len(failed) - 3} more' if len(failed) > 3 else '')
        found.append(dict(base, kind='checks', key=f'{entry["id"]}:checks:{tree["head"][:12]}',
                          text=f'{len(failed)} {"check fails" if len(failed) == 1 else "checks fail"} on {tree["head"][:7]}: {shown}.'))
    transcript = entry.get('transcript')
    if not working and transcript:
        try:
            quiet = now - Path(transcript).stat().st_mtime
        except OSError:
            quiet = 0
        if quiet > IDLE:
            found.append(dict(base, kind='idle', key=f'{entry["id"]}:idle', text=f'Idle for {_hours(quiet)}.'))
    return found


def swept(entry):
    return (entry.get('agent') in AGENTS and entry.get('role') != 'advisor' and entry.get('helper') != 'reviewer')


def scan(entries, folder_of, status, run=subprocess.run, now=None, checks=failed_checks, cursor=''):
    """One sweep: {'lines', 'read': the panes it looked at (worktree tab or not), 'cursor'}. At most TABS worktree tabs
    are read. With more, the scan starts after the tab the last sweep stopped at (cursor), so every tab gets its
    turn; the returned cursor is '' when this scan reached the end of the list. One odd tab never stops the sweep."""
    order = [entry for entry in entries if swept(entry)]
    at = next((index for index, entry in enumerate(order) if entry['id'] == cursor), -1)
    order = order[at + 1:] + order[:at + 1]
    lines, read, looked, stopped = [], [], set(), ''
    for index, entry in enumerate(order):
        if len(read) >= TABS:
            stopped = order[index - 1]['id']
            break
        looked.add(entry['id'])  # also a tab that left its worktree: its old lines must go
        try:
            folder = folder_of(entry)
            if not folder or not os.path.isdir(folder):
                continue
            tree = worktree(folder, run)
            if tree is None:
                continue  # not a worktree tab: it does not count toward TABS
            read.append(entry['id'])
            working = (status(entry) or {}).get('state') == 'Working'
            lines.extend(read_tab(entry, folder, working, run, now, checks, tree))
        except Exception:  # noqa: BLE001 - a tab that closes mid-read, or a repository git cannot read
            continue
    return {'lines': lines, 'read': looked, 'cursor': stopped}


def run_sweep(entries, folder_of, status, run=subprocess.run, now=None, checks=failed_checks):
    """The findings of one scan from the top of the list."""
    return scan(entries, folder_of, status, run, now, checks)['lines']


def load():
    """{'at', 'lines'} of the last sweep. An unreadable file reads as no sweep yet."""
    try:
        data = json.loads(path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'at': 0, 'lines': [], 'known': {}, 'cursor': ''}
    lines = data.get('lines') if isinstance(data, dict) else None
    if not isinstance(lines, list) or type(data.get('at')) not in (int, float):
        return {'at': 0, 'lines': [], 'known': {}, 'cursor': ''}
    good = [line for line in lines if isinstance(line, dict) and line.get('kind') in KINDS
            and all(isinstance(line.get(key), str) for key in ('pane', 'name', 'branch', 'text', 'key'))]
    known = data.get('known') if isinstance(data.get('known'), dict) else {}
    return {'at': data['at'], 'lines': good, 'cursor': data['cursor'] if isinstance(data.get('cursor'), str) else '',
            'known': {key: at for key, at in known.items() if isinstance(key, str) and type(at) in (int, float)}}


def save(at, lines, known=None, cursor=''):
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(f'.{file.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(json.dumps({'version': 1, 'at': at, 'lines': lines, 'known': known or {}, 'cursor': cursor}) + '\n', encoding='utf-8')
        os.replace(temp, file)
    finally:
        temp.unlink(missing_ok=True)


def public(live=None):
    """What the Factory view gets: the last sweep's lines for tabs that are still open, without the keys."""
    data = load()
    lines = [{key: line[key] for key in ('pane', 'name', 'branch', 'kind', 'text')} for line in data['lines']
             if live is None or line['pane'] in live]
    return {'at': int(data['at']), 'every': PERIOD, 'lines': lines}


def notice(new, silent=False):
    """One low-priority notification for the findings that are new in this sweep."""
    shown = [f'{line["name"]}: {line["text"]}' for line in new[:NOTICE_LINES]]
    if len(new) > NOTICE_LINES:
        shown.append(f'And {len(new) - NOTICE_LINES} more in the Factory view.')
    count = len(new)
    return {'title': f'Worktree sweep: {count} new {"finding" if count == 1 else "findings"}', 'body': '\n'.join(shown)[:400],
            'tag': 'worktree-sweep', 'go': 'factory', 'priority': 'low', 'silent': silent, 'url': '/chat/?go=factory'}


class Sweeper:
    """Runs the sweep from the monitor loop: at most once per PERIOD, in its own thread (git and gh can be slow)."""

    def __init__(self, notify=None, delivery=None, clock=time.time, period=PERIOD):
        self.notify, self.delivery, self.clock, self.period = notify, delivery, clock, period
        self.lock = threading.Lock()
        self.busy = False
        self.thread = None
        self.tried = 0  # a sweep that failed is not tried again before the next period
        self.last = None  # when the last sweep ran; read from the file once, in the worker thread

    def observe(self, entries, folder_of, status, run=subprocess.run, wait=False):
        """Start the worker when a sweep may be due. Returns True when it started it. The monitor thread reads no
        file here: the worker reads the time of the last sweep once after a start. wait=True waits for the worker."""
        with self.lock:
            if self.busy or self.clock() - max(self.last or 0, self.tried) < self.period:
                return False
            self.busy = True
            self.tried = self.clock()
        self.thread = threading.Thread(target=self._work, args=(list(entries), folder_of, status, run), daemon=True)
        self.thread.start()
        if wait:
            self.thread.join()
        return True

    def _work(self, entries, folder_of, status, run):
        try:
            if self.last is None:
                self.last = load()['at']
                if self.clock() - self.last < self.period:
                    self.tried = 0  # the sweep before the restart is still fresh: the next one is due from it
                    return
            started = self.clock()  # the period counts from the start: a slow sweep does not stretch it
            self.sweep(entries, folder_of, status, run)
            self.last = started
        except Exception as exc:  # noqa: BLE001 - the sweep must never take the monitor down
            print(f'worktree sweep failed: {exc!r}', flush=True)
        finally:
            with self.lock:
                self.busy = False

    def sweep(self, entries, folder_of, status, run=subprocess.run, checks=failed_checks):
        now = self.clock()
        last = load()
        known = {key: at for key, at in last['known'].items() if now - at < KNOWN}
        found = scan(entries, folder_of, status, run, now, checks, last['cursor'])
        # A tab that this scan did not reach (more than TABS worktree tabs) keeps its lines until its next turn.
        live = {entry['id'] for entry in entries}
        waiting = [line for line in last['lines'] if line['pane'] in live and line['pane'] not in found['read']] \
            if found['cursor'] or last['cursor'] else []
        lines = found['lines'] + waiting
        new = [line for line in found['lines'] if line['key'] not in known]
        known.update({line['key']: now for line in lines})
        save(now, lines, known, found['cursor'])
        mode = (self.delivery() if self.delivery else {}).get('low', 'off')
        if new and self.notify and mode != 'off':
            self.notify(notice(new, silent=mode == 'silent'), False)
        return lines
