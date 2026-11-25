"""Read subagents and background tasks for the selected conversation."""
from collections import OrderedDict, defaultdict, deque
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from html import unescape
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time

from antigravity import process_start
from agents import codex_database
import procinfo
import paths
import task_process
import tool_details

TAIL_BYTES = 256 * 1024
# One entry per transcript, agent log or metadata file. One open Claude tab needs about 44; 56 tabs needed
# 2,451 entries and about 54 MB (B65, 2026-11-14). At 200, switching tabs evicted the scans and each
# listing reread up to 67 MB of transcripts.
CACHE_LIMIT = 4096
_cache = OrderedDict()
_lock = threading.Lock()
_UNAVAILABLE = 'That agent is no longer available.'
TASKS_TMP = Path(tempfile.gettempdir()) / f'claude-{os.getuid()}'
PROJECTS = paths.CLAUDE / 'projects'
_tasks_cache = OrderedDict()
_MISSING = object()


def _cached(path, reader, *, incremental=False, namespace=None):
    info = path.stat()
    key = str(path) if namespace is None else (str(path), namespace)
    stamp = (info.st_mtime_ns, info.st_size)
    with _lock:
        previous = _cache.get(key)
        if previous is None or previous[0] != stamp:
            value = reader(path, previous) if incremental else reader(path)
            _cache[key] = (stamp, value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)
        return _cache[key][1], info


def _records(lines):
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(record, dict) and isinstance(record.get('type'), str):
            if record['type'] in ('user', 'assistant') and not isinstance(record.get('message'), dict):
                continue
            yield record


def _tokens(message):
    usage = message.get('usage')
    if not isinstance(usage, dict):
        return None
    counts = [usage.get(key, 0) for key in (
        'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')]
    return sum(counts) if all(isinstance(value, int) and not isinstance(value, bool) for value in counts) else None


def _main(path, previous=None):
    result = {'model': None, 'context': None, 'title': None, '_notifications': {}}
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - TAIL_BYTES)
        stream.seek(offset)
        tail = stream.read(TAIL_BYTES)
        if offset:
            tail = tail.partition(b'\n')[2]
        for record in _records(tail.splitlines()):
            message = record.get('message')
            if record.get('type') == 'assistant' and isinstance(message, dict) and isinstance(message.get('usage'), dict):
                result.update(model=message.get('model'), context=_tokens(message))
        # Titles and notifications can precede the tail. Scan once, then resume at
        # the last complete line when the transcript grows (including split writes).
        start = 0
        if previous is not None and size > previous[0][1]:
            old = previous[1]
            start = old['_offset']
            result.update(title=old['title'], _notifications=dict(old['_notifications']))
        stream.seek(start)
        while True:
            result['_offset'] = stream.tell()
            line = stream.readline()
            if not line:
                break
            for record in _records([line]):
                field = {'custom-title': 'customTitle', 'agent-name': 'agentName'}.get(record['type'])
                title = record.get(field) if field else None
                if isinstance(title, str) and title.strip():
                    result['title'] = title.strip()
                if record['type'] == 'user':
                    _notifications(_text(record['message'].get('content')), result['_notifications'])
            if not line.endswith(b'\n'):
                break
    return result


def _notifications(text, found):
    # Some producers put a JSON string inside the already decoded content.
    for _ in range(2):
        try:
            decoded = json.loads(text)
        except (ValueError, RecursionError):
            break
        if not isinstance(decoded, str):
            break
        text = decoded
    for match in re.finditer(r'<task-notification>(.*?)</task-notification>', text, re.S):
        body = match[1]
        identifier = re.search(r'<task-id>(.*?)</task-id>', body, re.S)
        status = re.search(r'<status>(completed|failed|killed)</status>', body)
        summary = re.search(r'<summary>(.*?)</summary>', body, re.S)
        if identifier and status:
            found[identifier[1].strip()] = (status[1], unescape(summary[1]).strip()[:240] if summary else None)


def _meta(path):
    data = json.loads(path.read_bytes())
    return data if isinstance(data, dict) else {}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part['text'] for part in content if isinstance(part, dict)
                         and part.get('type') == 'text' and isinstance(part.get('text'), str))
    return ''


def _parse(path):
    data = {'model': None, 'tokens': None, 'started': None, 'ended': None,
            'tools': 0, 'prompt': None, 'report': None, 'steps_cut': False, 'tool_ids': set(),
            '_results': {}, '_assistant': False}
    steps, calls = deque(), {}
    seen = False
    with path.open('rb') as stream:
        for record in _records(stream):
            if not seen:
                data['started'] = record.get('timestamp')
                seen = True
            data['ended'] = record.get('timestamp')
            message = record.get('message')
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            role = record.get('type')
            if role == 'user' and data['prompt'] is None:
                data['prompt'] = tool_details.bounded(_text(content), max_bytes=16384)[0]
            if role == 'assistant':
                data['_assistant'] = True
                data['model'] = message.get('model')
                if isinstance(message.get('usage'), dict):
                    data['tokens'] = _tokens(message)
            if isinstance(content, str) and role == 'assistant':
                content = [{'type': 'text', 'text': content}]
            if not isinstance(content, list) or role not in ('assistant', 'user'):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                step = None
                if role == 'assistant' and part.get('type') == 'text' and isinstance(part.get('text'), str):
                    data['report'] = tool_details.bounded(part['text'], max_bytes=16384)[0]
                    step = {'kind': 'text', 'text': tool_details.bounded(part['text'], max_bytes=4000)[0]}
                elif role == 'assistant' and part.get('type') == 'tool_use':
                    data['tools'] += 1
                    if isinstance(part.get('id'), str):
                        data['tool_ids'].add(part['id'])
                    summary = tool_details.summary(part)
                    step = dict(kind='tool', **{key: summary[key] for key in ('id', 'name', 'title', 'is_error', 'status')})
                    calls[step['id']] = step
                elif part.get('type') == 'tool_result':
                    identifier = part.get('tool_use_id')
                    if isinstance(identifier, str):
                        data['_results'][identifier] = tool_details.failed(part)
                        if identifier in calls:
                            calls[identifier].update(status='complete', is_error=data['_results'][identifier])
                if step is not None:
                    steps.append(step)
                    if len(steps) > 400:
                        dropped = steps.popleft()
                        if dropped.get('kind') == 'tool' and calls.get(dropped['id']) is dropped:
                            del calls[dropped['id']]
                        data['steps_cut'] = True
    if not seen:
        raise ValueError(_UNAVAILABLE)
    return dict(data, _has_prompt=data['prompt'] is not None, prompt=data['prompt'] or '', steps=list(steps))


def _folder(config):
    if config.get('agent') != 'claude' or not config.get('transcript'):
        return None
    return (Path(config['transcript']).with_suffix('') / 'subagents').resolve()


def _path(config, agent_id):
    if not isinstance(agent_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', agent_id):
        raise ValueError(_UNAVAILABLE)
    try:
        files, _ = _agent_files(config, _tasks_dir(config))
        if agent_id in files:
            return files[agent_id][-1]
    except (OSError, ValueError, RuntimeError):
        pass
    raise ValueError(_UNAVAILABLE)


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _owner_since(config):
    if not config.get('owner_pid') or not config.get('process_start') or config.get('pending_conversation'):
        return None
    try:
        if process_start(config['owner_pid']) != config['process_start']:
            return None
        return procinfo.start_ms(config['process_start']) / 1000
    except (OSError, ValueError):
        return None


def _fd_links(pid):
    """Keep proc access separate so tests never inspect real process fds."""
    found = []
    for fd in Path(f'/proc/{int(pid)}/fd').iterdir():
        try:
            found.append(Path(os.readlink(fd)))
        except OSError:
            continue
    return found


def _uuid(value):
    return isinstance(value, str) and re.fullmatch(
        r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value) is not None


def _tasks_dir(config, *, since=_MISSING):
    return _tasks_source(config, since=since)[0]


def _tasks_source(config, *, since=_MISSING):
    """Return (tasks folder, whether the live owner process holds it open)."""
    if config.get('agent') != 'claude':
        return None, False
    if since is _MISSING:
        since = _owner_since(config)
    if since is None:
        return None, False
    key = (config['owner_pid'], config['process_start'])
    now = time.monotonic()
    with _lock:
        cached = _tasks_cache.get(key)
        if cached and now - cached[0] < 30:
            return cached[1], cached[2]
    found, by_fd = None, False
    try:
        root = TASKS_TMP.resolve()
        if root.stat().st_uid != os.getuid():
            return None, False
        candidates = []
        try:
            candidates = [(link, True) for link in _fd_links(config['owner_pid'])]
        except (OSError, ValueError):
            pass
        if _uuid(config.get('thread')):
            candidates.extend((link, False) for link in root.glob(f'*/{config["thread"]}/tasks'))
        for candidate, from_fd in candidates:
            candidate = Path(candidate)
            resolved = candidate.resolve()
            if (candidate.name == 'tasks' and resolved.name == 'tasks' and _uuid(resolved.parent.name)
                    and resolved.parent.parent.parent == root and resolved.is_dir()):
                found, by_fd = resolved, from_fd
                break
    except (OSError, ValueError, RuntimeError):
        pass
    with _lock:
        _tasks_cache[key] = (now, found, by_fd)
        _tasks_cache.move_to_end(key)
        while len(_tasks_cache) > CACHE_LIMIT:
            _tasks_cache.popitem(last=False)
    return found, by_fd


def _agent_files(config, tasks_dir, *, metadata=False):
    """Return authorized files by id and the ids present in the current chat."""
    files, current = {}, set()
    folder = _folder(config)
    if folder is None:
        return files, current
    suffix = '.meta.json' if metadata else '.jsonl'
    earlier_folder = (folder.parent.parent / tasks_dir.parent.name / 'subagents'
                      if tasks_dir is not None else None)
    folders = [folder]
    if metadata and earlier_folder is not None and earlier_folder != folder:
        folders.append(earlier_folder)
    for directory in folders:
        for path in directory.glob(f'agent-*{suffix}'):
            identifier = path.name.removeprefix('agent-').removesuffix(suffix)
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
                continue
            try:
                resolved = path.resolve()
                in_directory = resolved.parent == directory if metadata else resolved.is_relative_to(directory)
                if not in_directory or not resolved.is_file():
                    continue
                files.setdefault(identifier, set()).add(resolved)
                if directory == folder:
                    current.add(identifier)
            except (OSError, ValueError, RuntimeError):
                continue
    if tasks_dir is not None:
        # Current-chat agents can also span /clear without an output symlink.
        if not metadata:
            for identifier in current:
                earlier = earlier_folder / f'agent-{identifier}.jsonl'
                try:
                    resolved = earlier.resolve()
                    if resolved.parent == earlier.parent and resolved.is_file():
                        files[identifier].add(resolved)
                except (OSError, ValueError, RuntimeError):
                    continue
        projects = PROJECTS.resolve()
        for link in tasks_dir.glob('*.output'):
            if not link.is_symlink():
                continue
            identifier = link.stem
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
                continue
            try:
                target = link.resolve()
                relative = target.relative_to(projects)
                if (len(relative.parts) != 4 or not _uuid(relative.parts[1])
                        or relative.parts[2] != 'subagents' or target.name != f'agent-{identifier}.jsonl'):
                    continue
                # /clear points the link at the new session at once, before the agent writes there, and an
                # agent can span several /clears. Its lines are in any session folder of this project.
                group = set()
                for extra in (projects / relative.parts[0]).glob(f'*/subagents/agent-{identifier}{suffix}'):
                    resolved = extra.resolve()
                    if _uuid(extra.parent.parent.name) and resolved.parent == extra.parent and resolved.is_file():
                        group.add(resolved)
                if group:
                    files.setdefault(identifier, set()).update(group)
            except (OSError, ValueError, RuntimeError):
                continue
    return {identifier: list(group) for identifier, group in files.items()}, current


def bar_candidates(config):
    """Read agent names before any listing filters, including agents carried across /clear."""
    # Claude Code 2.1.330 jbe -> sg -> Ndo reads the process's tasks. IQs keeps
    # eligible tasks across /clear, and n1e links local agents to the owner's tasks folder.
    tasks_dir = _tasks_dir(config)
    files, _ = _agent_files(config, tasks_dir, metadata=True)
    # Claude keeps an agent in memory when its metadata write fails. A known
    # transcript without metadata could have any name, so the pool is unknown.
    # Claude makes an agent's task output (a link, or a regular file when the
    # link fails) before its transcript exists. Claude 2.1.330 task ids start
    # with a kind letter, and "a" is local_agent, so any a-prefixed entry is an agent.
    known = set(_agent_files(config, tasks_dir, metadata=False)[0])
    if tasks_dir is not None:
        try:
            known.update(entry.stem for entry in tasks_dir.glob('a*.output'))
        except OSError as exc:
            raise ValueError(_UNAVAILABLE) from exc
    if known - set(files):
        raise ValueError(_UNAVAILABLE)
    records = []
    for identifier, group in files.items():
        names = set()
        for path in group:
            meta, _ = _cached(path, _meta)
            kind, description = meta.get('agentType'), meta.get('description')
            if any(value is not None and not isinstance(value, str) for value in (kind, description)):
                raise ValueError(_UNAVAILABLE)
            names.add((kind, description))
        # /clear can copy the same sidecar. Count that name once and retain any distinct names.
        records.extend({'id': identifier, 'type': kind, 'description': description}
                       for kind, description in names)
    return records


def _agent_data(files):
    parsed = []
    for path in files:
        data, _ = _cached(path, _parse)
        parsed.append((path, data))
    parsed.sort(key=lambda item: (_stamp(item[1]['started']) is not None, _stamp(item[1]['started']) or 0))
    data = deepcopy(parsed[0][1])
    steps = []
    results, tool_ids = {}, set()
    data.update(tools=0, steps_cut=False)
    prompt = None
    meta = {}
    for path, part in parsed:
        if prompt is None and part['_has_prompt']:
            prompt = part['prompt']
        if part['_assistant']:
            data['model'] = part['model']
        if part['tokens'] is not None:
            data['tokens'] = part['tokens']
        if part['report'] is not None:
            data['report'] = part['report']
        data['ended'] = part['ended']
        data['tools'] += part['tools']
        data['steps_cut'] |= part['steps_cut']
        steps.extend(deepcopy(part['steps']))
        tool_ids.update(part['tool_ids'])
        results.update(part['_results'])
        meta_path = path.with_suffix('.meta.json')
        try:
            if meta_path.resolve().parent == path.parent:
                meta, _ = _cached(meta_path, _meta)
        except (OSError, ValueError, RecursionError, RuntimeError):
            pass
    for step in steps:
        if step['kind'] == 'tool' and step['id'] in results:
            step.update(status='complete', is_error=results[step['id']])
    data.update(prompt=prompt or '', steps=steps[-400:], tool_ids=tool_ids,
                steps_cut=data['steps_cut'] or len(steps) > 400)
    return data, meta


# A new background agent can run briefly before Claude links its output into the tasks folder.
LINK_GRACE = 10


def _entry(agent_id, files, now, notifications, since, members=None):
    data, meta = _agent_data(files)
    status, summary = notifications.get(agent_id, (None, None))
    start = _stamp(data['started'])
    if meta.get('stoppedByUser') or status == 'killed':
        state = 'stopped'
    elif status == 'failed':
        state = 'failed'
    elif meta.get('finished') or status == 'completed':
        state = 'finished'
    else:
        state = 'running' if since is not None and (start is None or start >= since - 5) else 'stopped'
        if (state == 'running' and members is not None and meta.get('requestShape') == 'background'
                and agent_id not in members and (start is None or now - start > LINK_GRACE)):
            # The live process holds this tasks folder open. A background agent it owns has an
            # output link there; without one, the agent belonged to another (dead) process.
            state = 'stopped'
        if state == 'stopped' and not summary:
            summary = 'Claude exited before this agent finished.'
    end = now if state == 'running' else _stamp(data['ended'])
    entry = {'id': agent_id, 'description': meta.get('description'), 'type': meta.get('agentType'),
             'model': data['model'] or meta.get('model') or meta.get('pickedEffortModel'),
             'effort': meta.get('effort'), 'background': meta.get('requestShape') == 'background',
             'state': state, 'started': data['started'], 'ended': None if state == 'running' else data['ended'],
             'seconds': int(end - start) if start is not None and end is not None else None,
             'tokens': data['tokens'], 'tools': data['tools'], 'depth': meta.get('spawnDepth', 1),
             'parent': None, 'summary': summary if state in ('failed', 'stopped') else None}
    return entry, data, meta


_REF_LIMIT = 1024


def _task_refs(text, refs, root):
    """Add each "<root>/<slug>/<uuid>/tasks" folder that text mentions to refs. These are the only folders
    _tasks_dir() can return, so a tab's folder is mentioned exactly when its path is in refs."""
    prefix = root + '/'
    start = 0
    while len(refs) < _REF_LIMIT and (index := text.find(prefix, start)) >= 0:
        start = index + len(prefix)
        slash = text.find('/', start)
        if slash <= start:
            continue
        rest = text[slash + 1:slash + 43]
        if rest[36:] == '/tasks' and _uuid(rest[:36]):
            refs.add(text[index:slash + 43])


def _task_scan(path, previous=None, *, root):
    """Index task starts, notifications and tasks-folder mentions once, then scan only appended lines.
    The index does not depend on the tab that asks, so all tabs of one project share one scan per file."""
    data = {'starts': {}, 'calls': {}, 'notifications': {}, 'refs': set(), '_offset': 0}
    root_bytes = root.encode()
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        if previous is not None and size > previous[0][1]:
            old = previous[1]
            data = dict(old, starts=dict(old['starts']), calls=dict(old['calls']),
                        notifications=dict(old['notifications']), refs=set(old['refs']))
        stream.seek(data['_offset'])
        while True:
            data['_offset'] = stream.tell()
            line = stream.readline()
            if not line:
                break
            if root_bytes in line:
                _task_refs(line.decode('utf-8', errors='replace'), data['refs'], root)
            for record in _records([line]):
                message = record.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                text = _text(content)
                if root in text:
                    _task_refs(text, data['refs'], root)
                if record['type'] == 'assistant' and isinstance(content, list):
                    for part in content:
                        if (isinstance(part, dict) and part.get('type') == 'tool_use'
                                and part.get('name') in ('Bash', 'Monitor') and isinstance(part.get('id'), str)):
                            inputs = part.get('input')
                            inputs = inputs if isinstance(inputs, dict) else {}
                            command, description = inputs.get('command'), inputs.get('description')
                            data['calls'][part['id']] = {
                                'kind': 'monitor' if part['name'] == 'Monitor' else 'shell',
                                'command': command if isinstance(command, str) else None,
                                'description': description if isinstance(description, str) else None}
                if record['type'] != 'user':
                    continue
                _notifications(_text(content), data['notifications'])
                result = record.get('toolUseResult')
                if not isinstance(result, dict):
                    continue
                identifier = result.get('backgroundTaskId') or result.get('taskId')
                if not isinstance(identifier, str) or not re.fullmatch(r'[a-z0-9]{6,32}', identifier):
                    continue
                ids = [part.get('tool_use_id') for part in content if isinstance(part, dict)] if isinstance(content, list) else []
                call = next((data['calls'][key] for key in ids if key in data['calls']), {})
                data['starts'][identifier] = dict(call, started=record.get('timestamp'), _call_ids=ids)
            if not line.endswith(b'\n'):
                break
    return data


def _task_path(tasks_dir, identifier):
    if (tasks_dir is None or not isinstance(identifier, str)
            or not re.fullmatch(r'[a-z0-9]{6,32}', identifier)):
        raise ValueError(_UNAVAILABLE)
    path = tasks_dir / f'{identifier}.output'
    if path.is_symlink() or path.resolve().parent != tasks_dir or not path.is_file():
        raise ValueError(_UNAVAILABLE)
    return path


def _footer(path):
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 200))
        tail = stream.read(200).decode('utf-8', errors='replace').rstrip()
    match = re.search(r'(?:^|\n)(\[exited with code (-?\d+)\]|\[killed\])$', tail)
    if match is None:
        return None, None
    state = 'stopped' if match[1] == '[killed]' else 'finished' if int(match[2]) == 0 else 'failed'
    return state, match[1]


def _tasks(config, tasks_dir, files, now, since, *, include_finished=False):
    if tasks_dir is None:
        return []
    main = Path(config['transcript']).resolve()
    sources = {main: (None, True)}
    sources.setdefault(main.parent / f'{tasks_dir.parent.name}.jsonl', (None, False))
    # Only recent project transcripts may belong to this process. Inspect their
    # contents through the incremental index before admitting their task starts.
    for path in main.parent.glob('*.jsonl'):
        try:
            if _uuid(path.stem) and path.stat().st_mtime >= since:
                sources.setdefault(path, (None, False))
        except OSError:
            continue
    for identifier, group in files.items():
        for path in group:
            sources[path] = (identifier, False)
    # tasks_dir is always TASKS_TMP.resolve()/<slug>/<uuid>/tasks, so one scan per transcript and root
    # serves every tab of the project.
    starts, notifications, calls, scans = {}, {}, {}, []
    marker, root = str(tasks_dir), str(tasks_dir.parent.parent.parent)
    first = main.parent / f'{tasks_dir.parent.name}.jsonl'
    for path, (parent, in_scope) in sources.items():
        try:
            scan, _ = _cached(path, lambda p, old: _task_scan(p, old, root=root),
                              incremental=True, namespace=('tasks', root))
            if parent is None and path not in (main, first) and marker not in scan['refs']:
                continue
            notifications.update(scan['notifications'])
            calls.update(scan['calls'])
            scans.append((scan, parent, in_scope))
        except (OSError, ValueError, RuntimeError):
            continue
    # A foreground call can start before /clear and become a background task
    # afterwards. Resolve call ids across all authorized process transcripts.
    for scan, parent, in_scope in scans:
        if parent is None and in_scope:
            notifications.update(scan['notifications'])
        for identifier, start in scan['starts'].items():
            call = next((calls[key] for key in start['_call_ids'] if key in calls), {})
            starts[identifier] = dict(start, **call, parent=parent,
                                      _scope=in_scope or starts.get(identifier, {}).get('_scope', False))
    result, seen = [], set()
    ledger = task_process.stopped()
    for candidate in tasks_dir.glob('*.output'):
        try:
            seen.add(candidate.stem)
            path = _task_path(tasks_dir, candidate.stem)
            info = path.stat()
            (state, footer), _ = _cached(path, _footer)
            status, summary = notifications.get(candidate.stem, (None, None))
            if state is None:
                state = {'completed': 'finished', 'failed': 'failed', 'killed': 'stopped'}.get(status, 'running')
            if state == 'failed' and task_process.key(tasks_dir, candidate.stem) in ledger:
                # Tether stopped it with a signal; Claude records that as a non-zero exit.
                state, footer, summary = 'stopped', None, STOPPED_BY_TETHER
            start = starts.get(candidate.stem, {})
            if not start:
                # Claude makes an output file for every Bash call, so a foreground command can move to
                # the background. Only a start record (backgroundTaskId/taskId) makes it a background task.
                continue
            started = start.get('started')
            stamp = _stamp(started)
            if stamp is None:
                stamp = getattr(info, 'st_birthtime', info.st_ctime)
                started = datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z')
            ended = info.st_mtime if footer else None
            result.append({'id': candidate.stem, 'kind': start.get('kind', 'shell'),
                           'description': start.get('description'), 'command': start.get('command'),
                           'state': state, 'started': started,
                           'ended': datetime.fromtimestamp(ended, timezone.utc).isoformat().replace('+00:00', 'Z') if ended is not None else None,
                           'seconds': int((ended if ended is not None else now) - stamp),
                           'parent': start.get('parent'), 'summary': (footer or summary) if state in ('failed', 'stopped') else None,
                           '_scope': start['_scope'], '_output': path})
        except (OSError, ValueError, RuntimeError):
            continue
    result.extend(_monitors(config, tasks_dir, starts, notifications, ledger, seen, now, since,
                            include_finished=include_finished))
    result.sort(key=lambda item: _stamp(item['started']) or 0)
    return result


STOPPED_BY_TETHER = 'Stopped from Tether.'


def _monitors(config, tasks_dir, starts, notifications, ledger, seen, now, since, *, include_finished=False):
    """Claude Code 2.1.325 writes no output file for a monitor; read its state from its process."""
    waiting = {identifier: start for identifier, start in starts.items()
               if start.get('kind') == 'monitor' and identifier not in seen
               and (start['_scope'] or start.get('parent') is not None or include_finished)}
    if not waiting:
        return []
    pids = None
    found = []
    for identifier, start in waiting.items():
        stamp = _stamp(start.get('started'))
        if stamp is None:
            continue
        status, summary = notifications.get(identifier, (None, None))
        state = {'completed': 'finished', 'failed': 'failed', 'killed': 'stopped'}.get(status)
        if state is None:
            if since is None or stamp < since - 5:
                state, summary = 'stopped', 'Claude exited before this task finished.'
            else:
                if pids is None:
                    try:
                        pids = task_process.children(config['owner_pid'])
                    except (OSError, ValueError):
                        pids = []
                alive = task_process.running(config['owner_pid'], start, stamp, pids=pids)
                state, summary = ('running', None) if alive else ('finished', None)
        if task_process.key(tasks_dir, identifier) in ledger and state != 'running':
            state, summary = 'stopped', STOPPED_BY_TETHER
        found.append({'id': identifier, 'kind': 'monitor', 'description': start.get('description'),
                      'command': start.get('command'), 'state': state, 'started': start.get('started'),
                      'ended': None, 'seconds': int(now - stamp) if state == 'running' else None,
                      'parent': start.get('parent'), 'summary': summary if state in ('failed', 'stopped') else None,
                      '_scope': start['_scope'], '_output': None})
    return found


def _waiting_children(config, tasks, finished, since):
    waiting = defaultdict(lambda: {'shell': 0, 'monitor': 0})
    pids = None
    for task in tasks:
        parent, kind = task['parent'], task['kind']
        if parent not in finished or task['state'] != 'running':
            continue
        if kind == 'shell' or task['_output'] is not None:
            # An unfinished task record can outlive the Claude process that started it.
            stamp = _stamp(task['started'])
            if since is None or stamp is None or stamp < since - 5:
                continue
            if kind == 'monitor':
                if pids is None:
                    try:
                        pids = task_process.children(config['owner_pid'])
                    except (OSError, ValueError):
                        pids = []
                if not task_process.running(config['owner_pid'], task, stamp, pids=pids):
                    continue
        # _monitors already checked liveness for monitors without output.
        waiting[parent][kind] += 1
    return waiting


def _listing(config, *, limit=True, include_finished=False):
    result = {'main': {'model': None, 'context': None, 'title': None}, 'agents': [], 'tasks': []}
    since = _owner_since(config) if config.get('agent') == 'claude' else None
    tasks_dir, by_fd = _tasks_source(config, since=since)
    members = None
    if by_fd:
        try:
            members = {link.stem for link in tasks_dir.glob('*.output') if link.is_symlink()}
        except OSError:
            members = None
    try:
        files, current = _agent_files(config, tasks_dir)
        folder = _folder(config)
        if folder is None or (not folder.is_dir() and tasks_dir is None):
            return result
    except (OSError, ValueError, RuntimeError):
        return result
    notifications = {}
    try:
        main, _ = _cached(Path(config['transcript']), _main, incremental=True)
        result['main'] = {key: main[key] for key in ('model', 'context', 'title')}
        notifications = main['_notifications']
    except (OSError, ValueError):
        pass
    now = time.time()
    candidates = {}
    for identifier, group in files.items():
        try:
            entry, data, meta = _entry(identifier, group, now, notifications, since, members)
        except (OSError, ValueError, UnicodeError, RecursionError):
            continue
        if identifier not in current and entry['state'] not in ('running', 'finished') and not include_finished:
            continue
        candidates[identifier] = entry, data['tool_ids'], meta.get('toolUseId')

    sources = {identifier: group for identifier, group in files.items()
               if identifier in current or identifier in candidates}
    tasks = _tasks(config, tasks_dir, sources, now, since, include_finished=include_finished)
    # A waiting background agent can survive /clear. Resolve waiting before applying chat scope or limits.
    finished = {identifier for identifier, (entry, _, _) in candidates.items() if entry['state'] == 'finished'}
    waiting = _waiting_children(config, tasks, finished, since)
    owners, spawned = {}, {}
    for identifier, (entry, tool_ids, spawn_id) in candidates.items():
        if entry['state'] == 'finished' and identifier in waiting:
            start = _stamp(entry['started'])
            entry.update(state='running', ended=None, seconds=int(now - start) if start is not None else None,
                         waiting=waiting[identifier])
        if identifier not in current and entry['state'] != 'running' and not include_finished:
            continue
        result['agents'].append(entry)
        for tool_id in tool_ids:
            owners[tool_id] = identifier
        if isinstance(spawn_id, str):
            spawned[identifier] = spawn_id
    for entry in result['agents']:
        parent = owners.get(spawned.get(entry['id']))
        entry['parent'] = parent if parent != entry['id'] else None
    result['agents'].sort(key=lambda entry: (_stamp(entry['started']) is not None, _stamp(entry['started']) or 0))
    if limit:
        result['agents'] = result['agents'][-40:]
    shown = {item['id'] for item in result['agents']}
    for task in tasks:
        in_scope = task.pop('_scope')
        output = task.pop('_output')
        parent = task['parent']
        if parent in shown or (parent is None or parent in current) and (
                in_scope or include_finished or output is not None and task['state'] == 'running'):
            # Liveness checks use the full command. Bound it for the listing response.
            if task['command'] is not None:
                task['command'] = task['command'][:2000]
            result['tasks'].append(task)
    result['tasks'] = result['tasks'][-40:]
    return result


_CODEX_CALLS = ('function_call', 'custom_tool_call')
_CODEX_OUTPUTS = ('function_call_output', 'custom_tool_call_output')
_CODEX_STEPS = 400


def _codex_index(config):
    thread, transcript = config.get('thread'), config.get('transcript')
    if config.get('pending_conversation') or not _uuid(thread) or not transcript:
        return None, {}
    children = defaultdict(list)
    try:
        with closing(codex_database()) as db:
            parent = db.execute('SELECT model FROM threads WHERE id=? AND rollout_path=?',
                                (thread, str(transcript))).fetchone()
            if parent is None:
                return None, {}
            rows = db.execute('''
                WITH RECURSIVE tree(id) AS (
                    VALUES (?)
                    UNION
                    SELECT e.child_thread_id FROM thread_spawn_edges e JOIN tree ON e.parent_thread_id=tree.id
                )
                SELECT e.parent_thread_id, t.id, t.rollout_path, t.model, t.reasoning_effort, t.tokens_used, e.status
                FROM tree JOIN thread_spawn_edges e ON e.child_thread_id=tree.id JOIN threads t ON t.id=tree.id
                WHERE tree.id != ?
            ''', (thread, thread))
            for parent_id, *row in rows:
                children[parent_id].append(row)
    except sqlite3.Error:
        return None, {}
    return parent[0], children


def _codex_text(payload, kind):
    content = payload.get('content')
    if not isinstance(content, list):
        return ''
    return '\n'.join(item['text'] for item in content if isinstance(item, dict)
                     and item.get('type') == kind and isinstance(item.get('text'), str))


def _codex_records(stream):
    for line in stream:
        # Leave a split write for the next poll.
        if not line.endswith(b'\n'):
            break
        for record in _records([line]):
            payload = record.get('payload')
            if isinstance(payload, dict):
                yield record, payload


def _codex_scan(path, previous=None):
    with path.open('rb') as stream:
        header_line = stream.readline()
        fingerprint = sha256(header_line).digest()
        if (previous is not None and os.fstat(stream.fileno()).st_size > previous[0][1]
                and previous[1]['_header'] == fingerprint):
            data = dict(previous[1])
            stream.seek(data['_next_offset'])
        else:
            header = next(_records([header_line]), {})
            meta = header.get('payload')
            if header.get('type') != 'session_meta' or not isinstance(meta, dict):
                raise ValueError(_UNAVAILABLE)
            source = meta.get('source')
            subagent = source.get('subagent') if isinstance(source, dict) else None
            spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
            if not isinstance(spawn, dict) or not _uuid(spawn.get('parent_thread_id')):
                raise ValueError(_UNAVAILABLE)
            # A fork's rollout starts with copied parent records, including turn and tool events.
            ordinal = meta.get('subagent_history_start_ordinal', 1)
            if type(ordinal) is not int or ordinal < 1:
                raise ValueError(_UNAVAILABLE)
            for _ in range(ordinal - 1):
                if not stream.readline().endswith(b'\n'):
                    raise ValueError(_UNAVAILABLE)
            data = dict(id=meta.get('id'), spawn=spawn, started=header.get('timestamp'),
                        turn=None, turn_started=None, ended=None, state=None, tools=0,
                        prompt='', report=None, steps=[], steps_cut=False,
                        _header=fingerprint, _history_end=stream.tell())
        data['_next_offset'] = stream.tell()
        steps = deque(dict(step) for step in data['steps'])
        calls = {step['id']: step for step in steps if step['kind'] == 'tool'}
        agent_path = data['spawn'].get('agent_path')
        parent_path = agent_path.rpartition('/')[0] if isinstance(agent_path, str) else None
        for record, payload in _codex_records(stream):
            data['_next_offset'] = stream.tell()
            kind = payload.get('type')
            if record['type'] == 'event_msg':
                turn = payload.get('turn_id')
                if kind == 'task_started' and isinstance(turn, str) and turn:
                    data.update(turn=turn, turn_started=record.get('timestamp'), state='running', ended=None, report=None)
                elif data['state'] == 'running' and turn == data['turn'] and kind in ('task_complete', 'turn_aborted'):
                    report = payload.get('last_agent_message')
                    data.update(state='finished' if kind == 'task_complete' else 'stopped', ended=record.get('timestamp'),
                                report=tool_details.bounded(report)[0] if isinstance(report, str) else data['report'])
            if record['type'] != 'response_item':
                continue
            step = None
            call = payload.get('call_id')
            if kind in _CODEX_CALLS and isinstance(call, str) and call:
                step = dict(tool_details.summary(payload), kind='tool')
                calls[call] = step
                data['tools'] += 1
            elif kind in _CODEX_OUTPUTS and isinstance(call, str) and call in calls:
                calls[call].update(status='complete', is_error=tool_details.failed(payload))
            elif kind == 'agent_message' and not data['prompt'] and parent_path and (
                    payload.get('author') == parent_path and payload.get('recipient') == agent_path):
                data['prompt'] = tool_details.bounded(_codex_text(payload, 'input_text'))[0]
            elif kind == 'message' and payload.get('role') == 'assistant':
                if content := _codex_text(payload, 'output_text'):
                    if payload.get('phase') == 'final_answer':
                        data['report'] = tool_details.bounded(content)[0]
                    else:
                        step = {'kind': 'text', 'text': tool_details.bounded(content, max_bytes=4000)[0]}
            if step is not None:
                steps.append(step)
                if len(steps) > _CODEX_STEPS:
                    old = steps.popleft()
                    if old['kind'] == 'tool' and calls.get(old['id']) is old:
                        del calls[old['id']]
                    data['steps_cut'] = True
        data['steps'] = list(steps)
        return data


def _codex_workers(config):
    model, children = _codex_index(config)
    thread = config.get('thread')
    files = {}
    parents = deque([thread]) if children else deque()
    home = paths.CODEX.resolve()
    roots = (home / 'sessions', home / 'archived_sessions')
    while parents:
        parent = parents.popleft()
        for identifier, path, worker_model, effort, tokens, status in children.get(parent, ()):
            if not _uuid(identifier) or identifier in files or not isinstance(path, str) or not path:
                continue
            try:
                path = Path(path).resolve(strict=True)
                if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
                    continue
                data, _ = _cached(path, _codex_scan, incremental=True, namespace='codex-worker')
            except (OSError, ValueError, RuntimeError):
                continue
            if data['id'] != identifier or data['spawn']['parent_thread_id'] != parent:
                continue
            files[identifier] = (path, data, worker_model, effort, tokens, status, parent if parent != thread else None)
            parents.append(identifier)
    return model, files


def _codex_entry(worker, now, since):
    _, data, model, effort, tokens, edge, parent = worker
    spawn = data['spawn']
    started = _stamp(data['started'])
    turn_started = _stamp(data['turn_started']) or started
    state, ended = data['state'], data['ended']
    if state in (None, 'running'):
        if edge == 'open' and since is not None and turn_started is not None and turn_started >= since - 5:
            state = 'running'
        else:
            state, ended = 'stopped', data['turn_started'] or data['started']
    end = now if state == 'running' else _stamp(ended)
    name = spawn.get('agent_path')
    return {'id': data['id'], 'description': (name.rsplit('/', 1)[-1] if isinstance(name, str) else '')
            or spawn.get('agent_nickname') or data['id'], 'type': spawn.get('agent_role') or 'worker',
            'model': model, 'effort': effort, 'background': False, 'state': state, 'started': data['started'],
            'ended': ended, 'seconds': max(0, int(end - started)) if end is not None and started is not None else None,
            'tokens': tokens, 'tools': data['tools'], 'depth': spawn.get('depth', 1), 'parent': parent,
            'summary': None, 'can_stop': False}


def _codex_worker(config, agent_id):
    if not _uuid(agent_id):
        raise ValueError(_UNAVAILABLE)
    _, files = _codex_workers(config)
    if agent_id not in files:
        raise ValueError(_UNAVAILABLE)
    return files[agent_id]


def listing(config, *, limit=True):
    if config.get('agent') != 'codex':
        return _listing(config, limit=limit)
    model, files = _codex_workers(config)
    now, since = time.time(), _owner_since(config) if files else None
    entries = [_codex_entry(worker, now, since) for worker in files.values()]
    entries.sort(key=lambda entry: _stamp(entry['started']) or 0)
    result = {'main': {'model': model, 'context': None, 'title': None},
              'agents': entries[-40:] if limit else entries, 'tasks': []}
    if config.get('thread') is not None:
        result['thread'] = config['thread']
    return result


def detail(config, agent_id):
    if config.get('agent') == 'codex':
        worker = _codex_worker(config, agent_id)
        entry = _codex_entry(worker, time.time(), _owner_since(config))
        data = worker[1]
        return dict(entry, prompt=data['prompt'], steps=data['steps'], steps_cut=data['steps_cut'],
                    report=data['report'] if entry['state'] == 'finished' else None)
    try:
        _path(config, agent_id)
        entry = next((entry for entry in _listing(config, limit=False)['agents'] if entry['id'] == agent_id), None)
        if entry is None:
            raise ValueError(_UNAVAILABLE)
        files, _ = _agent_files(config, _tasks_dir(config))
        data, _ = _agent_data(files[agent_id])
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(_UNAVAILABLE) from exc
    entry.update(prompt=data['prompt'], steps=deepcopy(data['steps']),
                 report=data['report'] if entry['state'] == 'finished' else None)
    if data['steps_cut']:
        entry['steps_cut'] = True
    return entry


def tool(config, agent_id, call_id):
    if config.get('agent') == 'codex':
        path, data, *_ = _codex_worker(config, agent_id)
        if not call_id:
            raise ValueError('Choose a tool call.')
        call, result = None, None
        try:
            with path.open('rb') as stream:
                if sha256(stream.readline()).digest() != data['_header']:
                    raise ValueError(_UNAVAILABLE)
                stream.seek(data['_history_end'])
                for record, payload in _codex_records(stream):
                    if record['type'] != 'response_item' or payload.get('call_id') != call_id:
                        continue
                    if payload.get('type') in _CODEX_CALLS:
                        call = payload
                    elif payload.get('type') in _CODEX_OUTPUTS:
                        result = payload
                    if call is not None and result is not None:
                        break
        except OSError as exc:
            raise ValueError(_UNAVAILABLE) from exc
        if call is None:
            raise ValueError('Tool call is no longer available.')
        return dict(tool_details.build(call, result), images=0)
    _path(config, agent_id)
    if not any(item['id'] == agent_id for item in _listing(config, limit=False)['agents']):
        raise ValueError(_UNAVAILABLE)
    if not call_id:
        raise ValueError('Choose a tool call.')
    files, _ = _agent_files(config, _tasks_dir(config))
    call, result = None, None
    try:
        ordered = sorted(files[agent_id], key=lambda path: _stamp(_cached(path, _parse)[0]['started']) or 0)
        for path in ordered:
            with path.open('rb') as stream:
                for record in _records(stream):
                    if record['type'] not in ('assistant', 'user') or record.get('isMeta'):
                        continue
                    content = record['message'].get('content')
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get('type') == 'tool_use' and part.get('id') == call_id:
                            call = part
                        elif part.get('type') == 'tool_result' and part.get('tool_use_id') == call_id:
                            result = part
        if call is None:
            raise ValueError('Tool call is no longer available.')
    except OSError as exc:
        raise ValueError(_UNAVAILABLE) from exc
    return dict(tool_details.build(call, result), images=len(tool_details.images(result)))


def task(config, task_id):
    try:
        path = _task_path(_tasks_dir(config), task_id)
        entry = next((item for item in _listing(config, limit=False)['tasks'] if item['id'] == task_id), None)
        if entry is None:
            raise ValueError(_UNAVAILABLE)
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 64 * 1024))
            output = stream.read(64 * 1024).decode('utf-8', errors='replace')
        return dict(entry, output=output, output_cut=size > 64 * 1024)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(_UNAVAILABLE) from exc
