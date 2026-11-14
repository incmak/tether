"""Close helper tabs when their work ends (B55).

Agents start helper tabs (advisors, reviewers, task helpers) with agent_tab_cli. Before this module only the owner tab
closed them, and owners often forgot, so idle helpers piled up. The server now closes a helper when:
- its owner tab is gone, or the owner changed conversations (the helper no longer accepts its commands);
- it is a reviewer whose review link ended;
- it is an advisor or reviewer that has been idle for IDLE seconds (a reviewer only while its worker is idle too).
A task helper stays until its owner is gone: its result can matter to the user.
A tab that works, waits on a prompt, or that someone views stays open. Timers start fresh after a server restart.
"""
import json
import re
import subprocess
import sys
import time

import auto_resume
import helper_tabs
import paths
import review_links
from terminal_input import UUID

IDLE = 30 * 60          # advisors and linked reviewers
GRACE = 60              # owner gone, stale owner, or an ended review link
SWEEP = 30
QUIET = ('Idle', 'Shell')  # only a confirmed quiet state counts; Unavailable or Unknown never closes a tab
ADVISOR = ('advisor', 'second-advisor')
PANE = re.compile(r'%[0-9]+')


def close_pane(pane, run):
    """The shared close: forget the resume record, end the pane, drop its review links."""
    auto_resume.forget_pane(pane, run=run)
    run([paths.TMUX, 'kill-pane', '-t', pane], check=True, capture_output=True, timeout=5)
    review_links.remove(pane)  # links where the pane is the worker
    for worker, reviewers in review_links.reviewed_by().items():
        if pane in reviewers:
            review_links.remove(worker, pane)  # and where it is the reviewer


def _panes(run):
    """{pane: (helper record or None, role)} for every live tmux pane, from one tmux call."""
    result = run([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}\t#{@tether_role}\t#{' + helper_tabs.OPTION + '}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        return None
    panes = {}
    for line in result.stdout.splitlines():
        pane, _, rest = line.partition('\t')
        role, _, raw = rest.partition('\t')
        if not PANE.fullmatch(pane):
            continue
        record = None
        if raw:
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
            if not isinstance(record, dict) or not isinstance(record.get('owner_pane'), str) \
                    or not PANE.fullmatch(record['owner_pane']):
                record = None
        panes[pane] = (record, role)
    return panes


def _thread(entry):
    thread = entry.get('thread') if entry else None
    return thread if isinstance(thread, str) and UUID.fullmatch(thread) else None


class Closer:
    def __init__(self, now=time.time):
        self.now = now
        self.active = {}     # pane -> last time it was busy, viewed or sent a message (or first seen)
        self.since = {}      # (pane, condition) -> when the close condition was first seen
        self.last_sweep = float('-inf')

    def _idle(self, pane, entry, record, status, watching, now):
        if entry is None:
            return 0
        if status(entry).get('state') not in QUIET or watching(pane):
            self.active[pane] = now
            return 0
        base = self.active.setdefault(pane, now)
        sent = record.get('sent_at') if record else None
        if isinstance(sent, (int, float)) and not isinstance(sent, bool) and base < sent <= now:
            self.active[pane] = sent  # a new request to a reused helper restarts its timer
        return now - self.active[pane]

    def observe(self, entries, status, run, watching=lambda pane: False, log=None, lock=None, fresh=None):
        """Call often; it sweeps every SWEEP seconds. Returns [(pane, reason)] for the tabs it closed.

        lock is the server send lock and fresh an uncached status read: a helper that got a message or a prompt after
        the sweep read its state is never closed.
        """
        now = self.now()
        if now - self.last_sweep < SWEEP:
            return []
        self.last_sweep = now
        panes = _panes(run)
        if panes is None:
            return []
        by_id = {entry['id']: entry for entry in entries}
        links = review_links.reviewed_by()
        worker_of = {reviewer: worker for worker, reviewers in links.items() for reviewer in reviewers}
        idle = {pane: self._idle(pane, by_id.get(pane), record, status, watching, now)
                for pane, (record, _) in panes.items()}
        self.active = {pane: when for pane, when in self.active.items() if pane in panes}
        seen = set()
        closed = []
        for pane, (record, role) in panes.items():
            if record is None or pane not in by_id:
                continue  # not a helper, or a discovery miss
            found = self._reason(pane, record, role, by_id, panes, worker_of, idle)
            if found is None:
                continue
            condition, reason, wait = found
            key = (pane, condition)
            seen.add(key)
            if idle[pane] <= 0:
                self.since[key] = now  # a busy or viewed tab starts a fresh grace when it goes quiet
            started = self.since.setdefault(key, now)
            if idle[pane] <= 0 or now - started < wait:
                continue
            try:
                if not self._still_quiet(pane, record, by_id[pane], fresh or status, watching, run, lock):
                    self.active[pane] = self.since[key] = now  # it got work or a viewer: start both timers again
                    continue
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                print(f'helper close {pane} failed: {exc!r}', file=sys.stderr)
                continue
            self.active.pop(pane, None)
            closed.append((pane, reason))
            (log or (lambda text: print(text, file=sys.stderr)))(f'closed helper tab {pane}: {reason}')
        self.since = {key: when for key, when in self.since.items() if key in seen}
        return closed

    @staticmethod
    def _still_quiet(pane, record, entry, status, watching, run, lock):
        """Under the send lock: the record is unchanged and the tab is still quiet, then close it. False = keep."""
        if lock is not None and not lock.acquire(timeout=2):
            return False
        try:
            exists, now_record = helper_tabs.load(pane, run)
            if not exists or now_record is None or now_record.get('sent_at') != record.get('sent_at'):
                return False
            if status(entry).get('state') not in QUIET or watching(pane):
                return False
            close_pane(pane, run)
            return True
        finally:
            if lock is not None:
                lock.release()

    @staticmethod
    def _reason(pane, record, role, by_id, panes, worker_of, idle):
        """(condition, reason, wait) when a close condition holds now, else None."""
        quiet = idle[pane]
        owner = record['owner_pane']
        if owner not in panes:
            return 'owner-gone', 'its owner tab is gone', GRACE
        owner_thread, now_thread = record.get('owner_thread'), _thread(by_id.get(owner))
        if owner_thread and now_thread and owner_thread != now_thread:
            return 'owner-moved', 'its owner tab changed conversations', GRACE
        worker = worker_of.get(pane)
        if role == 'reviewer' and worker is None:
            return 'unlinked', 'its review ended', GRACE
        if worker is not None:
            # A linked reviewer waits for the next turn; close it only when both tabs have been quiet a long time.
            if min(quiet, idle.get(worker, quiet)) >= IDLE:
                return 'idle', 'the review has been idle for 30 minutes', 0
            return None
        if role in ADVISOR and quiet >= IDLE:
            return 'idle', 'the advisor has been idle for 30 minutes', 0
        return None
