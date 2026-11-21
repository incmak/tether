"""Stop one Claude subagent or background task without disturbing the main turn.

Claude's /tasks dialog stops agents, shells and monitors. Waiting agents appear in the agent bar
instead. Both paths require a verified screen identity. Shells have an exact process fallback."""
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


def close(entry, run, sleep, *, is_open=dialog_open):
    """Esc is safe only while the tasks dialog's hint remains visible. Return whether it was sent."""
    for step in range(3):
        if not is_open(visible(entry, run)):
            return step > 0
        press(entry, 'Escape', run=run)
        sleep(0.35)
    if is_open(visible(entry, run)):
        raise StopError('The tasks dialog did not close. Check the terminal view.')
    return True


def matches(description, shown):
    return (description == shown or shown.endswith('…') and bool(shown[:-1].strip())
            and description.startswith(shown[:-1].rstrip()))


SECTIONS = {'shell': 'Shells', 'agent': 'Local agents', 'monitor': 'Monitors'}
AGENT_SECTIONS = ('Local agents', 'Background agents')
ROW_STATUS = r'(?:running|completed|failed|stopped|killed)'
TASK_COUNT = r'\d+ (?:shells?|monitors?|(?:local |background )?agents?)'


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
        if re.fullmatch(rf'{TASK_COUNT}(?:\s*,\s*{TASK_COUNT})*|\d+ background tasks', line.strip()):
            if 'agent' in line or 'background tasks' in line:
                section = None  # Section headers follow the summary.
            else:
                section = 'Shells' if 'shell' in line else 'Monitors'
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


BAR_READ_ERROR = 'Could not read the agent bar. Check the terminal view.'
BAR_ID_ERROR = 'Could not identify that agent. Stop it in the terminal.'
BAR_CLOSE_ERROR = 'The agent bar did not close. Check the terminal view.'
BAR_NARROW_ERROR = 'The terminal is too narrow to identify that agent. Widen it or stop it in the terminal.'
WAITING_COUNT = r'([1-9]\d*) (shell|monitor)s?'
BAR_ROW = re.compile(r'(❯ )?\s*([●◯])\s+([1-9]\d* idle agents?|\S+)(?:\s{2,}(.+)| (stopped · .+))?')
PROMPT_ACTIONS = {'↓ to manage', '↓ to resume subagent', 'esc to interrupt', '← for agents'}
TASKS_PILL_ACTION = 'enter to view tasks'
ARTIFACT_ACTIONS = {'enter to view artifacts', 'enter to open in browser'}
STOP_ALL_ACTION = 'ctrl+x ctrl+k to stop all agents'
COLLAPSE_ACTION = 'esc to collapse'
ROW_ACTIONS = {'↑/↓ to select', 'enter to view', 'x to stop', 'x to clear', STOP_ALL_ACTION}
# Compare clipped actions with the other controls in Claude's 2.1.330 footer.
FOCUS_ACTIONS = PROMPT_ACTIONS | {TASKS_PILL_ACTION}
FOOTER_ACTIONS = FOCUS_ACTIONS | ROW_ACTIONS | ARTIFACT_ACTIONS | {
    'esc to stop', 'esc to return to team lead', COLLAPSE_ACTION, 'enter to expand', '← to go back',
    'ctrl+t to show tasks', 'ctrl+t to hide tasks', 'shift+tab to cycle', '? for shortcuts',
    '/tasks to see subagents', 'ctrl+c to copy', 'shift+click to native select',
    'option+click to native select', 'hold space to speak',
}


def waiting_row(text):
    """Keep the displayed text after the waiting counts, including any visible since suffix."""
    action, separator, value = text.partition(' · ')
    if not separator:
        action, separator, value = text.rpartition(' ·')
        if value:
            return None, None
    if not separator or not re.fullmatch(rf'waiting on {WAITING_COUNT}(?:, {WAITING_COUNT})*', action):
        return None, None
    if not value.endswith('…'):
        name, divider, tail = value.rpartition(' · ')
        if not (name and divider and tail.startswith('since ') and tail[6:].strip()):
            return value, None
    waiting = {'shell': 0, 'monitor': 0}
    for count, kind in re.findall(WAITING_COUNT, action):
        if waiting[kind]:
            return None, None
        waiting[kind] = int(count)
    return value, waiting


def agent_bar_parts(screen):
    """Locate the prompt, footer, and row block without interpreting the footer text."""
    lines = [line.rstrip() for line in screen.splitlines()]
    rules = [i for i, line in enumerate(lines) if re.fullmatch(r'─+', line)]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2:]
    prompt = lines[top + 1:bottom]
    if (not prompt or not re.match(r'^❯(?:\s|$)', prompt[0])
            or prompt[0][1:].strip().startswith('Message @')):
        return None
    below = [line for line in lines[bottom + 1:] if line]
    if not below:
        return None
    footer, *row_lines = below
    return prompt, footer, row_lines


def agent_bar(screen):
    """Read focus, rows and the main prompt draft from one visible Claude capture."""
    parts = agent_bar_parts(screen)
    if parts is None:
        return None
    prompt, footer, row_lines = parts
    parsed = []
    selected, main = [], []
    for index, line in enumerate(row_lines):
        row = BAR_ROW.fullmatch(line)
        if row is None:
            return None
        cursor, dot, kind, text, compact = row.groups()
        if cursor:
            selected.append(index)
        if kind == 'main':
            if dot != '●' or text or compact:
                return None
            main.append(index)
        elif dot == '●' or not (text or compact or re.fullmatch(r'[1-9]\d* idle agents?', kind)):
            return None
        value, waiting = waiting_row(text or '')
        parsed.append({'kind': kind, 'value': value, 'waiting': waiting})
    if len(main) != 1 or len(selected) > 1:
        return None

    actions = {_norm(part) for part in footer.split('·')}
    row_actions = actions - {STOP_ALL_ACTION, COLLAPSE_ACTION}
    action = None
    if selected == main and row_actions == {'↑/↓ to select'}:
        focus = 'row'
    elif selected and selected != main and row_actions in ({'enter to view', 'x to stop'},
                                                            {'enter to view', 'x to clear'}):
        focus, action = 'row', 'stop' if 'x to stop' in row_actions else 'clear'
    elif selected or actions & (ROW_ACTIONS | ARTIFACT_ACTIONS) or '❯' in footer:
        return None
    else:
        last_action = _norm(footer.rsplit('·', 1)[-1])
        if last_action.endswith('…'):
            prefix = last_action[:-1].rstrip()
            completions = [known for known in FOOTER_ACTIONS if known.startswith(prefix)]
            if len(completions) != 1 or completions[0] not in FOCUS_ACTIONS:
                return None
            actions.add(completions[0])
        elif any(last_action.startswith(known + ' ') for known in FOCUS_ACTIONS):
            return None
        if TASKS_PILL_ACTION in actions:
            if actions & PROMPT_ACTIONS:
                return None
            focus = 'pill'
        elif actions & PROMPT_ACTIONS:
            focus = 'prompt'
        else:
            return None
    return {'focus': focus, 'action': action, 'rows': parsed, 'selected': selected[0] if selected else None,
            'selected_text': row_lines[selected[0]] if selected else None,
            'draft': bool(prompt[0][1:].strip() or any(line.strip() for line in prompt[1:]))}


def rendered_description(meta_description):
    """Return the description Claude puts in the bar, or None for unsupported text."""
    # In the 2.1.330 bundle, follow _9e's "since" field to Vbe's held row, then
    # Ki (chunk-874m1hb1.js) and Tt. Ki normalizes whitespace and caps the input
    # at 800 UTF-16 units before Tt cuts at 200 display columns, including the ellipsis.
    # ANSI stripping, combining graphemes and widths outside this single-column
    # ASCII/common Latin set (plus middle dot and ellipsis) are not verified.
    if not isinstance(meta_description, str) or not re.fullmatch(r'[ -~\t-\r\xa0·…À-ÖØ-öø-ſ]*', meta_description):
        return None
    description = re.sub(r'[\t-\r \xa0]+', ' ', meta_description).strip(' ')
    return description if len(description) <= 200 else description[:199] + '…'


def bar_target(bar, kind, rendered, waiting):
    boundary = rendered + ' ·'
    values = [(index, row['value'], row['waiting']) for index, row in enumerate(bar['rows'])
              if row['kind'] == kind and row['value'] is not None]
    candidates = [index for index, value, counts in values if counts is not None and value.startswith(boundary)
                  and (value.endswith('…') or value.rpartition(' · ')[0] == rendered)]
    if len(candidates) > 1:
        raise StopError('Multiple agents share this name. Stop it in the terminal.')
    if not candidates and any((counts is None or value.endswith('…'))
                              and len(value.removesuffix('…')) < len(boundary) for _, value, counts in values):
        raise StopError(BAR_NARROW_ERROR)
    if not candidates or bar['rows'][candidates[0]]['waiting'] != waiting:
        raise StopError(BAR_ID_ERROR)
    return candidates[0]


def tasks_panel_open(screen):
    """Distinguish the /tasks panel's actions from the agent bar's own x to stop."""
    sections = re.split(r'(?m)^▔+[ \t]*$', screen)
    if len(sections) < 2 or not sections[-1].strip():
        return False
    footer = sections[-1].rstrip().splitlines()[-1]
    actions = tuple(action for action in map(_norm, footer.split('·')) if action != 'f to foreground')
    return actions in (
        ('↑/↓ to navigate', 'x to stop', 'Enter to view', 'Esc to close'),
        ('↑/↓ to select', 'Enter to view', 'x to stop', 'Esc to close'),
        ('← to go back', 'x to stop', 'Enter/Space to close', 'Esc to close'),
        ('← to go back', 'Esc/Enter/Space to close', 'x to stop'),
    )


def stop_from_agent_bar(entry, agent, target, run, sleep, pressed, observe_bar=None, *, escape_sent=False):
    """Navigate with Down, then stop only the freshly verified waiting agent."""
    def parse(screen):
        bar = agent_bar(screen)
        if bar is not None and observe_bar is not None:
            observe_bar(screen, bar)
        return bar

    def identify(bar):
        if bar is None:
            raise StopError(BAR_READ_ERROR)
        if bar['draft']:
            raise StopError('There is an unsent draft in the agent input. Clear it, then try again.')
        return bar_target(bar, target['kind'], rendered, agent['waiting'])

    rendered = rendered_description(agent['description'])
    if rendered is None:
        raise StopError(BAR_ID_ERROR)
    if not rendered:
        raise StopError(BAR_NARROW_ERROR)
    bar = parse(visible(entry, run))
    if bar is None:
        raise StopError(BAR_READ_ERROR)
    sent_bar_key = False
    pill_down = False
    try:
        for step in range(9):
            position = identify(bar)
            selected = bar['selected']
            if selected == position:
                break
            if (selected is not None and selected > position) or step == 8:
                raise StopError('Could not reach that agent in the agent bar. Stop it in the terminal.')
            from_prompt = bar['focus'] == 'prompt'
            pill_down = False
            press(entry, 'Down', run=run)
            sent_bar_key = True
            sleep(0.18)
            bar = parse(visible(entry, run))
            # Record the Down transition during navigation. A later footer
            # that looks like a pill cannot authorize cleanup by itself.
            pill_down = from_prompt and bar is not None and bar['focus'] == 'pill'

        if bar['action'] != 'stop':
            raise StopError(BAR_ID_ERROR)
        selected_text = bar['selected_text']
        row = bar['rows'][position]
        visible_prefix = row['value'].removesuffix('…')
        fitting = []
        for item in subagents.listing(entry, limit=False)['agents']:
            if item['state'] != 'running' or (item.get('type') or 'general-purpose') != row['kind']:
                continue
            name = rendered_description(item.get('description') or '')
            if name is None:
                raise StopError(BAR_ID_ERROR)
            if visible_prefix.startswith(name) or name.startswith(visible_prefix):
                fitting.append(item)
        bar = parse(visible(entry, run))
        position = identify(bar)
        if bar['selected'] != position or bar['action'] != 'stop' or bar['selected_text'] != selected_text:
            raise StopError(BAR_ID_ERROR)
        if len(fitting) > 1:
            raise StopError('Multiple agents share this name. Stop it in the terminal.')
        if not fitting or fitting[0]['id'] != agent['id'] or fitting[0].get('waiting') != agent['waiting']:
            raise StopError(BAR_ID_ERROR)
        pressed['x'] = True
        pill_down = False
        press(entry, 'x', run=run)
        sent_bar_key = True
        sleep(0.18)
    finally:
        if sent_bar_key:
            row_escapes = 0
            while True:
                screen = visible(entry, run)
                bar = parse(screen)
                parts = agent_bar_parts(screen)
                if parts is None:
                    break
                selected_rows = sum(bool(row[1]) for line in parts[2] if (row := BAR_ROW.fullmatch(line)))
                if selected_rows == 1 and row_escapes < 3:
                    row_escapes += 1
                elif selected_rows or not pill_down or escape_sent:
                    break
                # Collapse can preserve the row cursor. Read it again before
                # another Escape, even if the footer looks like pill focus.
                press(entry, 'Escape', run=run)
                escape_sent = True
                sleep(0.35)
            if bar is None or bar['focus'] != 'prompt':
                raise StopError(BAR_CLOSE_ERROR)


# The newest Claude Code version whose /tasks screens the stop tests and a live run were checked on.
CHECKED_TASKS_UI = '2.1.330'
SESSIONS = paths.CLAUDE / 'sessions'


def claude_version(entry):
    try:
        data = json.loads((SESSIONS / f'{int(entry["owner_pid"])}.json').read_text())
        version = data.get('version')
        return version if isinstance(version, str) and re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,40}', version) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def with_version(entry, message):
    """Name the Claude Code version when its Stop controls could not be read."""
    version = claude_version(entry)
    if version is None or version == CHECKED_TASKS_UI or not re.search(
            r'Could not (identify|read|see|reach)|dialog|agent bar|share this name|too narrow', message):
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
    if not is_task and 'waiting' not in item:
        try:
            body = subagents.detail(entry, item['id']).get('prompt') or ''
        except (OSError, ValueError, RuntimeError):
            pass
    return {'task': is_task, 'labels': labels, 'kind': item.get('type') or 'general-purpose',
            'started': subagents._stamp(item.get('started')), 'body': body}


def stop(entry, agent_id, run=subprocess.run, sleep=time.sleep, clock=time.time, *, allow_process_fallback=True,
         observe_list=None, observe_bar=None):
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
        waiting = not is_task and 'waiting' in agent

        pressed = {}
        bound = bind(entry, agent) if is_task and allow_process_fallback else None

        def by_terminal():
            terminal_input.verify(entry, run)
            # Do not drive an unverified pane, including a pane for another agent.
            if waiting:
                escape_sent = close(entry, run, sleep, is_open=tasks_panel_open)
            else:
                close(entry, run, sleep)
            try:
                if terminal_input.live_status(entry, run)['state'] not in ('Idle', 'Working'):
                    raise StopError(f'Claude is waiting for you. Answer its prompt before stopping a {noun}.')
                if waiting:
                    # The bar parser reads the prompt draft and also refuses the subagent input view.
                    stop_from_agent_bar(entry, agent, target, run, sleep, pressed, observe_bar, escape_sent=escape_sent)
                else:
                    if any(terminal_input.prompt_draft(entry, run)):
                        raise StopError(f'There is an unsent draft in the {noun} input. Clear it, then try again.')
                    stop_selected(entry, target, run, sleep, clock, pressed, observe_list=observe_list)
            finally:
                if not waiting:
                    close(entry, run, sleep)
            return confirm(entry, agent_id, is_task, sleep)

        # Tasks: /tasks first, so Claude records the task as stopped by the user. A signal makes
        # Claude report a failed command, and its agent may then debug or rerun it. For a shell the
        # signal is the fallback: it does not read the screen, so it still works when a Claude Code
        # update changes /tasks. Agents and monitors have no exact process marker, so they use
        # Claude's controls only. After x, Claude may already be stopping the task, so a failure after
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
