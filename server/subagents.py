"""Read Claude agents and background tasks for the selected process."""
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from html import unescape
from pathlib import Path
import re
import tempfile
import threading
import time

from antigravity import process_start
import procinfo
import paths
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
    if config.get('agent') != 'claude':
        return None
    if since is _MISSING:
        since = _owner_since(config)
    if since is None:
        return None
    key = (config['owner_pid'], config['process_start'])
    now = time.monotonic()
    with _lock:
        cached = _tasks_cache.get(key)
        if cached and now - cached[0] < 30:
            return cached[1]
    found = None
    try:
        root = TASKS_TMP.resolve()
        if root.stat().st_uid != os.getuid():
            return None
        candidates = []
        try:
            candidates = list(_fd_links(config['owner_pid']))
        except (OSError, ValueError):
            pass
        if _uuid(config.get('thread')):
            candidates.extend(root.glob(f'*/{config["thread"]}/tasks'))
        for candidate in candidates:
            candidate = Path(candidate)
            resolved = candidate.resolve()
            if (candidate.name == 'tasks' and resolved.name == 'tasks' and _uuid(resolved.parent.name)
                    and resolved.parent.parent.parent == root and resolved.is_dir()):
                found = resolved
                break
    except (OSError, ValueError, RuntimeError):
        pass
    with _lock:
        _tasks_cache[key] = (now, found)
        _tasks_cache.move_to_end(key)
        while len(_tasks_cache) > CACHE_LIMIT:
            _tasks_cache.popitem(last=False)
    return found


def _agent_files(config, tasks_dir):
    """Return authorized files by id and the ids present in the current chat."""
    files, current = {}, set()
    folder = _folder(config)
    if folder is None:
        return files, current
    for path in folder.glob('agent-*.jsonl'):
        identifier = path.stem[len('agent-'):]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
            continue
        try:
            resolved = path.resolve()
            if resolved.is_relative_to(folder) and resolved.is_file():
                files[identifier] = {resolved}
                current.add(identifier)
        except (OSError, ValueError, RuntimeError):
            continue
    if tasks_dir is not None:
        # Current-chat agents can also span /clear without an output symlink.
        for identifier in current:
            earlier = folder.parent.parent / tasks_dir.parent.name / 'subagents' / f'agent-{identifier}.jsonl'
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
                for extra in (projects / relative.parts[0]).glob(f'*/subagents/{target.name}'):
                    resolved = extra.resolve()
                    if _uuid(extra.parent.parent.name) and resolved.parent == extra.parent and resolved.is_file():
                        group.add(resolved)
                if group:
                    files.setdefault(identifier, set()).update(group)
            except (OSError, ValueError, RuntimeError):
                continue
    return {identifier: list(group) for identifier, group in files.items()}, current


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


def _entry(agent_id, files, now, notifications, since):
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
                                'command': command[:2000] if isinstance(command, str) else None,
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


def _tasks(config, tasks_dir, files, shown, now, since, *, include_finished=False):
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
            sources[path] = (identifier, identifier in shown)
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
    result = []
    for candidate in tasks_dir.glob('*.output'):
        try:
            path = _task_path(tasks_dir, candidate.stem)
            info = path.stat()
            (state, footer), _ = _cached(path, _footer)
            status, summary = notifications.get(candidate.stem, (None, None))
            if state is None:
                state = {'completed': 'finished', 'failed': 'failed', 'killed': 'stopped'}.get(status, 'running')
            start = starts.get(candidate.stem, {})
            if not start:
                # Claude makes an output file for every Bash call, so a foreground command can move to
                # the background. Only a start record (backgroundTaskId/taskId) makes it a background task.
                continue
            if state != 'running' and not start.get('_scope') and not include_finished:
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
                           'parent': start.get('parent'), 'summary': (footer or summary) if state in ('failed', 'stopped') else None})
        except (OSError, ValueError, RuntimeError):
            continue
    result.sort(key=lambda item: _stamp(item['started']) or 0)
    return result[-40:]


def _listing(config, *, limit=True, include_finished=False):
    result = {'main': {'model': None, 'context': None, 'title': None}, 'agents': [], 'tasks': []}
    since = _owner_since(config) if config.get('agent') == 'claude' else None
    tasks_dir = _tasks_dir(config, since=since)
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
    owners, spawned = {}, {}
    for identifier, group in files.items():
        try:
            entry, data, meta = _entry(identifier, group, now, notifications, since)
        except (OSError, ValueError, UnicodeError, RecursionError):
            continue
        if identifier not in current and entry['state'] != 'running' and not include_finished:
            continue
        result['agents'].append(entry)
        for tool_id in data['tool_ids']:
            owners[tool_id] = entry['id']
        if isinstance(meta.get('toolUseId'), str):
            spawned[entry['id']] = meta['toolUseId']
    for entry in result['agents']:
        parent = owners.get(spawned.get(entry['id']))
        entry['parent'] = parent if parent != entry['id'] else None
    result['agents'].sort(key=lambda entry: (_stamp(entry['started']) is not None, _stamp(entry['started']) or 0))
    if limit:
        result['agents'] = result['agents'][-40:]
    shown = {item['id'] for item in result['agents']}
    sources = {identifier: group for identifier, group in files.items() if identifier in current or identifier in shown}
    result['tasks'] = _tasks(config, tasks_dir, sources, shown, now, since, include_finished=include_finished)
    return result


def listing(config):
    return _listing(config)


def detail(config, agent_id):
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
