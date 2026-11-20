"""Read the Friday service journal and write one digest per finished voice call."""

import argparse
import ast
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import fcntl
from functools import cached_property
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata

import paths


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
    raw = message.encode('utf-8') if isinstance(message, str) else bytes(message)
    pid = str(row.get('_PID') or '')
    lifecycle = row.get('MESSAGE_ID') in LIFECYCLE_IDS
    if pid == '1' and row.get('UNIT') == 'tether-friday.service':
        lifecycle |= raw.startswith((b'Starting ', b'Started ', b'Stopping ', b'Stopped '))
    return Entry(row['__CURSOR'], datetime.fromtimestamp(int(row['__REALTIME_TIMESTAMP']) / 1e6).astimezone(),
                 pid, raw, row.get('_LINE_BREAK'), lifecycle)


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


def context_messages(raw):
    messages = ast.literal_eval(raw)
    if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise ValueError('expected a list of messages')
    result = []
    for message in messages:
        role = message.get('role')
        if role == 'system':
            continue
        if role not in ('user', 'assistant', 'tool'):
            raise ValueError('unexpected message role')
        content = message.get('content')
        if content is not None and not isinstance(content, str):
            raise ValueError('expected text content')
        if role == 'user' and (content or '').startswith(STATE_PREFIX):
            # Friday rewrites and moves this message on each request; keep it out of the transcript.
            continue
        item = {'role': role, 'content': content}
        if 'tool_call_id' in message:
            item['tool_call_id'] = message['tool_call_id']
        if message.get('tool_calls'):
            item['tool_calls'] = [
                {'id': tool.get('id'), 'name': tool['function']['name'], 'arguments': tool['function']['arguments']}
                for tool in message['tool_calls']
            ]
        result.append(item)
    return result


def message_key(message):
    # Friday shortens old tool results between requests, so a tool result matches by its call id.
    if message.get('role') == 'tool' and message.get('tool_call_id'):
        return 'tool', message['tool_call_id']
    if message.get('tool_calls'):
        return 'calls', tuple(call.get('id') or call['name'] for call in message['tool_calls'])
    return message.get('role'), message.get('content')


def context_overlap(merged, context):
    merged = [message_key(message) for message in merged if not message.get('gap')]
    context = [message_key(message) for message in context]
    for size in range(min(len(merged), len(context)), 0, -1):
        if merged[-size:] == context[:size]:
            return size
    return 0


def json_object(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


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

    def digest(self, end, ended):
        transcript, timeline, events = [], [], []
        flags, blocked = Counter(), Counter()
        audio, lost, jitter = [], [], []
        llm_calls = logged_requests = 0
        trimmed = unparsed = False
        for entry in self.entries:
            text = entry.text
            at = entry.at.isoformat()
            if text.startswith('Friday '):
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
                    context = context_messages(raw)
                except (SyntaxError, ValueError, TypeError, KeyError, RecursionError):
                    unparsed = True
                    continue
                overlap = context_overlap(transcript, context)
                added = context[overlap:]
                if transcript and not overlap:
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
            'id': f'{start}-{self.start.pid}', 'start': start, 'end': end.isoformat(),
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
    for at, item in timeline:
        clock = datetime.fromisoformat(at).strftime('%H:%M:%S')
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


def write_state(path, cursor, done):
    atomic_write(path, json.dumps({'cursor': cursor, 'done': done[-200:]}) + '\n')


def emit(directory, call, end, ended):
    data, timeline = call.digest(end, ended)
    day = directory / call.start.at.strftime('%Y-%m-%d')
    day.mkdir(mode=0o700, exist_ok=True)
    base = day / f"{call.start.at:%H%M%S}-{call.start.pid}"
    md_path = base.with_suffix('.md')
    atomic_write(base.with_suffix('.json'), json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    atomic_write(md_path, markdown(data, timeline))
    print(f"friday-call {call.start.at:%H:%M} {data['duration_s']:g}s turns={data['turns']} "
          f"ended={ended} flags={flag_text(data['flags'])} digest={md_path}", flush=True)
    return data['id']


def scan_locked(directory, since):
    state_path = directory / 'state.json'
    if since is None:
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            tail = journal('-n', '1')
            write_state(state_path, tail[-1]['__CURSOR'] if tail else None, [])
            return
        cursor, done = state['cursor'], state['done']
        rows = journal(f'--after-cursor={cursor}') if cursor else journal()
    else:
        cursor, done = None, []
        rows = journal('--since', since)

    def finish(call, end, ended, checkpoint):
        nonlocal cursor
        call_id = f'{call.start.at.isoformat()}-{call.start.pid}'
        if call_id not in done:
            # Flush the notification before acknowledging it in state.json.
            done.append(emit(directory, call, end, ended))
        if since is None:
            write_state(state_path, checkpoint, done)
            cursor = checkpoint

    previous = cursor
    call = None
    for entry in journal_entries(rows):
        match = entry.pipecat()
        peer = match and match[2].startswith(PEER) and match[3] == 'Initializing new peer connection'
        runner = RUNNER.match(match[3]) if match else None
        new_runner = runner[1] if runner and runner[2] == 'started' else None
        if call and (peer or entry.lifecycle or (entry.pid and entry.pid != call.start.pid)
                     or (new_runner and call.runner and new_runner != call.runner)):
            finish(call, entry.at, 'abrupt', previous)
            call = None
        if call is None and (peer or new_runner):
            # --after-cursor excludes its anchor. Keep the entry before the start.
            call = Call(entry, previous, new_runner)
        if call:
            call.runner = call.runner or new_runner
            call.entries.append(entry)
            if runner and runner[2] == 'finished' and call.runner == runner[1]:
                finish(call, entry.at, 'clean', entry.cursor)
                call = None
        previous = entry.cursor
    checkpoint = call.checkpoint if call else previous
    if since is None and checkpoint != cursor:
        write_state(state_path, checkpoint, done)


def scan(directory, since=None):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The directory inode stays stable while state.json is atomically replaced.
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
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
    directory = Path(os.environ.get('TETHER_FRIDAY_CALLS') or paths.CACHE / 'friday-calls').resolve()
    try:
        if args.command == 'show':
            print(args.digest.with_suffix('.md').read_text(encoding='utf-8'), end='')
        elif args.command == 'scan':
            scan(directory, args.since)
        else:
            if not math.isfinite(args.interval) or args.interval <= 0:
                parser.error('--interval must be positive and finite')
            reported = set()
            while True:
                try:
                    scan(directory)
                except JournalError as exc:
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
