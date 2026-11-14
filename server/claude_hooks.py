"""Quiet, fail-open agent hook capture and read-only live state."""
import json
import os
from pathlib import Path
import re
import sys
import time


EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PostToolUseFailure', 'PermissionRequest', 'PermissionDenied',
          'Notification', 'Stop', 'StopFailure', 'SubagentStart', 'SubagentStop',
          'SessionEnd', 'PostModelSwitch')
CODEX_EVENTS = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse',
                'PostToolUse', 'PermissionRequest', 'Stop', 'Interrupt')
MODES = {'default': '', 'plan': '⏸ plan mode on', 'acceptEdits': '⏵⏵ accept edits on',
         'auto': '⏵⏵ auto mode on', 'dontAsk': 'dontAsk',
         'bypassPermissions': '⏵⏵ bypass permissions on'}
# Slow thinking/replies can take many minutes; bound missed turn events at 30m.
TURN_MAX_AGE = 1800


def valid_session(session):
    return isinstance(session, str) and re.fullmatch(r'[a-zA-Z0-9-]+', session)


def short(value):
    return ' '.join(value.split())[:160] if isinstance(value, str) else ''


def update_tasks(state, data, now):
    """Parent-scoped snapshots replace provisional Bash starts, including []."""
    if now < max(state.get('tasks_at', 0), state.get('reported_at', 0)):
        return
    event = data['hook_event_name']
    if event in ('Stop', 'StopFailure', 'SubagentStop'):
        tasks = data.get('background_tasks')
        if isinstance(tasks, list):
            previous = list(state.get('background_tasks', {}).values())
            captured = {}
            for item in tasks:
                if not isinstance(item, dict) or not short(item.get('id')):
                    continue
                task = {key: item[key][:1000] for key in
                        ('id', 'type', 'status', 'description', 'command', 'agent_type', 'name', 'server', 'tool')
                        if isinstance(item.get(key), str) and item[key]}
                old = next((old for old in previous if old['id'] == task['id']), None)
                if old is None and task.get('type') == 'shell' and task.get('command'):
                    old = next((old for old in previous if old['id'].startswith('pending:')
                                and old.get('command') == task['command']), None)
                task['started'] = old['started'] if old else now
                if old:
                    previous.remove(old)
                captured[task['id']] = task
            state['background_tasks'] = captured
            state['tasks_at'] = now
        crons = data.get('session_crons')
        if isinstance(crons, list):
            state['session_crons'] = [
                {key: item[key][:1000] for key in ('id', 'schedule', 'prompt')
                 if isinstance(item.get(key), str) and item[key]}
                | ({'recurring': item['recurring']} if isinstance(item.get('recurring'), bool) else {})
                for item in crons if isinstance(item, dict)]
    elif event == 'PreToolUse' and data.get('tool_name') == 'Bash':
        tool = data.get('tool_input')
        if isinstance(tool, dict) and tool.get('run_in_background') is True:
            identity = short(data.get('tool_use_id')) or str(now)
            task = {'id': 'pending:' + identity, 'type': 'shell', 'status': 'running', 'started': now}
            task.update({key: tool[key][:1000] for key in ('command', 'description')
                         if isinstance(tool.get(key), str) and tool[key]})
            state.setdefault('background_tasks', {}).setdefault(task['id'], task)
            state['tasks_at'] = now


def task_labels(tasks):
    counts = {}
    for task in tasks:
        kind = {'subagent': 'agent', 'teammate': 'agent'}.get(task.get('type'), task.get('type') or 'task')
        counts[kind] = counts.get(kind, 0) + 1
    return [f'{count} {kind if count == 1 else kind + "s"}'
            for kind, count in counts.items()]


def update(state, data, now):
    event, agent = data['hook_event_name'], data.get('agent_id')
    update_tasks(state, data, now)
    # Subagent tool context must not replace the main session's mode or effort.
    if not agent and now >= state.get('settings_at', 0):
        if data.get('permission_mode') in MODES:
            state['mode'] = MODES[data['permission_mode']]
        effort = data.get('effort')
        if isinstance(effort, dict) and effort.get('level') in ('low', 'medium', 'high', 'xhigh', 'max'):
            state['effort'] = effort['level']
        state['settings_at'] = now
    if event == 'Notification' and data.get('notification_type') not in ('permission_prompt', 'idle_prompt'):
        return
    # Settings changes cannot revive an old turn.
    if event == 'PostModelSwitch':
        return
    # Removing a unique agent id is independent of main-turn ordering.
    if event == 'SubagentStop':
        state.setdefault('agents', {}).pop(agent, None)
        return
    if now < state.get('reported_at', 0):
        return
    agents = state.setdefault('agents', {})
    if event == 'SubagentStart' and isinstance(agent, str):
        agents[agent] = {'name': short(data.get('agent_type')) or agent, 'started': now}
    elif agent:
        return  # subagent completions must not end the parent's turn
    else:
        state['reported_at'] = now
    if agent or event in ('SubagentStart', 'SubagentStop'):
        return
    if event in ('UserPromptSubmit', 'SessionStart', 'SessionEnd'):
        state.pop('denied', None)
    elif event == 'PermissionDenied':
        tool = data.get('tool_input')
        tool = tool if isinstance(tool, dict) else {}
        state['denied'] = {
            'tool': short(data.get('tool_name')),
            'description': short(tool.get('description')),
            'command': short(tool.get('command') or tool.get('url') or tool.get('file_path') or ''),
            'detail': next((short(tool.get(key)) for key in
                            ('command', 'file_path', 'pattern', 'query', 'description') if short(tool.get(key))), ''),
            'reason': short(data.get('reason')), 'at': now}
    if state.get('agent') == 'codex':
        if event in ('PostToolUse', 'Stop', 'Interrupt', 'UserPromptSubmit', 'SessionStart', 'SessionEnd') or (
                event == 'PreToolUse' and short(data.get('tool_use_id')) != state.get('approval_tool_use_id')):
            state.pop('approval', None)
            state.pop('approval_tool_use_id', None)
        if event == 'PermissionRequest':
            tool = data.get('tool_input')
            tool = tool if isinstance(tool, dict) else {}
            state['approval'] = {'tool': short(data.get('tool_name')),
                                 'text': short(tool.get('description')) or short(tool.get('command')), 'at': now}
            state['approval_tool_use_id'] = short(data.get('tool_use_id')) or next(reversed(state.get('tools', {})), '')
    if event == 'SessionStart':
        state.update(ended=False, busy=False, waiting=False, tools={}, agents={})
        for key in ('background_tasks', 'session_crons', 'tasks_at'):
            state.pop(key, None)
    elif event == 'SessionEnd':
        state.update(ended=True, busy=False, waiting=False, tools={}, agents={})
    elif event == 'UserPromptSubmit':
        state.update(ended=False, busy=True, waiting=False, started=now, prompt_at=now, tools={})
    elif event in ('Stop', 'StopFailure', 'Interrupt') or (event == 'Notification' and data.get('notification_type') == 'idle_prompt'):
        state.update(busy=False, waiting=False, tools={})
    elif event == 'PermissionRequest' or (event == 'Notification' and data.get('notification_type') == 'permission_prompt'):
        state['waiting'] = True
    elif event == 'PreToolUse':
        state.update(busy=True, waiting=False)
        state.setdefault('started', now)
        tool = data.get('tool_input')
        tool = tool if isinstance(tool, dict) else {}
        state.setdefault('tools', {})[short(data.get('tool_use_id')) or short(data.get('tool_name'))] = {
            'title': short(data.get('tool_name')),
            **({'description': short(tool.get('description'))} if state.get('agent') != 'codex' else {}),
            'command': next((short(tool.get(key)) for key in ('command', 'description', 'file_path', 'pattern', 'query') if short(tool.get(key))), ''),
            'started': now}
    elif event in ('PostToolUse', 'PostToolUseFailure', 'PermissionDenied'):
        state['waiting'] = False
        tools = state.setdefault('tools', {})
        if event == 'PermissionDenied' and not data.get('tool_use_id'):
            for key in list(tools):
                if tools[key]['title'] == data.get('tool_name'):
                    tools.pop(key)
        else:
            tools.pop(short(data.get('tool_use_id')) or short(data.get('tool_name')), None)


def capture(data, now=None, agent='claude'):
    if (not isinstance(data, dict) or not valid_session(data.get('session_id'))
            or data.get('hook_event_name') not in (CODEX_EVENTS if agent == 'codex' else EVENTS)):
        return
    if (agent == 'claude' and data['hook_event_name'] == 'StopFailure'
            and data.get('error') in ('rate_limit', 'billing_error', 'account_on_hold')):
        try:
            from quota_notices import record
            text = data.get('last_assistant_message')
            text = text if isinstance(text, str) and text.strip() else 'Usage limit reached.'
            details = data.get('error_details')
            if isinstance(details, str) and details.strip() and details != text:
                text += '\n' + details
            record(agent, data['session_id'], text)
        except Exception:
            pass  # quota storage must not prevent lifecycle capture or fail the hook
    import fcntl  # only the writer needs a process lock
    now = time.time() if now is None else now
    folder = Path(os.environ.get('MOBILE_TMUX_CACHE') or Path.home() / '.cache/mobile-tmux') / 'hooks'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = data['session_id']
    target = folder / (session + '.json')
    # Atomic rename protects readers; a short lock also protects concurrent writers.
    with os.fdopen(os.open(folder / (session + '.lock'), os.O_CREAT | os.O_WRONLY, 0o600), 'w') as lock:
        deadline = time.monotonic() + .1
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(.005)
        try:
            state = json.loads(target.read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict) or state.get('session_id') != session or state.get('agent', agent) != agent:
            state = {'session_id': session}
        state['agent'] = agent
        update(state, data, now)
        temporary = folder / (session + '.' + str(os.getpid()) + '.tmp')
        try:
            with os.fdopen(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def transcript_newer(path, reported):
    """A newer prompt or completed response invalidates a missed lifecycle event.

    Streaming/tool records are normal during a turn. Inspect only a bounded tail;
    transcript writes can lag hooks, so allow one second for the same event.
    """
    from datetime import datetime
    with open(path, 'rb') as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 65536)
        stream.seek(start)
        if start:
            stream.readline()
        for line in stream:
            try:
                record = json.loads(line)
                if record.get('isSidechain') or record.get('isMeta'):
                    continue
                message = record.get('message') or {}
                content = message.get('content')
                prompt = record.get('type') == 'user' and (isinstance(content, str) or
                         isinstance(content, list) and any(part.get('type') == 'text' for part in content if isinstance(part, dict)))
                done = record.get('type') == 'assistant' and message.get('stop_reason') in ('end_turn', 'stop_sequence')
                if (prompt or done) and datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp() > reported + 1:
                    return True
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return False


def elapsed(started, now):
    seconds = max(0, int(now - started))
    return f'{seconds // 60}m {seconds % 60}s' if seconds >= 60 else f'{seconds}s'


def codex_transcript_newer(path, reported):
    """Read at most 256 KiB; only rollout lifecycle records invalidate hooks."""
    from datetime import datetime
    with open(path, 'rb') as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 262144)
        stream.seek(start)
        tail = stream.read(262144)
    if start:
        tail = tail.partition(b'\n')[2]
    for line in tail.splitlines():
        try:
            record = json.loads(line)
            if record.get('type') == 'event_msg' and record['payload']['type'] in (
                    'task_started', 'task_complete', 'turn_aborted'):
                stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
                if stamp > reported + 1:
                    return True
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return False


def codex_live(entry, now=None):
    """Return trusted Codex lifecycle state, or let callers use their fallback."""
    if entry.get('ambiguous') or not valid_session(entry.get('thread')):
        return None
    import math
    import paths
    now = time.time() if now is None else now
    try:
        state = json.loads((paths.CACHE / 'hooks' / (entry['thread'] + '.json')).read_text())
        reported = state['reported_at']
        if (state['session_id'] != entry['thread'] or state.get('agent') != 'codex' or state.get('ended')
                or not math.isfinite(reported) or now < reported):
            return None
        tools = state.get('tools', {})
        started = state.get('started', reported)
        if not math.isfinite(started) or started > now:
            return None
        if any(not math.isfinite(tool['started']) or tool['started'] > now for tool in tools.values()):
            return None
        if state.get('busy') and not tools and now - reported > TURN_MAX_AGE:
            return None
        if entry.get('transcript') and codex_transcript_newer(entry['transcript'], reported):
            return None
        tool = max(tools.values(), key=lambda item: item['started']) if tools else None
        return {'busy': bool(state.get('busy')), 'waiting': bool(state.get('waiting')),
                'approval': state.get('approval'), 'tool': tool, 'started': started}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def install_codex():
    """Add Tether hooks without changing existing groups or hook trust."""
    import shlex
    import stat
    import tempfile
    import paths
    path = paths.CODEX_HOOKS
    try:
        original = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        data = json.loads(original)
    except FileNotFoundError:
        original, mode, data = None, 0o600, {}
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError('Codex hooks must contain a hooks object.')
    hooks = data.setdefault('hooks', {})
    command = shlex.join([sys.executable, '-I', str(Path(__file__).resolve()), 'codex'])
    added = []
    for event in CODEX_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or any(not isinstance(group, dict)
                or not isinstance(group.get('hooks'), list)
                or any(not isinstance(hook, dict) for hook in group['hooks']) for group in groups):
            raise ValueError('Codex hook groups must contain hook lists.')
        if any(hook.get('type') == 'command' and hook.get('command') == command
               for group in groups for hook in group['hooks']):
            continue
        # Codex caps Interrupt hooks at 3 seconds.
        hooks[event] = [*groups, {'hooks': [{'type': 'command', 'command': command, 'timeout': 3 if event == 'Interrupt' else 5}]}]
        added.append(event)
    if not added:
        return added
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write((json.dumps(data, indent=2) + '\n').encode())
        if original is not None:
            try:
                backup_fd = os.open(str(path) + '.before-tether', os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            except FileExistsError:
                pass
            else:
                with os.fdopen(backup_fd, 'wb') as backup:
                    os.fchmod(backup.fileno(), mode)
                    backup.write(original)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return added


def overlay(config, result, now=None, screen_read_at=None):
    """Keep the existing footer schema and screen fields outside hook coverage."""
    if config.get('agent') != 'claude' or config.get('ambiguous') or not valid_session(config.get('thread')):
        return result
    import ask_bridge
    bridge_waiting = ask_bridge.enabled() and ask_bridge.pending.attention(config)
    if bridge_waiting:
        result = dict(result, spinner=None, running=None)
    import paths
    now = time.time() if now is None else now
    try:
        state = json.loads((paths.CACHE / 'hooks' / (config['thread'] + '.json')).read_text())
        reported = state['reported_at']
        age = now - reported
        if state['session_id'] != config['thread'] or state.get('ended') or age < 0:
            return result
        merged = dict(result)
        denied = state.get('denied')
        if (denied and denied['at'] > state.get('prompt_at', 0)
                and 0 <= now - denied['at'] <= TURN_MAX_AGE):
            name = f"{denied['tool']}({denied['detail']})" if denied['detail'] else denied['tool']
            notice = name + ' blocked by auto mode · /permissions'
            if denied['reason']:
                notice += ' · Auto mode: ' + denied['reason']
            notices = list(result.get('notices', []))
            for index, item in enumerate(notices):
                if re.search(r'blocked by auto mode', item, re.I):
                    notices[index] = notice
                    break
            else:
                notices.append(notice)
            merged['notices'] = notices
            merged['blocked'] = {
                'tool': denied['tool'], 'description': denied.get('description', ''),
                'command': denied.get('command', ''),
                'reason': re.sub(r'^Auto mode:\s*', '', denied['reason']).replace('[', '').replace(']', '').strip(),
                'at': denied['at']}
        for key in ('mode', 'effort'):
            if key in state:
                merged[key] = state[key]
        # Shift+Tab has no hook. Only a later, explicitly parsed mode line wins;
        # an empty mode may mean a dialog or a clipped footer, not default mode.
        if ('mode' in state and result.get('mode') and screen_read_at is not None
                and screen_read_at > max(reported, state.get('settings_at', reported))):
            from footer import mode_key
            if mode_key(result['mode']) != mode_key(state['mode']):
                merged['mode'] = result['mode']
        tools = state.get('tools', {})
        lifecycle_fresh = bool(tools) or age <= TURN_MAX_AGE
        if lifecycle_fresh and config.get('transcript') and transcript_newer(config['transcript'], reported):
            lifecycle_fresh = False
        if lifecycle_fresh:
            merged['spinner'] = None
            merged['running'] = None
            if state.get('busy') and not state.get('waiting') and not bridge_waiting:
                merged['spinner'] = result.get('spinner')
                if merged['spinner'] is None:
                    merged['spinner'] = {'verb': 'Working…', 'detail': elapsed(state.get('started', reported), now)}
                if tools:
                    tool = max(tools.values(), key=lambda item: item['started'])
                    merged['running'] = {key: tool[key] for key in ('title', 'command')}
                    merged['running']['title'] = ((result.get('running') or {}).get('title')
                                                  or tool.get('description', '') or tool['title'])
                    merged['running']['elapsed'] = elapsed(tool['started'], now)
        tasks_at = state.get('tasks_at', reported)
        # A Stop list stays true until the next Stop, so a newer transcript does not expire it.
        tasks_fresh = now >= tasks_at and (bool(tools) or now - tasks_at <= TURN_MAX_AGE)
        if tasks_fresh and 'background_tasks' in state:
            tasks = list(state['background_tasks'].values())
            merged['tasks'] = task_labels(tasks)
            merged['task_details'] = [{key: task[key] for key in
                                      ('id', 'type', 'status', 'description', 'command', 'started')
                                      if task.get(key) is not None and task.get(key) != ''} for task in tasks]
        # Screen rows include the main agent and richer task details. They lack
        # hook IDs, so even fresh empty hooks cannot safely remove those rows.
        merged['agents'] = list(result.get('agents', []))
        if any(task.get('type') in ('subagent', 'teammate') for task in merged.get('task_details', [])):
            # The Stop list names each subagent and its task, and the task chip shows it.
            # Screen rows would show the same agents a second time.
            merged['agents'] = [row for row in merged['agents'] if row['main']]
            return merged
        screen_names = [row['name'] for row in merged['agents'] if not row['main']]
        for agent in state.get('agents', {}).values():
            if agent['name'] in screen_names:
                screen_names.remove(agent['name'])
            else:
                merged['agents'].append({'name': agent['name'], 'task': '', 'detail': elapsed(agent['started'], now), 'main': False})
        return merged
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result


def session_start_context(data, agent, env=os.environ):
    """(SessionStart text, {kind: rules} to mark as sent once the text is printed).

    The prompt hooks send their rules once per conversation (prompt_context.py, B74): a new, cleared or resumed
    conversation gets them again on its next prompt. A compacted one gets them here at once, because a compaction can
    happen in the middle of a long turn. They come from the saved records, so this stays fast."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)  # python -I omits the script's folder.
    import prompt_context
    rules = prompt_context.saved(data) if data.get('source') == 'compact' else {}
    prompt_context.forget(data)
    parts = []
    if agent == 'claude' and env.get('TETHER_CDP_URL'):
        try:
            from agent_browser_cli import note
            parts.append(note())
        except Exception:
            pass  # the rules still go out
    parts += rules.values()
    return '\n\n'.join(part for part in parts if part), rules


def main():
    if sys.argv[1:] == ['install-codex']:
        added = install_codex()
        print('Added Codex hooks: ' + ', '.join(added) if added else 'Codex hooks already installed.')
        sys.exit(0)
    try:
        agent = 'codex' if sys.argv[1:] == ['codex'] else 'claude'
        occurred = time.time()
        data = json.load(sys.stdin)
        capture(data, occurred, agent=agent)
        if (agent == 'codex' and isinstance(data, dict) and valid_session(data.get('session_id'))
                and data.get('hook_event_name') == 'UserPromptSubmit'):
            here = str(Path(__file__).resolve().parent)
            if here not in sys.path:
                sys.path.insert(0, here)  # python -I omits the script's folder.
            import autonomy
            print(autonomy.hook_output(data, agent='codex'))
        if (isinstance(data, dict) and valid_session(data.get('session_id'))
                and data.get('hook_event_name') == 'SessionStart'):
            context, rules = session_start_context(data, agent)
            if context:
                print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': context}}),
                      flush=True)
            import prompt_context
            for kind, text in rules.items():
                prompt_context.mark(data, kind, text)  # only after the text is out
    except Exception:
        pass  # hook errors must never print context or fail Claude Code


if __name__ == '__main__':
    main()
