"""Claude Code's slash-command dialogs (/mcp, /config, /resume, /status ...), read as data for a Tether card.

Claude Code 2.1.325 draws each dialog below a row of "▔" and keeps it out of the transcript: the screen is the only
source. The parser keeps what the dialog shows as blocks (list, facts, meter, scale, search box, text) and reads the
keys from the dialog's own footer. The keys a browser may send are only the keys that footer offers (see `allowed`).
A dialog taller than the pane has its top rows in the scrollback; `read` then asks tmux for them.
"""
import hashlib
import re
import subprocess
import time

import choice_state
import detection_rules
import paths

RULE = detection_rules.pattern('claude', 'DIALOG_RULE')
MORE = detection_rules.pattern('claude', 'DIALOG_MORE', groups=2)
HINT = detection_rules.pattern('claude', 'DIALOG_HINT', groups=2)
FACT = detection_rules.pattern('claude', 'DIALOG_FACT', groups=2)
MOVE = detection_rules.pattern('claude', 'DIALOG_MOVE', groups=2)
PERCENT = detection_rules.pattern('claude', 'DIALOG_PERCENT', groups=1)
MARKS = '✔✓⚠✘✗◯◉☐☑●○×'
CURSORS = '❯›'
HISTORY = 200        # scrollback rows read for a dialog taller than the pane
MAX_ROWS = 260
MARGIN = 4           # a dialog row ends this many columns before the pane edge, or earlier
INDENT = 3           # every dialog row starts at this column or later; chat rows and the input box start left of it
KEYS = {'enter': ('Enter', 'Enter'), 'return': ('Enter', 'Enter'), 'esc': ('Escape', 'Esc'), 'escape': ('Escape', 'Esc'),
        'space': ('Space', 'Space'), 'tab': ('Tab', 'Tab'), 'shift+tab': ('BTab', 'Shift+Tab'),
        '↑': ('Up', '↑'), '↓': ('Down', '↓'), '←': ('Left', '←'), '→': ('Right', '→'),
        'up': ('Up', '↑'), 'down': ('Down', '↓'), 'left': ('Left', '←'), 'right': ('Right', '→'),
        '⇧←': ('S-Left', 'Shift+←'), '⇧→': ('S-Right', 'Shift+→')}
STEP = .15           # seconds between a key and the next look at the screen
CYCLE_STEP = .4      # seconds between presses of a key that cycles a choice
TYPE_STEP = .03      # seconds between typed characters
SETTLE = 10          # looks at the screen after a key before the key counts as "did nothing"


def _key(token):
    """(tmux key, label) for one key name of a footer hint, or None. "Type" stands for text entry."""
    low = token.lower()
    if low in KEYS:
        return KEYS[low]
    if low == 'type':
        return ('', 'Type')
    if match := re.fullmatch(r'ctrl\s?\+\s?([a-z])', low):
        return ('C-' + match[1], 'Ctrl+' + match[1].upper())
    if re.fullmatch(r'[a-z/?]', token):  # a capital letter is a different key; no dialog of 2.1.325 uses one
        return (token, token)
    return None


def hints(text):
    """Key hints of one footer text ("↑/↓ to navigate · Enter to confirm · Esc to cancel"), or None when any part is
    not a hint. Each hint: the keys it names and the effect in Claude's own words."""
    found = []
    for part in re.split(r'\s+[·•]\s+|,\s+', ' '.join(text.split())):
        match = HINT.fullmatch(part)
        move = MOVE.fullmatch(part) if not match else None
        if move and not re.fullmatch(r'\d+ more', move[2]):
            # "↑ tabs", "↓ stats" (/stats): the arrow moves the focus to that part. `go` marks it.
            key, label = KEYS[move[1]]
            found.append({'keys': [{'key': key, 'label': label}], 'effect': move[2], 'text': part, 'go': True})
            continue
        keys = [_key(token) for token in ([match[1]] if match[1] == '/' else match[1].split('/'))] if match else [None]
        if not all(keys):
            return None
        found.append({'keys': [{'key': key, 'label': label} for key, label in keys], 'effect': match[2].strip(), 'text': part})
    return found or None


MONTHS = {'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'}
SHADES = '·░▒▓█'
ENTRY_START = re.compile(r'(?<!\+ )(?<!\S)(?=(?:double tap|shift \+|ctrl \+|alt \+|backslash|/[a-z]+ (?:for|to)\b|[!/@] for\b))')


def _heatmap(body, index):
    """/stats activity chart: a row of month names, then one row of shade characters per weekday."""
    names = body[index][0].split()
    if len(names) < 6 or not set(names) <= MONTHS:
        return None
    rows, at, wrapped = [], index + 1, False
    if at < len(body) and body[at][0].split() and set(body[at][0].split()) <= MONTHS:
        names, at, wrapped = names + body[at][0].split(), at + 1, True  # a narrow pane wraps the month row
    while at < len(body) and len(rows) < 7 and (match := re.fullmatch(r'\s*(?:(\w{3}) )?\s*([' + SHADES + r']{10,})', body[at][0])):
        cells = match[2]
        at += 1
        if at < len(body) and re.fullmatch(r'\s*[' + SHADES + r']+', body[at][0]) and len(body[at][0]) - len(body[at][0].lstrip()) < match.start(2):
            cells, at, wrapped = cells + body[at][0].strip(), at + 1, True  # and each weekday row
        rows.append((match[1] or '', cells, match.start(2)))
    if len(rows) != 7 or len({start for _, _, start in rows}) != 1:
        return None
    start = rows[0][2]
    months = [[max(0, match.start() - start), match.group()] for match in re.finditer(r'\S+', body[index][0])]
    if wrapped:
        months = [[place * 4, name] for place, name in enumerate(names)]  # one name per four weeks, as in a wide pane
    block = {'type': 'heatmap', 'months': months, 'rows': [{'label': label, 'cells': ''.join(str(SHADES.index(char)) for char in cells)} for label, cells, _ in rows]}
    if at < len(body) and re.fullmatch(r'\s*Less [' + SHADES + r' ]+ More', body[at][0]):
        at += 1
    return block, at


def _paragraph(body, index):
    end = index
    while end < len(body) and body[end][0].strip():
        end += 1
    return end


def _shortcuts(body, index):
    """/help's shortcut table, read column by column into entries ("ctrl + o" / "verbose output"). None when any
    part does not read as "<keys> for|to <effect>": the table then stays as the terminal drew it."""
    end = _paragraph(body, index)
    rows = [text for text, _ in body[index:end]]
    if len(rows) < 2 or len(re.findall(r' (?:for|to)(?: |$)', ' '.join(rows))) < 4:
        return None
    cells = [[(match.start(), match.group()) for match in re.finditer(r'\S(?:.*?\S)?(?= {2,}|$)', row)] for row in rows]
    if len(cells[0]) == 1 and len(cells[1]) > 1:
        return None  # a heading with no blank row under it (narrow pane): the table starts on the next row
    starts = sorted({start for row in cells for start, _ in row})
    columns = [start for at, start in enumerate(starts) if not at or start - starts[at - 1] > 1]
    if not 2 <= len(columns) <= 4:
        return None
    items = []
    for column in columns:
        parts = [cell for row in cells for start, cell in row if abs(start - column) <= 1]
        widest = max(map(len, parts))
        text, last = '', ''
        for part in parts:
            # A word as wide as the column is cut there ("/keybinding" + "s to customize"): join it with no space.
            cut = last and ' ' not in last and len(last) == widest and not ENTRY_START.match(part)
            text += ('' if cut or not text else ' ') + part
            last = part
        for entry in ENTRY_START.split(text):
            match = re.fullmatch(r'(.+?) (?:for|to) (.+)', entry.strip())
            if entry.strip() and not match:
                return None
            if match:
                items.append({'keys': match[1], 'effect': match[2]})
    return ({'type': 'shortcuts', 'items': items}, end) if len(items) >= 4 else None


def _entries(body, index):
    """/stats Models: blocks that start with "● name" and continue on indented rows, drawn in two columns."""
    end = _paragraph(body, index)
    rows = [text for text, _ in body[index:end]]
    if len(rows) < 2 or not rows[0].strip().startswith('● ') or rows[1].strip().startswith('● '):
        return None
    second = re.search(r' {2,}●', rows[0][rows[0].index('●') + 1:])
    split = rows[0].index('●') + second.end() if second else None
    items = []
    for part in ([[row[:split] for row in rows], [row[split:] for row in rows]] if split else [rows]):
        for row in part:
            if row.strip().startswith('● '):
                items.append({'title': row.strip()[2:], 'lines': []})
            elif row.strip() and items:
                items[-1]['lines'].append(row.strip())
    return {'type': 'entries', 'items': items}, end


def _dim(style, tone):
    return style['dim'] or style['fg'] in tone


def _plain(row, tone):
    """True when a row has text and none of it is secondary (dim) text."""
    text, styles = row
    return any(not _dim(style, tone) for char, style in zip(text, styles) if char != ' ')


def _all_dim(row, tone):
    text, styles = row
    return bool(text.strip()) and all(_dim(style, tone) for char, style in zip(text, styles) if char != ' ')


def _cells(row, tone, start=0):
    """Split a row at gaps of two spaces, and where main text turns into secondary text."""
    text, styles = row
    cells, current, gap, dim = [], '', 0, None
    for index in range(start, len(text)):
        char = text[index]
        if char == ' ':
            gap += 1
            continue
        now = _dim(styles[index], tone)
        if current and (gap >= 2 or dim is not None and now != dim and gap >= 1):
            cells.append((current, dim))
            current = ''
        elif current and gap:
            current += ' ' * gap
        current += char
        gap, dim = 0, now
    if current:
        cells.append((current, dim))
    return cells


def _wrapped(previous, following, width):
    """True when `following` continues `previous`: its first word did not fit on the row before."""
    words = following.split()
    return bool(words and previous.strip()) and len(previous.rstrip()) + 1 + len(words[0]) > width - MARGIN


def _join(previous, following, width):
    """Join a wrapped row. A word longer than the pane (an id, a URL) is cut at the edge, with no space."""
    cut = len(previous.rstrip()) >= width - MARGIN + 1 and ' ' not in following.strip() and len(previous.split()[-1]) >= 16
    return previous + ('' if cut else ' ') + following.strip()


def _title(row, tone):
    """The dialog's first row: its title, a note in secondary text, and its tabs when it has them."""
    text, styles = row
    emphasis = [bool(style['reverse'] or style['bg'] is not None) for style in styles]
    parts = [(match.group().strip(), match.start(), match.end()) for match in re.finditer(r'\S(?:.*?\S)?(?= {2,}|$)', text)]
    if any(emphasis) and len(parts) >= 1:
        # The active tab is drawn " Config " with a background (the tab row has focus) or in reverse (the body has it).
        first = next(index for index, flag in enumerate(emphasis) if flag)
        last = len(emphasis) - 1 - emphasis[::-1].index(True)
        active = text[first:last + 1].strip()
        head = text[:first].strip()
        tail = text[last + 1:].strip()
        names = [name for name in re.split(r' {2,}', head) if name] + [active] + [name for name in re.split(r' {2,}', tail) if name]
        # "Settings  Status": with the first tab active only one space parts it from the title.
        bold = ''.join(char for char, style in zip(text, styles[:first]) if style['bold']).strip()
        title = bold if bold and head.startswith(bold) else names[0]
        rest = head[len(title):].strip()
        tabs = [name for name in re.split(r' {2,}', rest) if name] + [active] + [name for name in re.split(r' {2,}', tail) if name]
        if title and len(tabs) >= 2 and len(set(tabs)) == len(tabs):
            return {'title': title, 'tabs': tabs, 'tab': active, 'tab_focus': any(style['bg'] is not None for style in styles)}
    cells = _cells(row, tone)
    main = ' '.join(cell for cell, dim in cells if not dim) or text.strip()
    note = ' '.join(cell for cell, dim in cells if dim) if any(not dim for _, dim in cells) else ''
    return {'title': main, 'note': note}


def _footer(rows):
    """(start, hints) of the key hints that end the dialog. They are the last rows of the last paragraph; a narrow
    pane wraps them."""
    end = len(rows)
    start = end
    while start > 0 and rows[start - 1][0].strip() and end - start < 6:
        start -= 1
    for index in range(start, end):
        found = hints(' '.join(row[0].strip() for row in rows[index:end]))
        # Every dialog footer of 2.1.325 names Esc, or an arrow that moves the focus ("↓ stats"). Body text can look
        # like a hint: a clipped /help shortcut table ends in "ctrl + t to toggle  ctrl + s to ↓".
        if found and (any(key['key'] == 'Escape' for hint in found for key in hint['keys']) or any(hint.get('go') for hint in found)) and all(len(hint['effect']) <= 40 for hint in found):
            return index, found
    return end, []


def _item(row, tone, column, cursor):
    text, _ = row
    body = text[column:]
    mark = ''
    if body[:1] in MARKS and body[1:2] == ' ':
        mark, column = body[0], column + 2
    cells = _cells(row, tone, column)
    if not cells:
        return None
    label = cells[0][0]
    value = re.sub(r'^[·•]\s*', '', '  '.join(cell for cell, _ in cells[1:]))
    return {'text': label, 'value': value, 'mark': mark, 'detail': '', 'selected': cursor}


def _scale(rows, index, tone):
    """Claude's /effort slider: a pointer row and the row of level names under it. The current level is bold."""
    if index + 1 >= len(rows) or '▲' not in rows[index][0]:
        return None
    text, styles = rows[index + 1]
    bar = re.search(r'[─━▲]+', rows[index][0])
    options, value = [], None
    for match in re.finditer(r'\S+', text):
        if match.start() > bar.end() + 3:
            break
        options.append(match.group())
        if any(style['bold'] for style in styles[match.start():match.end()]):
            value = match.group()
    if len(options) < 2 or value is None:
        return None
    return {'type': 'scale', 'options': options, 'value': value, '_end': bar.end()}


def parse(frame):
    """The dialog in one styled frame (tmux capture-pane -e), or None."""
    rows = choice_state._rows(frame)
    while rows and not rows[-1][0].strip():
        rows.pop()
    rule = next((index for index in range(len(rows) - 1, -1, -1) if RULE.match(rows[index][0])), None)
    if rule is None or not 1 <= len(rows) - rule - 1 <= MAX_ROWS:
        return None
    width = len(rows[rule][0])
    rows = rows[rule + 1:]
    if any(text.strip() and len(text) - len(text.lstrip()) < INDENT for text, _ in rows):
        return None  # chat or the input box below the rule: that dialog is closed
    first = next((index for index, row in enumerate(rows) if row[0].strip()), None)
    if first is None:
        return None
    start, found = _footer(rows)
    # Secondary text has the colour of the key hints (it depends on the theme). Without hints, the usual greys.
    tone = {(5, 246), (5, 244), (5, 245), 90}
    if found and start < len(rows):
        tone |= {style['fg'] for char, style in zip(*rows[start]) if char != ' ' and style['fg'] is not None}
    title_row = rows[first]
    if (first + 1 < start and rows[first + 1][0].strip() and len(re.split(r' {2,}', title_row[0].strip())) >= 3
            and not any(style['reverse'] or style['bg'] is not None for style in title_row[1])
            and any(style['reverse'] or style['bg'] is not None for style in rows[first + 1][1])):
        # A narrow pane wraps the tab row, and the active tab is on its second row.
        more = rows[first + 1]
        cut = len(more[0]) - len(more[0].lstrip())
        title_row = (title_row[0] + '   ' + more[0][cut:], title_row[1] + title_row[1][-1:] * 3 + more[1][cut:])
        first += 1
    head = _title(title_row, tone)
    if head.get('tabs') and first + 1 < start and rows[first + 1][0].strip() and not any(
            style['bold'] or style['reverse'] or style['bg'] is not None for style in rows[first + 1][1]) and (
            len(rows[first][0].rstrip()) + 3 + len(rows[first + 1][0].strip().split('  ')[0]) > width - MARGIN):
        more_tabs = [name for name in re.split(r' {2,}', rows[first + 1][0].strip()) if name]
        if not set(more_tabs) & set(head['tabs']):
            head['tabs'] += more_tabs  # a narrow pane wraps the tab row
            first += 1
    if not head['title'] or head['title'][0] in CURSORS + '/':
        return None  # "/btw ..." is the side question; a cursor row is a list with no title
    body = rows[first + 1:start]
    cursor_rows = [index for index, (text, styles) in enumerate(body)
                   if (match := re.match(r'^(\s*)[' + CURSORS + r'] ', text)) and not _dim(styles[match.end(1)], tone)]
    marker_rows = [index for index, (text, _) in enumerate(body) if re.match(r'^\s*[' + CURSORS + r'] ', text)]
    column = None
    focus = 'list'
    if cursor_rows:
        column = len(re.match(r'^\s*[' + CURSORS + r'] ', body[cursor_rows[-1]][0]).group())
    elif len(marker_rows) == 1:
        # A dim cursor: the search box has the focus, and the cursor shows where Enter or ↓ goes.
        column = len(re.match(r'^\s*[' + CURSORS + r'] ', body[marker_rows[0]][0]).group())
        cursor_rows, focus = marker_rows, 'search'
    blocks, extra = [], []
    current = None   # the open list block
    previous = None  # the last item, while no blank row came after it
    index = 0

    def text_block(line, dim, mono, raw=None, source=None):
        last = blocks[-1] if blocks else None
        if last and last['type'] == 'text' and not last.get('_closed') and (
                mono and last['mono'] or (mono or last['mono']) and last['dim'] == dim or _wrapped(last['_rows'][-1], line, width)):
            last['lines'].append(line)
            last['mono'] = last['mono'] or mono
            last['dim'] = last['dim'] and dim
            last['_rows'].append(source if source is not None else line)
        else:
            blocks.append({'type': 'text', 'lines': [line], 'dim': dim, 'mono': mono, '_rows': [source if source is not None else line]})
        blocks[-1].setdefault('_raw', []).append(raw if raw is not None else line)

    while index < len(body):
        row = body[index]
        text, styles = row
        indent = len(text) - len(text.lstrip())
        stripped = text.strip()
        if not stripped:
            previous = None
            if blocks and blocks[-1]['type'] == 'text':
                blocks[-1]['_closed'] = True
            index += 1
            continue
        if stripped[0] == '╭' and index + 2 < len(body) and body[index + 2][0].strip()[:1] == '╰':
            inner = body[index + 1]
            field = re.sub(r'^\s*│\s?|\s*│\s*$', '', inner[0])
            icon = field.lstrip().startswith('⌕')
            value = field.lstrip().lstrip('⌕').strip()
            holder = _all_dim((value, inner[1][inner[0].find(value):inner[0].find(value) + len(value)]), tone) if value else False
            blocks.append({'type': 'search', 'placeholder': value if holder or value.endswith('…') else '',
                           'value': '' if holder or value.endswith('…') else value, 'focused': focus == 'search' or not column,
                           '_icon': icon})
            current = previous = None
            index += 3
            continue
        scale = _scale(body, index, tone)
        if scale:
            end = scale.pop('_end')
            labels = [cell for cell, _ in _cells(body[index - 1], tone)] if index and body[index - 1][0].strip() else []
            if blocks and blocks[-1]['type'] == 'text' and labels and blocks[-1]['lines'][-1].split() == ' '.join(labels).split():
                blocks[-1]['lines'].pop()
                if not blocks[-1]['lines']:
                    blocks.pop()
            scale['ends'] = labels[:2] if len(labels) == 2 else []
            blocks.append(scale)
            side = []
            for offset in (0, 1):
                for cell, dim in _cells(body[index + offset], tone, min(end + 3, len(body[index + offset][0]))):
                    keys = hints(cell)
                    extra.extend(keys) if keys else side.append(cell)
            for hint in extra[-1:] if side else []:
                hint['effect'] += ' ' + side[0].split()[0]  # "Tab to toggle" beside "Ultracode  off": say what it toggles
            if side:
                blocks.append({'type': 'text', 'lines': [' '.join(side)], 'dim': False, 'mono': False, '_closed': True})
            current = previous = None
            index += 2
            continue
        more = MORE.match(text)
        if more and column is not None:
            if current is None:
                current = {'type': 'list', 'items': [], 'more_above': 0, 'more_below': 0}
                blocks.append(current)
            current['more_above' if more[1] == '↑' else 'more_below'] = int(more[2])
            previous = None
            index += 1
            continue
        marker = re.match(r'^(\s*)([' + CURSORS + r'↑↓]) (?=\S)', text) if column is not None else None
        if marker and len(marker.group()) != column:
            marker = None
        if column is not None and (marker or indent >= column):
            if current is None or blocks[-1] is not current:
                current = {'type': 'list', 'items': [], 'more_above': 0, 'more_below': 0}
                blocks.append(current)
            deeper = indent > column and not marker
            dim = _all_dim(row, tone)
            if previous is not None and (deeper or dim):
                previous['detail'] = (previous['detail'] + ' ' + stripped).strip()
            elif deeper and not current['items']:
                text_block(stripped, dim, False)
                blocks[-1], blocks[-2] = blocks[-2], blocks[-1]
            elif not marker and (dim or styles[indent]['bold'] and stripped[0] not in MARKS):
                current['items'].append({'heading': ' '.join(stripped.split())})
                previous = None
            else:
                item = _item(row, tone, column, index in cursor_rows)
                if item:
                    if marker and marker[2] in '↑↓':
                        current['more_above' if marker[2] == '↑' else 'more_below'] = -1
                    current['items'].append(item)
                    previous = item
            index += 1
            continue
        current = previous = None
        special = _heatmap(body, index) or _entries(body, index) or _shortcuts(body, index)
        if special:
            blocks.append(special[0])
            index = special[1]
            continue
        if re.fullmatch(r'Less [' + SHADES + r' ]+ More', stripped) and any(block['type'] == 'heatmap' for block in blocks):
            index += 1  # the chart's legend; the card draws its own
            continue
        choices = [part.strip() for part in re.split(r' · ', re.sub(r'\s[✻✶✳✢✽*]$', '', stripped))]
        if len(choices) >= 2 and styles[indent]['bold'] is not None:
            # "All time · Last 7 days · Last 30 days" (/stats): the bold one is current, the others are grey.
            spans, at = [], indent
            for choice in choices:
                at = text.index(choice, at)
                spans.append(styles[at:at + len(choice)])
                at += len(choice)
            bold = [all(style['bold'] for style in span) for span in spans]
            if sum(bold) == 1 and all(flag or all(_dim(style, tone) for style in span) for flag, span in zip(bold, spans)):
                blocks.append({'type': 'options', 'options': choices, 'value': choices[bold.index(True)]})
                index += 1
                continue
        if any(style['bg'] is not None for char, style in zip(text, styles) if char in ' █▉▊▋▌▍▎▏') or re.match(r'^[█▉▊▋▌▍▎▏]+\s*$', stripped):
            # A usage bar. A wide pane puts "25% used" on the same row, a narrow pane on the next one.
            label = re.sub(r'^[█▉▊▋▌▍▎▏\s]+', '', stripped)
            after = next((at for at in (index + 1, index + 2) if at < len(body) and body[at][0].strip()), None)
            if not label and after is not None and PERCENT.search(body[after][0]):
                label = body[after][0].strip()
                index = after
            percent = PERCENT.search(label)
            if percent:
                blocks.append({'type': 'meter', 'percent': min(100, int(percent[1])), 'text': label})
                index += 1
                continue
        if any(style['reverse'] or style['bg'] is not None for char, style in zip(text, styles) if char != ' '):
            # A second tab row inside the body ("Overview  Models" in Stats). Tether shows it; it has no key hint.
            names = [name for name in re.split(r' {2,}', stripped) if name]
            active = ''.join(char for char, style in zip(text, styles) if style['reverse'] or style['bg'] is not None).strip()
            if len(names) >= 2 and active in names:
                blocks.append({'type': 'tabs', 'tabs': names, 'tab': active})
                index += 1
                continue
        columns = [re.fullmatch(r'([^:]{1,30}): ?(\S.*)', cell) for cell in re.split(r' {2,}', stripped)]
        if len(columns) >= 2 and all(columns):
            last = blocks[-1] if blocks else None
            if not (last and last['type'] == 'facts' and not last.get('_closed')):
                last = {'type': 'facts', 'rows': []}
                blocks.append(last)
            last['rows'].extend({'label': column[1].strip(), 'value': column[2].strip()} for column in columns)
            index += 1
            continue
        fact = FACT.match(stripped)
        pair = re.fullmatch(r'(\S(?:.*?\S)?) {2,}(\S.*)', stripped)
        if fact and not (styles[indent]['bold'] or _all_dim(row, tone) and not fact[2].startswith('http')):
            fact = None
        if not fact and pair and ' ' * 2 not in pair[2] and styles[indent]['bold'] and not all(style['bold'] for char, style in zip(text, styles) if char != ' '):
            fact = (None, pair[1], pair[2])  # "Help improve our AI models      false": a label and its value
        if fact and not re.search(r' {2,}\S+.*:\s', fact[2]):
            last = blocks[-1] if blocks else None
            if not (last and last['type'] == 'facts' and not last.get('_closed')):
                last = {'type': 'facts', 'rows': []}
                blocks.append(last)
            value = fact[2].strip()
            if key := re.fullmatch(r'(.*?)\s+((?:Tab|Space|Enter) to \w+)', value):
                extra.extend({**hint, 'effect': hint['effect'] + ' ' + fact[1].rstrip(':').strip()} for hint in hints(key[2]) or [])  # "Ultracode  off  Tab to toggle"
                value = key[1].strip()
            last['rows'].append({'label': fact[1].rstrip(':').strip(), 'value': value})
            index += 1
            continue
        if (blocks and blocks[-1]['type'] == 'facts' and not blocks[-1].get('_closed') and not styles[indent]['bold'] and index
                and body[index - 1][0].strip() and (indent > INDENT or _wrapped(body[index - 1][0], stripped, width))):
            # A long value wraps under its label, or at the pane edge.
            if side := hints(stripped):
                side[0]['effect'] += ' ' + blocks[-1]['rows'][-1]['label']
                extra.extend(side)  # "Tab to toggle" under "Ultracode  off"
                index += 1
                continue
            glue = '' if _join(body[index - 1][0], stripped, width) == body[index - 1][0] + stripped else ' '
            blocks[-1]['rows'][-1]['value'] += glue + stripped
            index += 1
            continue
        if blocks and blocks[-1]['type'] == 'facts':
            blocks[-1]['_closed'] = True
        dim = _all_dim(row, tone)
        if all(style['bold'] for char, style in zip(text, styles) if char != ' ') and not re.search(r' {2,}', stripped):
            blocks.append({'type': 'heading', 'text': stripped})
        else:
            inline = hints(stripped) if dim else None
            tail = None
            if dim and not inline and ' · ' in stripped:
                # "102 skills · enter/space to cycle, / to search, t to sort, Esc to close" (it can wrap).
                span = index
                while span + 1 < len(body) and body[span + 1][0].strip() and _all_dim(body[span + 1], tone):
                    span += 1
                joined = ' '.join(line[0].strip() for line in body[index:span + 1])
                lead, _, rest = joined.partition(' · ')
                tail = hints(rest)
                if tail and any(key['key'] == 'Escape' for hint in tail for key in hint['keys']):
                    extra.extend(tail)
                    text_block(lead, True, False)
                    index = span + 1
                    continue
            for inner in re.findall(r'\(([^()]*\bto\b[^()]*)\)', stripped):
                extra.extend(hints(inner) or [])  # "(↑/↓ to scroll)", "(ctrl+s to filter by marketplace)"
            if inline:
                extra.extend(inline)
            else:
                text_block(stripped, dim, bool(re.search(r'\S {2,}\S|[░▒▓█·─│]{3,}', stripped)), text[INDENT:].rstrip(), text)
        index += 1
    for block in blocks:
        block.pop('_closed', None)
        block.pop('_icon', None)
        raw = block.pop('_raw', None)
        sources = block.pop('_rows', None)
        if block['type'] == 'text' and block['mono'] and raw:
            block['lines'] = raw
        elif block['type'] == 'text':
            joined = block['lines'][0]
            for at in range(1, len(block['lines'])):
                joined = _join(sources[at - 1], block['lines'][at], width)[len(sources[at - 1]) - len(joined):] if False else (
                    joined + ('' if _join(sources[at - 1], block['lines'][at], width) == sources[at - 1] + block['lines'][at].strip() else ' ') + block['lines'][at])
            block['lines'] = [joined]
    blocks = [block for block in blocks if block['type'] != 'list' or block['items'] or block['more_above'] or block['more_below']]
    keys = found + [hint for hint in extra if hint['text'] not in {known['text'] for known in found}]
    focus = focus if column is not None else 'none'
    # What a key does depends on the tab, the focus, the marked row and the hints: Enter selects in /config's search box
    # and changes a setting in its list. The card sends this value back, so a key goes only to the screen the person saw.
    marked = next((item['text'] for block in blocks if block['type'] == 'list' for item in block['items'] if item.get('selected')), '')
    mode = hashlib.sha256('\0'.join([head['title'], head.get('tab') or '', focus, marked, *(hint['text'] for hint in keys)]).encode()).hexdigest()[:16]
    return {**head, 'blocks': blocks, 'hints': keys, 'focus': focus, 'mode': mode,
            'text': '\n'.join(text[INDENT:] if text.strip() else '' for text, _ in rows[first:]).strip('\n')}


def dialog_id(dialog):
    return hashlib.sha256(('dialog\0' + dialog['title']).encode()).hexdigest()


def capture(entry, run=subprocess.run, history=0):
    command = [paths.TMUX, 'capture-pane', '-e', '-p', '-t', entry['id']]
    if history:
        command += ['-S', '-' + str(history)]
    return choice_state.OSC.sub('', run(command, check=True, capture_output=True, text=True, timeout=5).stdout)


def tall(frame):
    """True when the frame can be the lower part of a dialog whose top row left the pane: no rule row, every row
    indented like a dialog, and key hints at the end."""
    rows = choice_state._rows(frame)
    while rows and not rows[-1][0].strip():
        rows.pop()
    if not rows or any(RULE.match(text) for text, _ in rows):
        return False
    if any(text.strip() and len(text) - len(text.lstrip()) < INDENT for text, _ in rows):
        return False
    return bool(_footer(rows)[1])


def read(entry, frame=None, run=subprocess.run):
    """The dialog the pane shows now, or None. `frame`: a styled capture of the visible pane the caller already has."""
    if frame is None:
        frame = capture(entry, run)
    found = parse(frame)
    if found is None and tall(frame):
        found = parse(capture(entry, run, HISTORY))
    return found


def allowed(dialog):
    """tmux keys the dialog's own footer offers. Esc always closes; a list always takes the arrow keys."""
    keys = {key['key'] for hint in dialog['hints'] for key in hint['keys'] if key['key']}
    keys.add('Escape')
    if any(block['type'] == 'list' for block in dialog['blocks']):
        keys.update(('Up', 'Down'))
    if dialog.get('tab_focus'):
        keys.update(('Left', 'Right'))
    return keys


def _items(dialog):
    return [item for block in dialog['blocks'] if block['type'] == 'list' for item in block['items'] if 'heading' not in item]


class Changed(RuntimeError):
    """The dialog on screen is not the one the browser acted on."""


def act(entry, body, run=subprocess.run, sleep=time.sleep):
    """Do one action of the card on the real dialog. The caller holds SEND_LOCK and has verified the pane.

    Actions: key (one of `allowed`), row (move the cursor to a list row), tab (open a tab), scale (set the slider),
    filter (replace the text of the search box). Each step reads the screen again; a dialog that closed or changed
    stops the action.
    """
    def look():
        dialog = read(entry, run=run)
        if dialog is None or dialog['title'] != body.get('title'):
            raise Changed('The dialog changed. Review it before you try again.')
        return dialog

    def press(*keys):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], *keys], check=True, capture_output=True, timeout=5)
        sleep(STEP)

    def type_text(text):
        # One character per call: Claude Code's /resume search drops text that arrives in one burst (it reads as a
        # paste). A lone ";" is tmux's command separator, so it goes escaped.
        for char in text:
            run([paths.TMUX, 'send-keys', '-l', '-t', entry['id'], '--', '\\;' if char == ';' else char], check=True, capture_output=True, timeout=5)
            sleep(TYPE_STEP)
        sleep(STEP)

    def marks(dialog):
        # What a move key can change. Not the whole text: a spinner or a loading row also changes it (seen in /stats),
        # and the next key would then go out before this one shows.
        return (dialog.get('tab'), dialog.get('tab_focus'), dialog['focus'], [hint['text'] for hint in dialog['hints']],
                [item['selected'] for item in _items(dialog)], [item['text'] for item in _items(dialog)][:1],
                [block.get('value', block.get('tab')) for block in dialog['blocks'] if block['type'] in ('scale', 'search', 'tabs', 'options')])

    def step(dialog, *keys, state=marks):
        """Press, then wait for Claude Code to draw the result. A decision on a screen that is not drawn yet presses
        the next key too early: the cursor then goes past its row, and a tab row wraps around."""
        before = state(dialog)
        press(*keys)
        for _ in range(SETTLE):
            dialog = look()
            if state(dialog) != before:
                break
            sleep(STEP)
        return dialog, state(dialog) != before

    dialog = look()
    action = body.get('action')
    if action in ('key', 'filter') and body.get('mode') != dialog['mode']:
        raise Changed('The dialog changed. Review it before you try again.')
    if action == 'key':
        key = body.get('key')
        if key not in allowed(dialog):
            raise ValueError('This dialog does not offer that key.')
        press(key)
        return
    if action == 'row':
        label, row = body.get('label'), body.get('row')
        if not isinstance(label, str) or not isinstance(row, int) or isinstance(row, bool):
            raise ValueError('Choose a row first.')
        idle = 0
        for _ in range(60):
            items = _items(dialog)
            places = [index for index, item in enumerate(items) if item['text'] == label]
            at = next((index for index, item in enumerate(items) if item['selected']), None)
            if not places or at is None:
                raise Changed('That row is no longer in the list. Review the dialog.')
            target = min(places, key=lambda index: abs(index - row))
            if at == target and dialog['focus'] == 'list':
                return
            # With the search box in focus the cursor only shows where Down goes: Down gives the list the focus.
            dialog, moved = step(dialog, 'Down' if at < target or dialog['focus'] != 'list' else 'Up')
            idle = 0 if moved else idle + 1
            if idle >= 2:
                break
        raise RuntimeError('Could not move to that row. Use the terminal for this dialog.')
    if action == 'tab':
        want = body.get('tab')
        if want not in (dialog.get('tabs') or []):
            raise ValueError('This dialog has no such tab.')
        idle, cycle = 0, False
        for _ in range(len(dialog['tabs']) * 2 + 6):
            if dialog.get('tab') == want:
                return
            if idle >= 2:
                break
            if want not in (dialog.get('tabs') or []):
                break
            offered = {key['key']: hint['effect'] for hint in dialog['hints'] for key in hint['keys']}
            # From the hint list, not from `offered`: /stats names ↑ twice ("↑ tabs" and "↑/↓ to scroll").
            to_tabs = next((key['key'] for hint in dialog['hints'] for key in hint['keys']
                            if re.search(r'\btabs?\b', hint['effect']) and key['key'] not in ('Left', 'Right', 'Tab')), None)
            if cycle:
                key = 'Tab'  # Tab goes forward and wraps. An arrow after it would go back and forth (seen in /plugin).
            elif dialog.get('tab_focus'):
                key = 'Right' if dialog['tabs'].index(want) > dialog['tabs'].index(dialog['tab']) else 'Left'
            elif to_tabs:
                key = to_tabs
            elif '/' in offered and re.search(r'search|filter', offered['/']):
                key = '/'  # the list has the focus; the search box leads to the tab row
            elif 'Tab' not in offered and not any(re.search(r'\bchange\b', effect) for effect in offered.values()):
                key = 'Tab'
            else:
                break
            dialog, moved = step(dialog, key)
            cycle = cycle or key == 'Tab' and moved
            idle = 0 if moved else idle + 1
        raise RuntimeError('Could not open that tab. Use the terminal for this dialog.')
    if action in ('subtab', 'option'):
        # A part of the body that Claude changes with one key: Tab for the views of /stats, "r to cycle dates" for its
        # date range. Both work only while the body has the focus; "↓ stats" in the footer says it does not yet.
        kind, name, want = ('tabs', 'tab', body.get('tab')) if action == 'subtab' else ('options', 'value', body.get('value'))
        choices = 'tabs' if action == 'subtab' else 'options'
        idle = 0
        for _ in range(12):
            part = next((block for block in dialog['blocks'] if block['type'] == kind), None)
            if part is None or want not in part[choices]:
                raise ValueError('This dialog has no such choice.')
            if part[name] == want:
                return
            offered = {key['key']: hint['effect'] for hint in dialog['hints'] for key in hint['keys']}
            enter = next((hint['keys'][0]['key'] for hint in dialog['hints'] if hint.get('go') and hint['keys'][0]['key'] == 'Down'), None)
            cycle = next((key for key, effect in offered.items() if re.search(r'\bcycle\b', effect)), None)
            if enter:
                key = enter
            elif action == 'option' and cycle:
                # /stats loads a date range for seconds and shows the old one meanwhile. Waiting for each would hold
                # SEND_LOCK that long, so press once per step of the cycle and let the card show the result.
                count = (part[choices].index(want) - part[choices].index(part[name])) % len(part[choices])
                for _ in range(count):
                    press(cycle)
                    sleep(CYCLE_STEP)
                return
            elif action == 'subtab' and 'Tab' not in offered and not any(re.search(r'\b(?:change|toggle)\b', effect) for effect in offered.values()):
                key = 'Tab'  # not in the footer; checked on Claude Code 2.1.325
            else:
                break
            dialog, moved = step(dialog, key)
            idle = 0 if moved else idle + 1
            if idle >= 2:
                break
        raise RuntimeError('Could not change that. Use the terminal for this dialog.')
    if action == 'scale':
        want = body.get('value')
        for _ in range(12):
            scale = next((block for block in dialog['blocks'] if block['type'] == 'scale'), None)
            if scale is None or want not in scale['options']:
                raise ValueError('This dialog has no such level.')
            if scale['value'] == want:
                return
            dialog, moved = step(dialog, 'Right' if scale['options'].index(want) > scale['options'].index(scale['value']) else 'Left')
            if not moved:
                break
        raise RuntimeError('Could not set that level. Use the terminal for this dialog.')
    if action == 'filter':
        text = body.get('text')
        if not isinstance(text, str) or len(text) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError('Search text can have at most 80 characters and no control characters.')
        box = next((block for block in dialog['blocks'] if block['type'] == 'search'), None)
        offered = {key['key']: hint['effect'] for hint in dialog['hints'] for key in hint['keys']}
        typing = any(key['label'] == 'Type' for hint in dialog['hints'] for key in hint['keys'])
        if box is None or not (typing or '/' in offered):
            raise ValueError('This dialog has no search box that Tether can type in.')
        if not typing:
            dialog, _ = step(dialog, '/')
            box = next((block for block in dialog['blocks'] if block['type'] == 'search'), None)
            if box is None or not any(key['label'] == 'Type' for hint in dialog['hints'] for key in hint['keys']):
                raise RuntimeError('Could not open the search box. Use the terminal for this dialog.')
        if box['value']:
            dialog, _ = step(dialog, *['BSpace'] * len(box['value']))
            box = next((block for block in dialog['blocks'] if block['type'] == 'search'), None)
            if box is None or box['value'] or not any(key['label'] == 'Type' for hint in dialog['hints'] for key in hint['keys']):
                raise RuntimeError('Could not clear the search box. Use the terminal for this dialog.')
        if text:
            type_text(text)
        return
    raise ValueError('Unknown dialog action.')
