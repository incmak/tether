"""Stop one Claude subagent or background task without disturbing the main turn.

Shells and monitors stop by signal (task_process), which does not depend on the terminal screen.
Agents run inside the Claude process, so they stop through /tasks. That path reads the screen and
refuses whenever the screen does not match what it expects; it never guesses a row."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

from model_picker import press, visible
import subagents
import task_process
import terminal_input


class StopError(RuntimeError):
    """The stop failed; the message is safe to show the user."""


def hint(text):
    return ' '.join(line.strip() for line in text.splitlines()[-14:])


def dialog_open(text):
    shown = hint(text)
    return ('x to stop' in shown or 'Esc/Enter/Space to close' in shown
            or '↑/↓ to select' in shown and 'Esc to close' in shown)


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


def rows(text):
    """Read selectable rows across all sections in screen order.

    With one kind of task, Claude Code 2.1.325 shows only a summary line ("2 active shells") and no section header."""
    found, section = [], None
    for line in text.splitlines():
        if '↑/↓ to select' in line:
            break
        summary = re.findall(r'\d+ active (shell|agent|monitor)s?\b', line)
        if summary and re.fullmatch(r'\s*\d+ active \w+(?:\s*·\s*\d+ active \w+)*\s*', line):
            section = SECTIONS[summary[0]] if len(summary) == 1 else None
            continue
        header = re.fullmatch(r'\s*(.+?) \(\d+\)\s*', line)
        if header:
            section = header[1]
            continue
        if section is None:
            continue
        match = re.fullmatch(r'\s*(❯)?\s*●\s*(.+?)\s*', line)
        if not match:
            continue
        label = re.sub(r'\s+(?:running|completed|failed|stopped|killed)(?:\s*·.*)?$', '', match[2]).rstrip()
        found.append({'label': label, 'selected': bool(match[1]), 'section': section})
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
    headers, shown, body = [], None, None
    for index, line in enumerate(lines):
        found = re.fullmatch(rf'{re.escape(target["kind"])} › (.+)', line)
        if found:
            headers.append(found[1].strip())
            if index + 1 < len(lines) and '·' in lines[index + 1]:
                shown = lines[index + 1].split('·', 1)[0].strip()
    if 'Prompt' in lines:
        body = []
        for line in lines[lines.index('Prompt') + 1:]:
            if 'to close' in line or line in ('Progress', 'Result', 'Response'):
                break
            body.append(line)
    # Progress lines also start with "›", so exactly one header must name the agent.
    named = len(headers) == 1 and any(matches(label, headers[0]) for label in labels)
    return named, shown, None if body is None else ' '.join(body)


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
        return section == 'Local agents'
    return section in ('Shells', 'Monitors') or any(word in section.lower() for word in ('shell', 'monitor'))


def stop_selected(entry, target, run, sleep, clock=time.time, pressed=None):
    """Open /tasks, find the target, and press x on it from its verified detail view.

    target: task (bool), labels (names /tasks may show), kind (agent type), started (epoch or None),
    body (prompt or command text that the detail view shows). pressed: a dict that gets
    pressed['x'] = True just before x is sent, so the caller knows Claude may already act on it."""
    noun = 'task' if target['task'] else 'agent'
    pressed = {} if pressed is None else pressed

    def press_x():
        pressed['x'] = True
        press(entry, 'x', run=run)
        sleep(0.18)
        visible(entry, run)

    def wait_for(text, tries=14):
        for _ in range(tries):
            sleep(0.3)
            screen = visible(entry, run)
            if text in hint(screen):
                return screen
        raise StopError('The tasks dialog changed. Check the terminal view.')

    def in_detail(screen):
        return 'Esc/Enter/Space to close' in hint(screen)

    press(entry, '-l', '/tasks', run=run)
    sleep(0.7)
    screen = visible(entry, run)
    if terminal_input.approval(screen) or dialog_open(screen):
        raise StopError('The terminal changed. Check the terminal view.')
    press(entry, 'Enter', run=run)
    try:
        screen = wait_for('x to stop')
    except StopError:
        raise StopError('The tasks dialog did not open. Check the terminal view.') from None
    if in_detail(screen):
        # With one task, /tasks can open its detail view at once.
        if fits(screen, target, clock()):
            press_x()
            return
        if '← to go back' not in hint(screen):
            raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
        press(entry, 'Left', run=run)
        screen = wait_for('↑/↓ to select')

    def is_target(row):
        return in_section(target, row['section']) and any(matches(label, row['label']) for label in target['labels'])

    def select(index, screen):
        for step in range(41):
            shown = hint(screen)
            if 'x to stop' not in shown or in_detail(screen):
                raise StopError('The tasks dialog changed. Check the terminal view.')
            if '↑/↓ to select' not in shown:
                raise StopError('Could not read the tasks dialog. Check the terminal view.')
            listed = rows(screen)
            if index >= len(listed) or not is_target(listed[index]):
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
        return wait_for('Esc/Enter/Space to close')

    if '↑/↓ to select' not in hint(screen):
        raise StopError('Could not read the tasks dialog. Check the terminal view.')
    listed = rows(screen)
    candidates = [index for index, row in enumerate(listed) if is_target(row)]
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
            press(entry, 'Left', run=run)
            screen = wait_for('↑/↓ to select')
        if len(fitting) != 1:
            raise StopError(f'{len(candidates)} {noun}s share this name and could not be told apart. Stop it in the terminal.'
                            if fitting else f'Could not identify that {noun}. Stop it in the terminal.')
        index = fitting[0]
    # A sole row is checked too: its name can match while its run time or command does not.
    detail = view(index, screen)
    if not fits(detail, target, clock()):
        raise StopError(f'Could not identify that {noun}. Stop it in the terminal.' if len(candidates) == 1
                        else 'The tasks dialog changed. Check the terminal view.')
    press_x()


# The newest Claude Code version whose /tasks screens the stop tests and a live run were checked on.
CHECKED_TASKS_UI = '2.1.325'
SESSIONS = Path.home() / '.claude' / 'sessions'


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


def bind(entry, task, tasks):
    """Name a shell or monitor's process before any terminal work: (Process, ledger key) or None.

    The signal fallback uses only this process. It never looks again later, because by then the task
    may have ended and a same-command twin could be the sole match. tasks: every task in the listing."""
    if subagents._owner_since(entry) is None:
        return None
    tasks_dir = subagents._tasks_dir(entry)
    if tasks_dir is None:
        return None
    output = tasks_dir / f'{task["id"]}.output'
    output = os.path.realpath(output) if output.exists() else None
    started = subagents._stamp(task.get('started'))
    if output is None and started is not None:
        # A monitor is matched by command and start time; a twin in the same window makes that ambiguous.
        window = task_process.START_BEFORE + task_process.START_AFTER
        for other in tasks:
            begun = subagents._stamp(other.get('started'))
            if (other.get('id') != task['id'] and other.get('command') == task.get('command')
                    and begun is not None and abs(begun - started) <= window):
                return None
    process = task_process.find(entry['owner_pid'], task, output, started)
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


def stop(entry, agent_id, run=subprocess.run, sleep=time.sleep, clock=time.time):
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
        description = agent.get('description')
        command = agent.get('command') if is_task else None
        first = command.strip().splitlines()[0] if isinstance(command, str) and command.strip() else None
        labels = [value for value in ((description, first) if is_task else (description,))
                  if isinstance(value, str) and value.strip()]
        if not labels:
            raise StopError(f'Could not identify that {noun}. Stop it in the terminal.')
        if is_task:
            body = first or ''
        else:
            try:
                body = subagents.detail(entry, agent_id).get('prompt') or ''
            except (OSError, ValueError, RuntimeError):
                body = ''
        target = {'task': is_task, 'labels': labels, 'kind': agent.get('type') or 'general-purpose',
                  'started': subagents._stamp(agent.get('started')), 'body': body}

        pressed = {}
        bound = bind(entry, agent, subagents._listing(entry, include_finished=True)['tasks']) if is_task else None

        def by_terminal():
            terminal_input.verify(entry, run)
            # Do not drive an unverified pane, including a pane for another agent.
            close(entry, run, sleep)
            try:
                if terminal_input.live_status(entry, run)['state'] not in ('Idle', 'Working'):
                    raise StopError(f'Claude is waiting for you. Answer its prompt before stopping a {noun}.')
                if any(terminal_input.prompt_draft(entry, run)):
                    raise StopError(f'There is an unsent draft in the {noun} input. Clear it, then try again.')
                stop_selected(entry, target, run, sleep, clock, pressed)
            finally:
                close(entry, run, sleep)
            return confirm(entry, agent_id, is_task, sleep)

        # Tasks: /tasks first, so Claude records the task as stopped by the user. A signal makes
        # Claude report a failed command, and its agent may then debug or rerun it. The signal is
        # the fallback: it does not read the screen, so it still works when a Claude Code update
        # changes /tasks. Agents have no process of their own, so they stop through /tasks only.
        # After x, Claude may already be stopping this task, and its process may be gone. A signal
        # then could reach a same-command twin, so a failure after x never falls back to it.
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
