"""Persistent worker/reviewer links and one bounded watcher step."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time

import paths
from terminal_input import DraftError

FILE = paths.CACHE / 'review-links.json'
MAX_LINKS = 50
MAX_ROUNDS = 3
EXTRA_ROUNDS = 2
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
        'grant_rounds' in link and sum(counts) >= link['grant_rounds'] + EXTRA_ROUNDS)


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
    if not _send_message(worker, text, state, send, clear):
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
          stalled_replies, turn_source, escalations, level, parent):
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
            for key in ('rounds', 'empty_rounds'):
                left = link.get(key, 0) - start.get(key, 0)
                if left > 0:
                    link[key] = left
                else:
                    link.pop(key, None)
            # The new task keeps only the failed reviews of its own turn. A limit reached in this turn alone stays.
            if start.get('paused') or not _at_round_limit(link):
                link.pop('paused', None)
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
            link.setdefault('restart', {key: link.get(key, 0) for key in ('rounds', 'empty_rounds', 'paused')})
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
                if reply.upper().startswith('APPROVED'):
                    text = (f"[Tether review] Approved by the reviewer tab ({link['reviewer']}):\n\n"
                            f'{_bounded_reply(reply, reviewer)}\n\nThe review is complete. Do the next step your instructions give '
                            'for an approved review, then stop.')
                    if _send_worker(link, worker, text, state, send, clear, feedback_sent):
                        for key in ('rounds', 'empty_rounds', 'paused', 'parent_stage', 'parent_missing', 'limit',
                                    'grant_rounds'):
                            link.pop(key, None)
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
                        link['awaiting'] = False
        if not link['awaiting']:
            for key in ('wait_ticks', 'idle_ticks', 'reviewer_before', 'reviewer_seen_working',
                        'retries', 'request_id', 'send_reply'):
                link.pop(key, None)
        return link
    if state == 'Working':
        if link['worker'] not in awaiting:
            link['seen_working'] = True
    elif state == 'Idle' and link.get('approved_turn') and not link['seen_working']:
        if _digest(last_reply(worker)) != link.get('approved_before'):
            link.pop('approved_turn', None)
            link.pop('approved_before', None)
    elif link['seen_working'] and state == 'Idle':
        if link.pop('approved_turn', False):
            link.pop('approved_before', None)
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
            text = (f"[Tether review] The {agent} tab “{link['name']}” ({link['worker']}) finished a turn. "
                    'Review its work: check the changes and the claims, then list what is wrong or missing. '
                    'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. '
                    f'Review id: {request_id}. '
                    f"Its last reply:\n\n{last_reply(worker)[:1500]}\n\nTranscript: {worker.get('transcript', '')}")
            before = _digest(last_reply(reviewer))
            mark = work_mark(worker) if work_mark is not None else None
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
            level=None, parent=None):
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
                                    work_mark, review_reply, stalled_replies, turn_source, escalations, level, parent)
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
