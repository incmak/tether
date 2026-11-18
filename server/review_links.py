"""Persistent worker/reviewer links and one bounded watcher step.

B205: a review is bound to a commit. The request names HEAD of the worker's folder (link 'review_commit'); an APPROVED
reply is forwarded as approval only when it names that commit, the worker is idle and the folder is still at that
commit. A clean approved commit stays on the link as 'approved_commit' (see approved_commits).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import tempfile
import threading
import time
import unicodedata

import paths
from terminal_input import DraftError

FILE = paths.CACHE / 'review-links.json'
MAX_LINKS = 50
MAX_ROUNDS = 3
EXTRA_ROUNDS = 2
# Counts that pause a review at MAX_ROUNDS: failed reviews, reviews of turns that changed nothing, and approvals
# that did not match the commit the tab was at.
COUNTS = ('rounds', 'empty_rounds', 'stale_rounds')
VERIFY_TICKS = 5  # ticks to wait for a readable worker commit before an approval counts as not matching
SCOPE_TICKS = 30  # ticks to wait for a readable commit before a review that was bound pauses and asks the user
NAMED = re.compile(r'APPROVED[\s:,.(`*\u2013\u2014-]*(?:for\s+)?(?:commit\s+)?[`*]*([0-9a-f]{7,64})\b', re.I)
FULL = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')  # a short hash can match two commits; only a full one names a commit
GRACE = 30
LOSS_TICKS = 30
START_WAIT = 300
AGENT_PROMPT_TTL = 600  # an agent prompt that started no turn in 10 minutes no longer blocks Mak's restart
SEND_GAP = 6
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_lock = threading.Lock()
_last_sent = {}
_feedback_turns = {}


def _read():
    try:
        links = json.loads(FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []
    return links if isinstance(links, list) else []


def _write(links):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=FILE.parent,
                                         prefix=FILE.name + '.', delete=False) as stream:
            temporary = stream.name
            json.dump(links, stream)
        os.replace(temporary, FILE)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def add(*, worker, reviewer, reviewer_thread, name, agent, seen_working=False):
    link = dict(worker=worker, reviewer=reviewer, reviewer_thread=reviewer_thread,
                name=name, agent=agent, created=time.time(), seen_working=seen_working, worker_thread=None)
    with _lock:
        links = [item for item in _read() if isinstance(item, dict)
                 and (item.get('worker'), item.get('reviewer')) != (worker, reviewer)]
        _write((links + [link])[-MAX_LINKS:])


def reviewed_by():
    return _reviewers()


def paused():
    return _reviewers(paused_only=True)


def _reviewers(*, paused_only=False):
    with _lock:
        result = {}
        for link in _read():
            if isinstance(link, dict) and (not paused_only or link.get('paused', False)) and all(
                    isinstance(link.get(key), str) and re.fullmatch(r'%[0-9]+', link[key])
                    for key in ('worker', 'reviewer')):
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
            if isinstance(link, dict) and isinstance(link.get('approved_commit'), str) and all(
                    isinstance(link.get(key), str) for key in ('worker', 'reviewer')):
                result.setdefault(link['worker'], {})[link['reviewer']] = {
                    'commit': link['approved_commit'], 'at': link.get('approved_at')}
        return result


def remove(worker, reviewer=None):
    with _lock:
        links = _read()
        remaining = [link for link in links if not isinstance(link, dict)
                     or link.get('worker') != worker
                     or (reviewer is not None and link.get('reviewer') != reviewer)]
        if len(remaining) == len(links):
            return False
        _write(remaining)
        return True


def _from_parent(stage, pane, thread):
    return stage is not None and pane == stage['pane'] and (
        thread is None or stage['thread'] is None or thread == stage['thread'])


def agent_prompt(worker, sender=None, sender_thread=None):
    """Mark an agent turn and let the parent grant two more rounds once per task."""
    with _lock:
        links = _read()
        marked = False
        for link in links:
            if isinstance(link, dict) and link.get('worker') == worker:
                link['agent_prompt'] = time.time()
                if 'limit' not in link and _from_parent(link.get('parent_stage'), sender, sender_thread):
                    link['limit'] = MAX_ROUNDS + EXTRA_ROUNDS
                    link['grant_rounds'] = link.get('rounds', 0) + link.get('empty_rounds', 0)
                    link.pop('paused', None)
                    link.pop('parent_stage')
                    link.pop('parent_missing', None)
                marked = True
        if marked:
            _write(links)
        return marked


def escalate(worker, sender, sender_thread=None):
    with _lock:
        links = _read()
        waiting = [link for link in links if isinstance(link, dict) and link.get('worker') == worker
                   and _from_parent(link.get('parent_stage'), sender, sender_thread)]
        if not waiting:
            raise ValueError('That tab has no review waiting on this tab.')
        for link in waiting:
            link['escalate'] = link.pop('parent_stage')['notice']
        _write(links)


def resume_stalled(worker, reviewer, request_id, action, *, last_reply=None, reviewer_entry=None):
    if not all(isinstance(pane, str) and re.fullmatch(r'%[0-9]+', pane)
               for pane in (worker, reviewer)) or not (
            isinstance(request_id, str) and re.fullmatch(r'[0-9a-f]{8}', request_id)):
        raise ValueError('This review is no longer stalled.')
    with _lock:
        links = _read()
        link = next((item for item in links if isinstance(item, dict)
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


def _agent_prompt_fresh(link):
    sent = link.get('agent_prompt')
    return isinstance(sent, (int, float)) and not isinstance(sent, bool) and time.time() - sent < AGENT_PROMPT_TTL


def _round_limit(link):
    return min(link.get('limit', MAX_ROUNDS), MAX_ROUNDS + EXTRA_ROUNDS)


def _at_round_limit(link, next_round=None):
    counts = [link.get(key, 0) + (key == next_round) for key in ('rounds', 'empty_rounds')]
    # Switching between changed and unchanged work cannot extend the grant.
    return max(counts) >= _round_limit(link) or (
        'grant_rounds' in link and sum(counts) >= link['grant_rounds'] + EXTRA_ROUNDS) or (
        link.get('stale_rounds', 0) >= MAX_ROUNDS)  # approvals in a row that did not match the tab's commit


INTRO = ('Review its work: check the changes and the claims, then list what is wrong or missing. '
         'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab.')


def request_pattern(request_id):
    """Matches the head of the review request with this id, as Tether sends it: the tab line, the fixed instructions
    and the id. Text may come before it (a working-directory note), and spaces may differ. A message that only quotes
    the id, or only starts like a request, does not match."""
    words = r'\s+'.join(re.escape(word) for word in INTRO.split())
    return re.compile(r'\[Tether review\]\s+The\s+\S+\s+tab\s+“.{0,300}?”\s+\(%[0-9]+\)\s+finished\s+a\s+turn\.\s+'
                      + words + r'\s+Review\s+id:\s+' + re.escape(request_id) + r'\.', re.S)


def _committed_since_approval(link, worker, scope):
    """The last approval covered uncommitted work, and the tab has committed it since (a new commit, a clean tree).
    That commit has no approval yet, so the turn that made it is reviewed. True, False, or None: not readable yet."""
    approved = link.get('approved_dirty')
    if not isinstance(approved, str):
        link.pop('approved_dirty', None)
        return False
    target = _call(scope, worker)
    if not isinstance(target, dict) or not isinstance(target.get('commit'), str):
        # Not readable now. One failed read must not lose the confirmation: None = ask again on the next tick.
        link['confirm_ticks'] = link.get('confirm_ticks', 0) + 1
        if link['confirm_ticks'] < VERIFY_TICKS:
            return None
        target = None
    elif target.get('dirty') is None and target['commit'] != approved:
        # A new commit, but the status is not readable: the tree may be clean. Ask again, as above.
        link['confirm_ticks'] = link.get('confirm_ticks', 0) + 1
        if link['confirm_ticks'] < VERIFY_TICKS:
            return None
    link.pop('approved_dirty', None)
    link.pop('confirm_ticks', None)
    if target is None or target['commit'] == approved or target.get('dirty') is not False:
        return False
    link['confirm_commit'] = approved
    return True


def _named_commit(reply):
    """The commit an approval names right after APPROVED, in lower case, or None. A hash later in the reply does not
    count: only the start says what the approval is for."""
    found = NAMED.match(reply)
    return found[1].lower() if found else None


def _merge(old, updated, stored):
    """`stored` with the step's changes (old -> updated). Another writer (agent_prompt, a stalled-review action) may
    change a link while a step runs. The step's fields win: they record a message that was already sent, and a lost
    record sends it again on the next tick. A newer agent_prompt mark is the one field the other writer keeps."""
    missing = object()
    merged = dict(stored)
    for key in set(old) | set(updated):
        if old.get(key, missing) == updated.get(key, missing):
            continue
        if key == 'agent_prompt' and stored.get(key, missing) != old.get(key, missing):
            continue
        if key in updated:
            merged[key] = updated[key]
        else:
            merged.pop(key, None)
    return merged


def _same(link, stored):
    return isinstance(stored, dict) and all(stored.get(key) == link.get(key) for key in ('worker', 'reviewer', 'created'))


def _linked(link):
    """The link is still in the store. A slow read may run between the step's start and a send; a link that was
    removed or replaced in that time must not send."""
    with _lock:
        return any(_same(link, stored) for stored in _read())


def _call(callback, entry):
    """A git read for one worker. A missing callback or any error gives None: the review is then not bound."""
    if callback is None:
        return None
    try:
        return callback(entry)
    except Exception:
        return None


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


def _send_worker(link, worker, text, state, send, clear, feedback_sent):
    if not _linked(link) or not _send_message(worker, text, state, send, clear):
        return False
    feedback_sent(link['worker'])
    if state == 'Working':
        link['worker_busy_send'] = True
        link['seen_working'] = True
        feedback = _feedback_turns[link['worker']]
        feedback['pending'].discard(link['reviewer'])
        feedback['working'].add(link['reviewer'])
    return True


def _parent_entry(stage, entries):
    entry = entries.get(stage['pane'])
    if entry is not None and entry.get('agent') in ('claude', 'codex') and entry.get('thread') == stage['thread']:
        return entry
    return None


def _findings_preview(findings):
    preview = findings.strip().replace('\n', ' ')
    return preview[:300] + ('…' if len(preview) > 300 else '')


def _parent_stage(link, worker, entries, reply, level, parent):
    if 'limit' in link or level is None or parent is None:
        return None
    try:
        if level(worker) not in (4, 5):
            return None
        owner = parent(worker)
        if owner is not None:
            pane, thread = owner
            stage = dict(pane=pane, thread=thread, notice=_findings_preview(reply), sent=False)
            if _parent_entry(stage, entries) is not None:
                return stage
    except Exception:  # a lookup failure keeps the existing path to Mak
        pass
    return None


def _send_parent_notice(link, entries, status, send, clear):
    stage = link.get('parent_stage')
    if stage is None or stage['sent']:
        return
    entry = _parent_entry(stage, entries)
    if entry is None:
        return
    cli = Path(__file__).resolve().with_name('agent_tab_cli.py')
    worker = link['worker']
    text = (f'[Tether review] “{link["name"]}” ({worker}) still has blocking review findings after {MAX_ROUNDS} rounds. '
            f'Open findings: “{stage["notice"]}”\n\n'
            f'To allow 2 more rounds, send it the next step: python3 {cli} send --pane {worker} "<what to fix>". '
            f'To hand it to Mak: python3 {cli} escalate --pane {worker}.')
    if _send_message(entry, text, status(entry)['state'], send, clear):
        with _lock:
            links = _read()
            for saved in links:
                if (isinstance(saved, dict) and saved.get('worker') == worker
                        and saved.get('reviewer') == link['reviewer'] and saved.get('created') == link.get('created')
                        and saved.get('parent_stage') == stage):
                    saved['parent_stage']['sent'] = True
            for saved in links:
                # Tether's notice is not a prompt from Mak: it must not restart the parent's own review loop.
                if isinstance(saved, dict) and saved.get('worker') == entry['id']:
                    saved['agent_prompt'] = time.time()
            _write(links)


def _step(link, entries, status, send, last_reply, awaiting, clear, feedback_sent, work_mark, review_reply,
          stalled_replies, turn_source, escalations, level, parent, scope=None, commit=None, fresh=None):
    worker = entries.get(link['worker'])
    reviewer = entries.get(link['reviewer'])
    if link['reviewer_thread'] is None:
        thread = reviewer.get('thread') if reviewer is not None else None
        if isinstance(thread, str) and UUID.fullmatch(thread):
            link['reviewer_thread'] = thread
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
    thread = worker.get('thread')
    if isinstance(thread, str) and UUID.fullmatch(thread):
        if link.get('worker_thread') is None:
            link['worker_thread'] = thread
        elif thread != link['worker_thread']:
            return _lost(link, 'worker conversation changed')
    link.pop('lost_ticks', None)
    link.pop('lost_reason', None)
    state = status(worker)['state']
    if state == 'Idle' and 'restart' in link:
        start = link.pop('restart')
        start = start if isinstance(start, dict) else {}
        source = turn_source(worker) if turn_source is not None else 'user'
        # A prompt from Mak starts a new task. A background event or another agent's prompt continues this one.
        by_agent = start.get('agent', False) or _agent_prompt_fresh(link)
        link.pop('agent_prompt', None)
        if not by_agent and source == 'user':
            for key in ('parent_stage', 'parent_missing', 'limit', 'grant_rounds'):
                link.pop(key, None)
            for key in COUNTS:
                left = link.get(key, 0) - start.get(key, 0)
                if left > 0:
                    link[key] = left
                else:
                    link.pop(key, None)
            # The new task keeps only the failed reviews of its own turn. A limit reached in this turn alone stays.
            if start.get('paused') or not _at_round_limit(link):
                link.pop('paused', None)
                link.pop('unreadable', None)
    if state == 'Idle' and link.pop('worker_busy_send', False):
        return link
    feedback = _feedback_turns.get(link['worker'])
    if state == 'Idle' and feedback is not None:
        # A grant can reach the store while the watcher records the findings turn as Working.
        if ('limit' in link and 'agent_prompt' in link and not link.get('awaiting')
                and link['reviewer'] in feedback['working']):
            link['seen_working'] = True
        feedback['working'].discard(link['reviewer'])
        if not feedback['pending'] and not feedback['working']:
            _feedback_turns.pop(link['worker'], None)
    if state == 'Working' and not link['seen_working'] and link['worker'] not in awaiting:
        if feedback is not None and link['reviewer'] in feedback['pending'] | feedback['working']:
            feedback['pending'].discard(link['reviewer'])
            # An awaiting link leaves seen_working false. Remember consumption
            # until Idle so later ticks of this same turn cannot reset its count.
            feedback['working'].add(link['reviewer'])
        else:
            # Decide at the end of the turn: only then does the transcript show what started it. Keep the counts
            # from the start, so feedback that lands during this turn still counts.
            link.setdefault('restart', {key: link.get(key, 0) for key in (*COUNTS, 'paused')})
    if state == 'Working' and isinstance(link.get('restart'), dict) and _agent_prompt_fresh(link):
        # Record the origin while the turn runs, so a turn longer than the TTL still counts as the agent's.
        link['restart'] = {**link['restart'], 'agent': True}  # a new dict: the saved copy must see the change
        link.pop('agent_prompt', None)
    if link.get('awaiting', False):
        reviewer_state = status(reviewer)['state']
        anchored = review_reply is not None and link.get('request_id')
        reply = review_reply(reviewer, link['request_id']) if anchored and reviewer_state == 'Idle' else None
        if reviewer_state == 'Working':
            link['reviewer_seen_working'] = True
        elif reviewer_state == 'Idle' and link.pop('reviewer_busy_send', False) and not anchored:
            return link
        elif reviewer_state == 'Idle' and (
                anchored and not reply or not anchored and not link.get('reviewer_seen_working', False)
                and _digest(last_reply(reviewer)) == link.get('reviewer_before')):
            # No turn seen yet. A turn shorter than one tick still changes the last reply; none for 30 ticks: give up.
            link['idle_ticks'] = link.get('idle_ticks', 0) + 1
            if link['idle_ticks'] >= 30:
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
                bound = link.get('review_commit') if approved else None
                named = _named_commit(reply) if approved else None
                if (bound or named) and state != 'Idle':
                    # A working tab can still commit. The approval waits until the tab is idle.
                    return link
                current = _call(commit, worker) if bound or named else None
                if bound or named:
                    # The cached state can be old, and the commit read takes time: read the state again, after the
                    # commit and right before the send.
                    live = _call(fresh, worker) if fresh is not None else {'state': state}
                    if not isinstance(live, dict) or live.get('state') != 'Idle':
                        return link
                current = current if isinstance(current, str) else None
                misnamed = bool(bound) and named != bound  # the full hash: a prefix can be ambiguous
                # A named commit that cannot be compared is not verified, also when the request was not bound.
                unverified = current is None and (bool(bound) and not misnamed or not bound and bool(named))
                if unverified and link.get('verify_ticks', 0) + 1 < VERIFY_TICKS:
                    # The worker's commit is unreadable now (a slow git, a folder that moved). Ask again next tick.
                    link['verify_ticks'] = link.get('verify_ticks', 0) + 1
                    return link
                # stale: the commit that the approval is for, when the tab is not at it ('' = it names none).
                stale = None
                if bound and (current != bound or misnamed):
                    stale = (named if named and FULL.fullmatch(named) else '') if misnamed else bound
                elif not bound and named and not (current and current == named):
                    stale = named  # no binding (no readable commit at the request), but the reply names a commit
                if stale is not None:
                    # Not an approval of the current work and not a finding either: ask for a review of the current
                    # commit. The count stops a loop.
                    link['stale_commit'] = stale
                    link['stale_rounds'] = link.get('stale_rounds', 0) + 1
                    link['seen_working'] = True
                    link['awaiting'] = False
                elif approved:
                    covers = '' if not bound else (
                        f" for commit {bound[:12]}" + ('' if link.get('review_clean') else ' and its uncommitted changes'))
                    uncommitted = bool(bound) and not link.get('review_clean')
                    text = (f"[Tether review] Approved by the reviewer tab ({link['reviewer']}){covers}:\n\n"
                            f'{_bounded_reply(reply, reviewer)}\n\nThe review is complete. '
                            + ('It covered uncommitted changes, so no commit is approved yet: commit the work in this '
                               'turn, and Tether asks the reviewer to confirm that commit. ' if uncommitted else '')
                            + 'Do the next step your instructions give for an approved review, then stop.')
                    if not _send_worker(link, worker, text, state, send, clear, feedback_sent):
                        pass  # not delivered: the same checks run again on the next tick
                    elif bound and _call(commit, worker) != bound:
                        # The commit moved between the read above and the delivery. The tab has the approval text
                        # (it names the commit), but its current work is not reviewed: ask for a review of it.
                        link['stale_commit'] = bound
                        link['stale_delivered'] = True
                        link['stale_rounds'] = link.get('stale_rounds', 0) + 1
                        link['seen_working'] = True
                        link['awaiting'] = False
                    else:
                        for key in (*COUNTS, 'paused', 'parent_stage', 'parent_missing', 'limit', 'grant_rounds',
                                    'unreadable', 'stale_commit', 'approved_commit', 'approved_at',
                                    'approved_dirty', 'confirm_commit', 'confirm_ticks', 'stale_delivered'):
                            link.pop(key, None)
                        # Proof of an approved commit needs a clean tree and a reply at the transcript anchor of this
                        # request. "Send latest reply" has no anchor: that reply may answer an earlier request.
                        # The tree must also be clean now: it can change during the review with HEAD in place.
                        now = _call(scope, worker) if bound and link.get('review_clean') and anchored else None
                        if isinstance(now, dict) and now.get('commit') == bound and now.get('dirty') is False:
                            link['approved_commit'] = bound
                            link['approved_at'] = int(time.time())
                        if uncommitted:
                            link['approved_dirty'] = bound
                        link['awaiting'] = False
                        link['approved_turn'] = True
                        link['approved_before'] = _digest(last_reply(worker))
                elif _at_round_limit(link):
                    link['paused'] = True
                    link['awaiting'] = False
                elif _at_round_limit(link, count):
                    if not _can_send(worker, state, clear):
                        return link
                    stage = _parent_stage(link, worker, entries, reply, level, parent)
                    limit = _round_limit(link)
                    findings = _bounded_reply(reply, reviewer)
                    if stage is not None:
                        text = (f'[Tether review] {MAX_ROUNDS} review rounds still found blocking problems. '
                                'Your parent tab decides the next step. Do not fix them in this turn.\n\n'
                                f'Open findings:\n\n{findings}\n\n'
                                'Reply with the open findings and what you tried. End with "Needs you: no" unless '
                                'a finding needs Mak (taste, new authority, or you disagree with the reviewer).')
                    else:
                        text = (f"[Tether review] Stopped after {limit} review rounds: the reviewer tab "
                                f"({link['reviewer']}) still finds blocking problems. Do not fix them in this turn. "
                                f"Mak decides the next step.\n\nOpen findings:\n\n{findings}\n\n"
                                'Reply with a short list of the open findings and what you tried, '
                                'then end with "Needs you: yes".')
                    if _send_worker(link, worker, text, state, send, clear, feedback_sent):
                        link.pop('empty_turn', None)
                        link[count] = link.get(count, 0) + 1
                        link.pop('stale_rounds', None)  # findings end a run of approvals that did not match
                        if stage is not None:
                            link['parent_stage'] = stage
                        elif not link.get('paused'):
                            escalations[link['worker'], link['reviewer']] = (_findings_preview(reply), limit)
                        link['paused'] = True
                        link['awaiting'] = False
                else:
                    text = (f"[Tether review] Feedback from the reviewer tab ({link['reviewer']}):\n\n"
                            f'{_bounded_reply(reply, reviewer)}\n\nFix what it raises, then stop and wait.')
                    if _send_worker(link, worker, text, state, send, clear, feedback_sent):
                        key = 'empty_rounds' if link.pop('empty_turn', False) else 'rounds'
                        link[key] = link.get(key, 0) + 1
                        link.pop('stale_rounds', None)  # findings end a run of approvals that did not match
                        link['awaiting'] = False
        if not link['awaiting']:
            for key in ('wait_ticks', 'idle_ticks', 'reviewer_before', 'reviewer_seen_working',
                        'retries', 'request_id', 'send_reply', 'verify_ticks'):
                link.pop(key, None)
        return link
    if state == 'Working':
        if link['worker'] not in awaiting:
            link['seen_working'] = True
    elif state == 'Idle' and link.get('approved_turn') and not link['seen_working']:
        if _digest(last_reply(worker)) != link.get('approved_before'):
            committed = _committed_since_approval(link, worker, scope)
            if committed is None:
                return link
            link.pop('approved_turn', None)
            link.pop('approved_before', None)
            if committed:
                link['seen_working'] = True  # the next tick asks for a review of the new commit
    elif link['seen_working'] and state == 'Idle':
        if link.get('approved_turn', False):
            # The turn that follows an approval is not reviewed, unless it committed work that was approved while
            # it was uncommitted.
            committed = _committed_since_approval(link, worker, scope)
            if committed is None:
                return link
            link.pop('approved_turn', None)
            link.pop('approved_before', None)
            if not committed:
                link['seen_working'] = False
                return link
        if link.get('paused', False) or _at_round_limit(link):
            link['paused'] = True
            link['seen_working'] = False
            return link
        reviewer_state = status(reviewer)['state']
        if not (reviewer_state == 'Idle' or reviewer_state == 'Working' and reviewer.get('agent') == 'claude') \
                or not clear(link['reviewer']):
            return link
        delivered = False
        if reviewer.get('writable'):
            agent = 'Claude' if link['agent'] == 'claude' else 'Codex'
            request_id = secrets.token_hex(4)
            target = _call(scope, worker)
            target = target if isinstance(target, dict) and isinstance(target.get('commit'), str) else None
            was_bound = 'review_commit' in link
            if target is None and was_bound and link.get('scope_ticks', 0) + 1 < SCOPE_TICKS:
                # The last review of this tab was bound to a commit. One failed read must not remove the binding.
                link['scope_ticks'] = link.get('scope_ticks', 0) + 1
                return link
            if target is None and was_bound:
                # Still unreadable. A timeout does not show that an approval is for the current commit: the review
                # stops and the user decides. The user's next prompt restarts reviews without the old binding.
                for key in ('review_commit', 'review_clean', 'stale_commit', 'scope_ticks'):
                    link.pop(key, None)
                link.update(paused=True, unreadable=True, seen_working=False)
                return link
            stale = link.get('stale_commit')
            binding = '' if not isinstance(stale, str) else (
                f'Your approval of {stale[:12]} reached the tab after it had moved to another commit, so it does not '
                'count for the current work. ' if stale and link.get('stale_delivered') else
                f'Your approval of {stale[:12]} was not forwarded: this tab is not at that commit. ' if stale
                else 'Your approval was not forwarded: it did not name the full commit hash right after APPROVED. ')
            if target is not None:
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
                if isinstance(confirm, str):
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
            text = (f"[Tether review] The {agent} tab “{link['name']}” ({link['worker']}) finished a turn. "
                    f'{INTRO} Review id: {request_id}. {binding}'
                    f"Its last reply:\n\n{last_reply(worker)[:1500]}\n\nTranscript: {worker.get('transcript', '')}")
            before = _digest(last_reply(reviewer))
            mark = work_mark(worker) if work_mark is not None else None
            if not _linked(link):
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
            link.pop('send_reply', None)
            link['request_id'] = request_id
            link.pop('stale_commit', None)
            link.pop('stale_delivered', None)
            link.pop('confirm_commit', None)
            link.pop('scope_ticks', None)
            if target is not None:
                link['review_commit'] = target['commit']
                link['review_clean'] = target.get('dirty') is False
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


def observe(entries, status, send, last_reply, *, alert=None, work_mark=None, review_reply=None, turn_source=None,
            level=None, parent=None, scope=None, commit=None, fresh=None):
    """Callbacks run outside the store lock: send takes SEND_LOCK, while add already holds it."""
    try:
        if not any(isinstance(entry.get('id'), str) and entry['id'].startswith('%') for entry in entries):
            return
        with _lock:
            original = _read()
        by_pane = {entry['id']: entry for entry in entries}
        awaiting = {link['reviewer'] for link in original
                    if isinstance(link, dict) and link.get('awaiting')
                    and isinstance(link.get('reviewer'), str)}
        def feedback_sent(pane):
            # Each link consumes the next feedback-caused turn independently.
            _feedback_turns[pane] = {
                'pending': {link['reviewer'] for link in original
                            if isinstance(link, dict) and link.get('worker') == pane
                            and isinstance(link.get('reviewer'), str)},
                'working': set(),
            }
        def clear(pane):
            return (pane not in awaiting
                    and time.monotonic() - _last_sent.get(pane, float('-inf')) >= SEND_GAP)
        stalled_replies = {}
        escalations = {}
        changes = []
        for link in original:
            try:
                copy = dict(link)
                stage = copy.get('parent_stage')
                if stage is not None:
                    # Discovery can miss a pane for a tick: escalate only when the parent stays gone.
                    if _parent_entry(stage, by_pane) is None:
                        copy['parent_missing'] = copy.get('parent_missing', 0) + 1
                    else:
                        copy.pop('parent_missing', None)
                if 'escalate' in copy or copy.get('parent_missing', 0) >= LOSS_TICKS:
                    copy.pop('parent_missing', None)
                    preview = copy.pop('escalate') if 'escalate' in copy else stage['notice']
                    copy.pop('parent_stage', None)
                    escalations[copy['worker'], copy['reviewer']] = (preview, _round_limit(copy))
                    updated = copy
                else:
                    updated = _step(copy, by_pane, status, send, last_reply, awaiting, clear, feedback_sent,
                                    work_mark, review_reply, stalled_replies, turn_source, escalations, level, parent,
                                    scope, commit, fresh)
            except Exception:  # a malformed link or one bad pane must not stop other reviews
                continue
            if updated != link:
                if updated is not None:
                    updated.pop('wait_ticks', None)
                    updated.pop('retries', None)
                changes.append((link, updated, copy.get('lost_reason')))
        with _lock:
            current = _read() if changes else original
            applied = []
            for old, updated, reason in changes:
                if old in current:  # preserve any links added or replaced during a callback
                    index = current.index(old)
                    if updated is None:
                        current.pop(index)
                    else:
                        current[index] = updated
                    applied.append((old, updated, reason))
                elif updated is not None:
                    # The same link (same pair and creation time) changed during a callback: keep both changes.
                    same = [index for index, stored in enumerate(current) if _same(old, stored)]
                    if len(same) == 1:
                        merged = _merge(old, updated, current[same[0]])
                        current[same[0]] = merged
                        applied.append((old, merged, reason))
            if applied:
                _write(current)
        for old, updated, reason in applied:
            title = body = None
            review = None
            if updated is None:
                print(f"review link removed: worker={old['worker']} reviewer={old['reviewer']} "
                      f"name={old['name']} reason={reason}", file=sys.stderr, flush=True)
                title = 'Review link lost'
                body = f'“{old["name"]}”: {reason}. Tether no longer reviews this tab.'
            elif (escalation := escalations.get((old['worker'], old['reviewer']))) is not None:
                preview, limit = escalation
                title = 'Review needs you'
                body = (f'“{old["name"]}”: the reviewer still found blocking problems after {limit} rounds. '
                        f'Answer in that tab to restart reviews. Open findings: “{preview}”')
            elif updated.get('paused') and not old.get('paused') and updated.get('unreadable'):
                title = 'Review needs you'
                body = (f'“{old["name"]}”: Tether cannot read the commit in the folder of that tab, so it cannot '
                        'tie the review to a commit. Answer in that tab to restart reviews.')
            elif updated.get('paused') and not old.get('paused') and updated.get('stale_rounds', 0) >= MAX_ROUNDS:
                title = 'Review needs you'
                body = (f'“{old["name"]}”: {MAX_ROUNDS} approvals in a row did not match the commit the tab '
                        'was at. Answer in that tab to restart reviews.')
            elif updated.get('paused') and not old.get('paused') and 'parent_stage' not in updated:
                title = 'Review paused'
                body = (f'“{old["name"]}” reached the round limit. '
                        'Answer in that tab to restart reviews.')
            elif updated.get('stalled') and not old.get('stalled'):
                print(f"review stalled: worker={old['worker']} reviewer={old['reviewer']} "
                      f"name={old['name']}", file=sys.stderr, flush=True)
                title = 'Review stalled'
                preview = stalled_replies[old['worker'], old['reviewer']].strip().replace('\n', ' ')
                preview = preview[:120] + ('…' if len(preview) > 120 else '')
                body = (f'“{old["name"]}”: Tether could not find the reviewer reply. '
                        f'Ask the reviewer again, or send its latest reply. Latest reply: “{preview}”')
                review = {'worker': old['worker'], 'reviewer': old['reviewer'], 'id': updated['stalled']}
            if title and alert is not None:
                try:
                    if review is not None:
                        alert(title, body, old['worker'], review=review)
                    else:
                        alert(title, body, old['worker'])
                except Exception:  # a failed notification must not lose other alerts
                    pass
        # Save the stage before the parent sees the notice and can respond. Read again: the parent may have
        # escalated or granted during this tick.
        with _lock:
            current = _read()
        for link in current:
            try:
                _send_parent_notice(link, by_pane, status, send, clear)
            except Exception:  # one bad pane must not block the other notices
                continue
    except Exception:  # disk and discovery errors must never stop the monitor
        return
