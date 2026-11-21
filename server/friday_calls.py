"""Read the Friday service journal and write one digest per finished voice call."""

import argparse
import ast
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import fcntl
from functools import cached_property
from itertools import count
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata

import paths
import tether_zone


HEURISTICS = {
    'tool_error': re.compile(r'^(?:Error|Invalid|No such|not found)', re.I),
    'assistant_failure': re.compile(
        r"does not exist|couldn't|could not|can't find|not found|sorry|I don't have|unable", re.I),
    'user_correction': re.compile(
        r"\b(?:no,|(?:not that|that's wrong|wrong|I said|which session|which chat|what do you mean)\b)", re.I),
    'affirmations': frozenset({'yes', 'yeah', 'yep', 'please do', 'send it', 'do it', 'go ahead', 'okay', 'ok'}),
}
FRIDAY_EVENTS = {
    'blocked ': 'blocked',
    'LLM error:': 'llm_error',
    'corrected false success': 'corrected_false_success',
    'dropped stray text:': 'dropped_stray_text',
    'Smart Turn unavailable;': 'smart_turn_unavailable',
    'context trimmed ': 'context_trimmed',
    'models loaded in ': 'models_loaded',
}
FRIDAY_FLAGS = {'llm_error', 'corrected_false_success', 'dropped_stray_text', 'smart_turn_unavailable'}
PIPECAT = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ \| (\w+)\s+\| (\S+) - ([\s\S]*)$')
CONTEXT = re.compile(r'^FridayLLM#\d+:\s+Generating LLM response\s+')
MAX_CONTEXT_BYTES = 4 * 1024 * 1024
INITIAL_LOOKBACK = 12 * 60 * 60
PEER_ATTACH_SECONDS = 5
PEER_EXPIRE_SECONDS = 30
FINISH_GRACE_SECONDS = 5
PEER = 'pipecat.transports.smallwebrtc.connection:_initialize:'
LLM = 'pipecat.services.openai.base_llm:get_chat_completions:'
RUNNER = re.compile(r"^WorkerRunner '([^']+)': (started|finished) running$")
MEDIA_WARNING = 'Media stream error while reading the audio; clearing track.'
# User messages that Friday injects; they are not the caller speaking (friday_agent.py).
STATE_PREFIX = 'Current Tether state: '
EVENT_PREFIX = 'Tether news to tell the user in one sentence; do not act on it: '
# Lines Pipecat prints at every call start. They stay in the events but raise no flag.
KNOWN_NOISE = (
    'RTVIProcessor and RTVIObserver found, skipping default ones.',
    'ttfs_p99_latency not set, using default',
    'No screen video transceiver is available',
    'STTSettings: the following fields are NOT_GIVEN: model, language.',  # B413
)
BLOCKED = re.compile(r'^blocked ([\w-]+):\s*(.*)$', re.DOTALL)
# systemd's UNIT_{STARTING,STARTED,STOPPING,STOPPED} message IDs.
LIFECYCLE_IDS = {
    '7d4958e842da4a758f6c1cdc7b36dcc5', '39f53479d3a045ac8e11786248231fbf',
    'de5b426a63be47a7b6ac3eaac82e2f6f', '9d1aaa27d60140bd96365438aad20286',
}


class JournalError(Exception):
    pass


@dataclass
class Entry:
    cursor: str
    at: datetime
    pid: str
    raw: bytes
    line_break: str | None
    lifecycle: bool

    @cached_property
    def text(self):
        return self.raw.decode('utf-8', errors='replace')

    def pipecat(self):
        return PIPECAT.match(self.text)

    @cached_property
    def peer_init(self):
        match = self.pipecat()
        return bool(match and match[2].startswith(PEER) and match[3] == 'Initializing new peer connection')

    def context(self):
        match = self.pipecat()
        if match and match[2].startswith(LLM):
            marker = CONTEXT.match(match[3])
            if marker:
                return match[3][marker.end():]
        return None


def journal(*options):
    # Without --all, journalctl JSON substitutes null for fields over 4096 bytes.
    command = [os.environ.get('TETHER_JOURNALCTL', 'journalctl'), '-u', 'tether-friday',
               '-o', 'json', '--no-pager', '--all', *options]
    try:
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace')
    except OSError as exc:
        raise JournalError(str(exc)) from exc
    if result.returncode:
        raise JournalError(result.stderr.strip() or f'journalctl exited {result.returncode}')
    try:
        return [json.loads(line) for line in result.stdout.split('\n') if line]
    except ValueError as exc:
        raise JournalError('journalctl returned invalid JSON') from exc


def decode_entry(row):
    message = row.get('MESSAGE') or ''
    raw = message.encode('utf-8', 'replace') if isinstance(message, str) else bytes(message)
    pid = clean_text(str(row.get('_PID') or ''))
    lifecycle = row.get('MESSAGE_ID') in LIFECYCLE_IDS
    if pid == '1' and row.get('UNIT') == 'tether-friday.service':
        lifecycle |= raw.startswith((b'Starting ', b'Started ', b'Stopping ', b'Stopped '))
    at = tether_zone.now(int(row['__REALTIME_TIMESTAMP']) / 1e6)
    # Keep elapsed-time arithmetic correct across a daylight-saving change.
    at = at.astimezone(timezone(at.utcoffset()))
    return Entry(row['__CURSOR'], at, pid, raw, row.get('_LINE_BREAK'), lifecycle)


def journal_entries(rows):
    pending = None
    for row in rows:
        entry = decode_entry(row)
        if pending:
            context = pending.context()
            if (entry.pid == pending.pid and entry.text and not entry.pipecat()
                    and not entry.text.startswith('Friday ') and not entry.lifecycle
                    and (pending.line_break == 'line-max'
                         or (context is not None and not context.rstrip().endswith(']')))):
                pending.raw += entry.raw
                del pending.text
                pending.cursor = entry.cursor
                pending.line_break = entry.line_break
                continue
            yield pending
        pending = entry
    if pending:
        yield pending


def clean_text(value):
    if isinstance(value, str):
        return value.encode('utf-8', 'replace').decode('utf-8')
    if isinstance(value, dict):
        return {clean_text(key): clean_text(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(map(clean_text, value))
    return value


def context_messages(raw, flags):
    messages = ast.literal_eval(raw)
    if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise ValueError('expected a list of messages')
    result = []
    for message in messages:
        role = message.get('role')
        if role == 'system':
            continue
        if role not in ('user', 'assistant', 'tool', 'developer'):
            flags['unknown_role'] += 1
            continue
        content = message.get('content')
        if content is not None and not isinstance(content, str):
            raise ValueError('expected text content')
        if role == 'user' and (content or '').startswith(STATE_PREFIX):
            # Friday rewrites and moves this message on each request; keep it out of the transcript.
            continue
        item = {'role': role, 'content': content}
        if 'tool_call_id' in message:
            call_id = message['tool_call_id']
            if call_id is not None and not isinstance(call_id, str):
                raise ValueError('expected text tool call id')
            item['tool_call_id'] = call_id
        if isinstance(tools := message.get('tool_calls'), (list, tuple)):
            calls = []
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(function := tool.get('function'), dict):
                    continue
                name, arguments = function.get('name'), function.get('arguments')
                if not isinstance(name, str):
                    continue
                if not isinstance(arguments, str):
                    json.dumps(arguments)
                call_id = tool.get('id')
                calls.append({'id': call_id if isinstance(call_id, str) else None,
                              'name': name, 'arguments': arguments})
            if calls:
                item['tool_calls'] = calls
        result.append(clean_text(item))
    return result


def message_key(message):
    # Friday shortens old tool results between requests, so a tool result matches by its call id.
    if message.get('role') == 'tool' and message.get('tool_call_id'):
        return 'tool_id', message['tool_call_id']
    if message.get('tool_calls'):
        return 'calls', tuple(call.get('id') or call['name'] for call in message['tool_calls'])
    return message.get('role'), message.get('content')


def context_overlap(merged, context):
    merged = [message_key(message) for message in merged]
    context = [message_key(message) for message in context]
    for size in range(min(len(merged), len(context)), 0, -1):
        if merged[-size:] == context[:size]:
            return size
    return 0


def json_object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def merge_context(transcript, context, by_id):
    merged = [message for message in transcript if not message.get('gap')]
    overlap = context_overlap(merged, context)
    added = []
    for index, message in enumerate(context):
        # Use IDs outside the overlap too. Text and tool names can legitimately repeat.
        key = None
        if ((message['role'] == 'tool' and message.get('tool_call_id'))
                or ((calls := message.get('tool_calls')) and all(call['id'] for call in calls))):
            key = message_key(message)
        existing = merged[index - overlap] if index < overlap else by_id.get(key)
        if existing is None:
            added.append(message)
            if key is not None:
                by_id[key] = message
            continue
        content = message['content']
        if content != existing['content'] and not (
                message['role'] == 'tool' and json_object(content or '').get('shortened') is True):
            existing['content'] = content
    return overlap, added


def transcript_flags(messages, flags):
    users = []
    turns = 0
    for message in messages:
        role = message.get('role')
        content = message.get('content') or ''
        if role == 'tool' and ('error' in json_object(content) or HEURISTICS['tool_error'].match(content.lstrip())):
            flags['tool_error'] += 1
        if role == 'assistant' and HEURISTICS['assistant_failure'].search(content):
            flags['assistant_failure'] += 1
        if role == 'user' and content.startswith(EVENT_PREFIX):
            continue
        if role == 'user':
            turns += 1
            if HEURISTICS['user_correction'].search(content):
                flags['user_correction'] += 1
            stripped = ''.join(char for char in content.lower() if unicodedata.category(char)[0] != 'P')
            normalized = ' '.join(stripped.split())
            affirmations = HEURISTICS['affirmations']
            if normalized and (
                    (users and normalized in affirmations and users[-1] in affirmations)
                    or any(normalized == old
                           or (len(old.split()) >= 3 and f' {old} ' in f' {normalized} ')
                           or (len(normalized.split()) >= 3 and f' {normalized} ' in f' {old} ')
                           for old in users)):
                flags['user_repeat'] += 1
            users.append(normalized)
    return turns


def pipecat_version():
    python = paths.HOME / '.local/share/tether/friday-venv/bin/python'
    script = ("from importlib import metadata, util\n"
              "if util.find_spec('pipecat') is not None:\n"
              "    print(metadata.version('pipecat-ai'))\n")
    try:
        return subprocess.check_output([python, '-I', '-c', script], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5).strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def number(data, key):
    value = data.get(key)
    if type(value) in (float, int) and math.isfinite(value):
        return value
    return None


@dataclass
class Call:
    start: Entry
    checkpoint: str | None
    runner: str | None
    entries: list[Entry] = field(default_factory=list)

    @property
    def id(self):
        # IDs and paths keep the host clock so an existing digest can be found after a preference change.
        return f'{self.start.at.astimezone().isoformat()}-{self.start.pid}-{self.runner or "peer"}'

    def digest(self, end, ended):
        transcript, timeline, events = [], [], []
        by_id = {}
        flags, blocked = Counter(), Counter()
        audio, lost, jitter = [], [], []
        llm_calls = logged_requests = 0
        trimmed = unparsed = False
        for entry in self.entries:
            text = entry.text
            at = entry.at.isoformat()
            if entry.peer_init and entry is not self.entries[0]:
                event = {'t': at, 'kind': 'reconnect', 'text': text}
            elif text.startswith('Friday '):
                body = text.removeprefix('Friday ')
                label, _, payload = body.partition(' ')
                if label in ('tts', 'loop'):
                    continue
                if label in ('metrics', 'net'):
                    data = json_object(payload)
                    fields = (('stop_to_first_audio', audio),) if label == 'metrics' else (
                        ('packetsLost', lost), ('jitter', jitter))
                    for key, samples in fields:
                        if (value := number(data, key)) is not None:
                            samples.append(value)
                    if label == 'metrics':
                        llm_calls += number(data, 'llm_calls') or 0
                    continue
                kind = next((kind for prefix, kind in FRIDAY_EVENTS.items() if body.startswith(prefix)), label)
                if kind in FRIDAY_FLAGS:
                    flags[kind] += 1
                elif kind == 'context_trimmed':
                    trimmed = True
                elif match := BLOCKED.match(body):
                    tool, reason = match.groups()
                    blocked[tool, reason] += 1
                    flags[f'blocked:{tool}'] += 1
                event = {'t': at, 'kind': kind, 'text': text}
            elif (raw := entry.context()) is not None:
                logged_requests += 1
                try:
                    if len(raw) > MAX_CONTEXT_BYTES or len(raw.encode('utf-8')) > MAX_CONTEXT_BYTES:
                        unparsed = True
                        continue
                    context = context_messages(raw, flags)
                except Exception:
                    unparsed = True
                    continue
                overlap, added = merge_context(transcript, context, by_id)
                if transcript and added and not overlap:
                    # A trim drops old messages; any other mismatch is a merge failure worth a look.
                    added.insert(0, {'gap': True})
                    if not trimmed:
                        flags['transcript_gap'] += 1
                trimmed = False
                transcript.extend(added)
                timeline.extend((at, message) for message in added)
                continue
            elif (match := entry.pipecat()) and match[1] in ('WARNING', 'ERROR'):
                kind = 'pipecat_warning' if match[1] == 'WARNING' else 'pipecat_error'
                benign = (any(match[3].startswith(noise) or noise in match[3] for noise in KNOWN_NOISE)
                          or (match[3] == MEDIA_WARNING and abs((end - entry.at).total_seconds()) <= 2))
                if not benign:
                    flags[kind] += 1
                event = {'t': at, 'kind': kind, 'text': text}
            else:
                continue
            events.append(event)
            timeline.append((at, event))
        if repeat_groups := sum(count >= 2 for count in blocked.values()):
            flags['blocked_repeat'] = repeat_groups
        if ended == 'abrupt':
            flags['ended_abrupt'] = 1
        turns = transcript_flags(transcript, flags)
        start = self.start.at.isoformat()
        data = {
            'id': self.id, 'start': start, 'end': end.isoformat(),
            'duration_s': max(0, (end - self.start.at).total_seconds()), 'ended': ended,
            'pid': self.start.pid, 'turns': turns, 'transcript': transcript, 'events': events,
            'metrics': {'max_stop_to_first_audio': max(audio, default=None),
                        'median_stop_to_first_audio': statistics.median(audio) if audio else None,
                        # Some versions omit llm_calls, and failed calls can finish before metrics print.
                        'llm_calls': max(llm_calls, logged_requests)},
            'net': {'max_packetsLost': max(lost, default=None), 'max_jitter': max(jitter, default=None)},
            'flags': dict(sorted(flags.items())),
        }
        if unparsed:
            data.update(transcript='unparsed', pipecat_version=pipecat_version())
            data['flags']['transcript_unparsed'] = 1
            timeline = [(at, event) for at, event in timeline if 'kind' in event]
        return data, timeline


def flag_text(flags):
    return ','.join(f'{flag}:{count}' for flag, count in flags.items()) or 'none'


def markdown(data, timeline):
    lines = [f"# Friday call at {data['start']}", '',
             f"Duration {data['duration_s']:g}s. Ended {data['ended']}. Flags {flag_text(data['flags'])}.", '']
    if data['transcript'] == 'unparsed':
        lines += [f"Transcript unparsed (Pipecat {data['pipecat_version']}).", '']
    zone = tether_zone.zone()
    for at, item in timeline:
        clock = datetime.fromisoformat(at).astimezone(zone).strftime('%H:%M:%S')
        if item.get('gap'):
            lines.append(f'- {clock} **Transcript gap**')
        elif 'kind' in item:
            text = item['text']
            if item['kind'] == 'blocked':
                text = '✗ ' + text.removeprefix('Friday ')
            lines.append(f"- {clock} {text}")
        else:
            lines.append(f"- {clock} **{item['role']}:** {item['content'] or ''}")
            for tool in item.get('tool_calls', []):
                args = tool['arguments']
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                lines.append(f"  - `→ {tool['name']}({args})`")
    return '\n'.join(lines) + '\n'


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def write_state(path, cursor, done, lookback, pending):
    state = {'cursor': cursor, 'done': done[-200:], 'pending': pending}
    if cursor is None and lookback is not None:
        state['since'] = lookback
    atomic_write(path, json.dumps(state) + '\n')


def private_directory(path, *, parents=False):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=parents, exist_ok=True)
        mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        raise OSError(f'refusing symlink directory: {path}')
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0o700)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def read_digest(path):
    with open(path, encoding='utf-8', opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW)) as stream:
        return stream.read()


def digest_base(day, call):
    stamp = call.start.at.astimezone()
    stem = re.sub(r'[^A-Za-z0-9_-]', '_',
                  f'{stamp:%H%M%S}-{stamp.microsecond // 1000:03d}-{call.start.pid}-{call.runner or "peer"}')
    base = day / stem
    for suffix in count(2):
        try:
            existing = json.loads(read_digest(base.with_suffix('.json')))
        except FileNotFoundError:
            try:
                os.lstat(base.with_suffix('.md'))
            except FileNotFoundError:
                return base
        except OSError as exc:
            if exc.errno != errno.ELOOP:
                raise
        except ValueError:
            pass
        else:
            if isinstance(existing, dict) and existing.get('id') == call.id:
                return base
        base = day / f'{stem}-{suffix}'


def write_digest(directory, call, end, ended):
    stamp = call.start.at
    day = directory / stamp.astimezone().strftime('%Y-%m-%d')
    os.close(private_directory(day))
    base = digest_base(day, call)
    md_path = base.with_suffix('.md')
    json_path = base.with_suffix('.json')
    try:
        data, timeline = call.digest(end, ended)
        rendered = markdown(data, timeline)
        json_text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
        # Fail here, not mid-write, on text the file encoding cannot hold.
        json_text.encode('utf-8'), rendered.encode('utf-8')
        duration, turns, flags = data['duration_s'], data['turns'], data['flags']
    except Exception as exc:
        # Only a digest that cannot be built falls back. A failed write raises,
        # so the next scan retries the full digest.
        duration, turns, flags = max(0, (end - stamp).total_seconds()), 0, {'digest_error': 1}
        error = type(exc).__name__
        minimal = {'id': call.id, 'start': stamp.isoformat(), 'end': end.isoformat(), 'error': error, 'flags': flags}
        json_text = json.dumps(minimal, indent=2) + '\n'
        rendered = f"# Friday call at {minimal['start']}\n\nDigest error: {error}\n"
    atomic_write(json_path, json_text)
    atomic_write(md_path, rendered)
    return (f'friday-call {stamp:%H:%M} {duration:g}s turns={turns} '
            f'ended={ended} flags={flag_text(flags)} digest={md_path}')


def scan_locked(directory, since):
    state_path = directory / 'state.json'
    initializing = False
    lookback = None
    cursor, done, pending = None, [], []
    if since is None:
        try:
            state = json.loads(read_digest(state_path))
        except FileNotFoundError:
            initializing = True
        else:
            cursor, done = state['cursor'], state['done']
            lookback = state.get('since')
            pending = state.get('pending', [])
            if pending:
                for notification in pending:
                    print(notification['line'], flush=True)
                pending.clear()
                write_state(state_path, cursor, done, lookback, pending)
    now = time.time()
    if initializing:
        lookback = f'@{now - INITIAL_LOOKBACK:.6f}'
    window_start = since if since is not None else lookback
    if cursor:
        rows = journal(f'--after-cursor={cursor}')
    elif window_start is not None:
        rows = journal('--since', window_start)
    else:
        rows = journal()

    def finish(call, end, ended, checkpoint):
        nonlocal cursor
        if initializing:
            return
        summary = None
        if call.id not in done:
            summary = write_digest(directory, call, end, ended)
            done.append(call.id)
            if since is None:
                pending.append({'id': call.id, 'line': summary})
        if since is None:
            write_state(state_path, checkpoint, done, lookback, pending)
            cursor = checkpoint
        if summary is not None:
            print(summary, flush=True)
            if since is None:
                pending.pop()
                write_state(state_path, cursor, done, lookback, pending)

    previous = cursor
    call = None
    finished_at = None
    pending_peer, peer_checkpoint = None, None
    for entry in journal_entries(rows):
        match = entry.pipecat()
        runner = RUNNER.match(match[3]) if match else None
        new_runner = runner[1] if runner and runner[2] == 'started' else None
        if pending_peer and (entry.lifecycle or entry.pid != pending_peer.pid
                             or (entry.at - pending_peer.at).total_seconds() > PEER_EXPIRE_SECONDS):
            pending_peer = None
        boundary = call and (entry.lifecycle or (entry.pid and entry.pid != call.start.pid)
                             or (new_runner and (finished_at or new_runner != call.runner)))
        grace_elapsed = finished_at and (entry.at - finished_at).total_seconds() > FINISH_GRACE_SECONDS
        if call and (boundary or grace_elapsed):
            checkpoint = peer_checkpoint if pending_peer else previous
            finish(call, finished_at or entry.at, 'clean' if finished_at else 'abrupt', checkpoint)
            call = None
            finished_at = None
        if call is None and new_runner:
            # The cursor must also replay the peer entry when this call has one.
            if pending_peer and (entry.at - pending_peer.at).total_seconds() <= PEER_ATTACH_SECONDS:
                call = Call(entry, peer_checkpoint, new_runner, [pending_peer])
            else:
                call = Call(entry, previous, new_runner)
            pending_peer = None
        if entry.peer_init:
            pending_peer, peer_checkpoint = entry, previous
        if call and finished_at is None:
            call.entries.append(entry)
            if runner and runner[2] == 'finished' and call.runner == runner[1]:
                finished_at = entry.at
        elif call and entry.pid == call.start.pid and entry.text.startswith(
                ('Friday metrics ', 'Friday net ', 'Friday bye ')):
            call.entries.append(entry)
        previous = entry.cursor
    checkpoint = previous
    if pending_peer and now - pending_peer.at.timestamp() < PEER_EXPIRE_SECONDS:
        checkpoint = peer_checkpoint
    if call:
        if finished_at and (initializing or now - finished_at.timestamp() >= FINISH_GRACE_SECONDS):
            finish(call, finished_at, 'clean', checkpoint)
        else:
            checkpoint = call.checkpoint
    if since is None and (initializing or checkpoint != cursor):
        write_state(state_path, checkpoint, done, lookback, pending)


def scan(directory, since=None):
    # The directory inode stays stable while state.json is atomically replaced.
    descriptor = private_directory(directory, parents=True)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        scan_locked(directory, since)
    finally:
        os.close(descriptor)


def error_text(exc):
    return ' '.join(str(exc).splitlines())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('scan').add_argument('--since')
    watch = commands.add_parser('watch')
    watch.add_argument('--interval', type=float, default=15)
    commands.add_parser('show').add_argument('digest', type=Path)
    args = parser.parse_args(argv)
    directory = Path(os.environ.get('TETHER_FRIDAY_CALLS') or paths.CACHE / 'friday-calls').absolute()
    try:
        if args.command == 'show':
            print(read_digest(args.digest.with_suffix('.md')), end='')
        elif args.command == 'scan':
            scan(directory, args.since)
        else:
            if not math.isfinite(args.interval) or args.interval <= 0:
                parser.error('--interval must be positive and finite')
            reported = set()
            while True:
                try:
                    scan(directory)
                except (JournalError, OSError) as exc:
                    error = error_text(exc)
                    if error not in reported:
                        reported.add(error)
                        # stdout: the Monitor tool wakes the watching tab on stdout lines only.
                        print(f'friday-call-watch error: {error}', flush=True)
                time.sleep(args.interval)
    except (JournalError, OSError, ValueError, KeyError) as exc:
        print(f'friday-call error: {error_text(exc)}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
