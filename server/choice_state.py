"""Read-only choice state from one tmux -e frame. This grants no key authority.

IDs are visible numbers, or frame-local zero-based row indices. Focus is an
index into items, not an input-focus claim. A bare overflow arrow has count -1
(present, count unknown). No transcript or title supplies missing screen state.
"""
import re


SGR = re.compile(r'\x1b\[([0-9;:]*)m')
OSC = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
RULE = re.compile(r'^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║-]*$')
ROW = re.compile(r'^(?P<indent>\s*)(?:(?P<cursor>[>❯›])\s+)?'
                 r'(?:(?P<number>\d+)[.)]\s+)?'
                 r'(?:(?P<box>\[[xX× ]\]|[☒☑☐◉◯\u25ef✔✓])\s+)?'
                 r'(?:(?P<number_after>\d+)[.)]\s+)?(?P<label>\S.*)$')
MORE = re.compile(r'^\s*([↑↓])(?:\s+(\d+)\s+more)?\s*$')
KEY_START = re.compile(r'^\s*(?:Enter|Space|Esc|Tab|left/right|Keyboard:|ctrl\+[abwr]|Type to search)\b|^\s*[↑↓←]', re.I)
HINT_END = re.compile(r'\besc(?:ape)?(?:\s+to)?\s+(?:cancel|back|close|interrupt|clear)\s*$', re.I)
ACCEPT = re.compile(r'\benter(?:\s+to)?\s+(?:select|confirm|continue|save|submit answer|edit|open)\b', re.I)
CHAT = re.compile(r'^\s*(?:[●⏺✻✶✳✢✽]\s|⎿|[>❯›]\s*$)')


def _number(match):
    return match['number'] or match['number_after']


def _rows(frame):
    """Keep character attributes; SGR state persists across physical rows."""
    state = {'bold': False, 'dim': False, 'reverse': False, 'underline': False,
             'fg': None, 'bg': None}
    rows = []
    for raw in OSC.sub('', frame).splitlines():
        text, styles, pos = '', [], 0
        for match in SGR.finditer(raw):
            part = raw[pos:match.start()]
            text += part
            styles.extend([state.copy() for _ in part])
            codes = [int(c or 0) for c in match[1].replace(':', ';').split(';')]
            i = 0
            while i < len(codes):
                code = codes[i]
                if code == 0:
                    state = dict.fromkeys(state, False)
                    state.update(fg=None, bg=None)
                elif code in (1, 2, 4, 7):
                    state[{1: 'bold', 2: 'dim', 4: 'underline', 7: 'reverse'}[code]] = True
                elif code in (22, 24, 27):
                    for key in {22: ('bold', 'dim'), 24: ('underline',), 27: ('reverse',)}[code]:
                        state[key] = False
                elif code in (38, 48) and i + 1 < len(codes):
                    count = 3 if codes[i + 1] == 2 else 1 if codes[i + 1] == 5 else 0
                    if count and i + count + 1 < len(codes):
                        state['fg' if code == 38 else 'bg'] = tuple(codes[i + 1:i + count + 2])
                        i += count + 1
                elif code in (39, 49):
                    state['fg' if code == 39 else 'bg'] = None
                elif 30 <= code <= 37 or 90 <= code <= 97:
                    state['fg'] = code
                elif 40 <= code <= 47 or 100 <= code <= 107:
                    state['bg'] = code
                i += 1
            pos = match.end()
        part = raw[pos:]
        text += part
        styles.extend([state.copy() for _ in part])
        if '\x1b' in text or any(ord(c) < 32 and c != '\t' for c in text):
            return []  # not a capture-pane SGR frame
        rows.append((text.rstrip(), styles))
    return rows


def _emphasis(styles):
    return any(s['reverse'] or s['underline'] or s['bg'] is not None for s in styles)


def _heading(row):
    text, styles = row
    return (bool(text.strip()) and not re.match(r'^\s*[>❯›☒☑☐←→]', text)
            and any(s['bold'] for s in styles)
            and not any(s['reverse'] or s['underline'] or s['fg'] is not None or s['bg'] is not None for s in styles))


def _footer(rows):
    # Only a complete footer at the live end can bound a list. Wrapped hints
    # retain their words. A clipped or unfamiliar instruction stays raw text.
    end = len(rows)
    while end and (not rows[end - 1][0].strip() or RULE.fullmatch(rows[end - 1][0])
                   or re.fullmatch(r'─{3,}[^─\n]+─+', rows[end - 1][0])):
        end -= 1
    for start in range(max(0, end - 8), end):
        if MORE.fullmatch(rows[start][0]):
            continue
        if not KEY_START.match(rows[start][0]) and not re.match(r'^\s*option \d+/\d+\s*\|', rows[start][0]):
            continue
        if any((match := ROW.fullmatch(text)) and (match['cursor'] or _number(match) or match['box'])
               for text, _ in rows[start:end]):
            continue
        text = ' '.join(row[0].strip() for row in rows[start:end])
        if (ACCEPT.search(text) or re.search(r'\bType to search\b', text, re.I)) and HINT_END.search(text):
            return start, end, [part.strip() for part in re.split(r'\s*[·•|]\s*', text) if part.strip()]
    return None


def _tabs(rows):
    for text, styles in rows:
        # Claude question chips have checkboxes; Codex groups use two spaces.
        if re.search(r'[☒☑☐]', text):
            spans = list(re.finditer(r'[☒☑☐✔✓]\s+([^☒☑☐✔✓←→]+)', text))
        elif re.search(r'\S {2,}\S', text):
            spans = list(re.finditer(r'\S(?:.*?\S)?(?= {2,}|$)', text))
        else:
            continue
        if not spans:
            continue
        tabs = []
        for match in spans:
            label = (match[1] if match.lastindex else match[0]).strip()
            if not label or '…' in label or '...' in label:
                return None
            tabs.append({'label': label, 'active': _emphasis(styles[match.start():match.end()])})
        if sum(tab['active'] for tab in tabs) == 1:
            return tabs
    return None


def _read(frame, agent):
    """Keep surface bounds with the read record for picker dispatch."""
    if agent not in ('claude', 'codex', 'antigravity'):
        return None
    rows = _rows(frame)
    footer = _footer(rows)
    if footer is None:
        return None
    stop, end, hints = footer
    matches = [(i, ROW.fullmatch(text)) for i, (text, _) in enumerate(rows[:stop])]
    anchors = [(i, m) for i, m in matches if m and (m['cursor'] or _number(m) or m['box'])]
    # A checkbox chip above the question is not an item. Begin at a cursor or
    # numbered row, or a styled row only when there is no marker at all.
    strong = [(i, m) for i, m in anchors if m['cursor'] or _number(m)]
    if not strong:
        strong = [(i, m) for i, m in matches if m and (_emphasis(rows[i][1]) or
                  any(s['bold'] and not s['dim'] and s['fg'] is not None for s in rows[i][1]))]
    if not strong:
        return None
    cursor_rows = [(i, m) for i, m in strong if m['cursor']]
    anchor, match = (cursor_rows or strong)[-1]
    numbered = _number(match) is not None
    multi = match['box'] is not None
    label_column = match.start('label')
    gutter = len(match['indent']) + (2 if match['cursor'] else 0)
    first = anchor
    # Walk only the contiguous list, not the conversation or question header.
    while first > 0:
        text = rows[first - 1][0]
        m = ROW.fullmatch(text)
        if not text.strip() and not numbered and agent == 'codex':
            break
        if not text.strip() or MORE.fullmatch(text) or RULE.fullmatch(text):
            first -= 1
        elif m and ((numbered and _number(m)) or (multi and m['box'])
                    or (not numbered and not multi and (m['cursor'] or len(m['indent']) >= label_column))):
            first -= 1
        else:
            break
    # Leading blank/rule/count rows belong to the list, but the header must be
    # visible. Never reconstruct a vertically clipped list from a footer alone.
    header_end = first
    if not any(text.strip() and not RULE.fullmatch(text) for text, _ in rows[:header_end]):
        return None
    header_start = header_end - 1
    while header_start >= 0:
        text = rows[header_start][0]
        # Claude qE draws a one-row filter inside a rounded border. That
        # border belongs to the header, not to a separate surface.
        if text.lstrip().startswith('╰') and RULE.fullmatch(text):
            if (header_start >= 2 and re.match(r'^\s*│\s*⌕\s', rows[header_start - 1][0])
                    and re.fullmatch(r'\s*╭─+╮\s*', rows[header_start - 2][0])):
                header_start -= 3
                continue
        if text.strip() and RULE.fullmatch(text):
            break
        if _heading(rows[header_start]):
            header_start -= 1
            break
        if CHAT.match(rows[header_start][0]) or re.match(r'^\s*[>❯›]', rows[header_start][0]):
            return None
        header_start -= 1
    header = rows[header_start + 1:header_end]
    tabs = _tabs(header)
    filter_text = None
    for text, _ in header:
        if m := re.match(r'^\s*(?:│\s*)?(?:⌕|Search\s*(?:›|:)|Filter:)\s*(.*?)\s*(?:│)?$', text, re.I):
            filter_text = m[1]
        elif re.fullmatch(r'\s*(?:Type to (?:search|filter).*|Search conversations[….]*)\s*', text):
            filter_text = ''
    # Codex immediate search is an unlabelled paragraph between blank lines.
    # It is distinguishable only with a styled title and the source layout.
    if filter_text is None and agent == 'codex':
        nonempty = [(i, text) for i, (text, _) in enumerate(header) if text.strip()]
        if len(nonempty) >= 2:
            i, text = nonempty[-1]
            if (i > 0 and not header[i - 1][0].strip() and any(_heading(row) for row in header[:i])
                    and not any(s['bold'] or s['dim'] or s['reverse'] for s in header[i][1])
                    and not re.search(r'[☒☑☐←→]', text)):
                filter_text = text.strip()
    if filter_text is not None and ('…' in filter_text or '...' in filter_text):
        return None
    items, styles, boxes, counts = [], [], [], {'↑': 0, '↓': 0}
    for text, attrs in rows[header_end:stop]:
        if not text.strip() or RULE.fullmatch(text):
            continue
        if m := MORE.fullmatch(text):
            if counts[m[1]]:
                return None
            counts[m[1]] = int(m[2]) if m[2] else -1
            continue
        m = ROW.fullmatch(text)
        if not m or CHAT.match(text):
            return None
        is_item = bool(_number(m)) if numbered else bool(m['box']) if multi else (
            bool(m['cursor']) or len(m['indent']) == label_column)
        if not is_item:
            if items and len(m['indent']) >= label_column:
                items[-1]['detail'] = ' '.join(filter(None, (items[-1].get('detail'), text.strip())))
                continue
            # Codex multiselect may show a selection preview below the list.
            if (multi and items and len(m['indent']) == gutter
                    and text.strip() == ' · '.join(item['text'] for item in items if item.get('checked'))):
                continue
            return None
        label = m['label'].strip()
        if '…' in label or '...' in label:
            return None
        parts = re.split(r' {2,}', label, maxsplit=1)
        item = {'id': int(_number(m)) if numbered else len(items), 'text': parts[0],
                'selected': bool(m['cursor'])}
        if len(parts) > 1:
            item['detail'] = parts[1]
        if m['box']:
            item['checked'] = m['box'] in ('[x]', '[X]', '[×]', '☒', '☑', '◉', '✔', '✓')
        items.append(item)
        styles.append(attrs[m.start('label'):m.end('label')])
        boxes.append(m['box'])
    if not items or len({item['id'] for item in items}) != len(items):
        return None
    if any(re.search(r'\bspace(?:\s+to)?\s+toggle\b', hint, re.I) for hint in hints) and not any(boxes):
        return None
    if any('checked' in item for item in items):
        # Mixed checkbox and free-answer rows are valid only with a toggle hint.
        if not all('checked' in item for item in items) and not any(re.search(r'\bspace\b', hint, re.I) for hint in hints):
            return None
        multi = any(box not in (None, '◉', '◯', '\u25ef') for box in boxes)
    if numbered:
        numbers = [item['id'] for item in items]
        if numbers != list(range(numbers[0], numbers[-1] + 1)):
            return None
        counts['↑'] = counts['↑'] or numbers[0] - 1
        if m := re.search(r'\boption (\d+)/(\d+)\b', ' '.join(hints)):
            total = int(m[2])
            if total < numbers[-1] or int(m[1]) not in numbers:
                return None
            counts['↓'] = counts['↓'] or total - numbers[-1]
    marked = [i for i, item in enumerate(items) if item['selected']]
    emphasized = [i for i, style in enumerate(styles) if _emphasis(style)]
    if not marked and not emphasized:
        # Claude uses a coloured, bold label; descriptions and checks can also
        # be coloured. Only a unique non-dim bold label can supply focus.
        emphasized = [i for i, style in enumerate(styles)
                      if any(s['bold'] and not s['dim'] and s['fg'] is not None for s in style)]
    if len(marked) > 1 or len(emphasized) > 1 or (marked and emphasized and marked != emphasized):
        return None
    focus = (marked or emphasized or [None])[0]
    if focus is None:
        return None
    items[focus]['selected'] = True
    # Disabled is reported only with an explicit rendered reason. Dim alone
    # also means an ordinary description, so it cannot prove disabled state.
    for item in items:
        if re.search(r'\(disabled\)|\[disabled\]', item['text'], re.I):
            item['disabled'] = True
    choice = {'selection': 'multi' if multi else 'single', 'items': items, 'focus': focus,
              'filter': filter_text, 'tabs': tabs, 'more_above': counts['↑'],
              'more_below': counts['↓'], 'hints': hints}
    return {'choice': choice, 'numbered': numbered,
            'text': '\n'.join(text for text, _ in rows[header_start + 1:end]).strip(),
            'hint': ' '.join(text.strip() for text, _ in rows[stop:end])}


def parse(frame, agent):
    """Return the active visible choice record, or None for uncertain frames."""
    found = _read(frame, agent)
    return found['choice'] if found is not None else None


def ask_current(frame):
    """Claude's native question, only when its complete surface is visible.

    This is display state, not write authority. Never fill gaps from a
    transcript: checkbox chips mean answered, background means active, the
    cursor means focused, and [✔] means checked. Submit is not a question.
    """
    rows = _rows(frame)
    footer = _footer(rows)
    if footer is None:
        return None
    stop, _, hints = footer
    if ' '.join(hints) != 'Enter to select Tab/Arrow keys to navigate Esc to cancel':
        return None
    chips = [(i, re.fullmatch(r'\s*←\s+(.+?)\s+✔ Submit\s+→\s*', text))
             for i, (text, _) in enumerate(rows[:stop])]
    chips = [(i, match) for i, match in chips if match]
    if len(chips) != 1:
        return None
    top, chip = chips[0]
    tabs = []
    text, styles = rows[top]
    for match in re.finditer(r'([☐☒])\s+([^☐☒]+?)(?=\s+[☐☒]|$)', chip[1]):
        label = match[2].strip()
        if not label or '…' in label or '...' in label:
            return None
        start, end = chip.start(1) + match.start(), chip.start(1) + match.end()
        tabs.append({'label': label, 'active': _emphasis(styles[start:end]),
                     'answered': match[1] == '☒'})
    active = [i for i, tab in enumerate(tabs) if tab['active']]
    if not 1 <= len(tabs) <= 4 or len(active) != 1:
        return None
    # Require the whole chip row, not a partial label before the first chip.
    if not re.fullmatch(r'(?:[☐☒]\s+[^☐☒]+\s*)+', chip[1]):
        return None
    item_row = re.compile(r'^\s*(?P<cursor>❯\s+)?(?P<id>\d+)\.\s+'
                          r'(?P<text>(?:(?P<box>\[[ ✔]\])\s+)?(?P<label>\S.*))$')
    first = next((i for i in range(top + 1, stop) if item_row.fullmatch(rows[i][0])), None)
    if first is None:
        return None
    question = ' '.join(text.strip() for text, _ in rows[top + 1:first] if text.strip())
    if not question or '…' in question or any(RULE.fullmatch(text) or CHAT.match(text)
                                            for text, _ in rows[top + 1:first] if text.strip()):
        return None
    options, divider = [], False
    for text, _ in rows[first:stop]:
        if not text.strip():
            continue
        if RULE.fullmatch(text):
            divider = True
            continue
        if match := item_row.fullmatch(text):
            if divider and match['label'] != 'Chat about this':
                return None
            option = {'id': int(match['id']), 'label': match['label'], 'text': match['text'],
                      'description': '', 'selected': bool(match['cursor']), 'kind': 'option'}
            if match['box']:
                option['checked'] = match['box'] == '[✔]'
            options.append(option)
        elif options and len(text) - len(text.lstrip()) >= 5 and not divider:
            # Native multiselect draws an unnumbered Next below the free field.
            if text.strip() != 'Next' or options[-1]['label'] != 'Type something':
                options[-1]['description'] = ' '.join(filter(None, (options[-1]['description'], text.strip())))
        else:
            return None
    if (len(options) < 4 or not divider or options[-1]['label'] != 'Chat about this'
            or [o['id'] for o in options] != list(range(1, len(options) + 1))
            or sum(o['selected'] for o in options) != 1
            or any('…' in o['label'] or '...' in o['label'] for o in options)):
        return None
    multi = 'checked' in options[0]
    if any(('checked' in o) != multi for o in options[:-1]):
        return None
    options[-2]['kind'], options[-1]['kind'] = 'free', 'chat'
    index = active[0]
    return {'index': index, 'total': len(tabs), 'header': tabs[index]['label'],
            'question': question, 'multi': multi, 'options': options, 'tabs': tabs,
            'keys': [str(o['id']) for o in options[:-2]] +
                    ['Up', 'Down', 'Left', 'Right', 'Tab', 'Enter', 'Escape']}


def cursor_picker(frame, agent):
    """Codex picker display fields; existing Claude display fields stay intact."""
    if agent != 'codex':
        return None
    found = _read(frame, agent)
    if found is None or found['numbered']:
        return None
    choice = found['choice']
    return {'text': found['text'],
            # The detail goes with each row: the UI shows it, and picker_choose checks it before a key.
            'items': [{'text': item['text'], 'selected': item['selected'], **({'detail': item['detail']} if item.get('detail') else {})}
                      for item in choice['items']],
            'more_above': choice['more_above'], 'more_below': choice['more_below'],
            'hint': found['hint']}
