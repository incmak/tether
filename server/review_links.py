"""Persistent worker/reviewer links and one bounded watcher step.

B205: a review is bound to a commit. The request names HEAD of the worker's folder (link 'review_commit'); an APPROVED
reply is forwarded as approval only when it names that commit, the worker is idle and the folder is still at that
commit. A clean approved commit stays on the link as 'approved_commit' (see approved_commits).
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unicodedata

import agents
import audit
import helper_tabs
import paths
import review_scope
from terminal_input import DraftError

FILE = paths.CACHE / 'review-links.json'
MAX_LINKS = 50
MAX_SAVED_CAPS = 100
MAX_ROUNDS = 3
EXTRA_ROUNDS = 2
# Counts that pause a review at MAX_ROUNDS: failed reviews, reviews of turns that changed nothing, and approvals
# that could not be verified for the current commit.
COUNTS = ('rounds', 'empty_rounds', 'stale_rounds')
CAP_DECISION = ('counts', 'limit', 'grant_rounds', 'paused', 'unreadable', 'parent_stage', 'parent_missing',
                'escalate', 'cap_reset')
CAP_FIELDS = (*CAP_DECISION, 'restart', 'review_prompts', 'review_turn_at', 'agent_prompt', 'feedback_turn')
LEGACY_COUNTS = (*COUNTS, 'round_sources')
UNOWNED = ''  # B268 saved records can predate reviewer identity.
SAVED_CAP_TTL = 24 * 60 * 60
CAP_ERROR = 'This worker reached the review round cap. Mak or the parent must decide before linking a reviewer.'
UPGRADE_PAUSE = 'upgrade'
UPGRADE_ERROR = 'The review state from the upgrade needs attention. Stop any existing review link, then link the tabs again in the browser.'
DELIVERY_ERROR = 'Review feedback is being delivered. Try linking again.'
VERIFY_TICKS = 5  # ticks to wait for readable Git state at approval or after its follow-up
SCOPE_WAIT = 60  # seconds from the first failed scope read before a bound review pauses
SCOPE_BACKOFF = (2, 4, 8, 15)
SCOPE_RETRY_FIELDS = ('scope_ticks', 'scope_since', 'scope_retry_at')
NAMED = re.compile(r'APPROVED[\s:,.(`*\u2013\u2014-]*(?:for\s+)?(?:commit\s+)?[`*]*([0-9a-f]{7,64})\b', re.I)
MENTIONED_COMMIT = re.compile(r'\bcommit\s+[`*]*([0-9a-f]{7,64})\b', re.I)
FULL = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')  # a short hash can match two commits; only a full one names a commit
GRACE = 30
LOSS_TICKS = 30
START_WAIT = 300
AGENT_PROMPT_TTL = 600  # an agent prompt that started no turn in 10 minutes no longer blocks Mak's restart
SEND_GAP = 6
TRANSCRIPT_TAIL_BYTES = 256 * 1024
PENDING_IDLE_TICKS = 150  # about five minutes of Idle polls with an incomplete reply
REPLY_PENDING = object()  # the request reached the transcript and its turn has yet to end
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_lock = threading.Lock()
_last_sent = {}
_feedback_in_flight = {}


def _has_worker(row):
    return (isinstance(row, dict) and isinstance(row.get('worker'), str)
            and (row.get('worker_thread') is None or isinstance(row['worker_thread'], str)))


def _is_cap(row):
    return _has_worker(row) and row.get('cap') is True


def _saved(row):
    return isinstance(row, dict) and isinstance(row.get('saved_at'), (int, float))


def _is_link(row):
    return (_has_worker(row) and not _is_cap(row) and not _saved(row)
            and isinstance(row.get('reviewer'), str))


def _cap_key(row):
    return row['worker'], row.get('worker_thread')


def _cap_for(rows, key):
    return next((row for row in rows if _is_cap(row) and _cap_key(row) == key), None)


def _new_cap(worker, thread, name=''):
    return dict(cap=True, worker=worker, worker_thread=thread, name=name or worker)


def _matches(row, worker, thread):
    return row['worker'] == worker and (thread is None or row.get('worker_thread') in (None, thread))


def _reviewer_key(row):
    reviewer = row.get('reviewer')
    thread = row.get('reviewer_thread') or ''
    return f'{reviewer}:{thread}' if isinstance(reviewer, str) else UNOWNED


def _clean_counts(values):
    return {field: values[field] for field in COUNTS if type(values.get(field)) is int and values[field] > 0}


def _clean_reviewer_counts(values):
    if not isinstance(values, dict):
        return {}
    return {key: counts for key, entry in values.items()
            if isinstance(entry, dict) and (counts := _clean_counts(entry))}


def _is_time(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def _sanitize(row):
    """Validate the values that migration and the watcher read from the store."""
    if not _has_worker(row):
        return row
    row = dict(row)
    row.pop('follow_up_ticks', None)  # obsolete idle-poll counter
    for fields, check in (
        ((*COUNTS, 'limit', 'grant_rounds', 'parent_missing', 'confirm_ticks', 'lost_ticks', 'idle_ticks',
          'pending_idle_ticks', 'scope_ticks', 'verify_ticks'),
         lambda value: type(value) is int and value >= 0),
        (('created', 'saved_at'), lambda value: type(value) in (int, float)),
        (('agent_prompt', 'review_turn_at', 'upgrade_at'), _is_time),
        (('scope_since', 'scope_retry_at', 'follow_up_checked_at', 'follow_up_untimed_at'),
         lambda value: (type(value) is int or type(value) is float and math.isfinite(value)) and value >= 0),
        (('unreadable', 'pending_reset', 'pending_adoption', 'seen_working', 'upgrade_alerted', 'stale_unreadable'),
         lambda value: type(value) is bool),
        (('name', 'escalate', 'cap_reset'), lambda value: isinstance(value, str)),
    ):
        for field in fields:
            if field in row and not check(row[field]):
                row.pop(field)
    paused = row.get('paused')
    if type(paused) is not bool and not (_is_cap(row) and paused == UPGRADE_PAUSE):
        row.pop('paused', None)
    if _is_link(row) or _is_cap(row):
        row.setdefault('name', row['worker'])
    if _is_link(row):
        row.setdefault('agent', '')
        row.setdefault('reviewer_thread', None)
        row.setdefault('seen_working', False)
    if 'counts' in row:
        row['counts'] = _clean_reviewer_counts(row['counts'])
    approved = row.pop('approved_state', None)
    if (_is_link(row) and isinstance(approved, dict)
            and approved.keys() - {'folder', 'at'} == {'commit', 'dirty', 'mark'}
            and (approved['commit'] is None or isinstance(approved['commit'], str))
            and (approved['dirty'] is None or type(approved['dirty']) is bool)
            and (approved['mark'] is None or isinstance(approved['mark'], str))
            and (approved.get('folder') is None or isinstance(approved['folder'], str))
            and ('at' not in approved or
                 (type(approved['at']) is int or type(approved['at']) is float and math.isfinite(approved['at']))
                 and approved['at'] >= 0)):
        # Older approvals used a different digest. Keep their commit and dirty state.
        folder = row.get('review_folder')
        row['approved_state'] = approved if 'folder' in approved else {
            **approved, 'mark': None, 'folder': folder if isinstance(folder, str) else None}
    correction = row.pop('stale_correction', None)
    if (_is_link(row) and isinstance(correction, dict)
            and correction.keys() == {'commit', 'now', 'request', 'requested'}
            and isinstance(correction['commit'], str) and FULL.fullmatch(correction['commit'])
            and (correction['now'] is None or isinstance(correction['now'], str) and FULL.fullmatch(correction['now']))
            and correction['now'] != correction['commit']
            and isinstance(correction['request'], str)
            and re.fullmatch(r'[0-9a-f]{8}|[0-9a-f]{64}', correction['request'])
            and type(correction['requested']) is bool):
        row['stale_correction'] = correction
    for field in ('review_folder', 'follow_up_folder'):
        if not isinstance(row.get(field), str):
            row.pop(field, None)
    stage = row.pop('parent_stage', None)
    if (isinstance(stage, dict) and isinstance(stage.get('pane'), str) and isinstance(stage.get('notice'), str)
            and (stage.get('thread') is None or isinstance(stage['thread'], str))):
        stage = {**stage, 'thread': stage.get('thread'), 'sent': stage.get('sent') is True}
        if type(stage.get('failed')) is not int:
            stage.pop('failed', None)
        row['parent_stage'] = stage
    restart = row.pop('restart', None)
    if isinstance(restart, dict):
        row['restart'] = _clean_counts(restart)
        if 'counts' in restart:
            row['restart']['counts'] = _clean_reviewer_counts(restart['counts'])
        row['restart'].update({field: restart[field] for field in ('paused', 'agent')
                               if type(restart.get(field)) is bool})
    elif restart is True:  # a restart without baseline counts from an older store
        row['restart'] = {}
    prompts = row.pop('review_prompts', None)
    if isinstance(prompts, list):
        valid = [marker for marker in prompts if isinstance(marker, str) or
                 (isinstance(marker, list) and len(marker) == 2 and isinstance(marker[0], str)
                  and _is_time(marker[1]))]
        if valid:
            row['review_prompts'] = valid
    feedback = row.get('feedback_turn')
    if (not isinstance(feedback, dict) or type(feedback.get('sent_at')) not in (int, float)
            or feedback.get('phase') not in ('queued', 'pending', 'working')):
        row.pop('feedback_turn', None)
    return row


def _counts_since(counts, baseline):
    return {field: count - baseline.get(field, 0) for field, count in counts.items()
            if count > baseline.get(field, 0)}


def _reviewer_counts_since(counts, baseline):
    return {reviewer: left for reviewer, own in counts.items()
            if (left := _counts_since(own, baseline.get(reviewer, {})))}


def _merge_counts(first, second):
    merged = dict(first)
    for reviewer, counts in second.items():
        own = merged.get(reviewer, {})
        merged[reviewer] = {field: max(own.get(field, 0), counts.get(field, 0)) for field in own.keys() | counts.keys()}
    return merged


def _keep_latest_time(cap, field, rows):
    latest = max((row[field] for row in rows if field in row), default=None)
    if latest is not None:
        cap[field] = latest


def _migrate(rows, cutoff):
    """Preserve a single legacy row exactly. Multiple rows cannot share a review decision safely."""
    legacy = {}
    current = {}
    links = []
    for row in rows:
        if _is_cap(row):
            current[_cap_key(row)] = row
        elif _has_worker(row) and (_saved(row) or _is_link(row)):
            if not _saved(row) or row['saved_at'] > cutoff or _counts_at_limit(row, row):
                legacy.setdefault(_cap_key(row), []).append(row)
            if not _saved(row):
                link = {key: value for key, value in row.items() if key not in (*CAP_FIELDS, *LEGACY_COUNTS)}
                links.append(link)
        else:
            links.append(row)
    for key, peers in legacy.items():
        if key in current:
            continue
        name = next((row['name'] for row in peers if row.get('name') and row['name'] != key[0]), key[0])
        cap = _new_cap(*key, name)
        ambiguous = len(peers) > 1
        counts = {}
        for row in peers:
            reviewer = _reviewer_key(row)
            own = _clean_counts(row)
            restart = row.get('restart')
            if ambiguous and restart is not None and not restart.get('agent'):
                own = _counts_since(own, restart)
            if own:
                counts = _merge_counts(counts, {reviewer: own})
        if ambiguous:
            cap.update(paused=UPGRADE_PAUSE, upgrade_at=time.time())
            _keep_latest_time(cap, 'agent_prompt', peers)
        else:
            row, = peers
            cap.update({field: row[field] for field in CAP_FIELDS if field in row})
            if 'restart' in cap:
                restart = cap['restart']
                cap['restart'] = {field: restart[field] for field in ('paused', 'agent') if field in restart}
                baseline = _clean_counts(restart)
                cap['restart']['counts'] = {_reviewer_key(row): baseline} if baseline else {}
        if counts:
            cap['counts'] = counts
        if ambiguous and any(not _saved(row) and isinstance(row.get('restart'), dict) and row['restart'].get('agent')
                             for row in peers):
            # An old watcher may have consumed the parent's marker. The flag is the only evidence of its turn.
            cap['restart'] = {**_restart_state(cap), 'agent': True}
        if all(_saved(row) for row in peers):
            cap['saved_at'] = max(row['saved_at'] for row in peers)
        current[key] = cap
    return links + list(current.values())


def _load_rows():
    try:
        rows = json.loads(FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _needs_migration(rows):
    keys = {_cap_key(row) for row in rows if _is_cap(row)}
    return any((_has_worker(row) and _saved(row) and not _is_cap(row)) or (_is_link(row) and
               (_cap_key(row) not in keys or any(field in row for field in (*CAP_FIELDS, *LEGACY_COUNTS)))) for row in rows)


def _read():
    rows = _load_rows()
    cutoff = time.time() - SAVED_CAP_TTL
    migrate = _needs_migration(rows)
    rows = [_sanitize(row) for row in rows]
    if migrate or _needs_migration(rows):
        rows = _migrate(rows, cutoff)
    return [row for row in rows if not _is_cap(row) or not _saved(row)
            or row['saved_at'] > cutoff or _at_cap(row) or _cap_key(row) in _feedback_in_flight]


def _backup_legacy_store():
    backup = FILE.with_name(FILE.name + '.pre-b278')
    if backup.exists() or not _needs_migration(_load_rows()):
        return
    # Publish a complete copy once. If the copy fails, leave the original in place.
    with tempfile.NamedTemporaryFile(dir=FILE.parent) as copy, FILE.open('rb') as original:
        shutil.copyfileobj(original, copy)
        copy.flush()
        try:
            os.link(copy.name, backup)
        except FileExistsError:
            pass


def _write(rows, *, adopt=()):
    live = {}
    retained = [row for row in rows if not _is_cap(row)]
    for row in retained:
        if _is_link(row):
            live.setdefault(_cap_key(row), []).append(row)
    saved = []
    for row in rows:
        if not _is_cap(row):
            continue
        key = _cap_key(row)
        if key in adopt or row.get('pending_adoption'):
            _adopt_orphaned_counts(row, live.get(key, ()))
        if key in live:
            row.pop('saved_at', None)
            retained.append(row)
        elif (_saved(row) or key in _feedback_in_flight or
              (key[1] and any(row.get(field) for field in ('counts', 'paused', 'unreadable')))):
            if not _saved(row):
                row['saved_at'] = time.time()
            if key in _feedback_in_flight:
                retained.append(row)  # a concurrent stop must keep the row until delivery settles
            else:
                saved.append(row)
    saved.sort(key=lambda row: (bool(row.get('paused') or row.get('unreadable')), row['saved_at']))
    retained.extend(saved[max(0, len(saved) - MAX_SAVED_CAPS):])
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=FILE.parent,
                                         prefix=FILE.name + '.', delete=False) as stream:
            temporary = stream.name
            json.dump(retained, stream)
        _backup_legacy_store()
        os.replace(temporary, FILE)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _check_delivery(worker, thread):
    if any(pane == worker and (thread is None or bound in (None, thread))
           for pane, bound in _feedback_in_flight):
        raise ValueError(DELIVERY_ERROR)


def _check_cap(rows, worker, thread):
    caps = [row for row in rows if _is_cap(row) and _matches(row, worker, thread)]
    if any(cap.get('paused') == UPGRADE_PAUSE for cap in caps):
        raise ValueError(UPGRADE_ERROR)
    if any(_at_cap(cap) for cap in caps):
        raise ValueError(CAP_ERROR)
    _check_delivery(worker, thread)


def check_cap(worker, thread):
    with _lock:
        _check_cap(_read(), worker, thread)


def _binding_decision(cap):
    return {field: cap[field] for field in CAP_DECISION if field in cap and field not in ('counts', 'cap_reset')}


def _merge_turn_tracking(bound, unbound):
    first, second = bound.get('restart'), unbound.get('restart')
    if first is not None and second is not None:
        bound['restart'] = {
            'counts': _merge_counts(first.get('counts', {}), second.get('counts', {})),
            **{field: first.get(field, False) or second.get(field, False) for field in ('paused', 'agent')},
        }
    elif second is not None:
        bound['restart'] = second
    prompts = []
    for marker in [*bound.get('review_prompts', ()), *unbound.get('review_prompts', ())]:
        if marker not in prompts:
            prompts.append(marker)
    if prompts:
        bound['review_prompts'] = sorted(prompts, key=lambda marker: float('-inf') if isinstance(marker, str)
                                         else marker[1])
    for field in ('review_turn_at', 'agent_prompt'):
        _keep_latest_time(bound, field, (bound, unbound))
    for field in ('feedback_turn', 'cap_reset'):
        if field in unbound:
            bound.setdefault(field, unbound[field])


def _bind_worker(rows, worker, thread):
    """Pin links that were started before discovery knew the worker's thread."""
    if thread is None or (worker, None) in _feedback_in_flight or (worker, thread) in _feedback_in_flight:
        return False
    unbound = _cap_for(rows, (worker, None))
    if unbound is None:
        return False
    bound = _cap_for(rows, (worker, thread))
    pending_reset = unbound.pop('pending_reset', False)
    if bound is None or pending_reset:
        if bound is not None:
            rows.remove(bound)
        unbound['worker_thread'] = thread
    else:
        rows.remove(unbound)
        first_counts, second_counts = bound.get('counts', {}), unbound.get('counts', {})
        decision = _binding_decision(unbound)
        conflict = bool(decision) and decision != _binding_decision(bound)
        if conflict:
            first, second = bound.get('restart', {}), unbound.get('restart', {})
            if not first.get('agent'):
                first_counts = _reviewer_counts_since(first_counts, first.get('counts', {}))
            if not second.get('agent'):
                second_counts = _reviewer_counts_since(second_counts, second.get('counts', {}))
            for field in (*CAP_DECISION, 'restart'):
                bound.pop(field, None)
            bound['paused'] = UPGRADE_PAUSE
            _keep_latest_time(bound, 'agent_prompt', (bound, unbound))
        else:
            _merge_turn_tracking(bound, unbound)
        if bound.get('paused') == UPGRADE_PAUSE:
            bound['upgrade_at'] = min((cap['upgrade_at'] for cap in (bound, unbound) if 'upgrade_at' in cap),
                                      default=time.time())
            if unbound.get('upgrade_alerted'):
                bound['upgrade_alerted'] = True
        counts = _merge_counts(first_counts, second_counts)
        if counts:
            bound['counts'] = counts
        if conflict and (first.get('agent') and not _saved(bound) or second.get('agent') and not _saved(unbound)):
            bound['restart'] = {**_restart_state(bound), 'agent': True}
        if unbound.get('pending_adoption'):
            bound['pending_adoption'] = True
    for link in rows:
        if not _is_link(link) or not _matches(link, worker, thread):
            continue
        link['worker_thread'] = thread
    return True


def _move_counts(cap, previous, reviewer):
    counts = dict(cap['counts'])
    counts[reviewer] = counts.pop(previous)
    cap['counts'] = counts
    start = dict(cap.get('restart', {}).get('counts', {}))
    if previous in start:
        start[reviewer] = start.pop(previous)
        cap['restart'] = {**cap['restart'], 'counts': start}


def _adopt_counts(cap, link):
    """Bind anonymous saved counts or counts for this pane before its thread was known."""
    reviewer = _reviewer_key(link)
    counts = cap.get('counts', {})
    if reviewer in counts:
        return
    unbound = _reviewer_key(dict(reviewer=link['reviewer']))
    previous = unbound if unbound in counts else UNOWNED
    if previous in counts:
        _move_counts(cap, previous, reviewer)


def _adopt_orphaned_counts(cap, links):
    cap.pop('pending_adoption', None)
    if len(links) != 1:
        return
    reviewer = _reviewer_key(links[0])
    counts = cap.get('counts', {})
    if reviewer in counts:
        return
    if _cap_key(cap) in _feedback_in_flight:
        # The departing reviewer can still record a round under its old key.
        cap['pending_adoption'] = True
    elif counts:
        def severity(owner):
            entry = counts[owner]
            rounds = entry.get('rounds', 0)
            return max(rounds + entry.get('empty_rounds', 0), entry.get('stale_rounds', 0)), rounds

        _move_counts(cap, max(counts, key=severity), reviewer)


def add(*, worker, reviewer, reviewer_thread, name, agent, seen_working=False, worker_thread=None, reset=False):
    link = dict(worker=worker, reviewer=reviewer, reviewer_thread=reviewer_thread,
                name=name, agent=agent, created=time.time(), seen_working=seen_working, worker_thread=worker_thread)
    key = _cap_key(link)
    with _lock:
        rows = _read()
        if reset:
            _check_delivery(worker, worker_thread)
        else:
            _check_cap(rows, worker, worker_thread)
            if worker_thread is None and any(
                    _matches(row, worker, None) and row.get('worker_thread') is not None
                    and (_is_cap(row) and (_saved(row) or row.get('counts'))
                         or _is_link(row) and row['reviewer'] == reviewer)
                    for row in rows if _is_cap(row) or _is_link(row)):
                raise ValueError("Wait for the worker's conversation to load before linking this reviewer.")
        _bind_worker(rows, worker, worker_thread)
        cap = _cap_for(rows, key)
        if not reset and cap is not None and cap.get('paused') == UPGRADE_PAUSE:
            raise ValueError(UPGRADE_ERROR)
        if reset and cap is not None:
            rows.remove(cap)
        if cap is None or reset:
            cap = _new_cap(worker, worker_thread, name)
            if reset:
                cap['cap_reset'] = secrets.token_hex(8)
                if worker_thread is None:
                    cap['pending_reset'] = True
            rows.append(cap)
        if reviewer_thread is not None:
            _adopt_counts(cap, link)  # this reviewer's own counts from before its thread was known
        links = [row for row in rows if _is_link(row)]
        remaining = [row for row in links if (row['worker'], row['reviewer']) != (worker, reviewer)]
        remaining = (remaining + [link])[-MAX_LINKS:]
        adopt = {key} | {_cap_key(row) for row in links if row not in remaining}
        _write([row for row in rows if _is_cap(row)] + remaining, adopt=adopt)
        audit.record('review.link', f'Linked reviewer {reviewer} to {name}', worker=worker, reviewer=reviewer)


def reviewed_by():
    return _reviewers()


def linked_worker_threads(worker, reviewer, reviewer_thread):
    """The worker conversations this reviewer conversation's links are bound to (None: not bound yet)."""
    if reviewer_thread is None:
        return set()
    with _lock:
        return {row.get('worker_thread') for row in _read()
                if _is_link(row) and row['worker'] == worker and row['reviewer'] == reviewer
                and row.get('reviewer_thread') in (None, reviewer_thread)}


def paused():
    return _reviewers(paused_only=True)


def _waits_on_mak(cap):
    """The review waits on Mak: an escalation not yet sent, or a pause that only Mak's answer ends.

    A pause that the parent tab still decides (`parent_stage`) or a pause for an agent upgrade does not.
    """
    if 'escalate' in cap:
        return True
    return 'parent_stage' not in cap and cap.get('paused') != UPGRADE_PAUSE and bool(_at_cap(cap) or cap.get('unreadable'))


def unreviewed_work(worker, reviewer, scope):
    """Whether pending review work or a live Git change should keep this idle reviewer open."""
    worker_thread = helper_tabs._thread(worker)
    reviewer_thread = helper_tabs._thread(reviewer)
    with _lock:
        rows = _read()
        link = next((row for row in rows if _is_link(row) and _matches(row, worker['id'], worker_thread)
                     and row['reviewer'] == reviewer['id']
                     and (reviewer_thread is None or row.get('reviewer_thread') in (None, reviewer_thread))), None)
        if link is None:
            return False
        cap = _cap_for(rows, _cap_key(link)) or {}
    if cap.get('paused') or _waits_on_mak(cap):
        return False
    # An approval follow-up is open until it is cleared, or until an unchanged completion after the verdict, whose
    # prompt has no time, may have been the approval's own turn.
    verdict_at = (link.get('approved_state') or {}).get('at')
    open_follow_up = link.get('approved_turn') and not (
        verdict_at is not None and link.get('follow_up_untimed_at', 0) > verdict_at)
    if (open_follow_up or any(link.get(key) for key in ('seen_working', 'awaiting', 'stalled', 'request_id'))
            or any(key.startswith('stale_') for key in link)):
        return True
    changed, _ = _follow_up_change(link, worker, scope)
    return changed is True


def escalated(entries):
    """Live worker panes whose review waits on Mak (B400: a hidden helper tab shows again until Mak answers)."""
    threads = {entry['id']: helper_tabs._thread(entry) for entry in entries}
    with _lock:
        return {row['worker'] for row in _read()
                if _is_cap(row) and row['worker'] in threads and _waits_on_mak(row)
                and row.get('worker_thread') in (None, threads[row['worker']])}


def in_parent_stage(pane, thread=None):
    try:
        with _lock:
            return any(_is_cap(row) and _matches(row, pane, thread)
                       and isinstance(row.get('parent_stage'), dict) for row in _read())
    except OSError:
        return False


def _reviewers(*, paused_only=False):
    with _lock:
        rows = _read()
        result = {}
        for link in rows:
            if not _is_link(link) or not all(re.fullmatch(r'%[0-9]+', link[key]) for key in ('worker', 'reviewer')):
                continue
            if paused_only:
                cap = _cap_for(rows, _cap_key(link)) or {}
                if not (cap.get('paused') or cap.get('unreadable')):
                    continue
            reviewers = result.setdefault(link['worker'], [])
            if link['reviewer'] not in reviewers:
                reviewers.append(link['reviewer'])
        return result


def approved_commits():
    """{worker: {reviewer: {'commit': full hash, 'at': Unix seconds}}} for approvals bound to a commit with a clean
    tree. An approval of uncommitted work, or of a folder with no readable commit, is not listed."""
    with _lock:
        result = {}
        for link in _read():
            if _is_link(link) and isinstance(link.get('approved_commit'), str):
                result.setdefault(link['worker'], {})[link['reviewer']] = {
                    'commit': link['approved_commit'], 'at': link.get('approved_at')}
        return result


def remove(worker, reviewer=None):
    with _lock:
        rows = _read()
        stopped = [row for row in rows if _is_link(row) and row['worker'] == worker
                   and reviewer in (None, row['reviewer'])]
        if not stopped:
            return False
        _write([row for row in rows if row not in stopped], adopt={_cap_key(row) for row in stopped})
        audit.record('review.stop', f"Stopped review for {stopped[0]['name']}", worker=worker,
                     **({'reviewer': reviewer} if reviewer else {}))
        return True


def close(pane):
    with _lock:
        rows = _read()
        stopped = [row for row in rows if _is_link(row) and row['reviewer'] == pane]
        remaining = [row for row in rows if row not in stopped and
                     not (isinstance(row, dict) and row.get('worker') == pane)]
        if remaining != rows:
            _write(remaining, adopt={_cap_key(row) for row in stopped})


def _from_parent(stage, pane, thread):
    # The watcher binds the stage to the parent's conversation before it sends the notice. Until then, nobody decides.
    return stage is not None and pane == stage['pane'] and thread is not None and thread == stage['thread']


def _parent_can_decide(cap):
    stage = cap['parent_stage']
    # A detached worker can get its grant after terminal delivery but before the watcher records the notice.
    return not _saved(cap) or stage.get('sent') or _feedback_in_flight.get(_cap_key(cap)) == stage


def hold_agent_prompt(worker, *, worker_thread=None):
    """Mark the worker's live caps before a parent prompt is delivered: its turn can finish before the send
    returns. Returns an undo for a failed send. agent_prompt() still runs after delivery for the grant."""
    with _lock:
        rows = _read()
        at = time.time()
        held = {}
        for cap in rows:
            if _is_cap(cap) and _matches(cap, worker, worker_thread) and not _saved(cap):
                held[_cap_key(cap)] = cap.get('agent_prompt')
                cap['agent_prompt'] = at
        if held:
            _write(rows)

    def undo():
        with _lock:
            rows = _read()
            changed = False
            for cap in rows:
                # A bind during delivery can move the marker to another key of the same worker.
                if not (_is_cap(cap) and cap['worker'] == worker and cap.get('agent_prompt') == at):
                    continue
                previous = held.get(_cap_key(cap), max((time for time in held.values() if time is not None),
                                                       default=None))
                if previous is None:
                    cap.pop('agent_prompt')
                else:
                    cap['agent_prompt'] = previous
                changed = True
            if changed:
                _write(rows)
    return undo


def agent_prompt(worker, sender=None, sender_thread=None, *, worker_thread=None):
    """Mark an agent turn and let the parent grant two more rounds once per task."""
    with _lock:
        rows = _read()
        marked = False
        for cap in rows:
            if not (_is_cap(cap) and _matches(cap, worker, worker_thread)):
                continue
            stage = cap.get('parent_stage')
            grant = 'limit' not in cap and _from_parent(stage, sender, sender_thread)
            if _saved(cap) and not (grant and _parent_can_decide(cap)):
                continue
            cap['agent_prompt'] = time.time()
            if grant:
                cap['limit'] = MAX_ROUNDS + EXTRA_ROUNDS
                cap['grant_rounds'] = max((entry.get('rounds', 0) + entry.get('empty_rounds', 0)
                                           for entry in cap.get('counts', {}).values()), default=0)
                for field in ('paused', 'parent_stage', 'parent_missing'):
                    cap.pop(field, None)
                if _saved(cap):
                    cap['saved_at'] = time.time()
            marked = True
        if marked:
            _write(rows)
        return marked


def escalate(worker, sender, sender_thread=None, *, worker_thread=None):
    with _lock:
        rows = _read()
        waiting = [cap for cap in rows if _is_cap(cap) and _matches(cap, worker, worker_thread)
                   and _from_parent(cap.get('parent_stage'), sender, sender_thread)
                   and _parent_can_decide(cap)]
        if not waiting:
            raise ValueError('That tab has no review waiting on this tab.')
        for cap in waiting:
            cap['escalate'] = cap.pop('parent_stage')['notice']
        _write(rows)


def resume_stalled(worker, reviewer, request_id, action, *, last_reply=None, reviewer_entry=None):
    if not all(isinstance(pane, str) and re.fullmatch(r'%[0-9]+', pane)
               for pane in (worker, reviewer)) or not (
            isinstance(request_id, str) and re.fullmatch(r'[0-9a-f]{8}', request_id)):
        raise ValueError('This review is no longer stalled.')
    with _lock:
        links = _read()
        link = next((item for item in links if _is_link(item)
                     and item.get('worker') == worker and item.get('reviewer') == reviewer
                     and item.get('stalled') == request_id), None)
        if link is None:
            raise ValueError('This review is no longer stalled.')
        if action not in ('resend', 'latest'):
            raise ValueError('Choose resend or latest.')
        if action == 'latest' and (last_reply is None or reviewer_entry is None
                                   or _digest(last_reply(reviewer_entry)) != link.get('stalled_reply')):
            raise ValueError('The reviewer replied again. Open the tab.')
        if action == 'latest':
            link['send_reply'] = link['stalled_reply']
        link.pop('stalled')
        link.pop('stalled_reply', None)
        if action == 'resend':
            link['seen_working'] = True
        else:
            link.update(awaiting=True, reviewer_seen_working=True, idle_ticks=0)
            link.pop('request_id', None)
        _write(links)
        return dict(link)


def _digest(text):
    return hashlib.sha256((text or '').encode()).hexdigest()


def _bounded_reply(reply, reviewer):
    if len(reply) <= 12000:
        return reply
    return reply[:12000] + f"\n\n[Cut at 12000 characters. Full reply: {reviewer.get('transcript', '')}]"


def _lost(link, reason):
    link['lost_ticks'] = link.get('lost_ticks', 0) + 1
    link['lost_reason'] = reason
    return None if link['lost_ticks'] >= LOSS_TICKS else link


def _agent_prompt_fresh(cap, at=None):
    sent = cap.get('agent_prompt')
    return sent is not None and (time.time() if at is None else at) < sent + AGENT_PROMPT_TTL


def _round_limit(cap):
    return min(cap.get('limit', MAX_ROUNDS), MAX_ROUNDS + EXTRA_ROUNDS)


def _counts_at_limit(cap, counts, next_round=None):
    failed = [counts.get(key, 0) + (key == next_round) for key in ('rounds', 'empty_rounds')]
    # Switching between changed and unchanged work cannot extend the grant, and a granted task never runs more
    # than MAX_ROUNDS + EXTRA_ROUNDS failed reviews for one reviewer.
    return max(failed) >= _round_limit(cap) or (
        'grant_rounds' in cap and sum(failed) >= min(cap['grant_rounds'] + EXTRA_ROUNDS, MAX_ROUNDS + EXTRA_ROUNDS)) or (
        counts.get('stale_rounds', 0) >= MAX_ROUNDS)  # approvals in a row that could not be verified


def _at_round_limit(cap):
    return any(_counts_at_limit(cap, counts) for counts in cap.get('counts', {}).values())


def _at_cap(cap):
    # An unreadable pause can survive an agent relink. A round cap still requires a restart or grant.
    return (cap.get('paused') and not cap.get('unreadable')) or _at_round_limit(cap)


def _pause_at_limit(cap, link):
    if _at_round_limit(cap) and not cap.get('paused'):
        cap.update(paused=True, name=link['name'])
        return True
    return False


def _record_round(cap, link, field):
    """Stage the increment and its pause for the same locked write in _apply_step."""
    reviewer = _reviewer_key(link)
    counts = cap.get('counts', {})
    own = dict(counts.get(reviewer, {}))
    if field != 'stale_rounds':
        own.pop('stale_rounds', None)  # findings end this reviewer's run of mismatched approvals
        _clear_restart_counts(cap, reviewer, ('stale_rounds',))
    own[field] = own.get(field, 0) + 1
    cap['counts'] = {**counts, reviewer: own}
    return _pause_at_limit(cap, link)


def _clear_restart_counts(cap, reviewer, fields):
    restart = cap.get('restart')
    baseline = restart.get('counts', {}) if restart is not None else {}
    if reviewer in baseline:
        own = {field: count for field, count in baseline[reviewer].items() if field not in fields}
        cap['restart'] = {**restart, 'counts': {**baseline, reviewer: own}}


INTRO = ('Review its work: check the changes and the claims, then list what is wrong or missing. '
         'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab.')


# Match the request's tab line, fixed instructions and id. Allow a folder note and wrapped whitespace.
REQUEST_PATTERN = re.compile(
    r'\[Tether review\]\s+The\s+\S+\s+tab\s+“.{0,300}?”\s+\(%[0-9]+\)\s+finished\s+a\s+turn\.\s+'
    + r'\s+'.join(re.escape(word) for word in INTRO.split()) + r'\s+Review\s+id:\s+([0-9a-f]{8})\.', re.S)


def _follow_up_change(link, worker, scope):
    """Compare the verdict with one live scope. Return (changed or None, scope for the request)."""
    before = link.get('approved_state')
    if before is None:  # older links may only have the review's binding
        commit = link.get('approved_dirty') or link.get('review_commit')
        before = dict(commit=commit, dirty=False if commit and link.get('review_clean') else None,
                      folder=link.get('review_folder'))
    folder, at = before.get('folder'), before.get('at')
    fields = ('commit', 'dirty', 'mark')
    if (at is None or scope is None) and all(before.get(field) is None for field in fields):
        return False, None  # no verdict baseline
    # With no saved folder, resolve it as the verdict's HEAD read did: the usual request rule.
    entry = dict(_review_entry(worker, folder), review_after=at) if folder else worker
    live = _call(scope, entry)
    if not isinstance(live, dict) or not isinstance(live.get('commit'), str):
        return None, None
    dirty, mark, before_mark = live.get('dirty'), live.get('mark'), before.get('mark')
    if type(dirty) is not bool or not isinstance(mark, str):
        return None, None
    if at is not None and folder and live.get('folder') and live['folder'] != folder:
        return True, live  # a newer gate record selected a worktree with no baseline
    changed = (before.get('commit') not in (None, live['commit'])
               or type(before.get('dirty')) is bool and dirty != before['dirty']  # a new or removed draft
               or before_mark is not None and not review_scope.same_work_mark(before_mark, mark))
    if not changed and any(before.get(field) is None for field in fields):
        return None, None
    return changed, live


def _clear_follow_up(link):
    for key in ('approved_turn', 'approved_before', 'approved_dirty', 'confirm_ticks', 'follow_up_untimed_at'):
        link.pop(key, None)


def _named_commit(reply):
    """The commit an approval names right after APPROVED, in lower case, or None. A hash later in the reply does not
    count: only the start says what the approval is for."""
    found = NAMED.match(reply)
    return found[1].lower() if found else None


def _merge(old, updated, stored):
    """Apply the step's changes and keep fields changed only by another writer."""
    missing = object()
    merged = dict(stored)
    for key in set(old) | set(updated):
        if old.get(key, missing) == updated.get(key, missing):
            continue
        if key == 'agent_prompt' and stored.get(key, missing) != old.get(key, missing):
            continue
        if key == 'review_prompts':
            before, after = old.get(key, []), updated.get(key, [])
            pending = [marker for marker in stored.get(key, []) if marker not in before or marker in after]
            pending.extend(marker for marker in after if marker not in before and marker not in pending)
            if pending:
                merged[key] = pending
            else:
                merged.pop(key, None)
        elif key in updated:
            merged[key] = updated[key]
        else:
            merged.pop(key, None)
    return merged


def _link_id(link):
    return link['worker'], link['reviewer'], link.get('created')


def _link_for(rows, link):
    return next((row for row in rows if _is_link(row) and _link_id(row) == _link_id(link)), None)


def _decision(cap):
    return tuple(cap.get(field) for field in CAP_DECISION)


def _reviewer_ready(link, entries):
    reviewer = entries.get(link['reviewer'])
    thread = helper_tabs._thread(reviewer) if reviewer is not None else None
    return thread is not None and link.get('reviewer_thread') in (None, thread)


def _binding_pending(rows, key, entries):
    # A just-added browser reset wins before delivery only if it can bind to this conversation.
    worker, thread = key
    entry = entries.get(worker)
    if thread is None or entry is None or helper_tabs._thread(entry) != thread:
        return False
    unbound = _cap_for(rows, (worker, None))
    return bool(unbound and unbound.get('pending_reset') and any(
        _is_link(row) and _cap_key(row) == (worker, None) and _reviewer_ready(row, entries) for row in rows))


def _linked(link, snapshot, entries, *, delivery=None, blocking=None, correction=None):
    """Check the link and decision again after the transcript reads and before terminal I/O."""
    with _lock:
        rows = _read()
        stored = _link_for(rows, link)
        key = _cap_key(link)
        cap = _cap_for(rows, key)
        if (stored is None or _cap_key(stored) != key or cap is None
                or _decision(cap) != _decision(snapshot) or key in _feedback_in_flight
                or _binding_pending(rows, key, entries)):
            return False
        if blocking is not None and _at_cap(blocking):
            return False
        if delivery is not None:
            # A competing tick may have consumed or replaced this request while its reply was read.
            if correction is not None:
                if stored.get('stale_correction') != correction:
                    return False
            elif not stored.get('awaiting') or any(stored.get(field) != link.get(field)
                    for field in ('request_id', 'send_reply', 'reviewer_before')):
                return False
            # The browser holds SEND_LOCK when it calls add. Never take that lock while holding _lock.
            _feedback_in_flight[key] = delivery
        return True


def _release(key, delivery, *, writing=False):
    if _feedback_in_flight.get(key) is delivery:
        del _feedback_in_flight[key]
        if not writing:
            # A refused send has no round to write, but a concurrent stop may still need to transfer counts.
            rows = _read()
            cap = _cap_for(rows, key)
            if cap is not None and cap.get('pending_adoption'):
                _write(rows)


def _call(callback, entry):
    """An optional lookup for one worker. A missing callback or lookup error gives None."""
    if callback is None:
        return None
    try:
        return callback(entry)
    except Exception:
        return None


def _review_entry(worker, folder):
    """Pass the request's folder to a Git lookup without changing the live pane entry."""
    return dict(worker, review_folder=folder) if isinstance(folder, str) and folder else worker


def _verdict_state(link, worker, scope, commit=None, verified_folder=None):
    """Read the verdict's worktree before delivery can start another worker turn. When the scope read fails, keep the
    approved commit and the worktree that the HEAD read just verified, so a later commit there is still a change."""
    folder = link.get('review_folder') or verified_folder
    live = _call(scope, _review_entry(worker, folder))
    live = live if isinstance(live, dict) else {}
    return dict(commit=live.get('commit') or commit, dirty=live.get('dirty'), mark=live.get('mark'),
                folder=folder or live.get('folder'))


def _request_text(value):
    # A field cannot start a new line or mimic the delimiter that the UI uses to find the worker's reply.
    value = value.replace('\r\n', '\n').removesuffix('\n')
    return value.replace('\n', ' ').replace('Its last reply:', r'Its last reply\:')


def _checklist_binding(result, folder):
    if not isinstance(result, dict):
        return ''
    if result.get('status') == 'unchecked':
        reason = result.get('reason')
        if isinstance(reason, str) and reason.strip():
            return f'Reliability checklist: not checked ({_request_text(reason)[:200]}). '
        return ''
    rows = result.get('rows')
    if result.get('status') != 'hit' or not isinstance(rows, list) or not rows:
        return ''
    fields = ('id', 'title', 'check', 'incident')
    if not all(isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key].strip() for key in fields)
               for row in rows):
        return ''
    lines = [f'\nReliability checklist: this diff hits {len(rows)} row(s) of '
             'origin/main:docs/reliability-checklist.md. Confirm each check in your reply:']
    for row in rows[:8]:
        row_id, title, check, incident = (row[key] for key in fields)
        line = _request_text(f'- {row_id} {title}: {check} (Incident: {incident})')
        lines.append(line if len(line) <= 400 else f'{line[:399]}…')
    if len(rows) > 8:
        lines.append(f'- and {len(rows) - 8} more: run python3 scripts/checklist.py --repo {folder}')
    return '\n'.join(lines) + '\n'


def _can_send(entry, state, clear):
    return (state == 'Idle' or state == 'Working' and entry.get('agent') == 'claude') and clear(entry['id'])


def _send_message(entry, text, state, send, clear):
    if not _can_send(entry, state, clear):
        return False
    try:
        send(entry, text, state)
    except (DraftError, RuntimeError):
        return False
    _last_sent[entry['id']] = time.monotonic()
    return True


def _send_worker(link, cap, snapshot, worker, text, state, send, clear, entries, delivery,
                 *, summary=False, blocking=False, correction=None):
    if not (_can_send(worker, state, clear)
            and _linked(link, snapshot, entries, delivery=delivery, blocking=cap if blocking else None,
                        correction=correction)):
        return False
    key = _cap_key(link)
    sent_at = time.time()
    sent = False
    try:
        sent = _send_message(worker, text, state, send, clear)
    finally:
        if not sent:
            # Another reviewer in this tick can try, including when the terminal rejects the text.
            with _lock:
                _release(key, delivery)
    if not sent:
        return False
    if summary or correction is not None:
        kind = 'review-summary' if summary else 'review-correction'
        cap['review_prompts'] = [*cap.get('review_prompts', ()), [kind, sent_at]]
    cap['feedback_turn'] = {'sent_at': sent_at, 'phase': 'queued' if state == 'Working' else 'pending'}
    if state == 'Working':
        link['worker_busy_send'] = True
        if correction is None:
            link['seen_working'] = True
    return True


def _send_correction(link, correction, cap, snapshot, entries, status, send, clear, delivery):
    worker = entries.get(link['worker'])
    if worker is None or helper_tabs._thread(worker) != link.get('worker_thread') or not _reviewer_ready(link, entries):
        return
    now = correction['now']
    detail = (f'This tab is now at {now}.' if now else
              "Tether could not read this tab's commit after the approval, so the approval cannot be checked.")
    end = (CORRECTION_PAUSED if _at_cap(cap) or cap.get('unreadable') else
           CORRECTION_REQUESTED if link['stale_correction']['requested'] else CORRECTION_WAIT)
    text = f"{CORRECTION_HEAD}{correction['commit']} only. {detail} {CORRECTION_ACTION} {end}"
    # Claude can receive the correction while working, after it may already have acted on the approval.
    if _send_worker(link, cap, snapshot, worker, text, status(worker)['state'], send, clear, entries, delivery,
                    correction=correction):
        link.pop('stale_correction')


def _prompt_kind(marker):
    return marker if isinstance(marker, str) else marker[0]  # old stores have no send time


def _idle_source(link, cap, turn_read):
    source, started_at, finished_at = turn_read
    pending = cap.get('review_prompts', [])
    feedback = cap.get('feedback_turn')
    observed = ('restart' in cap or link['seen_working'] or
                feedback and feedback['phase'] == 'working'
                and ['review-correction', feedback['sent_at']] in pending)
    fresh = finished_at is None or finished_at > cap.get('review_turn_at', float('-inf'))
    clipped = source in ('unknown', 'unknown-background')
    # Use the prompt's time when visible. An earlier queued prompt can finish after a later send.
    turn_at = started_at if started_at is not None else finished_at
    if fresh and (observed or finished_at is not None or not clipped):
        if source == 'user' and started_at is not None:
            # A later user prompt can finish between ticks, after the marked turn already ran.
            pending = [marker for marker in pending if isinstance(marker, str) or marker[1] >= started_at]
        for index, marker in enumerate(pending):
            if source != _prompt_kind(marker) and not (clipped and not link.get('worker_busy_send')):
                continue
            if (not isinstance(marker, str) and
                    (turn_at is not None and turn_at < marker[1]
                     or finished_at is not None and finished_at < marker[1])):
                continue
            source = _prompt_kind(marker)
            pending = pending[:index] + pending[index + 1:]
            break
        if pending:
            cap['review_prompts'] = pending
        else:
            cap.pop('review_prompts', None)
    if not pending and cap.get('paused') != UPGRADE_PAUSE:
        cap.pop('review_turn_at', None)
    elif pending and finished_at is not None and fresh:
        cap['review_turn_at'] = finished_at
    if feedback:
        sent_at = feedback['sent_at']
        after_send = ((turn_at is None or turn_at >= sent_at) and
                      (finished_at is None or finished_at >= sent_at))
        if after_send and source in ('review-feedback', 'review-summary', 'review-correction'):
            cap.pop('feedback_turn')
        elif feedback['phase'] == 'queued':
            # An idle tick can be the end of the turn that was already running when we queued feedback.
            cap['feedback_turn'] = {**feedback, 'phase': 'pending'}
        elif after_send and (feedback['phase'] == 'working' or source == 'user' and started_at is not None):
            cap.pop('feedback_turn')
            # The correction's turn ended unmatched (Antigravity has no prompt time). Its marker must not outlive it.
            if (stuck := ['review-correction', sent_at]) in cap.get('review_prompts', ()):
                cap['review_prompts'] = [marker for marker in cap['review_prompts'] if marker != stuck]
                if not cap['review_prompts']:
                    cap.pop('review_prompts')
            if source == 'user' and started_at is not None:
                cap.setdefault('restart', _restart_state(cap))
            elif source == 'user' or clipped:
                source = 'review-feedback'
    if cap.get('paused') == UPGRADE_PAUSE:
        # Only a new user prompt can release an upgrade pause. An old turn may still be running at migration.
        if source == 'unknown' or (source == 'user' and started_at is not None
                                   and started_at < cap.get('upgrade_at', float('-inf'))):
            return 'background', started_at, finished_at, clipped
    if source in ('unknown', 'unknown-background'):
        source = 'background' if source == 'unknown-background' else 'user'
    return source, started_at, finished_at, clipped


def _restart_state(cap):
    return {'counts': cap.get('counts', {}), 'paused': bool(cap.get('paused') or cap.get('unreadable'))}


PARENT_SUMMARY = (f'[Tether review] {MAX_ROUNDS} review rounds still found blocking problems. '
                  'Your parent tab decides the next step. Do not fix them in this turn.\n\nOpen findings:\n\n')
PARENT_SUMMARY_END = ('Reply with the open findings and what you tried. End with "Needs you: no" unless '
                      'a finding needs Mak (taste, new authority, or you disagree with the reviewer).')
PARENT_NOTICE = re.compile(r'^\[Tether review\] “[^\n]*” \((%[0-9]+)\) still has blocking review findings after '
                           rf'{MAX_ROUNDS} rounds\. Open findings: “')


def _parent_options(worker):
    cli = Path(__file__).resolve().with_name('agent_tab_cli.py')
    return (f'To allow 2 more rounds, send it the next step: python3 {cli} send --pane {worker} "<what to fix>". '
            f'To hand it to Mak: python3 {cli} escalate --pane {worker}.')


FEEDBACK = (
    (re.compile(r'^\[Tether review\] Feedback from the reviewer tab \(%[0-9]+\):\n\n'),
     '\n\nFix what it raises, then stop and wait.'),
    (re.compile(r'^\[Tether review\] Stopped after [0-9]+ review rounds: the reviewer tab \(%[0-9]+\) '
                r'still finds blocking problems\. Do not fix them in this turn\. Mak decides the next step\.'),
     'then end with "Needs you: yes".'),
    (re.compile(r'^\[Tether review\] Approved by the reviewer tab \(%[0-9]+\)'
                r'(?: for commit (?:[0-9a-f]{12}|[0-9a-f]{40}|[0-9a-f]{64})'
                r'(?: and its uncommitted changes)?)?:\n\n'),
     'the next step your instructions give for an approved review, then stop.'),
)
CORRECTION_HEAD = '[Tether review] Correction: the approval above is for commit '
CORRECTION_ACTION = 'Do not push, deploy or merge on that approval.'
CORRECTION_PAUSED = 'The review is paused: Mak decides.'
CORRECTION_REQUESTED = 'Tether asked the reviewer to review the current commit.'
CORRECTION_WAIT = 'Wait for the next review.'


def prompt_source(text):
    if text.startswith('[Working directory changed to '):
        text = text.partition('. Run commands and resolve relative paths there.] ')[2]
    if text.startswith(PARENT_SUMMARY) and text.rstrip().endswith(PARENT_SUMMARY_END):
        return 'review-summary'
    notice = PARENT_NOTICE.match(text)
    if notice and text.rstrip().endswith(_parent_options(notice[1])):
        return 'review-notice'
    if text.startswith(CORRECTION_HEAD) and any(text.rstrip().endswith(f'{CORRECTION_ACTION} {end}')
            for end in (CORRECTION_PAUSED, CORRECTION_REQUESTED, CORRECTION_WAIT)):
        return 'review-correction'
    if any(head.match(text) and text.rstrip().endswith(tail) for head, tail in FEEDBACK):
        return 'review-feedback'
    return None


def _parent_entry(stage, entries):
    entry = entries.get(stage['pane'])
    if entry is not None and entry.get('agent') in ('claude', 'codex'):
        thread = helper_tabs._thread(entry)
        if thread is not None and stage['thread'] in (None, thread):
            return entry
    return None


def _findings_preview(findings):
    preview = findings.strip().replace('\n', ' ')
    return preview[:300] + ('…' if len(preview) > 300 else '')


def _parent_stage(worker, entries, reply, parent):
    owner = _call(parent, worker)
    if owner is None:
        return None
    pane, thread = owner
    entry = entries.get(pane)
    if entry is not None and entry.get('agent') != 'shell':
        live_thread = helper_tabs._thread(entry)
        if entry.get('agent') not in ('claude', 'codex') or (
                live_thread is not None and thread not in (None, live_thread)):
            return None
        thread = live_thread or thread
    return dict(pane=pane, thread=thread, notice=_findings_preview(reply), sent=False)


def _send_parent_notice(cap, entries, status, send, clear):
    stage = cap.get('parent_stage')
    if stage is None or stage['sent']:
        return
    entry = _parent_entry(stage, entries)
    if entry is None:
        return
    key = _cap_key(cap)
    worker = key[0]
    text = (f'[Tether review] “{cap["name"]}” ({worker}) still has blocking review findings after {MAX_ROUNDS} rounds. '
            f'Open findings: “{stage["notice"]}”\n\n{_parent_options(worker)}')
    state = status(entry)['state']
    if not _can_send(entry, state, clear):
        return
    delivery = stage
    with _lock:
        rows = _read()
        current = _cap_for(rows, key)
        if (current is None or _decision(current) != _decision(cap) or key in _feedback_in_flight
                or _binding_pending(rows, key, entries)):
            return
        _feedback_in_flight[key] = delivery
    try:
        sent_at = time.time()
        sent = _send_message(entry, text, state, send, clear)
        with _lock:
            rows = _read()
            current = _cap_for(rows, key)
            if current is not None and current.get('parent_stage') == stage:
                if sent:
                    current['parent_stage']['sent'] = True
                else:
                    current['parent_stage']['failed'] = stage.get('failed', 0) + 1
            if sent:
                parent = _cap_for(rows, (entry['id'], helper_tabs._thread(entry))) or _cap_for(rows, (entry['id'], None))
                if parent is not None and not _saved(parent):
                    parent['review_prompts'] = [*parent.get('review_prompts', ()), ['review-notice', sent_at]]
            _release(key, delivery, writing=True)
            _write(rows)
    finally:
        with _lock:
            _release(key, delivery)


def _step_parent(cap, entries, status, send, clear):
    """Advance or escalate a parent decision even after the last reviewer stops."""
    key = _cap_key(cap)
    worker = entries.get(key[0])
    if worker is not None and key[1] is not None and helper_tabs._thread(worker) != key[1]:
        return None
    stage = cap.get('parent_stage')
    if stage is None and 'escalate' not in cap:
        return None
    updated = dict(cap)
    if stage is not None:
        entry = _parent_entry(stage, entries)
        if entry is None:
            updated['parent_missing'] = updated.get('parent_missing', 0) + 1
        else:
            updated.pop('parent_missing', None)
            if stage['thread'] is None:
                updated['parent_stage'] = {**stage, 'thread': entry['thread']}
    preview = updated.pop('escalate', None)
    if stage is not None and (updated.get('parent_missing', 0) >= LOSS_TICKS or stage.get('failed', 0) >= LOSS_TICKS):
        preview = stage['notice']
    if preview is not None:
        updated.pop('parent_stage', None)
        updated.pop('parent_missing', None)
    if updated != cap:
        with _lock:
            rows = _read()
            stored = _cap_for(rows, key)
            if (stored is None or _decision(stored) != _decision(cap) or key in _feedback_in_flight
                    or _binding_pending(rows, key, entries)):
                return None
            updated = _merge(cap, updated, stored)
            rows[rows.index(stored)] = updated
            _write(rows)
    if preview is not None:
        return _cap_alert(updated, preview)
    _send_parent_notice(updated, entries, status, send, clear)
    return None


def _turn_ended_on_limit(worker):
    """Detect a terminal Claude limit or login error from the bounded transcript tail."""
    path = worker.get('transcript')
    if worker.get('agent') != 'claude' or not isinstance(path, str) or not path:
        return False
    try:
        with open(path, 'rb') as stream:
            end = stream.seek(0, os.SEEK_END)
            start = max(0, end - TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            data = stream.read(end - start)
        if start:
            data = data.partition(b'\n')[2]  # The first line can start inside a record.
        last_assistant = {}
        for line in data.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                return False
            if record.get('isSidechain') or record.get('isMeta'):
                continue
            if record.get('type') == 'assistant':
                last_assistant = record
            elif agents.person_prompt(record) or (
                    record.get('type') == 'user' and isinstance(record.get('scheduledTaskId'), str)):
                last_assistant = {}
        return (last_assistant.get('isApiErrorMessage') is True
                and last_assistant.get('error') in ('rate_limit', 'authentication_failed'))
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        return False


def _step(link, cap, snapshot, entries, status, send, last_reply, awaiting, clear, delivery,
          work_mark, review_reply, stalled_replies, turn_source, turn_reads, sources, level, parent, reply_effect,
          scope=None, commit=None, fresh=None, folder=None):
    worker = entries.get(link['worker'])
    reviewer = entries.get(link['reviewer'])
    if link['reviewer_thread'] is None:
        thread = reviewer.get('thread') if reviewer is not None else None
        if isinstance(thread, str) and UUID.fullmatch(thread):
            link['reviewer_thread'] = thread
            _adopt_counts(cap, link)
        else:
            if time.time() - link.get('created', 0) <= START_WAIT:
                return link
            link['lost_reason'] = 'reviewer never started'
            return None
    elif reviewer is not None and reviewer.get('thread') != link['reviewer_thread']:
        return _lost(link, 'reviewer conversation changed')
    if worker is None or reviewer is None:
        if time.time() - link.get('created', 0) < GRACE:
            return link
        return _lost(link, 'worker tab is gone' if worker is None else 'reviewer tab is gone')
    thread = helper_tabs._thread(worker)
    if thread is not None and thread != link.get('worker_thread'):
        return _lost(link, 'worker conversation changed')
    link.pop('lost_ticks', None)
    link.pop('lost_reason', None)
    state = status(worker)['state']
    key = _cap_key(link)
    turn = key, cap.get('cap_reset')
    # Only the correction's own pending turn is not a work turn. A marker left by another turn must not block reviews.
    correction_turn = ((feedback := cap.get('feedback_turn')) is not None
                       and ['review-correction', feedback['sent_at']] in cap.get('review_prompts', ()))
    source = started_at = finished_at = None
    clipped = False
    upgrade_paused = cap.get('paused') == UPGRADE_PAUSE
    between_ticks = state == 'Idle' and upgrade_paused and 'restart' not in cap
    if state == 'Idle' and (between_ticks or 'restart' in cap or link['seen_working'] or link.get('approved_turn')
                            or cap.get('review_prompts') or cap.get('feedback_turn') or turn in sources):
        if key not in turn_reads:
            read = (_call(turn_source, worker) if between_ticks else
                    turn_source(worker) if turn_source is not None else 'user')
            turn_reads[key] = read if isinstance(read, tuple) else (read, None, None)
        if upgrade_paused:
            # A parent records its marker before it sends the prompt. The store has it once its turn is readable.
            with _lock:
                stored = _cap_for(_read(), key)
            if stored is not None:
                _keep_latest_time(cap, 'agent_prompt', (cap, stored))
        if between_ticks:
            # This probe must leave the queued turn markers alone.
            candidate, started, finished = turn_reads[key]
            if (candidate == 'user' and _is_time(started) and _is_time(finished)
                    and started >= cap.get('upgrade_at', float('inf'))
                    and finished > cap.get('review_turn_at', float('-inf'))
                    and not _agent_prompt_fresh(cap, started)):
                source, started_at, finished_at = candidate, started, finished
                cap['restart'] = _restart_state(cap)
        else:
            if turn not in sources:
                sources[turn] = _idle_source(link, cap, turn_reads[key])
            source, started_at, finished_at, clipped = sources[turn]
    summary = source == 'review-summary'
    if state == 'Idle' and 'restart' in cap:
        start = cap.pop('restart')
        start = start if isinstance(start, dict) else {}
        if upgrade_paused:
            # An older turn may finish before the queued parent prompt starts. Keep the original send time.
            if _is_time(finished_at) and finished_at <= snapshot.get('review_turn_at', float('-inf')):
                by_agent = True  # A transcript can return a turn that was judged before.
            elif _is_time(started_at):
                # The latest turn may not be the one seen Working. Judge it by its own start. A flag without a
                # marker comes from an old watcher that consumed the marker: it covers only a turn from before
                # the pause.
                by_agent = _agent_prompt_fresh(cap, started_at) or (
                    'agent_prompt' not in cap and start.get('agent') and started_at < cap.get('upgrade_at', float('inf')))
            else:
                by_agent = start.get('agent') or _agent_prompt_fresh(cap)
            if by_agent and _is_time(finished_at):
                # Older watchers could consume the prompt marker before the agent turn ended.
                cap['review_turn_at'] = max(finished_at, cap.get('review_turn_at', float('-inf')))
        else:
            by_agent = start.get('agent', False) or _agent_prompt_fresh(cap)
            if not (summary or source == 'review-notice' or source == 'background' and cap.get('review_prompts')):
                cap.pop('agent_prompt', None)
        # A clipped turn can request a review, but it cannot authorize a cap reset.
        if not by_agent and source == 'user' and not clipped:
            cap.pop('agent_prompt', None)
            for field in ('parent_stage', 'parent_missing', 'limit', 'grant_rounds', 'escalate'):
                cap.pop(field, None)
            cap['counts'] = _reviewer_counts_since(cap.get('counts', {}), start.get('counts', {}))
            if start.get('paused') or not _at_round_limit(cap):
                cap.pop('paused', None)
                cap.pop('unreadable', None)
                cap.pop('upgrade_alerted', None)
                cap.pop('upgrade_at', None)
            cap['cap_reset'] = secrets.token_hex(8)
    if state == 'Idle' and link.pop('worker_busy_send', False) and not summary:
        if any(_prompt_kind(marker) == 'review-summary' for marker in cap.get('review_prompts', ())):
            link['seen_working'] = False
        if not link.get('approved_turn') or finished_at is None:
            return link
        link['seen_working'] = False  # a queued approval needs transcript evidence for this completion
    if source in ('review-summary', 'review-notice'):
        link['seen_working'] = False
        if summary and 'limit' in cap and 'agent_prompt' in cap and not _agent_prompt_fresh(cap):
            cap['agent_prompt'] = time.time()
    if state == 'Working':
        feedback = cap.get('feedback_turn')
        if feedback is not None:
            if feedback['phase'] == 'pending':
                cap['feedback_turn'] = {**feedback, 'phase': 'working'}
        elif (not link['seen_working'] or cap.get('paused') == UPGRADE_PAUSE) and link['worker'] not in awaiting:
            cap.setdefault('restart', _restart_state(cap))
    if state == 'Working' and isinstance(cap.get('restart'), dict) and _agent_prompt_fresh(cap):
        cap['restart'] = {**cap['restart'], 'agent': True}
        # During an upgrade pause the marker stays: a queued parent turn may start after this one.
        if not upgrade_paused and 'limit' not in cap and not cap.get('review_prompts'):
            cap.pop('agent_prompt', None)
    if cap.get('paused') == UPGRADE_PAUSE:
        # Keep watching for a new Mak turn while all review requests and reply delivery are on hold.
        link['seen_working'] = state == 'Working'
        return link
    if link.get('awaiting', False):
        if cap.get('unreadable'):
            link['awaiting'] = False
            return link
        reviewer_state = status(reviewer)['state']
        anchored = review_reply is not None and link.get('request_id')
        reply = review_reply(reviewer, link['request_id']) if anchored and reviewer_state == 'Idle' else None
        pending = reply is REPLY_PENDING
        if reviewer_state == 'Working':
            link['reviewer_seen_working'] = True
            link.pop('pending_idle_ticks', None)
        elif reviewer_state == 'Idle' and not pending and link.pop('reviewer_busy_send', False) and not anchored:
            return link
        elif reviewer_state == 'Idle' and (
                pending or anchored and not reply or not anchored and not link.get('reviewer_seen_working', False)
                and _digest(last_reply(reviewer)) == link.get('reviewer_before')):
            if pending:
                # The transcript has the request. Give its turn longer to finish or show Working again.
                link.pop('idle_ticks', None)
                counter, limit = 'pending_idle_ticks', PENDING_IDLE_TICKS
            else:
                counter, limit = 'idle_ticks', 30
            link[counter] = link.get(counter, 0) + 1
            if link[counter] >= limit:
                if anchored:
                    link['stalled'] = link['request_id']
                    latest = last_reply(reviewer)
                    link['stalled_reply'] = _digest(latest)
                    stalled_replies[link['worker'], link['reviewer']] = latest
                link['awaiting'] = False
        elif reviewer_state == 'Idle':
            raw = reply if anchored else last_reply(reviewer)
            if not anchored and 'send_reply' in link and _digest(raw) != link['send_reply']:
                link['stalled'] = secrets.token_hex(4)
                link['stalled_reply'] = _digest(raw)
                stalled_replies[link['worker'], link['reviewer']] = raw
                link['awaiting'] = False
            else:
                reply = raw.strip()
                count = 'empty_rounds' if link.get('empty_turn') else 'rounds'
                approved = reply.upper().startswith('APPROVED')
                forwarded = False
                verdict = None
                bound = link.get('review_commit') if approved else None
                named = _named_commit(reply) if approved else None
                review_entry = _review_entry(worker, link.get('review_folder')) if bound else worker
                if (bound or named) and state != 'Idle':
                    # A working tab can still commit. The approval waits until the tab is idle.
                    return link
                # Resolve the worktree once, so the HEAD read and the saved baseline name the same folder.
                verified_folder = _call(folder, review_entry) if bound or named else None
                verified_folder = verified_folder if isinstance(verified_folder, str) and verified_folder else None
                if verified_folder:
                    review_entry = _review_entry(worker, verified_folder)
                current = _call(commit, review_entry) if bound or named else None
                current = current if isinstance(current, str) else None
                misnamed = bool(bound) and named != bound  # the full hash: a prefix can be ambiguous
                # A named commit that cannot be compared is not verified, also when the request was not bound.
                unverified = current is None and (bool(bound) and not misnamed or not bound and bool(named))
                if unverified and link.get('verify_ticks', 0) + 1 < VERIFY_TICKS:
                    # The worker's commit is unreadable now (a slow git, a folder that moved). Ask again next tick.
                    link['verify_ticks'] = link.get('verify_ticks', 0) + 1
                    return link
                # stale: the commit that the approval is for, when we cannot accept it ('' = it names none).
                stale = None
                if bound and (current != bound or misnamed):
                    stale = (named if named and FULL.fullmatch(named) else '') if misnamed else bound
                elif not bound and named and not (current and current == named):
                    stale = named  # no binding (no readable commit at the request), but the reply names a commit
                if approved and stale is None:
                    verdict = _verdict_state(link, worker, scope,
                                             current if current and current == (bound or named) else None,
                                             verified_folder)
                    if (bound or named) and verdict['commit'] is not None and verdict['commit'] != (bound or named):
                        stale = bound or named
                if bound or named:
                    # The cached state can be old. Refresh it after the Git reads and before the send.
                    live = _call(fresh, worker) if fresh is not None else {'state': state}
                    if not isinstance(live, dict) or live.get('state') != 'Idle':
                        return link
                if stale is not None:
                    # Not an approval of the current work and not a finding either: ask for a review of the current
                    # commit. The count stops a loop.
                    link['stale_commit'] = stale
                    link['stale_unreadable'] = bool(stale) and current is None
                    if verdict is not None and verdict['folder']:
                        link['follow_up_folder'] = verdict['folder']
                    _record_round(cap, link, 'stale_rounds')
                    link['seen_working'] = True
                    link['awaiting'] = False
                elif approved:
                    uncommitted = bool(bound) and not link.get('review_clean')
                    covers = f' for commit {bound}' if bound else ''
                    condition = ''
                    next_step = 'Do'
                    if uncommitted:
                        covers += ' and its uncommitted changes'
                        condition = ('It covered uncommitted changes, so no commit is approved yet: commit the work '
                                     'in this turn, and Tether asks the reviewer to confirm that commit. ')
                    elif bound:
                        folder = link.get('review_folder')
                        location = "in this tab's worktree (`git rev-parse HEAD`)"
                        if folder and all(unicodedata.category(c)[0] != 'C'
                                          and unicodedata.category(c) not in ('Zl', 'Zp') for c in folder):
                            quoted = shlex.quote(folder)
                            location = f'in {quoted} (`git -C {quoted} rev-parse HEAD`)'
                        condition = (
                            f'The approval is for commit {bound} only. Before you push, deploy or merge, '
                            f'check that HEAD {location} is {bound}. '
                            'If HEAD is different or you cannot read it, do not act on this approval. '
                            'Stop and wait for the next review. ')
                        next_step = 'If HEAD is that hash, do'
                    text = (f"[Tether review] Approved by the reviewer tab ({link['reviewer']}){covers}:\n\n"
                            f'{_bounded_reply(reply, reviewer)}\n\nThe review is complete. {condition}'
                            f'{next_step} the next step your instructions give for an approved review, then stop.')
                    before = _digest(last_reply(worker)) if worker.get('agent') in ('codex', 'antigravity') else None
                    forwarded = _send_worker(link, cap, snapshot, worker, text, state, send, clear, entries, delivery)
                    if not forwarded:
                        pass  # not delivered: the same checks run again on the next tick
                    elif bound and (after_send := _call(commit, review_entry)) != bound:
                        # The tab has the approval text. A different commit or a failed read needs a new review.
                        now = after_send if isinstance(after_send, str) and FULL.fullmatch(after_send) else None
                        link['stale_commit'] = bound
                        link['stale_delivered'] = True
                        link['stale_unreadable'] = now is None
                        request = link.get('request_id') or link.get('send_reply') or link.get('reviewer_before')
                        link['stale_correction'] = dict(commit=bound, now=now, request=request, requested=False)
                        _record_round(cap, link, 'stale_rounds')
                        link['seen_working'] = True
                        link['awaiting'] = False
                    else:
                        reviewer_key = _reviewer_key(link)
                        cap['counts'] = {owner: counts for owner, counts in cap.get('counts', {}).items()
                                         if owner != reviewer_key}
                        _clear_restart_counts(cap, reviewer_key, COUNTS)
                        _clear_follow_up(link)
                        for key in ('stale_commit', 'approved_commit', 'approved_at', 'stale_delivered', 'confirm_commit',
                                    'stale_unreadable', 'stale_correction'):
                            link.pop(key, None)
                        # Proof of an approved commit needs a clean tree and a reply at the transcript anchor of this
                        # request. "Send latest reply" has no anchor: that reply may answer an earlier request.
                        # The tree must also be clean now: it can change during the review with HEAD in place.
                        if bound and link.get('review_clean') and anchored and verdict['dirty'] is False:
                            now = _call(scope, review_entry)
                            if isinstance(now, dict) and now.get('commit') == bound and now.get('dirty') is False:
                                link['approved_commit'] = bound
                                link['approved_at'] = int(time.time())
                        if uncommitted:
                            link['approved_dirty'] = bound
                        link['awaiting'] = False
                        link['approved_turn'] = True
                        if before is not None:
                            link['approved_before'] = before
                else:
                    if _at_cap(cap):
                        _pause_at_limit(cap, link)
                        link['awaiting'] = False
                    else:
                        if not _can_send(worker, state, clear):
                            return link
                        own = cap.get('counts', {}).get(_reviewer_key(link), {})
                        stop = _counts_at_limit(cap, own, count)
                        stage = None
                        findings = _bounded_reply(reply, reviewer)
                        if stop:
                            failed = own.get('rounds', 0) + own.get('empty_rounds', 0) + 1
                            parent_level = 'limit' not in cap and _call(level, worker) in (4, 5)
                            capped = parent_level and failed >= MAX_ROUNDS + EXTRA_ROUNDS
                            stage = _parent_stage(worker, entries, reply, parent) if parent_level and not capped else None
                            limit = failed if capped else _round_limit(cap)
                            if stage is not None:
                                text = f'{PARENT_SUMMARY}{findings}\n\n{PARENT_SUMMARY_END}'
                            else:
                                text = (f"[Tether review] Stopped after {limit} review rounds: the reviewer tab "
                                        f"({link['reviewer']}) still finds blocking problems. Do not fix them in this turn. "
                                        f"Mak decides the next step.\n\nOpen findings:\n\n{findings}\n\n"
                                        'Reply with a short list of the open findings and what you tried, '
                                        'then end with "Needs you: yes".')
                        else:
                            text = (f"[Tether review] Feedback from the reviewer tab ({link['reviewer']}):\n\n"
                                    f'{findings}\n\nFix what it raises, then stop and wait.')
                        verdict = _verdict_state(link, worker, scope)
                        if not _send_worker(link, cap, snapshot, worker, text, state, send, clear,
                                            entries, delivery, summary=stage is not None, blocking=True):
                            return link
                        forwarded = True
                        link.pop('stale_correction', None)
                        paused_now = _record_round(cap, link, count)
                        link.pop('empty_turn', None)
                        if stop:
                            cap['name'] = link['name']
                            if stage is not None:
                                cap['parent_stage'] = stage
                            elif paused_now:
                                reply_effect['alert'] = (_findings_preview(reply), limit)
                        link['awaiting'] = False
                if forwarded:
                    # Preserve the last delivered verdict while a newer request waits for its own verdict.
                    link['approved_state'] = dict(verdict, at=cap['feedback_turn']['sent_at'])
                if forwarded and reply:
                    mentioned = MENTIONED_COMMIT.search(reply)
                    reply_effect['verdict'] = approved, named or (mentioned[1].lower() if mentioned else None)
        if not link['awaiting']:
            for key in ('wait_ticks', 'idle_ticks', 'pending_idle_ticks', 'reviewer_before', 'reviewer_seen_working',
                        'retries', 'request_id', 'send_reply', 'verify_ticks'):
                link.pop(key, None)
        return link
    if state == 'Working':
        if link['worker'] not in awaiting and not correction_turn:
            link['seen_working'] = True
    elif state == 'Idle' and (link['seen_working'] or link.get('approved_turn')):
        target = None
        if link.get('approved_turn'):
            approved = link.get('approved_state') or {}
            at = approved.get('at', feedback['sent_at'] if feedback is not None else None)
            checked_at = link.get('follow_up_checked_at', 0)
            new_finish = (finished_at is not None and at is not None
                          and finished_at > max(at, checked_at))
            observed_finish = link['seen_working'] and (finished_at is None or finished_at > checked_at)
            # Some Codex and Antigravity transcripts have no completion timestamp.
            reply_mark = None
            if finished_at is None and 'approved_before' in link and worker.get('agent') in ('codex', 'antigravity'):
                reply_mark = _digest(last_reply(worker))
            new_reply = not link['seen_working'] and reply_mark is not None and reply_mark != link['approved_before']
            if not (observed_finish or new_finish or new_reply or link.get('confirm_ticks')):
                return link
            changed, target = _follow_up_change(link, worker, scope)
            if changed is None:
                link['confirm_ticks'] = link.get('confirm_ticks', 0) + 1
                if link['confirm_ticks'] < VERIFY_TICKS:
                    return link
                # Give up on this turn after VERIFY_TICKS failed scope reads.
            if new_finish:
                link['follow_up_checked_at'] = finished_at
            approved_dirty = link.get('approved_dirty')
            if at is None or started_at is None and finished_at is None:
                # Preserve the observed-turn and reply-digest paths when the transcript has no times.
                handled_approval = finished_at is None and source in ('review-feedback', 'user')
            elif started_at is None:
                # The prompt has no time, so it may be an earlier queued prompt. Keep the marker; the idle guard
                # still lets the reviewer close once this checked completion left the work unchanged.
                handled_approval = False
                if not changed:
                    link['follow_up_untimed_at'] = finished_at
            else:
                handled_approval = (source == 'review-feedback' and started_at >= at
                                    or source == 'user' and started_at > at)
            if changed or handled_approval:
                _clear_follow_up(link)
            else:
                link.pop('confirm_ticks', None)
                if reply_mark is not None:
                    link['approved_before'] = reply_mark  # do not recheck the same untimed completion
            link['seen_working'] = bool(changed)
            if not changed:
                return link
            folder = target.get('folder')
            if folder:
                link['follow_up_folder'] = folder
            if (approved_dirty and folder == approved.get('folder', link.get('review_folder'))
                    and target['commit'] != approved_dirty and target.get('dirty') is False):
                link['confirm_commit'] = approved_dirty
        if _turn_ended_on_limit(worker):
            link.pop('confirm_commit', None)
            link.pop('follow_up_folder', None)
            link['seen_working'] = False
            return link
        if _at_cap(cap) or cap.get('unreadable'):
            _pause_at_limit(cap, link)
            link.pop('confirm_commit', None)
            link.pop('follow_up_folder', None)
            link['seen_working'] = False
            return link
        reviewer_state = status(reviewer)['state']
        if not (reviewer_state == 'Idle' or reviewer_state == 'Working' and reviewer.get('agent') == 'claude') \
                or not clear(link['reviewer']):
            return link
        delivered = False
        if reviewer.get('writable'):
            was_bound = 'review_commit' in link or 'follow_up_folder' in link
            if was_bound and 'scope_retry_at' in link:
                now = time.time()
                # A clock rollback can put the saved retry farther away than any backoff we set.
                if now < link['scope_retry_at'] <= now + SCOPE_BACKOFF[-1]:
                    return link
            if target is None:
                target = _call(scope, _review_entry(worker, link.get('follow_up_folder')))
            target = target if isinstance(target, dict) and isinstance(target.get('commit'), str) else None
            if target is None and was_bound:
                now = time.time()
                since = min(link.get('scope_since', now), now)  # a clock moved back must not stretch the wait
                deadline = since + SCOPE_WAIT
                if now < deadline:
                    # A legacy count has no timestamp. Start a fresh wait on its first failed read.
                    failures = link.get('scope_ticks', 0) if 'scope_since' in link else 0
                    delay = SCOPE_BACKOFF[min(failures, len(SCOPE_BACKOFF) - 1)]
                    link.update(scope_ticks=failures + 1, scope_since=since, scope_retry_at=min(now + delay, deadline))
                    return link
                # Still unreadable. A timeout does not show that an approval is for the current commit: the review
                # stops and the user decides. The user's next prompt restarts reviews without the old binding.
                for key in ('review_commit', 'review_clean', 'review_folder', 'stale_commit', 'stale_delivered',
                            'stale_unreadable', 'confirm_commit', 'follow_up_folder', *SCOPE_RETRY_FIELDS):
                    link.pop(key, None)
                cap['unreadable'] = True
                link['seen_working'] = False
                return link
            stale = link.get('stale_commit')
            if isinstance(stale, str) and stale and link.get('stale_unreadable'):
                if link.get('stale_delivered'):
                    binding = (f'Your approval of {stale[:12]} reached the tab. Tether could not read the tab\'s '
                               'commit after delivery. Review the current commit and confirm it if it passes. ')
                else:
                    binding = (f'Your approval of {stale[:12]} was not forwarded because Tether could not read the '
                               "tab's commit. Check the current commit before approving this review. ")
            else:
                binding = '' if not isinstance(stale, str) else (
                    f'Your approval of {stale[:12]} reached the tab after it had moved to another commit, so it does not '
                    'count for the current work. ' if stale and link.get('stale_delivered') else
                    f'Your approval of {stale[:12]} was not forwarded: this tab is not at that commit. ' if stale
                    else 'Your approval was not forwarded: it did not name the full commit hash right after APPROVED. ')
            if target is not None:
                for key in SCOPE_RETRY_FIELDS:
                    link.pop(key, None)
                folder = shlex.quote(_request_text(str(target.get('folder') or '')))
                binding = (
                    f"Commit under review: {target['commit']} in {folder}"
                    + (' plus uncommitted changes' if target.get('dirty') else
                       '' if target.get('dirty') is False else ' (Tether could not read the status of its files)') + '. '
                    + (f'Read the review rules first: `git -C {folder} show origin/main:REVIEW.md`. '
                       if target.get('rules') else '')
                    + f"An approval counts only for this commit: start it with `APPROVED {target['commit']}` "
                    '(the full hash). '
                    + binding
                    + _checklist_binding(target.get('checklist'), folder))
                confirm = link.get('confirm_commit')
                if isinstance(confirm, str) and link.get('review_folder') in (None, target.get('folder')):
                    binding += (f'You approved this work as commit {confirm[:12]} plus uncommitted changes. The tab '
                                'has committed since: check that this commit holds that work and nothing else. ')
                ui = target.get('ui')
                if ui is None and 'ui' in target:
                    binding += ('Tether could not list the changed UI files. Check the diff for UI changes yourself; '
                                'a UI change needs before and after screenshots at 320 px and 1280 px. ')
                elif isinstance(ui, list) and ui and all(isinstance(path, str) for path in ui):
                    separator = '\n' if binding.endswith('\n') else ' '
                    binding += (
                        f'This diff changes the UI ({len(ui)} file(s), for example {_request_text(ui[0])}). '
                        'Do not approve without before and after screenshots at 320 px and 1280 px for each changed '
                        "screen: the worker's reply must name the folder that holds them. Open the screenshots and "
                        f'check them against the claims.{separator}')
            # File names and checklist rows come from the repository. No control, format (direction marks, zero
            # width), surrogate, private or unassigned character and no line separator may reach the terminal.
            binding = ''.join(
                '?' if c != '\n' and (unicodedata.category(c)[0] == 'C' or unicodedata.category(c) in ('Zl', 'Zp'))
                else c for c in binding)
            agent = 'Claude' if link['agent'] == 'claude' else 'Codex'
            request_id = secrets.token_hex(4)
            text = (f"[Tether review] The {agent} tab “{link['name']}” ({link['worker']}) finished a turn. "
                    f'{INTRO} Review id: {request_id}. {binding}'
                    f"Its last reply:\n\n{last_reply(worker)[:1500]}\n\nTranscript: {worker.get('transcript', '')}")
            before = _digest(last_reply(reviewer))
            mark = work_mark(_review_entry(worker, target and target.get('folder'))) if work_mark is not None else None
            if not _linked(link, snapshot, entries):
                return link  # stopped or replaced during the reads above: no request for a link that is gone
            try:
                send(reviewer, text, reviewer_state)
                _last_sent[link['reviewer']] = time.monotonic()
                delivered = True
            except (DraftError, RuntimeError):
                pass
        if delivered:
            link.pop('stalled', None)
            link.pop('stalled_reply', None)
            link.pop('pending_idle_ticks', None)
            link.pop('send_reply', None)
            link['request_id'] = request_id
            link.pop('stale_commit', None)
            link.pop('stale_delivered', None)
            link.pop('stale_unreadable', None)
            link.pop('confirm_commit', None)
            link.pop('follow_up_folder', None)
            for key in SCOPE_RETRY_FIELDS:
                link.pop(key, None)
            link.pop('review_folder', None)
            if target is not None:
                link['review_commit'] = target['commit']
                link['review_clean'] = target.get('dirty') is False
                if isinstance(target.get('folder'), str):
                    link['review_folder'] = target['folder']
                if (correction := link.get('stale_correction')) and correction['now'] in (None, target['commit']):
                    link['stale_correction'] = {**correction, 'requested': True}
            else:
                link.pop('review_commit', None)
                link.pop('review_clean', None)
            if mark is not None and mark == link.get('work_mark'):
                link['empty_turn'] = True
            else:
                link.pop('empty_turn', None)
            link['work_mark'] = mark
            link['seen_working'] = False
            link['awaiting'] = True
            link['reviewer_seen_working'] = False
            link['reviewer_before'] = before
            if reviewer_state == 'Working':
                link['reviewer_busy_send'] = True
    return link


def _cap_alert(cap, preview=None, limit=None, *, unreadable=False):
    if preview is not None:
        restart = ('Link the tabs again from Tether to restart reviews.' if _saved(cap)
                   else 'Answer in that tab to restart reviews.')
        return ('Review needs you',
                f'“{cap["name"]}”: the reviewer still found blocking problems after {limit or _round_limit(cap)} rounds. '
                f'{restart} Open findings: “{preview}”')
    if any(counts.get('stale_rounds', 0) >= MAX_ROUNDS for counts in cap.get('counts', {}).values()):
        reason = "Tether could not read the tab's commit for the latest approval. " if unreadable else ''
        return ('Review needs you',
                f'“{cap["name"]}”: {MAX_ROUNDS} approvals in a row could not be verified for the current commit. '
                f'{reason}Answer in that tab to restart reviews.')
    if 'parent_stage' not in cap and 'escalate' not in cap:
        return ('Review paused', f'“{cap["name"]}” reached the round limit. Answer in that tab to restart reviews.')
    return None


def _apply_step(old, updated, snapshot, cap, entries, delivery):
    """Commit one link step and its cap change together. A stopped link can still finish a reserved delivery."""
    with _lock:
        key = _cap_key(old)
        reserved = _feedback_in_flight.get(key) is delivery
        try:
            if old == updated and snapshot == cap:
                return False, None
            rows = _read()
            link = _link_for(rows, old)
            stored = _cap_for(rows, key)
            if (stored is None or link is None and not reserved
                    or link is not None and _cap_key(link) != key):
                return False, None
            if not reserved and (key in _feedback_in_flight or
                                 updated is not None and _binding_pending(rows, key, entries)):
                return False, None
            if stored.get('cap_reset') != snapshot.get('cap_reset'):
                return False, None
            if _decision(stored) != _decision(snapshot) and not reserved:
                if _decision(cap) != _decision(snapshot) or old.get('awaiting') and not (
                        updated is not None and updated.get('awaiting')):
                    # A grant or escalation raced with the decision. Re-read the same reply on the next tick.
                    return False, None
            if link is not None:
                if updated is None:
                    rows.remove(link)
                else:
                    merged_link = _merge(old, updated, link)
                    if not reserved and old.get('stale_correction') != link.get('stale_correction'):
                        # A request send must not restore a correction another tick has already retired.
                        merged_link.pop('stale_correction', None)
                        if 'stale_correction' in link:
                            merged_link['stale_correction'] = link['stale_correction']
                    rows[rows.index(link)] = merged_link
            merged = _merge(snapshot, cap, stored)
            rows[rows.index(stored)] = merged
            _release(key, delivery, writing=True)
            _write(rows, adopt={key} if updated is None else ())
            return True, merged
        finally:
            _release(key, delivery)


def _claim_upgrade_alerts(rows, entries):
    alerts = []
    for cap in rows:
        if not _is_cap(cap) or cap.get('paused') != UPGRADE_PAUSE or cap.get('upgrade_alerted'):
            continue
        key = _cap_key(cap)
        worker = entries.get(key[0])
        current = helper_tabs._thread(worker) if worker is not None else None
        if key[1] is None:
            bound = {other['worker_thread'] for other in rows
                     if _is_cap(other) and other['worker'] == key[0] and other.get('worker_thread') is not None
                     and other.get('paused') == UPGRADE_PAUSE}
            # The current conversation's bound cap alerts. Without a discovered thread the unbound cap may be the
            # same conversation, so it waits. The bind carries the marker.
            if current in bound or current is None and bound:
                continue
        name = cap['name']
        if worker is not None and helper_tabs._thread(worker) == key[1]:
            name = worker.get('pane_name') or worker.get('agent_name') or worker.get('label') or name
        alerts.append((key, cap.get('upgrade_at'), ('Review needs you', f'“{name}”: {UPGRADE_ERROR}')))
    return alerts


def observe(entries, status, send, last_reply, *, alert=None, work_mark=None, review_reply=None, turn_source=None,
            level=None, parent=None, scope=None, commit=None, fresh=None, folder=None):
    """Callbacks run outside the store lock: send takes SEND_LOCK, while add already holds it."""
    try:
        by_pane = {entry['id']: entry for entry in entries}
        if not any(isinstance(pane, str) and pane.startswith('%') for pane in by_pane):
            return
        with _lock:
            rows = _read()
            bindable = set()
            for link in rows:
                if not _is_link(link) or link.get('worker_thread') is not None:
                    continue
                if _reviewer_ready(link, by_pane):
                    bindable.add(link['worker'])
            bound = False
            for worker in bindable & by_pane.keys():
                bound |= _bind_worker(rows, worker, helper_tabs._thread(by_pane[worker]))
            upgrade_alerts = _claim_upgrade_alerts(rows, by_pane) if alert is not None else []
            if bound or upgrade_alerts or any(_is_cap(row) and row.get('pending_adoption')
                                             and _cap_key(row) not in _feedback_in_flight for row in rows):
                _write(rows)
        links = [row for row in rows if _is_link(row)]
        upgrade_paused = {_cap_key(row) for row in rows if _is_cap(row) and row.get('paused') == UPGRADE_PAUSE}
        awaiting = {link['reviewer'] for link in links if link.get('awaiting') and _cap_key(link) not in upgrade_paused}

        def clear(pane):
            return pane not in awaiting and time.monotonic() - _last_sent.get(pane, float('-inf')) >= SEND_GAP

        def notify(key, message, **kwargs):
            if alert is not None and message is not None:
                try:
                    alert(*message, key[0], **kwargs)
                except Exception:  # a failed notification must not lose other alerts
                    return False
                return True
            return False

        for key, upgrade_at, message in upgrade_alerts:
            # Success means the callback returned. Push delivery runs in a daemon thread and is not tracked here.
            if notify(key, message):
                with _lock:
                    stored_rows = _read()
                    stored = _cap_for(stored_rows, key)
                    if (stored is not None and stored.get('paused') == UPGRADE_PAUSE
                            and stored.get('upgrade_at') == upgrade_at):
                        stored['upgrade_alerted'] = True
                        _write(stored_rows)
                        _cap_for(rows, key)['upgrade_alerted'] = True

        stalled_replies = {}
        turn_reads = {}
        sources = {}
        for original in links:
            key = _cap_key(original)
            delivery = object()
            link = dict(original)
            reply_effect = {}
            try:
                with _lock:
                    rows = _read()
                    current = _link_for(rows, original)
                    snapshot = _cap_for(rows, key)
                if current is None or snapshot is None or _cap_key(current) != key:
                    continue
                link = dict(current)
                cap = dict(snapshot)
                updated = _step(link, cap, snapshot, by_pane, status, send, last_reply, awaiting, clear,
                                delivery, work_mark, review_reply, stalled_replies, turn_source,
                                turn_reads, sources, level, parent, reply_effect, scope, commit, fresh, folder)
                # Give any new verdict priority. Only a correction already stored before this tick can be sent.
                if (updated is not None and 'lost_reason' not in updated and 'verdict' not in reply_effect
                        and (correction := current.get('stale_correction')) and 'stale_correction' in updated):
                    _send_correction(updated, correction, cap, snapshot, by_pane, status, send, clear, delivery)
                if updated is not None and updated != current:
                    updated.pop('wait_ticks', None)
                    updated.pop('retries', None)
                applied, merged = _apply_step(current, updated, snapshot, cap, by_pane, delivery)
            except Exception:  # one bad pane must not stop other reviews
                with _lock:
                    _release(key, delivery)
                continue
            if not applied:
                continue
            name = link['name']
            if 'verdict' in reply_effect:
                approved, named = reply_effect['verdict']
                reviewer = by_pane[link['reviewer']]
                verdict = 'Approved review' if approved else 'Requested changes'
                audit.record('review.verdict', f'{verdict} for {name}', approved=approved,
                             actor={'source': 'server', 'pane': reviewer['id'], 'tab': reviewer.get('pane_name')},
                             worker=key[0], **({'commit': named[:12]} if named else {}))
            if updated is None:
                reason = link['lost_reason']
                print(f"review link removed: worker={key[0]} reviewer={link['reviewer']} name={name} reason={reason}",
                      file=sys.stderr, flush=True)
                notify(key, ('Review link lost', f'“{name}”: {reason}. Tether no longer reviews this tab.'))
            elif merged.get('paused') and (not snapshot.get('paused') or 'alert' in reply_effect):
                notify(key, _cap_alert(merged, *reply_effect.get('alert', ()),
                                       unreadable=updated.get('stale_unreadable', False)))
            elif merged.get('unreadable') and not snapshot.get('unreadable'):
                notify(key, ('Review needs you',
                             f'“{name}”: Tether cannot read the commit in the folder of that tab, so it cannot '
                             'tie the review to a commit. Answer in that tab to restart reviews.'))
            elif updated.get('stalled') and not current.get('stalled'):
                reviewer = link['reviewer']
                print(f"review stalled: worker={key[0]} reviewer={reviewer} name={name}", file=sys.stderr, flush=True)
                preview = stalled_replies[key[0], reviewer].strip().replace('\n', ' ')
                preview = preview[:120] + ('…' if len(preview) > 120 else '')
                notify(key, ('Review stalled',
                             f'“{name}”: Tether could not find the reviewer reply. '
                             f'Ask the reviewer again, or send its latest reply. Latest reply: “{preview}”'),
                       review={'worker': key[0], 'reviewer': reviewer, 'id': updated['stalled']})
        with _lock:
            caps = [row for row in _read() if _is_cap(row)]
        for cap in caps:
            try:
                notify(_cap_key(cap), _step_parent(cap, by_pane, status, send, clear))
            except Exception:
                continue
    except Exception:  # disk and discovery errors must never stop the monitor
        return
