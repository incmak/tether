"""Close helper tabs when their work ends (B55).

Agents start helper tabs (advisors, reviewers, task helpers) with agent_tab_cli. Before this module only the owner tab
closed them, and owners often forgot, so idle helpers piled up. The server now closes a helper when:
- its owner tab is gone, or the owner changed conversations (the helper no longer accepts its commands);
- it is a reviewer whose review link ended;
- it is an advisor or reviewer that has been idle for IDLE seconds (a linked reviewer also needs a quiet worker
  with no work pending review).
A task helper stays until its owner is gone: its result can matter to the user.
A tab that works, waits on a prompt, or that someone views stays open. Timers start fresh after a server restart.
"""
import re
import subprocess
import sys
import time

import auto_resume
import groups
import helper_tabs
import paths
import review_links
import task_tree
from terminal_input import UUID

IDLE = 30 * 60          # advisors and linked reviewers
GRACE = 60              # owner gone, stale owner, or an ended review link
SWEEP = 30
QUIET = ('Idle', 'Shell')  # only a confirmed quiet state counts; Unavailable or Unknown never closes a tab
ADVISOR = ('advisor', 'second-advisor')
PANE = re.compile(r'%[0-9]+')
CLOSE_CHANGED = 'The helpers changed. Review the list again.'
CLOSE_FORMAT = '#{pid}:#{start_time}\t#{pane_id}\t#{session_id}\t#{pane_pid}'


def close_pane(pane, run, *, guard=None):
    """The shared close: forget the resume record, end the pane, drop its review links."""
    if guard is None:
        auto_resume.forget_pane(pane, run=run)
    with groups.closing(pane, run):  # its auto stack goes when empty or root-only
        if guard is not None:
            guard()
        run([paths.TMUX, 'kill-pane', '-t', pane], check=True, capture_output=True, timeout=5)
        if guard is not None:
            auto_resume.forget_pane(pane, run=run)
    release_reviews(pane)


def close_identity(entry):
    return (entry['id'], entry.get('tmux_session'), entry.get('agent'), helper_tabs.verified_thread(entry),
            entry.get('owner_pid'), entry.get('process_start'), entry.get('pane_pid'), entry.get('pane_start'))


def close_bucket(status):
    state = status.get('state')
    if state not in ('Idle', 'Shell', 'Working', 'Approval', 'Input needed'):
        return None
    if state == 'Shell':
        status = dict(status, state='Idle')
    return 'idle' if task_tree.state(status) == 'idle' else 'working'


def close_matching_pane(pane, run, *, expected, bucket, server, fresh, status):
    """Read the conversation and its state after Undo is ready, immediately before the kill."""
    def guard():
        current = next((entry for entry in fresh() if entry['id'] == pane), None)
        if current is None or expected != close_identity(current) or bucket != close_bucket(status(current)):
            raise helper_tabs.Stale(CLOSE_CHANGED)
        result = run([paths.TMUX, 'display-message', '-p', '-t', pane, CLOSE_FORMAT],
                     capture_output=True, text=True, timeout=5)
        identity = result.stdout.rstrip('\n').split('\t')
        if (result.returncode or len(identity) != 4 or identity[:3] != [server, pane, current['tmux_session']]
                or current.get('pane_pid') is not None and identity[3] != str(current['pane_pid'])):
            raise helper_tabs.Stale(CLOSE_CHANGED)

    close_pane(pane, run, guard=guard)


def release_reviews(pane):
    try:
        review_links.close(pane)
    except OSError as exc:
        # The pane is gone even if its review record cannot be updated. Keep the close eligible for Undo.
        print(f'could not release review links for {pane}: {exc}', file=sys.stderr)


def _panes(run):
    """{pane: (helper record or None, role)} for every live tmux pane, from one tmux call."""
    result = run([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}\t#{@tether_role}\t#{' + helper_tabs.OPTION + '}\t#{@group_by}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        return None
    panes = {}
    for line in result.stdout.splitlines():
        pane, _, rest = line.partition('\t')
        role, _, raw = rest.partition('\t')
        if not PANE.fullmatch(pane):
            continue
        raw, _, group_by = raw.partition('\t')
        record = helper_tabs._record(raw)
        panes[pane] = (record, role, group_by)
    return panes


def _thread(entry):
    thread = entry.get('thread') if entry else None
    return thread if isinstance(thread, str) and UUID.fullmatch(thread) else None


class Closer:
    def __init__(self, now=time.time, *, scope=None):
        self.now = now
        self.scope = scope
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

    def observe(self, entries, status, run, watching=lambda pane: False, log=None, lock=None, fresh=None, task_sweep=None):
        """Call often; it sweeps every SWEEP seconds. Returns [(pane, reason)] for the tabs it closed.

        lock is the server send lock and fresh an uncached status read: a helper that got a message or a prompt after
        the sweep read its state is never closed.
        """
        now = self.now()
        if now - self.last_sweep < SWEEP:
            return []
        self.last_sweep = now
        if task_sweep is not None:
            task_sweep()
        panes = _panes(run)
        if panes is None:
            return []
        by_id = {entry['id']: entry for entry in entries}
        links = review_links.reviewed_by()
        worker_of = {reviewer: worker for worker, reviewers in links.items() for reviewer in reviewers}
        idle = {pane: self._idle(pane, by_id.get(pane), record, status, watching, now)
                for pane, (record, _, _) in panes.items()}
        self.active = {pane: when for pane, when in self.active.items() if pane in panes}
        seen = set()
        closed = []
        for pane, (record, role, group_by) in panes.items():
            if record is None or pane not in by_id:
                continue  # not a helper, or a discovery miss
            found = self._reason(pane, record, role, by_id, panes, worker_of, idle, group_by)
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
                worker = by_id.get(worker_of.get(pane)) if condition == 'idle' else None
                if not self._still_quiet(pane, (record, role, group_by), by_id[pane], fresh or status, watching, run, lock, worker):
                    self.active[pane] = self.since[key] = now  # it got work or a viewer: start both timers again
                    continue
            except (OSError, groups.GroupError, subprocess.SubprocessError, RuntimeError) as exc:
                print(f'helper close {pane} failed: {exc!r}', file=sys.stderr)
                continue
            self.active.pop(pane, None)
            closed.append((pane, reason))
            (log or (lambda text: print(text, file=sys.stderr)))(f'closed helper tab {pane}: {reason}')
        self.since = {key: when for key, when in self.since.items() if key in seen}
        return closed

    def _still_quiet(self, pane, placement, entry, status, watching, run, lock, worker=None):
        """Under the send lock, close an unchanged helper if it and any linked worker are still quiet."""
        if lock is not None and not lock.acquire(timeout=2):
            return False
        try:
            with groups.LOCK(), helper_tabs.record_lock():
                current = _panes(run)
                if current is None or current.get(pane) != placement:
                    return False
                if status(entry).get('state') not in QUIET or watching(pane):
                    return False
                if worker is not None and (status(worker).get('state') not in QUIET or watching(worker['id'])
                                           or review_links.unreviewed_work(worker, entry, self.scope)):
                    return False
                import undo  # here: undo.py imports this module
                closed = undo.capture_safe(pane, run, [entry])
                close_pane(pane, run)
                undo.record_tab_close(closed, run)  # Tether closed it: Cmd+Z brings it back
                return True
        finally:
            if lock is not None:
                lock.release()

    def _reason(self, pane, record, role, by_id, panes, worker_of, idle, group_by=''):
        """(condition, reason, wait) when a close condition holds now, else None."""
        quiet = idle[pane]
        if record.get('kind') == 'own' and record.get('moved_by') == 'user':
            return None  # a helper kept open by Mak is now a separate task
        if task_tree.is_child(record, role, group_by):
            owner, bound = (helper_tabs.placement_owner(record) if record.get('moved_by') == 'user'
                            else (record['owner_pane'], record.get('owner_thread')))
            if owner not in panes:
                return 'owner-gone', 'its owner tab is gone', GRACE
            now_thread = _thread(by_id.get(owner))
            if bound and now_thread and bound != now_thread:
                return 'owner-moved', 'its owner tab changed conversations', GRACE
        worker = worker_of.get(pane)
        if role == 'reviewer' and worker is None:
            return 'unlinked', 'its review ended', GRACE
        if worker is not None:
            # A linked reviewer waits for the next turn; close it only when both tabs have been quiet a long time.
            if min(quiet, idle.get(worker, quiet)) >= IDLE:
                worker_entry = by_id.get(worker)
                if worker_entry is not None and review_links.unreviewed_work(worker_entry, by_id[pane], self.scope):
                    return None
                return 'idle', 'the review has been idle for 30 minutes', 0
            return None
        if role in ADVISOR and quiet >= IDLE:
            return 'idle', 'the advisor has been idle for 30 minutes', 0
        return None
