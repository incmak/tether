"""Claude Code's /btw side question, shown as a Tether card.

Claude Code draws the question and its answer in a terminal overlay only: no transcript record and no hook
carries them (checked on 2.1.325). The overlay offers "c to copy", which puts the answer's raw Markdown in a
tmux buffer. Tether presses that key once per answer, reads the new buffer, and removes it. The chat keeps the
question and answer after the overlay closes.
"""
import hashlib
import json
import re
import subprocess
import threading
import time

import detection_rules
import paths

RULE = detection_rules.pattern('claude', 'SIDE_RULE')
QUESTION = detection_rules.pattern('claude', 'SIDE_QUESTION', groups=1)
FOOTER = detection_rules.pattern('claude', 'SIDE_FOOTER')
COPY = detection_rules.pattern('claude', 'SIDE_COPY')
FORK = detection_rules.pattern('claude', 'SIDE_FORK')

GRACE = 10       # seconds a closed overlay's state stays, so a redraw does not fetch the answer again
RETRY = 2        # seconds between attempts to fetch an answer
ATTEMPTS = 3
WAIT = 2.0       # seconds to wait for Claude's buffer after the copy key
HISTORY_TAIL = 262144
MAX_ANSWER = 200000

# server.py sets this to its SEND_LOCK: the copy key must not interleave with a message that Tether types.
SEND_LOCK = threading.Lock()
BOLD = re.compile(r'\x1b\[(?:[0-9;:]*;)?1(?:;[0-9;:]*)?m')
LOCK = threading.Lock()
_seen = {}       # pane id -> state of the overlay that pane shows


def detect(screen, styled=None):
    """The overlay on screen, or None. `text` is the answer as the terminal drew it (the fallback body).

    The overlay lists the earlier questions of this session above the answer. The bold row is the one the answer
    belongs to, so `styled` (the same frame with its colours) is needed to tell them apart.
    """
    lines = screen.rstrip().splitlines()
    rule = next((index for index in range(len(lines) - 1, -1, -1) if RULE.match(lines[index])), None)
    if rule is None:
        return None
    # A narrow pane wraps the key hints. They are the last rows, after a blank row.
    start = len(lines)
    while start > rule + 1 and lines[start - 1].strip() and len(lines) - start < 4:
        start -= 1
    footer = ' '.join(' '.join(lines[start:]).split())
    if not FOOTER.search(footer):
        return None
    body = lines[rule + 1:start]
    first = next((index for index, line in enumerate(body) if line.strip()), None)
    if first is None or not QUESTION.match(body[first]):
        return None
    end = first
    while end < len(body) and QUESTION.match(body[end]):
        end += 1
    questions = [' '.join(QUESTION.match(line).group(1).split()) for line in body[first:end]]
    current = len(questions) - 1
    rows = styled.rstrip().splitlines() if styled else []
    if len(questions) > 1 and len(rows) == len(lines):
        bold = [index for index in range(len(questions)) if BOLD.search(rows[rule + 1 + first + index])]
        if len(bold) == 1:
            current = bold[0]
    answer = body[end:]
    width = min((len(line) - len(line.lstrip()) for line in answer if line.strip()), default=0)
    text = '\n'.join(line[width:].rstrip() for line in answer).strip('\n')
    done = bool(COPY.search(footer))
    return {'shown': questions[current], 'done': done, 'text': text if done else '', 'fork': bool(FORK.search(footer))}


def full_question(entry, shown, history=None):
    """The whole question from Claude Code's input history; the overlay cuts a long one short with an ellipsis."""
    prefix = shown[:-1].rstrip() if shown.endswith('…') else shown
    try:
        with open(history or paths.CLAUDE / 'history.jsonl', 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - HISTORY_TAIL))
            rows = stream.read().decode(errors='ignore').splitlines()
    except OSError:
        return prefix
    for row in reversed(rows):
        if '/btw' not in row:
            continue
        try:
            record = json.loads(row)
        except ValueError:
            continue
        display = record.get('display')
        if (isinstance(display, str) and record.get('sessionId') == entry.get('thread')
                and (match := re.match(r'\s*/btw\s+(.*)', display, re.S))
                and ' '.join(match.group(1).split()).startswith(prefix)):
            return match.group(1).strip()
    return prefix


def _letters(text):
    return re.sub(r'[\W_]+', '', text)


def matches(markdown, drawn):
    """True when the copied Markdown is the answer on screen. Another pane's copy can land in the same moment.

    The terminal wraps words and redraws a table as rows, so compare words, not lines.
    """
    seen = _letters(drawn)
    words = re.findall(r'[^\W_]{4,}', markdown[:600])[:6]
    if not words:
        return bool(_letters(markdown)) and _letters(markdown)[:12] in seen
    return sum(word in seen for word in words) * 2 >= len(words)


def _buffers(run):
    result = run([paths.TMUX, 'list-buffers', '-F', '#{buffer_name}\t#{buffer_created}'],
                 check=False, capture_output=True, text=True, timeout=5)
    return set(result.stdout.splitlines()) if result.returncode == 0 else set()


def copy_answer(entry, shown, run=subprocess.run, sleep=time.sleep, capture=None, verify=None):
    """Press the overlay's copy key and return the raw Markdown it put in a tmux buffer, or None."""
    import terminal_input as terminal
    capture = capture or terminal.capture
    verify = verify or terminal.verify
    with SEND_LOCK:
        verify(entry, run)
        frame = capture(entry, run, styled=True)
        found = detect(terminal.prompt_actions.plain(frame), frame)
        if not found or not found['done'] or found['shown'] != shown:
            return None
        before = _buffers(run)
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'c'], check=True, capture_output=True, timeout=5)
        made, waited = set(), 0.0
        while waited < WAIT:
            sleep(0.1)
            waited += 0.1
            now = _buffers(run) - before
            if now and now == made:  # Claude sets the clipboard twice (a tmux command and an escape sequence)
                break
            made = now
        answer = None
        for row in sorted(made):
            name = row.split('\t')[0]
            read = run([paths.TMUX, 'show-buffer', '-b', name], check=False, capture_output=True, text=True, timeout=5)
            ours = read.returncode == 0 and matches(read.stdout, found['text'])
            if ours:
                answer = answer or read.stdout.strip('\n')[:MAX_ANSWER]
                # Remove only the buffers this key made. The user's own buffers stay.
                run([paths.TMUX, 'delete-buffer', '-b', name], check=False, capture_output=True, timeout=5)
        return answer


def _fetch(entry, state, copy, save):
    try:
        answer = copy(entry, state['shown'])
    except (OSError, RuntimeError, subprocess.SubprocessError):
        answer = None
    with LOCK:
        state['fetching'] = False
        if answer:
            state['answer'] = answer
    if answer:
        try:
            save(entry, state['question'], answer)
        except OSError:
            pass


def observe(entry, screen, styled=None, now=None, copy=copy_answer, save=None, start=None):
    """The attention card for an open /btw overlay, or None. Starts one background fetch per answer."""
    if entry.get('agent') != 'claude':
        return None
    found = detect(screen, styled)
    key = entry['id']
    if not found and key not in _seen:
        return None  # the usual case: no clock read, no lock
    now = time.monotonic() if now is None else now
    identity = (entry.get('owner_pid'), entry.get('process_start'), entry.get('thread'))
    with LOCK:
        state = _seen.get(key)
        if not found:
            if state and now - state['last'] >= GRACE:
                _seen.pop(key, None)
            return None
        if not state or state['identity'] != identity or state['shown'] != found['shown']:
            state = {'identity': identity, 'shown': found['shown'], 'question': full_question(entry, found['shown']),
                     'answer': None, 'fetching': False, 'tries': 0, 'tried': -RETRY, 'last': now}
            _seen[key] = state
            while len(_seen) > 64:
                _seen.pop(next(iter(_seen)))
        state['last'] = now
        fetch = (found['done'] and not state['answer'] and not state['fetching'] and entry.get('writable')
                 and state['tries'] < ATTEMPTS and now - state['tried'] >= RETRY)
        if fetch:
            state.update(fetching=True, tries=state['tries'] + 1, tried=now)
        answer = state['answer']
        coming = found['done'] and not answer and bool(entry.get('writable')) and (state['fetching'] or state['tries'] < ATTEMPTS)
        question = state['question']
    if fetch:
        if save is None:
            import terminal_replies
            save = terminal_replies.add_side
        run = start or (lambda work: threading.Thread(target=work, daemon=True).start())
        run(lambda: _fetch(entry, state, copy, save))
    body = answer or found['text']
    card = {'id': hashlib.sha256(('side' + question).encode()).hexdigest(), 'reason': 'side', 'requiresResponse': False,
            'question': question, 'text': '' if coming else body, 'pending': not found['done'] or coming, 'markdown': bool(answer),
            'fork': found['done'] and found['fork']}
    return card


def open_on(entry, run=subprocess.run):
    """True when the pane shows a finished side answer that offers the fork key."""
    import terminal_input as terminal
    frame = terminal.capture(entry, run, styled=True)
    found = detect(terminal.prompt_actions.plain(frame), frame)
    return bool(found and found['done'] and found['fork'])
