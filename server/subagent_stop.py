"""Stop one Claude subagent or background task without disturbing the main turn.

Shells and monitors stop by signal (task_process), which does not depend on the terminal screen.
Agents run inside the Claude process, so they stop through /tasks. That path reads the screen and
refuses whenever the screen does not match what it expects; it never guesses a row."""
import json
import os
import re
import subprocess
import time

from model_picker import press, visible
import paths
import subagents
import task_process
import terminal_input


class StopError(RuntimeError):
    """The stop failed; the message is safe to show the user."""


def has_action(screen, action):
    """Match a whole footer action, including when its text wraps across lines."""
    label = r'\s+'.join(re.escape(word) for word in action.split())
    return re.search(rf'(?:^|[·\n])\s*{label}\s*(?:[·\n]|$)',
                     '\n'.join(screen.splitlines()[-14:])) is not None


def list_navigation(screen):
    """Recognize the list navigation action in Claude Code 2.1.325 and 2.1.327."""
    return has_action(screen, '↑/↓ to select') or has_action(screen, '↑/↓ to navigate')


def detail_open(screen):
    return (has_action(screen, 'Esc/Enter/Space to close')
            or (has_action(screen, 'Enter/Space to close') and has_action(screen, 'Esc to close')))


def dialog_open(screen):
    return (has_action(screen, 'x to stop') or detail_open(screen)
            or list_navigation(screen) and has_action(screen, 'Esc to close'))


def close(entry, run, sleep):
    """Esc is safe only while the tasks dialog's hint remains visible."""
    for _ in range(3):
        if not dialog_open(visible(entry, run)):
            return
        press(entry, 'Escape', run=run)
        sleep(0.35)
    if dialog_open(visible(entry, run)):
        raise StopError('The tasks dialog did not close. Check the terminal view.')


def matches(description, shown):
    return (description == shown or shown.endswith('…') and bool(shown[:-1].strip())
            and description.startswith(shown[:-1].rstrip()))


SECTIONS = {'shell': 'Shells', 'agent': 'Local agents', 'monitor': 'Monitors'}
AGENT_SECTIONS = ('Local agents', 'Background agents')
ROW_STATUS = r'(?:running|completed|failed|stopped|killed)'


def task_section(section):
    return any(word in section.lower() for word in ('shell', 'monitor'))


def rows(text):
    """Read selectable rows across all sections in screen order.

    With one kind of task, Claude Code 2.1.325 shows only a summary line ("2 active shells") and no section header."""
    lines = text.splitlines()
    title = next((index for index in reversed(range(len(lines))) if lines[index].strip() == 'Background'), None)
    if title is None:
        return []
    found, section = [], None
    for line in lines[title + 1:]:
        if list_navigation(line):
            break
        summary = re.findall(r'\d+ active (shell|agent|monitor)s?\b', line)
        if summary and re.fullmatch(r'\s*\d+ active \w+(?:\s*·\s*\d+ active \w+)*\s*', line):
            section = SECTIONS[summary[0]] if len(summary) == 1 else None
            continue
        header = re.fullmatch(r'\s*(.+?) \(\d+\)\s*', line)
        if header:
            section = header[1]
            continue
        if line.strip() in ('Shells', 'Monitors', *AGENT_SECTIONS):
            section = line.strip()
            continue
        if section is None:
            continue
        match = re.fullmatch(r'\s*(❯)?\s*●\s*(.+?)\s*', line)
        if not match:
            continue
        is_task = task_section(section)
        if is_task:
            # Prefer the padded status column so words in the command do not look like status.
            status = (re.search(rf'\s{{2,}}{ROW_STATUS}\b(.*)$', match[2])
                      or re.search(rf'\s+{ROW_STATUS}\b(.*)$', match[2]))
        else:
            status = re.search(rf'\s+{ROW_STATUS}(?:\s*·\s*.*)?$', match[2])
        label = match[2][:status.start()].rstrip() if status else match[2]
        row = {'label': label, 'selected': bool(match[1]), 'section': section}
        # A session task ends at the status. Keep other rows in cursor order.
        if is_task and (not status or status[1]):
            row['unverified_task'] = True
        found.append(row)
    return found


def seconds(text):
    """'1m 43s' -> 103; '2h 05m' -> 7500; None when there is no duration."""
    parts = re.findall(r'(\d+)\s*([hms])\b', text)
    if not parts:
        return None
    return sum(int(value) * {'h': 3600, 'm': 60, 's': 1}[unit] for value, unit in parts)


def _norm(text):
    return ' '.join((text or '').split())


def detail_facts(screen, target):
    """Read an open detail view: (identifies the target by name, elapsed seconds, rounding, body text)."""
    lines = [line.strip() for line in screen.splitlines()]
    labels = target['labels']
    if target['task']:
        # Shell details show "Command:"; monitor details show "Script:" (Claude Code 2.1.325).
        command = next((found[1] for found in (re.fullmatch(r'(?:Command|Script):\s+(.+)', line) for line in lines)
                        if found), None)
        runtime = next((found[1] for found in (re.fullmatch(r'Runtime:\s+(.+)', line) for line in lines) if found), None)
        if command:
            headers = [command.strip()]
        else:
            # Older detail views put the command or description on the first header line.
            first = next((line for line in lines if line), '')
            headers = [first, first.split(' › ', 1)[-1]]
        named = any(matches(label, header) for label in labels for header in headers)
        return named, runtime, command
    headers, shown, body, body_start = [], None, None, 0
    kind = target['kind']
    for index, line in enumerate(lines):
        found = re.fullmatch(rf'{re.escape(kind)} › (.+)', line)
        if found:
            headers.append(found[1].strip())
            body_start = index + 1
            if index + 1 < len(lines) and '·' in lines[index + 1]:
                shown = lines[index + 1].split('·', 1)[0].strip()
        elif index and lines[index - 1] and line.startswith(f'{kind} · '):
            # In 2.1.327 the name is above a metadata line that starts with the agent type.
            elapsed = line.split(' · ', 2)[1]
            if re.fullmatch(r'\d+[hms](?:\s+\d+[hms])*', elapsed):
                headers.append(lines[index - 1])
                shown = elapsed
                body_start = index + 1
    detail_lines = lines[body_start:]
    if 'Prompt' in detail_lines:
        body = []
        for line in detail_lines[detail_lines.index('Prompt') + 1:]:
            if 'to close' in line or line in ('Progress', 'Result', 'Response'):
                break
            body.append(line)
    # Progress lines also start with "›", so exactly one header must name the agent.
    named = len(headers) == 1 and any(matches(label, headers[0]) for label in labels)
    return named, shown, None if body is None else ' '.join(body).strip()


def fits(screen, target, now):
    """True when the open detail view shows this exact agent or task, not only its name.

    A fact the view does not show is skipped; same-name rows with no telling fact all fit, and the caller refuses."""
    named, shown, body = detail_facts(screen, target)
    if not named:
        return False
    if target.get('started') is not None and shown is not None:
        elapsed = seconds(shown)
        if elapsed is None:
            return False
        # Without a seconds part the view rounds to minutes.
        slack = 5 if re.search(r'\d+\s*s\b', shown) else 90
        if abs((now - target['started']) - elapsed) > slack:
            return False
    expected = _norm(target.get('body'))
    if expected and body is not None:
        body = _norm(body)
        if not body:
            return False
        cut = body.endswith('…')
        body = body.rstrip('…').rstrip()
        if not (expected == body or expected.startswith(body) and (cut or len(body) >= 20)):
            return False
    return True


def in_section(target, section):
    if not target['task']:
        return section in AGENT_SECTIONS
    return task_section(section)


def matches_row(target, row):
    return in_section(target, row['section']) and any(matches(label, row['label']) for label in target['labels'])


def is_target(target, row):
    return not row.get('unverified_task') and matches_row(target, row)


def open_task_detail(entry, target, run, sleep, clock=time.time, observe_list=None):
    """Open /tasks and return the target's verified detail. Leave the dialog open.

    target: task (bool), labels (names /tasks may show), kind (agent type), started (epoch or None),
    body (prompt or command text that the detail view shows). observe_list, if given, sees the
    list and its parsed rows before any row navigation."""
    noun = 'task' if target['task'] else 'agent'

    def wait_for(ready, tries=14):
        for _ in range(tries):
            sleep(0.3)
            screen = visible(entry, run)
            if ready(screen):
                return screen
        raise StopError('The tasks dialog changed. Check the terminal view.')

    def in_list(screen):
        return (not detail_open(screen) and list_navigation(screen)
                and has_action(screen, 'Enter to view') and has_action(screen, 'x to stop'))

    press(entry, '-l', '/tasks', run=run)
    sleep(0.7)
    screen = visible(entry, run)
    if terminal_input.approval(screen) or dialog_open(screen):
        raise StopError('The terminal changed. Check the terminal view.')
    press(entry, 'Enter', run=run)
    try:
        screen = wait_for(lambda screen: has_action(screen, 'x to stop'))
    except StopError:
        raise StopError('The tasks dialog did not open. Check the terminal view.') from None
    if detail_open(screen):
        # With one task, /tasks can open its detail view at once.
        if fits(screen, target, clock()):
            return screen
        if not has_action(screen, '← to go back'):
            raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
        press(entry, 'Left', run=run)
        screen = wait_for(in_list)

    def read_list(screen):
        if not in_list(screen):
            raise StopError('Could not read the tasks dialog. Check the terminal view.')
        listed = rows(screen)
        if any(row.get('unverified_task') and matches_row(target, row) for row in listed):
            raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
        if observe_list is not None:
            observe_list(screen, listed)
        return listed

    def select(index, screen):
        for step in range(41):
            listed = read_list(screen)
            if index >= len(listed) or not is_target(target, listed[index]):
                raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
            selected = [position for position, row in enumerate(listed) if row['selected']]
            if len(selected) != 1:
                raise StopError(f'Could not see which {noun} is selected. Check the terminal view.')
            if selected[0] == index:
                return screen
            if step == 40:
                break
            press(entry, 'Down' if index > selected[0] else 'Up', run=run)
            sleep(0.18)
            screen = visible(entry, run)
        raise StopError(f'Could not reach that {noun}. Stop it in the terminal.')

    def view(index, screen):
        """Open the row's detail view; Enter is pressed only on a verified candidate row."""
        select(index, screen)
        press(entry, 'Enter', run=run)
        return wait_for(detail_open)

    listed = read_list(screen)
    candidates = [index for index, row in enumerate(listed) if is_target(target, row)]
    if not candidates:
        raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
    index = candidates[0]
    if len(candidates) > 1:
        # Same name: open each one and compare its run time and prompt or command.
        fitting = []
        for index in candidates:
            detail = view(index, screen)
            if fits(detail, target, clock()):
                fitting.append(index)
            if not has_action(detail, '← to go back'):
                raise StopError('The tasks dialog changed. Check the terminal view.')
            press(entry, 'Left', run=run)
            screen = wait_for(in_list)
        if len(fitting) != 1:
            raise StopError(f'{len(candidates)} {noun}s share this name and could not be told apart. Stop it in the terminal.'
                            if fitting else f'Could not identify that {noun}. Stop it in the terminal.')
        index = fitting[0]
    # A sole row is checked too: its name can match while its run time or command does not.
    detail = view(index, screen)
    if not has_action(detail, 'x to stop'):
        raise StopError('The tasks dialog changed. Check the terminal view.')
    if not fits(detail, target, clock()):
        raise StopError(f'Could not identify that {noun}. Stop it in the terminal.' if len(candidates) == 1
                        else 'The tasks dialog changed. Check the terminal view.')
    return detail


def stop_selected(entry, target, run, sleep, clock=time.time, pressed=None, observe_list=None):
    """Press x only from the target's verified detail view."""
    open_task_detail(entry, target, run, sleep, clock, observe_list=observe_list)
    if pressed is not None:
        pressed['x'] = True
    press(entry, 'x', run=run)
    sleep(0.18)
    visible(entry, run)


# The newest Claude Code version whose /tasks screens the stop tests and a live run were checked on.
CHECKED_TASKS_UI = '2.1.325'
SESSIONS = paths.CLAUDE / 'sessions'


def claude_version(entry):
    try:
        data = json.loads((SESSIONS / f'{int(entry["owner_pid"])}.json').read_text())
        version = data.get('version')
        return version if isinstance(version, str) and re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,40}', version) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def with_version(entry, message):
    """Name a Claude Code update as the likely cause when /tasks could not be read."""
    version = claude_version(entry)
    if version is None or version == CHECKED_TASKS_UI or not re.search(r'Could not (identify|read|see|reach)|dialog', message):
        return message
    return f'{message} This tab runs Claude Code {version}; Tether checked Stop on {CHECKED_TASKS_UI}.'


def bind(entry, task):
    """Name a shell's process before any terminal work: (Process, ledger key) or None.

    Only a shell is bound: its open tasks/<id>.output file names it exactly. A monitor has no such
    marker. Its command and start time can match a same-command twin that starts later, so a
    monitor stops through /tasks only. The signal fallback uses only this binding and never looks
    again, because by then the task may have ended."""
    if subagents._owner_since(entry) is None:
        return None
    tasks_dir = subagents._tasks_dir(entry)
    if tasks_dir is None:
        return None
    output = tasks_dir / f'{task["id"]}.output'
    if task.get('kind') == 'monitor' or not output.exists():
        return None
    process = task_process.find(entry['owner_pid'], task, os.path.realpath(output), None)
    return None if process is None else (process, task_process.key(tasks_dir, task['id']))


def stop_process(bound):
    """Stop the bound process by signal. False when there is none; True when it is gone."""
    if bound is None:
        return False
    process, key = bound
    if not task_process.same(process):
        return True  # It ended on its own; confirm reads the state Claude recorded.
    task_process.record(key)
    if not task_process.stop(process):
        raise StopError('The task did not stop. Check the terminal view.')
    return True


def confirm(entry, agent_id, is_task, sleep):
    noun, collection = ('task', 'tasks') if is_task else ('agent', 'agents')
    for attempt in range(21):
        agent = next((item for item in subagents.listing(entry)[collection] if item['id'] == agent_id), None)
        if agent is None:
            # Earlier-chat rows leave the public listing as soon as they stop.
            confirmed = subagents._listing(entry, include_finished=True)
            agent = next((item for item in confirmed[collection] if item['id'] == agent_id), None)
        if agent is not None and agent['state'] in (('stopped', 'finished', 'failed') if is_task else ('stopped',)):
            return {'ok': True, 'state': agent['state']}
        if attempt < 20:
            sleep(0.25)
    raise StopError(f'The {noun} did not stop. Check the terminal view.')


def task_target(entry, item, is_task):
    """Build the screen identity from the task or agent's live listing."""
    command = item.get('command') if is_task else None
    first = command.strip().splitlines()[0] if isinstance(command, str) and command.strip() else None
    labels = [value for value in (item.get('description'), first) if isinstance(value, str) and value.strip()]
    if not labels:
        noun = 'task' if is_task else 'agent'
        raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
    body = first or ''
    if not is_task:
        try:
            body = subagents.detail(entry, item['id']).get('prompt') or ''
        except (OSError, ValueError, RuntimeError):
            pass
    return {'task': is_task, 'labels': labels, 'kind': item.get('type') or 'general-purpose',
            'started': subagents._stamp(item.get('started')), 'body': body}


def stop(entry, agent_id, run=subprocess.run, sleep=time.sleep, clock=time.time, *, allow_process_fallback=True,
         observe_list=None):
    noun = 'agent'
    try:
        if entry.get('agent') != 'claude':
            raise StopError('Only Claude sessions support stopping subagents.')
        listed = subagents.listing(entry)
        agent = next((item for item in listed['agents'] if item['id'] == agent_id), None)
        is_task = False
        if agent is None:
            agent = next((item for item in listed.get('tasks', []) if item['id'] == agent_id), None)
            is_task = agent is not None
        if is_task:
            noun = 'task'
        elif agent is None:
            subagents._path(entry, agent_id)  # Preserve validation and unavailable errors.
        if agent is None or agent['state'] != 'running':
            raise StopError(f'That {noun} is not running.')
        target = task_target(entry, agent, is_task)

        pressed = {}
        bound = bind(entry, agent) if is_task and allow_process_fallback else None

        def by_terminal():
            terminal_input.verify(entry, run)
            # Do not drive an unverified pane, including a pane for another agent.
            close(entry, run, sleep)
            try:
                if terminal_input.live_status(entry, run)['state'] not in ('Idle', 'Working'):
                    raise StopError(f'Claude is waiting for you. Answer its prompt before stopping a {noun}.')
                if any(terminal_input.prompt_draft(entry, run)):
                    raise StopError(f'There is an unsent draft in the {noun} input. Clear it, then try again.')
                stop_selected(entry, target, run, sleep, clock, pressed, observe_list=observe_list)
            finally:
                close(entry, run, sleep)
            return confirm(entry, agent_id, is_task, sleep)

        # Tasks: /tasks first, so Claude records the task as stopped by the user. A signal makes
        # Claude report a failed command, and its agent may then debug or rerun it. For a shell the
        # signal is the fallback: it does not read the screen, so it still works when a Claude Code
        # update changes /tasks. Agents and monitors have no exact process marker, so they stop
        # through /tasks only. After x, Claude may already be stopping the task, so a failure after
        # x never falls back to a signal.
        try:
            return by_terminal()
        except StopError as exc:
            if is_task and not pressed and stop_process(bound):
                return confirm(entry, agent_id, True, sleep)
            raise StopError(with_version(entry, str(exc))) from exc
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            if is_task and not pressed and stop_process(bound):
                return confirm(entry, agent_id, True, sleep)
            raise
    except StopError:
        raise
    except (ValueError, RuntimeError) as exc:
        raise StopError(str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise StopError(f'Could not stop the {noun}. Check the terminal view.') from exc
