"""What Claude Code shows under and above its input box: the suggested next prompt, right-aligned notices
("Update installed · /restart to apply", "4% until auto-compact"), the effort badge, the mode line and background agents.
Read-only: it parses one capture of the pane."""
import json
from pathlib import Path
import re
import time

from terminal_input import ANSI_SGR, dim_prompt_text

RULE = re.compile(r'^─+')
EFFORT = re.compile(r'[○◔◐◕●]\s*(low|medium|high|xhigh|max)\b', re.I)
UPDATE = re.compile(r'update (?:installed|available)|/restart\b', re.I)
GOAL = re.compile(r'/goal active(?:\s*\(([^)]*)\))?', re.I)
SPINNER = re.compile(r'^\S\s+([A-Z][^()…]*…)\s*\((\d[^)]*)\)')
RUNNING = re.compile(r'^[● ] (\S.*?) · (\d[\dhms ]*\S)$')  # Claude blinks the ● of a running tool, so half the reads show a space
CALL = re.compile(r'^[● ] ([A-Z]\w*)\((.*)$')  # "● Bash(command…": the call shape of a running tool
CALL_TIME = re.compile(r'^\s+\((\d[\dhms ]*\S)(?: · timeout [^)]*)?\)$')  # "(1m 43s · timeout 8m 20s)" under its output
DENIED = re.compile(r'\d+ actions? not run · permission denied')
HEADS_UP = re.compile(r'^✦ (.+)$')
CHOICE = re.compile(r'(\d):\s+(.+?)(?=\s{2,}\d:|$)')
PICKER_ROW = re.compile(r'^❯\s*\d+\.\s')
PICKER_KEYS = re.compile(r'Enter to (?:select|confirm)|↑/↓ to navigate', re.I)
MORE = re.compile(r'^\s*↓ (\d+) more\b')
URL = re.compile(r'https?://\S+')
# The keys each card state takes (from the Claude Code UI), for when the card is taller than the screen and its key line is cut off.
CARD_KEYS = {
    'offer': [('1', 'Learn more'), ('2', 'Knew this already'), ('0', 'Dismiss')],
    'explained': [('1', 'Understood'), ('2', 'Chat in main session'), ('0', 'Dismiss')],
    'ready': [('1', 'Helpful'), ('2', 'Not helpful'), ('3', 'Didn’t read')],
}
AGENT = re.compile(r'^\s*([●◯○])\s+(\S+)(?:\s{2,}(.*?))?(?:\s{2,}(\d[\dhms ]*\S.*))?\s*$')
AGENT_LOOSE = re.compile(r'^\s*([●◯○])\s+(\S+)\s(.*)$')
CODEX_CONTEXT = re.compile(r'\bContext\s+(\d+(?:\.\d+)?)%\s+(left|used)\b|(\d+(?:\.\d+)?)%\s+context\s+(left|used)\b', re.I)
CODEX_MODEL = re.compile(r'^\s*([\w.-]+)(?:\s+(minimal|low|medium|high|xhigh|extra high|max))?(?:\s+(fast|ultrafast))?\s+·', re.I)


def codex_screen(screen):
    """Only the bottom status line, never percentages quoted in conversation text."""
    for line in reversed(ANSI_SGR.sub('', screen).splitlines()):
        context, model = CODEX_CONTEXT.search(line), CODEX_MODEL.match(line)
        if not model and not (context and re.match(r'^\s*(?:Context\s+)?\d', line, re.I)):
            continue
        result = {'model': model[1]} if model else {}
        if context:
            value, direction = float(context[1] or context[3]), context[2] or context[4]
            result['context'] = {'percent': max(0, min(100, 100 - value if direction.lower() == 'left' else value))}
        if model and model[2]:
            result['effort'] = model[2].lower()
        if model and model[3]:
            result['tier'] = model[3].lower()
        credits = re.search(r'\bcredits(?: balance)?\s*:?\s+(\d+(?:\.\d+)?|unlimited)\b', line, re.I)
        if credits:
            result['credits'] = {'unlimited': True} if credits[1].lower() == 'unlimited' else {'balance': credits[1]}
        result['windows'] = [{'label': '5h' if match[1].lower() == '5h' else '168h',
                              'percent': 100 - float(match[2]) if (match[3] or '').lower() == 'left' else float(match[2]), 'resets_at': None}
                             for match in re.finditer(r'\b(5h|7d|weekly)\s+(\d+(?:\.\d+)?)%\s*(left|used)?', line, re.I)]
        return result
    return {}


def codex(report):
    """Expose Codex metrics through the same status/effort strip as Claude."""
    result = {key: report[key] for key in ('model', 'effort', 'tier', 'credits') if report.get(key) not in (None, '', {})}
    context = report.get('context') or {}
    if context.get('percent') is not None:
        result['context'] = context
    result['windows'] = [window for window in report.get('windows', []) if window.get('percent') is not None]
    parts = [report['model']] if report.get('model') else []
    if context.get('percent') is not None:
        parts.append(f"ctx {context['percent']:.0f}%")
    result['status'] = ' | '.join(parts)
    return result


def input_anchor(plain):
    """Require both borders so a sent ❯ message or a picker is not an input box."""
    for at in range(len(plain) - 1, 0, -1):
        border = plain[at - 1].strip()
        if not plain[at].startswith('❯') or not border.startswith('─') and not border.endswith('─'):
            continue
        if not any(RULE.match(line.strip()) for line in plain[at + 1:]):
            continue
        if PICKER_ROW.match(plain[at]) and any(PICKER_KEYS.search(line) for line in plain[at:]):
            return None
        return at
    return None


def notice_line(line):
    """Known notices can sit closer to the left edge in narrow panes."""
    return line.startswith(' ') and bool(line.startswith(' ' * 12) or UPDATE.search(line) or GOAL.search(line)
        or '/effort' in line or re.search(r'\d+% until\b|new task\? /clear|/clear to save|paste again to', line))


def notice_continuation(previous, text):
    """A lowercase wrap continues an unfinished phrase, never a complete notice."""
    if text.startswith('/effort') and EFFORT.search(previous) and '/effort' not in previous:
        return True
    if text.startswith('/restart') and UPDATE.search(previous) and previous.endswith('·'):
        return True
    return bool(text[:1].islower() and not re.search(
        r'[.!?…:;·]$|(?:until auto-compact|context used|tokens|/effort|paste again to expand|'
        r'/restart(?: to apply)?|Restart to (?:apply|update))$', previous))


def notice_styles(lines):
    """First/last text colour and faint state per row; SGR state crosses rows."""
    dim, colour = False, None
    styles = []
    for line in lines:
        row = []
        for part in re.split(r'(\x1b\[[0-9;]*m)', line):
            if not ANSI_SGR.fullmatch(part):
                if part.strip():
                    row.append((dim, colour))
                continue
            codes = [int(code or 0) for code in part[2:-1].split(';')]
            at = 0
            while at < len(codes):
                code = codes[at]
                if code in (38, 48, 58) and at + 1 < len(codes):
                    count = 2 if codes[at + 1] == 5 else 4 if codes[at + 1] == 2 else 0
                    if code == 38:
                        colour = tuple(codes[at:at + count + 1])
                    at += count + 1
                    continue
                if code == 0:
                    dim, colour = False, None
                elif code in (2, 22):
                    dim = code == 2
                elif code == 39 or 30 <= code <= 37 or 90 <= code <= 97:
                    colour = None if code == 39 else (code,)
                at += 1
        styles.append((row[0], row[-1]) if row else (None, None))
    return styles


def parse(styled):
    lines = [line.rstrip() for line in styled.splitlines()]
    plain = [ANSI_SGR.sub('', line) for line in lines]
    # Claude's input box: a ❯ line right under a rule. A sent message in the chat history also starts with ❯, but has no rule above it.
    # When a dialog (a permission prompt) replaces the input box, no line qualifies, and the dialog is not read as the footer.
    at = input_anchor(plain)
    result = {'suggestion': '', 'notices': [], 'effort': '', 'mode': '', 'status': '', 'agents': [], 'update': '',
              'goal': '', 'tasks': [], 'ide': '', 'spinner': None, 'running': None, 'denied': None, 'heads_up': None,
              'observed': at is not None}
    if at is None:
        return result
    text = dim_prompt_text('\n'.join(lines[at:at + 1]))
    if text and text != 'Press up to edit queued messages' and not text.startswith('Try '):
        result['suggestion'] = text
    # Right-aligned lines just above the top rule: notices and the effort badge.
    top = at - 1
    start = top
    while start and plain[start - 1].startswith(' ') and plain[start - 1].strip():
        start -= 1
    styles = notice_styles(lines[:top])
    above = []
    notice_start = top
    for index in range(start, top):
        line, text = plain[index], plain[index].strip()
        if above and notice_continuation(above[-1], text) and styles[index - 1][1] == styles[index][0]:
            above[-1] += ' ' + text
        elif notice_line(line):
            if not above:
                notice_start = index
            above.append(text)
        else:
            above = []
            notice_start = top
    for item in above:
        effort = EFFORT.search(item)
        if effort and '/effort' in item:
            result['effort'] = effort[1].lower()
        else:
            # "◎ /goal active (10h) · ✔ Update installed · /restart": a new notice starts at a symbol after " · ".
            result['notices'] += [part.strip() for chunk in re.split(r'\s{3,}', item) for part in re.split(r' · (?=[^\x00-\x7f])', chunk) if part.strip()]
    # Under the bottom rule: status line, mode line, agents.
    below = plain[at + 1:]
    while below and not RULE.match(below[0].strip()):
        below = below[1:]
    for line in below[1:]:
        stripped = line.strip()
        if not stripped or re.search(r'paste again to expand', stripped, re.I):
            continue
        agent = AGENT.match(line)
        # A narrow pane puts one space after the name; the row is still an agent, not a notice.
        loose = None if agent else AGENT_LOOSE.match(line)
        if agent and agent[2] != '❯':
            result['agents'].append({'name': agent[2], 'task': (agent[3] or '').strip(), 'detail': (agent[4] or '').strip(), 'main': agent[1] == '●'})
        elif loose and loose[2] != '❯':
            result['agents'].append({'name': loose[2], 'task': loose[3].strip(), 'detail': '', 'main': loose[1] == '●'})
        elif stripped.startswith(('⏵', '⏸')) or 'shift+tab' in stripped or re.search(r'\bmode on\b', stripped):
            mode, *tasks = re.sub(r'\s*\(shift\+tab to cycle\)', '', stripped).split(' · ')
            # "⧉ …" is the IDE context (open file or selected lines), not background work.
            result['mode'] = mode
            result['tasks'] = [task.strip() for task in tasks if task.strip() and not task.strip().startswith('⧉')]
            result['ide'] = next((task.replace('⧉', '').strip() for task in tasks if task.strip().startswith('⧉')), '')
        elif not result['status']:
            result['status'] = stripped
        else:
            result['notices'] += [part.strip() for part in re.split(r'\s{3,}', stripped) if part.strip()]
    result['notices'] = [item for item in result['notices'] if not re.search(r'paste again to expand', item, re.I)]
    goal = next((GOAL.search(item) for item in result['notices'] if GOAL.search(item)), None)
    if goal:
        result['goal'] = goal[1] or 'active'
        result['notices'] = [item for item in result['notices'] if not GOAL.search(item)]
    transcript(plain[:notice_start], result)
    for item in result['notices'] + [result['status'], result['mode']]:
        if UPDATE.search(item):
            result['update'] = item
            break
    return result


MODES = ('default', 'accept', 'plan', 'auto', 'bypass')


def mode_key(mode):
    """'⏵⏵ accept edits on' -> 'accept'; no mode line means Claude's default mode."""
    text = (mode or '').lower()
    if text in ('', 'default', 'dontask'):
        return 'default'
    return next((key for key in MODES[1:] if re.search(r'\b' + key + r'\b', text)), None)


def set_mode(read, press, target, sleep=time.sleep, tries=6):
    """Press Shift+Tab until the footer shows the target mode. read() returns the footer; press() sends one Shift+Tab.
    Waits for each press to land, so fast taps on the phone cannot skip past the mode you asked for."""
    if target not in MODES:
        raise ValueError('Unknown mode.')
    def current_mode():
        result = read()
        current = mode_key(result.get('mode'))
        if result.get('observed') is False or current is None:
            raise RuntimeError('Cannot read the permission mode. Widen the pane or use the terminal view.')
        return current

    current = current_mode()
    seen = [current]
    for _ in range(tries):
        if current == target:
            return {'mode': target}
        press()
        for _ in range(12):
            sleep(.15)
            now = current_mode()
            if now != current:
                break
        current = now
        if current in seen:
            break  # went all the way round
        seen.append(current)
    if current == target:
        return {'mode': target}
    raise RuntimeError(f'{target.capitalize()} mode is not offered in this session. Modes found: {", ".join(seen)}.')


def card_actions(lines, start):
    """Read every adjacent action row, including continuations of a wrapped label."""
    choices, end = [], start
    while end < len(lines):
        row = lines[end]
        if re.match(r'\s*\d:\s', row):
            choices += [(key, label.strip()) for key, label in CHOICE.findall(row.strip())]
        elif choices and row.startswith('  ') and row.strip() and not notice_line(row):
            key, label = choices[-1]
            choices[-1] = (key, label + row.strip())
        else:
            break
        end += 1
    return choices, end


def action_prefix(choices, state):
    # Ignore spaces at physical wraps; only the final visible label may be truncated.
    expected = CARD_KEYS[state]
    if not choices or len(choices) > len(expected):
        return False
    for index, ((key, label), (wanted_key, wanted_label)) in enumerate(zip(choices, expected)):
        label = re.sub(r'\s+', '', label).removesuffix('…')
        wanted_label = re.sub(r'\s+', '', wanted_label)
        if key != wanted_key or not label or (wanted_label != label and
                (index != len(choices) - 1 or not wanted_label.startswith(label))):
            return False
    return True


def heads_up(lines):
    """One ✦ card from Claude Code's suggestion feature, in any state:
    offer (Learn more / Knew this already / Dismiss), making ("Making your page…", no keys), ready (a page link),
    explained (sections under the headline), asked, failed ("Couldn't …" with 0: OK).
    The card must be the last thing above the input box; an older card higher up is history, not a question."""
    title, body, detail, options, more, end, in_detail = lines[0][2:].strip(), [], [], [], 0, len(lines), False
    inline = re.search(r'(?:\s+·)?\s+(?=\d:\s)', title)  # "Couldn't write that page · 0: OK"
    if inline:
        title, options = title[:inline.start()], [{'key': k, 'label': v.strip()} for k, v in CHOICE.findall(title[inline.end():])]
        lines = lines[:1]
    for offset, follow in enumerate(lines[1:], 1):
        if re.match(r'\s*\d:\s', follow):
            choices, end = card_actions(lines, offset)
            options = [{'key': key, 'label': label} for key, label in choices]
            break
        found = UP_MORE.match(follow) or MORE.match(follow)
        if found:
            more, end = int(found[1]), offset + 1
            break
        if notice_line(follow) or (follow.strip() and not follow.startswith('  ')):
            end = offset
            break
        if not follow.strip():
            in_detail = True  # the headline ends at the first blank line; sections follow
            if detail and detail[-1]:
                detail.append('')
            continue
        if in_detail and detail and detail[-1] and follow.startswith('    '):
            detail[-1] += ' ' + follow.strip()  # a wrapped bullet
        else:
            (detail if in_detail else body).append(follow.strip())
    rest = lines[end:]
    if any(line.strip() and not notice_line(line) and not MORE.match(line) and not UP_MORE.match(line) for line in rest):
        return None
    scroll = next((UP_MORE.match(line) or MORE.match(line) for line in rest if UP_MORE.match(line) or MORE.match(line)), None)
    if scroll:
        more = int(scroll[1])
    head = ' '.join([title] + body)
    state = ('making' if head.startswith(('Making your page', 'One moment')) else 'ready' if head.startswith('Learning page ready')
             else 'failed' if head.startswith(('Couldn', 'Not published')) else 'explained' if detail else 'offer')
    if options and state in CARD_KEYS:
        choices = [(option['key'], option['label']) for option in options]
        if state == 'offer' and action_prefix(choices, 'explained'):
            state = 'explained'
        if not action_prefix(choices, state):
            return None
        options = [{'key': key, 'label': label} for key, label in CARD_KEYS[state]]
    if not options and more and state in CARD_KEYS:
        options = [{'key': key, 'label': label} for key, label in CARD_KEYS[state]]
    link = URL.search(' '.join([head] + detail))
    while detail and not detail[-1]:
        detail.pop()
    if not options and state not in ('making', 'failed'):
        return None
    return {'text': head, 'state': state, 'detail': detail, 'url': link[0].rstrip('.,)') if link else None, 'more': more, 'options': options}


def transcript(plain, result):
    """The live part of the screen above the input: the spinner, a running tool call and a Heads up card."""
    lines = plain[-40:]
    skip_to = 0
    for index, line in enumerate(lines):
        spinner = SPINNER.match(line)
        if spinner:
            result['spinner'] = {'verb': spinner[1], 'detail': spinner[2]}
        if index >= skip_to:
            found = running_tool(lines, index)
            if found:
                result['running'], skip_to = found
        denied = DENIED.search(line)
        if denied:
            reason = next((more.strip()[len('Reason:'):].strip() for more in lines[index + 1:index + 3] if more.strip().startswith('Reason:')), '')
            result['denied'] = {'text': denied[0], 'reason': reason}
    # Card headlines can remain visible above a long explanation. Tools still use only the last 40 rows.
    for index, line in enumerate(plain):
        if HEADS_UP.match(line):
            card = heads_up(plain[index:])
            if card:
                result['heads_up'] = card
    if not result['heads_up']:
        result['heads_up'] = clipped_card(plain)
    # A finished run leaves no spinner with an ellipsis, so a stale "Running" line is dropped with it.
    if not result['spinner']:
        result['running'] = None
        result['denied'] = None



def running_tool(lines, index):
    """A running tool call that starts at lines[index]: its title, time and command, and the index after its title.
    A narrow pane wraps the title ("● Testing the wrapper with no" / "  output · 19s"), so up to two more lines join it.
    A permission note ("⎿  1 action not run · permission denied") can come before the "⎿  $ command" line."""
    line = lines[index]
    call = CALL.match(line)
    if call:
        return running_call(lines, index, call)
    if not (line.startswith('● ') or (line.startswith('  ') and len(line) > 2 and line[2] != ' ' and (not index or not lines[index - 1].strip()))):
        return None
    head, end = line, index + 1
    while not RUNNING.match(head) and end < min(len(lines), index + 3) and lines[end].startswith('  ') and lines[end][2:3].strip() and '⎿' not in lines[end]:
        head += ' ' + lines[end].strip()
        end += 1
    running = RUNNING.match(head)
    if not running or end >= len(lines) or not lines[end].lstrip().startswith('⎿'):
        return None
    notes = [at for at in range(end, min(len(lines), end + 6)) if lines[at].lstrip().startswith('⎿')]
    at = next((at for at in notes if lines[at].split('⎿', 1)[1].strip().startswith('$')), notes[0])
    command = lines[at].split('⎿', 1)[1].strip()
    # A long command wraps onto indented lines below; join up to two of them, stopping at Claude's own hints.
    for more in lines[at + 1:at + 3]:
        if not more.startswith('    ') or not more.strip() or more.strip().startswith(('(ctrl+', '⎿', 'Reason:')):
            break
        command += ' ' + more.strip()
    return {'title': running[1], 'elapsed': running[2], 'command': command.removeprefix('$').strip()}, end


def running_call(lines, index, call):
    """The call shape: "● Bash(command" (wrapped onto indented lines, ending in ")"), "⎿  output", then a time line
    "(6s)" or "(1m 43s · timeout 8m 20s)". Only a running call has the time line."""
    command, end = call[2], index + 1
    while end < min(len(lines), index + 6) and lines[end].startswith('      ') and not command.endswith(')'):
        command += ' ' + lines[end].strip()
        end += 1
    if not command.endswith(')') or end >= len(lines) or not lines[end].lstrip().startswith('⎿'):
        return None
    for at in range(end, min(len(lines), end + 12)):
        if not lines[at].strip() or (at > end and not lines[at].startswith('    ')):
            return None
        elapsed = CALL_TIME.match(lines[at])
        if elapsed:
            return {'title': call[1], 'elapsed': elapsed[1], 'command': command[:-1].strip()}, end
    return None


UP_MORE = re.compile(r'^\s*↑ (\d+) more\b')


def clipped_card(lines):
    """An explained ✦ card taller than the screen: Claude Code scrolls it, so the ✦ headline is gone and a
    "↑ N more" line sits under the keys. Keep the visible part; card_note adds the full text from the store."""
    # Right-aligned notices ("2% until auto-compact") can sit between the card and the input box.
    rows = [(at, line) for at, line in enumerate(lines) if line.strip() and not notice_line(line)]
    if not rows:
        return None
    last = len(rows) - 1
    above = UP_MORE.match(rows[last][1])
    tail = rows[last][0] if above else len(lines)
    end = next((at for at, row in reversed(rows[:last] if above else rows) if re.match(r'\s*\d:\s', row)), None)
    if end is None:
        return None
    # Walk back to the first row of the same action block.
    while end and lines[end - 1].strip() and lines[end - 1].startswith('  ') and not notice_line(lines[end - 1]):
        end -= 1
    while end < tail and not re.match(r'\s*\d:\s', lines[end]):
        end += 1
    choices, after = card_actions(lines, end)
    if any(line.strip() and not notice_line(line) for line in lines[after:tail]):
        return None
    # A narrow pane drops the keys that do not fit ("0: Dismiss"), so the visible keys need only start the list.
    if not action_prefix(choices, 'explained'):
        return None
    choices = CARD_KEYS['explained']
    start = end
    while start and not (lines[start - 1].strip() and not lines[start - 1].startswith('  ')):
        start -= 1
    detail = []
    for follow in lines[start:end]:
        text = re.sub(r'\s+\[[-+]\]$', '', follow.strip())  # the collapse toggle at the right edge
        if not text:
            if detail and detail[-1]:
                detail.append('')
        elif follow.startswith('    ') and detail and detail[-1]:
            detail[-1] += ' ' + text  # a wrapped bullet
        else:
            detail.append(text)
    while detail and not detail[-1]:
        detail.pop()
    return {'text': 'Heads up', 'state': 'explained', 'detail': detail, 'url': None, 'more': int(above[1]) if above else 0,
            'options': [{'key': key, 'label': label.strip()} for key, label in choices], 'clipped': True}


CARD_STORE = Path.home() / '.claude' / 'plugins' / 'store'


def card_note(card, thread, store=None, *, screen=''):
    """Add the full explanation of an explained ✦ card, from Claude Code's own store (read-only), and the note that
    "Chat in main session" would put in the input box: the screen shows only part of a long explanation."""
    fragment = False
    if not card and screen:
        plain = [ANSI_SGR.sub('', line).rstrip() for line in screen.splitlines()]
        at = input_anchor(plain)
        rows = [row for row in plain[:at - 1] if row.strip() and not notice_line(row)] if at is not None else []
        fragment = bool(rows and (UP_MORE.match(rows[-1]) or (re.match(r'^\s*1:\s+U', rows[-1])
            and action_prefix(CHOICE.findall(rows[-1].strip()), 'explained'))))
    if not thread or (not card and not fragment) or (card and card.get('state') != 'explained'):
        return card
    for path in sorted((store or CARD_STORE).glob('cc-plugin-you-should-know_*.json')):
        try:
            sessions = json.loads(path.read_text()).get('sessions') or []
        except (OSError, ValueError, AttributeError):
            continue
        entry = next((item for item in sessions if isinstance(item, dict) and item.get('id') == thread), None)
        view = (entry or {}).get('view')
        if not isinstance(view, dict) or view.get('kind') != 'explained' or not isinstance(view.get('text'), str):
            continue
        line = view.get('line') or ''
        if not card:
            card = {'text': 'Heads up', 'state': 'explained', 'detail': [], 'url': None, 'more': 0,
                    'options': [{'key': key, 'label': label} for key, label in CARD_KEYS['explained']], 'clipped': True}
        if card.get('clipped'):
            card = dict(card, text=f"{entry.get('tag') or 'Heads up'} · {line}")
        elif line[:40] not in card['text']:
            continue  # the store moved on to another card
        quoted = '\n'.join('>' if not row else '> ' + row for row in f"{entry.get('tag') or 'Heads up'} · {line}\n\n{view['text'].strip()}".split('\n'))
        return dict(card, full=view['text'].strip(), note=f'Here is a note offered by a side agent:\n\n{quoted}\n\n')
    return card
