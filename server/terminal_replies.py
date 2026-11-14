"""Codex answers its slash commands (/cd, /pwd, /status ...) on the terminal only; nothing reaches its history file.
After such a command is sent, read what the terminal added and keep it as a chat notice."""
import difflib
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
import time
import uuid
import detection_rules
import paths
from activity import insert_by_time

CACHE = paths.CACHE / 'replies'
LOCK = threading.Lock()
KEEP = 50
MAX_TEXT = 2000
SPINNER = detection_rules.pattern('codex', 'REPLIES_SPINNER')
BOX = detection_rules.pattern('codex', 'REPLIES_BOX')


def key(entry):
    return '%s-%s' % (entry.get('owner_pid'), entry.get('process_start'))


CONTEXT_LINE = detection_rules.pattern('codex', 'REPLIES_CONTEXT')


def is_noise(line):
    text = line.strip()
    return (not text or BOX.match(text) is not None or text.startswith('›')
            or ('Context' in text and ('% left' in text or '% used' in text)) or text.startswith('Tip:') or FOOTER.match(text) is not None or CONTEXT_LINE.match(text) is not None)


PICKER = detection_rules.pattern('codex', 'REPLIES_PICKER')
FOOTER = detection_rules.pattern('codex', 'REPLIES_FOOTER')
ITEM = re.compile(r'^\s*(?:[-*•◦]|\d+[.)])\s')


def unwrap(lines):
    """Join the lines a narrow terminal broke in the middle of a sentence; bullets, numbered items and blank lines stay apart."""
    result = []
    for line in lines:
        text = line.strip()
        if not text:
            result.append('')
        elif result and result[-1] and not ITEM.match(line):
            result[-1] += ('' if text.startswith('/') and '://' in result[-1] else ' ') + text
        else:
            result.append(text)
    return '\n'.join(result).strip()


def output_lines(screen):
    """Exclude the input and its wrapped continuations before diffing terminal-only replies."""
    prompt = False
    lines = []
    for line in screen.splitlines():
        text = line.strip()
        if text.startswith('›'):
            prompt = True
            continue
        if not text or re.match(r'^[•◦*]\s', text) or is_noise(line):
            prompt = False
        if not prompt and not SPINNER.search(text):
            lines.append(line.rstrip())
    return lines


def added_lines(before, after):
    """Lines the terminal gained between two screen captures, minus the prompt box and footer."""
    old, new = output_lines(before), output_lines(after)
    result = []
    for tag, _, _, start, end in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ('insert', 'replace'):
            result += [line for line in new[start:end] if not is_noise(line)]
    return [re.sub(r'^\s*[•◦]\s?', '', line).rstrip() for line in result]


LOGO = re.compile(r'[▀▄█▌▐░▒▓]+')
CLAUDE_LOGO = re.compile(r'[▀▄█▌▐▛▜▝▘▙▟░▒▓]+')


CLAUDE_BANNER_START = detection_rules.pattern('claude', 'BANNER_START')
CLAUDE_BANNER_END = detection_rules.pattern('claude', 'BANNER_END')
AGY_BANNER_END = detection_rules.pattern('antigravity', 'BANNER_END')


def banner_text(agent, screen):
    """What an agent prints when it starts (version, folder, model, tips, notices), as plain text; '' when none is on screen."""
    lines = screen.splitlines()
    if agent == 'codex':
        begin = next((i for i, line in enumerate(lines) if 'OpenAI Codex' in line), None)
        if begin is None:
            return ''
        block = []
        for line in lines[begin:]:
            if line.strip().startswith('›') or ('Context' in line and '·' in line):
                break
            block.append(line.strip().lstrip('>_').strip() if not block else line.strip())
    elif agent == 'antigravity':
        begin = next((i for i, line in enumerate(lines) if 'Antigravity CLI' in line), None)
        if begin is None:
            return ''
        block, previous_boxed = [], False
        for line in lines[begin:]:
            if AGY_BANNER_END.match(line):
                break
            boxed = line.strip().startswith('┃')
            text = re.sub(r'^\s*┃\s?', '', LOGO.sub(' ', line)).strip()
            if boxed and block and block[-1] and not previous_boxed:
                block.append('')
            previous_boxed = boxed
            if text and '@' not in text and text != 'esc to dismiss':
                block.append(text)
    elif agent == 'claude':
        begin = next((i for i, line in enumerate(lines) if CLAUDE_BANNER_START.search(line)), None)
        if begin is None:
            return ''
        block = []
        for line in lines[begin:begin + 12]:
            if CLAUDE_BANNER_END.match(line):
                break
            block.append(re.sub(r'^\s*▎\s?', '', CLAUDE_LOGO.sub(' ', line)).strip())
    else:
        return ''
    paragraphs, current = [], []
    for line in block + ['']:
        if line:
            current.append(line)
        elif current:
            wrapped = current[0].startswith('Tip') or len(current) > 3  # narrow panes wrap prose; short blocks are header lines
            paragraphs.append(''.join((' ' if index and not part.startswith('/') else '') + part for index, part in enumerate(current))
                              if wrapped else '\n'.join(current))
            current = []
    return '\n'.join(paragraphs)


def started_iso(entry):
    from agents import started_ms
    try:
        return datetime.fromtimestamp(started_ms(entry['process_start']) / 1000, timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


MISSED = {}  # process key -> when its banner was last looked for and not found
RETRY = 30


def capture_banner(entry, read):
    """Keep the start-up banner as the first chat notice of this agent process, once; it is on the terminal only."""
    if entry.get('agent') not in ('codex', 'antigravity', 'claude') or not entry.get('process_start'):
        return
    if time.monotonic() - MISSED.get(key(entry), -RETRY) < RETRY or any(n['id'] == 'terminal:banner' for n in saved(entry)):
        return  # a banner that has scrolled away is not worth a 500-line capture on every update
    try:
        text = banner_text(entry['agent'], read(entry))
    except Exception:
        return
    if not text:
        MISSED[key(entry)] = time.monotonic()
    else:
        notice = {'id': 'terminal:banner', 'role': 'assistant', 'kind': 'banner', 'text': text[:MAX_TEXT], 'time': started_iso(entry)}
        with LOCK:
            notices = [n for n in saved(entry) if n['id'] != 'terminal:banner'] + [notice]
            CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = CACHE / (key(entry) + '.json')
            temporary = CACHE / (key(entry) + '.' + str(os.getpid()) + '.tmp')
            temporary.write_text(json.dumps(notices[-KEEP:]))
            temporary.chmod(0o600)
            temporary.replace(target)


def saved(entry):
    try:
        value = json.loads((CACHE / (key(entry) + '.json')).read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def add(entry, text):
    notice = {'id': 'terminal:' + uuid.uuid4().hex[:12], 'role': 'assistant', 'kind': 'notice', 'text': text[:MAX_TEXT],
              'time': datetime.now(timezone.utc).isoformat()}
    with LOCK:
        notices = (saved(entry) + [notice])[-KEEP:]
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = CACHE / (key(entry) + '.json')
        temporary = CACHE / (key(entry) + '.' + str(os.getpid()) + '.tmp')
        temporary.write_text(json.dumps(notices))
        temporary.chmod(0o600)
        temporary.replace(target)
    return notice


SIDE_MAX = 60000


def add_side(entry, question, answer):
    """Keep a /btw side question and its answer in the chat. Claude Code writes them to no file."""
    ident = 'terminal:side:' + hashlib.sha256((question + '\0' + answer).encode()).hexdigest()[:12]
    notice = {'id': ident, 'role': 'assistant', 'kind': 'side', 'title': question[:MAX_TEXT], 'text': answer[:SIDE_MAX],
              'time': datetime.now(timezone.utc).isoformat()}
    name = side_key(entry)
    with LOCK:
        old = saved_side(entry)
        if any(item.get('id') == ident for item in old):
            return None
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = CACHE / (name + '.json')
        temporary = CACHE / (name + '.' + str(os.getpid()) + '.tmp')
        temporary.write_text(json.dumps((old + [notice])[-KEEP:]))
        temporary.chmod(0o600)
        temporary.replace(target)
    return notice


def side_key(entry):
    """Side answers belong to the conversation: a resumed chat keeps them, and a new chat in the same process does not."""
    thread = entry.get('thread')
    return 'side-' + (hashlib.sha256(str(thread).encode()).hexdigest()[:32] if thread else key(entry))


def saved_side(entry):
    try:
        value = json.loads((CACHE / (side_key(entry) + '.json')).read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def record(entry, before, read, sleep=time.sleep, polls=8):
    """Wait for the terminal to settle after a slash command, then keep whatever it printed."""
    previous, after = None, before
    for poll in range(polls):
        sleep(0.4)
        try:
            after = read(entry)
        except Exception:
            return None
        if after == previous and poll >= 1:
            break
        previous = after
    if PICKER.search(after) or SPINNER.search(after):  # menus and active turns are not terminal-only replies
        return None
    lines = added_lines(before, after)
    text = unwrap(lines)
    return add(entry, text) if text else None


def merge(entry, messages):
    """Put saved terminal replies among the chat messages by the time they were captured."""
    notices = saved(entry)
    if entry.get('agent') == 'codex':
        # Older captures mixed a wrapped input and the live spinner into slash replies.
        notices = [notice for notice in notices if notice.get('id') == 'terminal:banner' or not SPINNER.search(notice.get('text', ''))]
    if entry.get('agent') == 'claude':
        notices = notices + saved_side(entry)
    if not notices:
        return messages
    # saved before banners had their own, quieter style
    return insert_by_time(messages, [dict(n, kind='banner') if n['id'] == 'terminal:banner' else n for n in notices])
