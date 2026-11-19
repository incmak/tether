"""Antigravity live conversation identity, history, and official usage data."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

from activity import merge as merge_activity, notice as notice_item, action as activity_action, group_actions
import paths
import procinfo

ROOT = paths.ANTIGRAVITY
CACHE = paths.CACHE / 'antigravity'
NOTICES_LOCK = threading.Lock()


def saved_notices(thread):
    try:
        return json.loads((CACHE / 'notices' / (thread + '.json')).read_text())
    except (OSError, ValueError):
        return []


def capture_notices(entry, screen):
    """Capture native quota warnings omitted from the exported transcript."""
    found = []
    lines = screen.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r'^\s*⚠\s*Individual quota reached\b', line, re.I):
            continue
        parts = [line.strip()]
        for following in lines[index + 1:index + 13]:
            if not following.strip() or following.startswith(('>', '─', '●', '○')):
                break
            parts.append(following.strip())
        text = '\n'.join(parts)
        error = re.search(r'Error ID:\s*([\w-]+(?:\n[\da-fA-F-]+)*)', text)
        identity = error[1].replace('\n', '') if error else hashlib.sha256(re.sub(r'Resets.*', '', text, flags=re.S).encode()).hexdigest()
        step = re.search(r'-(\d+)$', identity) if error else None
        found.append({'id': 'notice:' + identity, 'role': 'assistant', 'kind': 'notice',
                      'text': text, 'time': datetime.now(timezone.utc).isoformat(),
                      'step_index': int(step[1]) if step else None})
    if not found:
        return
    with NOTICES_LOCK:
        notices = {notice['id']: notice for notice in saved_notices(entry['thread'])}
        changed = False
        for notice in found:
            previous = notices.get(notice['id'])
            if previous and previous['text'] == notice['text']:
                continue
            if previous:
                notice['time'] = previous['time']
            notices[notice['id']] = notice
            changed = True
        if changed:
            folder = CACHE / 'notices'
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = folder / (entry['thread'] + '.json')
            temporary = folder / (entry['thread'] + '.' + str(os.getpid()) + '.tmp')
            temporary.write_text(json.dumps(list(notices.values())))
            temporary.chmod(0o600)
            temporary.replace(target)


def owner_pid(pids):
    if not pids:
        return None
    pid_set = set(pids)
    for pid in pids:
        try:
            proc = Path(procinfo.ROOT)
            if procinfo.stat_exists(pid, proc):
                ppid = procinfo.parent(pid, proc)
                if ppid not in pid_set:
                    return pid
        except (OSError, IndexError, ValueError):
            continue
    return pids[0]


def locked_elsewhere(pids):
    """Conversations that another running agy already holds: a fresh agy in the same folder must not borrow them."""
    mine, held = {int(pid) for pid in pids}, set()
    proc = Path(procinfo.ROOT)
    for pid in procinfo.pids(proc):
        if pid in mine:
            continue
        try:
            if procinfo.comm(pid, proc) not in ('agy', 'exe'):
                continue
            for target in procinfo.resolved_fds(pid, proc):
                if target.parent == ROOT / 'presence' and target.suffix == '.lock':
                    held.add(target.stem)
        except OSError:
            continue
    return held


def live_entry(pids, cwd=None):
    for pid in pids:
        try:
            paths = list(procinfo.resolved_fds(pid, Path(procinfo.ROOT)))
            conversations = {path.stem for path in paths if path.parent == ROOT / 'presence' and path.suffix == '.lock'}
            if len(conversations) != 1:
                continue
            conversation = conversations.pop()
            if not re.fullmatch(r'[a-zA-Z0-9-]+', conversation):
                continue
            transcript = ROOT / 'brain' / conversation / '.system_generated/logs/transcript.jsonl'
            return {'thread': conversation, 'transcript': str(transcript), 'owner_pid': pid,
                    'writable': True, 'process_start': process_start(pid)}
        except OSError:
            continue
    owner = owner_pid(pids)
    if cwd and owner:
        # Only a conversation reported since this process started can be its own: a fresh agy has none yet, and borrowing the
        # newest one in the same folder showed another conversation's chat and context.
        try:
            began = procinfo.start_ms(process_start(owner)) / 1000
        except (OSError, ValueError):
            began = 0
        candidates = []
        for path in CACHE.glob('*.json'):
            if path.name.endswith(('-bridge-status.json', '-restart-draft.json')):
                continue
            try:
                if path.stat().st_mtime < began:  # a report is written right after its reported_at, so an older file cannot qualify
                    continue
                data = json.loads(path.read_text())
                if data.get('cwd') == cwd and data.get('conversation_id') and data.get('reported_at', 0) >= began:
                    candidates.append(data)
            except (OSError, ValueError):
                continue
        if candidates:  # the /proc walk is only worth doing when there is something to borrow
            taken = locked_elsewhere(pids)
            candidates = [data for data in candidates if data['conversation_id'] not in taken]
        if candidates:
            latest = max(candidates, key=lambda c: c.get('reported_at', 0))
            conversation = latest['conversation_id']
            # agy has reported transcript paths under an older folder (~/.gemini/antigravity); use the one that exists
            transcript = next((str(path) for path in (ROOT / 'brain' / conversation / '.system_generated/logs/transcript.jsonl',
                                                      Path(latest.get('transcript_path') or '/nonexistent')) if path.is_file()),
                              latest.get('transcript_path') or str(ROOT / 'brain' / conversation / '.system_generated/logs/transcript.jsonl'))
            try:
                started = process_start(owner)
                return {'thread': conversation, 'transcript': str(transcript), 'owner_pid': owner,
                        'writable': True, 'process_start': started}
            except OSError:
                pass
    if owner:
        try:
            started = process_start(owner)
            pending = 'pending-' + str(owner) + '-' + started
            return {'thread': pending, 'transcript': str(ROOT / 'brain' / pending / '.system_generated/logs/transcript.jsonl'),
                    'owner_pid': owner, 'process_start': started, 'writable': True, 'pending_conversation': True}
        except OSError:
            pass
    return {}



def _agy_tool_parts(call):
    args = call.get('args') if isinstance(call, dict) and isinstance(call.get('args'), dict) else {}

    def value(key):
        return str(args.get(key, '')).strip().strip('"')

    name = call.get('name', 'tool') if isinstance(call, dict) else 'tool'
    command = value('CommandLine')
    first_line = command.split('\n', 1)[0] if command else None
    return name, first_line, value('toolAction') or value('toolSummary'), value('AbsolutePath')


def agy_tool_line(call):
    """A short description of one Antigravity tool call."""
    name, command, action, _ = _agy_tool_parts(call)
    return 'Ran ' + command if command is not None else action or name


def factory_state():
    steps = {}

    def messages():
        # Factory consumes the full tail before it reads messages.
        for position in sorted(steps):
            yield from steps[position]

    return {'steps': steps, 'messages': messages()}


def factory_consume(state, index, record):
    calls = record.get('tool_calls')
    if record.get('type') != 'PLANNER_RESPONSE' or not isinstance(calls, list):
        return
    step = record.get('step_index')
    position = step if isinstance(step, int) else index
    messages = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name, command, action, path = _agy_tool_parts(call)
        if not isinstance(name, str):
            continue
        messages.append({'tool': {'name': name or 'tool', 'title': command if command is not None else action or path,
                                  'started': record.get('created_at'), 'status': 'pending', 'is_error': False}})
    state['steps'][position] = messages


def bucket_for(model_name):
    name = (model_name or '').lower()
    return '3p' if any(word in name for word in ('claude', 'sonnet', 'opus')) else 'gemini' if 'gemini' in name else None


def window_active(label, model_name):
    """A bucket's generic windows (gemini-5h, gemini-weekly) apply to all its models; a model-named one (gemini-argon-wtu-weekly) only to that model."""
    bucket = bucket_for(model_name)
    if not bucket or not label.startswith(bucket + '-'):
        return False
    specific = [part for part in label[len(bucket) + 1:].split('-') if part not in ('5h', 'weekly', 'wtu', 'daily')]
    return all(part in (model_name or '').lower() for part in specific)


def account_quota():
    """Quota windows are account-wide, so the most recent report of any conversation stands in for one that has none."""
    def modified(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0
    for path in sorted(CACHE.glob('*-*-*-*-*.json'), key=modified, reverse=True):  # newest report first: the first with a quota wins
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('quota'):
            return {'quota': data['quota']}
    return {}


def transcript_message(record, index, clean):
    """The chat text in one transcript step; repeated steps replace earlier text."""
    kind = record.get('type')
    if kind not in ('USER_INPUT', 'PLANNER_RESPONSE', 'AGENT_RESPONSE'):
        return None
    content = record.get('content', '')
    if not isinstance(content, str) or not content.strip():
        return None
    if kind == 'USER_INPUT':
        user_request = re.search(r'<USER_REQUEST>(.*?)</USER_REQUEST>', content, re.DOTALL)
        if user_request:
            content = user_request.group(1).strip()
        else:
            content = content.split('<ADDITIONAL_METADATA>', 1)[0].strip()
    content = clean(content)
    if not content.strip():
        return None
    return {'id': str(record.get('step_index', index)), 'role': 'user' if kind == 'USER_INPUT' else 'assistant',
            'text': content, 'time': record.get('created_at')}


def snapshot(entry, clean, read_records, normalize_usage):
    messages, positions, extras = {}, {}, []
    for index, record in read_records(entry['transcript']):
        kind = record.get('type')
        step = record.get('step_index', index)
        position = step if isinstance(step, int) else index
        if kind == 'CHECKPOINT':
            extras.append((position, notice_item(f'{step}.c', record.get('created_at'), 'Context compacted.')))
        elif kind == 'ERROR_MESSAGE':
            text = str(record.get('content') or record.get('error') or '').strip()
            if text:
                extras.append((position, notice_item(f'{step}.e', record.get('created_at'), text)))
        if kind == 'PLANNER_RESPONSE' and isinstance(record.get('tool_calls'), list):
            for number, call in enumerate(record['tool_calls']):
                extras.append((position, activity_action(f'{step}.t{number}', record.get('created_at'), agy_tool_line(call))))
        message = transcript_message(record, index, clean)
        if message is None:
            continue
        key = message['id']
        positions[key] = record.get('step_index') if isinstance(record.get('step_index'), int) else index
        messages[key] = message
    metrics = {}
    try:
        metrics = json.loads((CACHE / (entry['thread'] + '.json')).read_text())
    except (OSError, ValueError):
        pass
    if not metrics.get('quota'):  # an agy started without the status hook: the account quota still shows from its newest report
        metrics = dict(metrics, **account_quota())
    context = metrics.get('context_window', {})
    windows = []
    model = metrics.get('model', {})
    model_name = (str(model.get('id', '')) + ' ' + str(model.get('display_name', ''))).lower()
    bucket = bucket_for(model_name)
    for name, quota in metrics.get('quota', {}).items():
        if not isinstance(quota, dict):
            continue
        reset = quota.get('reset_time')
        try:
            reset = datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp() if isinstance(reset, str) else reset
        except ValueError:
            reset = None
        fraction = quota.get('remaining_fraction')
        period = 'weekly' if 'week' in name.lower() else 'five_hour' if any(s in name.lower() for s in ('5h', 'five-hour', 'five_hour')) else None
        windows.append({'label': name, 'percent': (1 - fraction) * 100 if isinstance(fraction, (float, int)) else None,
                        'resets_at': reset, 'period': period, 'active': window_active(name, model_name)})
    timeline = [(positions[key], message) for key, message in messages.items()]
    for notice in saved_notices(entry['thread']):
        position = notice.get('step_index')
        timeline.append((position if position is not None else max(positions.values(), default=0) + 1, notice))
    timeline.extend(extras)
    timeline.sort(key=lambda item: item[0])
    return {'messages': group_actions(merge_activity([message for _, message in timeline])), 'busy': metrics.get('agent_state') in ('thinking', 'working', 'tool_use'),
            'usage': dict(normalize_usage({'percent': context.get('used_percentage'), 'tokens': context.get('total_input_tokens'),
                                           'limit': context.get('context_window_size')}, windows, metrics.get('reported_at')),
                          model=model.get('display_name') or model.get('id') or ''),
            'thread': entry['thread'], 'agent': 'antigravity', 'session': entry['id'], 'writable': entry['writable'],
            'messaging_note': ''}


def process_start(pid):
    return procinfo.start_ticks(pid)
