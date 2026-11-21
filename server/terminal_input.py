"""Guarded input and visible permission prompts for live agent tmux panes."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import shlex
import sqlite3
import subprocess
import time
import uuid

from antigravity import process_start, ROOT as AGY_ROOT, CACHE as AGY_CACHE, capture_notices
from quota_notices import capture as capture_quota_notices
from codex_provider import PROFILE_NAME
import detection_rules
import groups
import login_flow
import paths
import procinfo
import window_size
import prompt_actions
import choice_state
import claude_hooks
import permissions_dialog
import uploads

LAUNCHERS = {'codex': 'codex',
             'claude': 'claude',
             'antigravity': 'agy'}


# tmux allows spaces in session names. It turns . and : into _, so they are refused here instead.
SESSION_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9_-])?')
NAME_HELP = 'Use letters, numbers, spaces, - or _ (up to 64), starting with a letter or number.'


def valid_name(name):
    return isinstance(name, str) and bool(SESSION_NAME.fullmatch(name))


def create_session(name, cwd, run=subprocess.run):
    if not valid_name(name):
        raise ValueError(NAME_HELP)
    result = run([*paths.tmux_new_session(), 'new-session', '-d', '-s', name, '-c', cwd, '-P', '-F', '#{pane_id}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('A session with that name already exists.' if 'duplicate session' in result.stderr
                           else 'Could not create the tmux session.')
    pane = result.stdout.strip()
    if not re.fullmatch(r'%\d+', pane):
        raise RuntimeError('Session creation could not be confirmed. Refresh the tabs before retrying.')
    return pane


def resume_command(identifier, *, agent='claude', profile=None):
    """Build a resume command only from a validated agent, UUID, and profile."""
    if not isinstance(identifier, str) or not UUID.fullmatch(identifier.lower()):
        raise ValueError('Choose a valid chat id.')
    if agent not in ('claude', 'codex'):
        raise ValueError('Choose Claude or Codex.')
    if profile is not None and (not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile)):
        raise ValueError('Choose a supported Codex provider.')
    if agent == 'claude':
        return 'claude --resume ' + identifier
    return 'codex' + (f' -p {shlex.quote(profile)}' if profile is not None else '') + ' resume ' + identifier


def resume_session(name, cwd, identifier, run=subprocess.run, *, agent='claude', profile=None):
    command = resume_command(identifier, agent=agent, profile=profile)
    pane = create_session(name, cwd, run=run)
    # The pty buffers input even while the new shell starts.
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command],
        check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'],
        check=True, capture_output=True, timeout=5)
    return pane


UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def fork_name(entry, entries):
    """The fork's name: the chat's name (agent, then pane, then tmux session) plus the next free number in that session."""
    base = ' '.join((entry.get('agent_name') or entry.get('pane_name') or entry.get('label') or 'chat').split())
    base = re.sub(r' \d+$', '', base)[:34].strip() or 'chat'
    taken = {other.get('agent_name') or other.get('pane_name') for other in entries if other.get('tmux_session') == entry.get('tmux_session')}
    number = 2
    while f'{base} {number}' in taken:
        number += 1
    return f'{base} {number}'


def new_window_after(pane, run=subprocess.run, *, cwd=None, group=None, by_user=False):
    """Open after the source window, preserving its directory and manual size. The new pane joins the source pane's
    group (@group, groups.py), unless group is given: "" puts it in Unstacked, a group id in that group.
    by_user marks the placement before launch and tries to remove the new pane if marking fails."""
    source = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{window_id}\t#{pane_current_path}\t#{@group}'],
                 capture_output=True, text=True, timeout=5)
    fields = source.stdout.rstrip('\n').split('\t', 1)
    if len(fields) == 2 and '\t' in fields[1]:
        fields[1], label = fields[1].rsplit('\t', 1)
    else:
        label = ''
    if source.returncode or len(fields) != 2 or not re.fullmatch(r'@[0-9]+', fields[0]) or not fields[1]:
        raise RuntimeError(source.stderr.strip() or 'Could not split the pane.')
    window, folder = fields
    label = label if group is None else group
    size = run([paths.TMUX, 'display-message', '-p', '-t', window,
                '#{window-size}\t#{window_width}\t#{window_height}'],
               capture_output=True, text=True, timeout=5)
    dimensions = size.stdout.strip().split('\t')
    if (size.returncode or len(dimensions) != 3
            or any(not re.fullmatch(r'[1-9][0-9]*', value) for value in dimensions[1:])):
        raise RuntimeError(size.stderr.strip() or 'Could not split the pane.')
    result = run([paths.TMUX, 'new-window', '-a', '-t', window, '-c', folder if cwd is None else cwd,
                  '-P', '-F', '#{pane_id}'], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Could not split the pane.')
    new_pane = result.stdout.strip()
    if not re.fullmatch(r'%[0-9]+', new_pane):
        raise RuntimeError('Pane creation could not be confirmed.')
    set_option = ['set-option', '-p', '-t', new_pane]
    options = [*set_option, '@group', label] if re.fullmatch(r'g[0-9a-f]{8}', label or '') else []
    if by_user:
        if options:
            options.append(';')
        options.extend([*set_option, groups.BY, 'user'])
    if options:  # before any launch command is sent
        try:
            placed = run([paths.TMUX, *options], capture_output=True, text=True, timeout=5)
            if by_user and placed.returncode:
                raise RuntimeError(placed.stderr.strip() or 'Could not mark the new tab as user-placed.')
        except (OSError, RuntimeError, subprocess.SubprocessError):
            if by_user:
                try:
                    run([paths.TMUX, 'kill-pane', '-t', new_pane], capture_output=True, text=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass  # Report the marking failure even if cleanup also fails.
            raise
    if dimensions[0] == 'manual':
        run([paths.TMUX, 'resize-window', '-t', new_pane, '-x', dimensions[1], '-y', dimensions[2]],
            check=True, capture_output=True, text=True, timeout=5)
    return new_pane


def fork_chat(entry, entries, cwd, run=subprocess.run):
    """Open a new window in the pane's tmux session with a copy of its chat: `claude --resume ID --fork-session` or `codex fork ID`.
    The original chat is not touched. Returns (pane, name)."""
    agent, thread = entry.get('agent'), entry.get('thread')
    if agent not in ('claude', 'codex'):
        raise ValueError('Only Claude and Codex chats can be copied to a new chat.')
    if not isinstance(thread, str) or not UUID.fullmatch(thread) or entry.get('pending_conversation'):
        raise ValueError('Send a first message in this chat, then try again.')
    name = fork_name(entry, entries)
    try:
        pane = new_window_after(entry['id'], run, cwd=cwd)
    except RuntimeError as error:
        raise RuntimeError('Could not open a new window in this tmux session.') from error
    # A copy keeps the provider of the chat it copies (codex -p foundry), whatever new panes use now.
    profile = entry.get('codex_profile') if agent == 'codex' else None
    command = (f"claude --resume {thread} --fork-session --name {shlex.quote(name)}" if agent == 'claude'
               else f"codex fork -p {profile} {thread}" if isinstance(profile, str) and PROFILE_NAME.fullmatch(profile)
               else f"codex fork {thread}")
    run([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', name, ';', 'set-option', '-p', '-t', pane, '@chat_name_at', str(int(time.time() * 1000))],
        check=True, capture_output=True, timeout=5)
    # The pty buffers input even while the new shell starts.
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'], check=True, capture_output=True, timeout=5)
    return pane, name


# OSC sequences such as terminal links (ESC ]8;id=…;file:///… ESC \\). A styled capture keeps them; only colors are wanted.
OSC = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


def capture(entry, run=subprocess.run, styled=False):
    text = run([paths.TMUX, 'capture-pane', *(['-e'] if styled else []), '-p', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout
    return OSC.sub('', text) if styled else text


def capture_screen(entry, lines=80, run=subprocess.run):
    """The last `lines` lines of the pane (scrollback included), for the read-only live screen view."""
    lines = max(20, min(int(lines), 500))
    text = run([paths.TMUX, 'capture-pane', '-p', '-t', entry['id'], '-S', '-' + str(lines)],
               check=True, capture_output=True, text=True, timeout=5).stdout
    return '\n'.join(line.rstrip() for line in text.rstrip().splitlines()[-lines:])[-200000:]


COUNTDOWN = re.compile(r'(block\s+this\s+request\s+in\s+)(?:\d+[:hms.]?\s?)+', re.I)
DIALOG_FRAME = detection_rules.pattern('common', 'DIALOG_FRAME')
APPROVAL_QUESTION = detection_rules.pattern('common', 'APPROVAL_QUESTION')
APPROVAL_REJECT = re.compile(r'(?:No\b|Reject\b|Cancel\b|Exit\b|Don.t trust\b|Do not trust\b)', re.I)


def joined_lines(lines):
    """Fold physical rows for phrase detection; keep the original rows for identities and labels."""
    return ' '.join(line.strip() for line in lines)




def approval(screen):
    # Expose the native chooser; every submitted choice requires an explicit
    # user selection tied to this exact request. Never auto-accept a default.
    lines = ANSI_SGR.sub('', screen).splitlines()
    questions = []
    for i, line in enumerate(lines):
        if not line.strip() or RULE_LINE.match(line) or NUMBERED_ITEM.match(line):
            continue
        paragraph = []
        for row in lines[i:i + 8]:
            if not row.strip() or RULE_LINE.match(row) or NUMBERED_ITEM.match(row):
                break
            paragraph.append(re.sub(r'^\s*[│┃╭╰┌└]\s*', '', row))
        if APPROVAL_QUESTION.search(joined_lines(paragraph)):
            questions.append((i, joined_lines(paragraph)))
    if not questions:
        return None
    start, question = questions[-1]
    trust = bool(re.search(r'Quick safety check|trust.*(?:folder|directory|project)|(?:folder|directory|project).*trust',
                           question, re.I))
    choices = []
    choice_numbers = []
    for i, line in enumerate(lines[start + 1:], start + 1):
        match = re.match(r'^\s*([>❯›]?)\s*(\d+)\.\s+(.+)', line)
        if match:
            choices.append((i, bool(match[1]), match[3]))
            choice_numbers.append(int(match[2]))
        elif trust and (match := re.match(r'^\s*([>❯›]?)\s*((?:Yes|No|Trust|Don.t trust|Do not trust|Exit)\b[^?]*)$', line)):  # capitalised, and not a wrapped question line
            choices.append((i, bool(match[1]), match[2]))
            choice_numbers.append(len(choices))
    if not choices or not any(APPROVAL_REJECT.match(c[2]) for c in choices):
        return None
    selected = next((i for i, c in enumerate(choices) if c[1]), None)
    # Fingerprint includes the visible tool request, independent of cursor position.
    preceding = [i for i in range(start) if re.search(r'^\s*(?:Requesting permission for:|Command|\$ )', lines[i])]
    # Claude draws its dialog under a full rule: start there, so chat history above the dialog stays out of the card.
    tops = [i for i in range(max(0, start - 15), start) if re.match(r'^\s*\u2500{10,}\s*$', lines[i])]
    begin = preceding[-1] if preceding else tops[-1] + 1 if tops else max(0, start - 15)
    # The card shows the request, not the dialog frame: drop rule lines and the generic question (the card title says it).
    shown = [line for line in lines[begin:choices[0][0]] if not DIALOG_FRAME.match(line)]
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(shown)).strip()
    options = []
    numbers = []
    last = choices[-1][0] + 1
    for index, (line, _, label) in enumerate(choices):
        number = choice_numbers[index]
        numbers.append(number)
        end = choices[index + 1][0] if index + 1 < len(choices) else len(lines)
        continuation = []
        for at in range(line + 1, end):
            part = lines[at]
            if not part.strip() or RULE_LINE.match(part) or INPUT_HINT.search(part) or re.match(r'^\s*(?:Enter|Esc)\b', part, re.I):
                break
            continuation.append(part.strip())
            if index == len(choices) - 1:
                last = at + 1
        options.append({'id': number, 'text': ' '.join([label, *continuation])})
    if any(re.match(r'^\s*[>❯›](?:\s|$)', line) and not NUMBERED_ITEM.match(line)
           or CHAT_LINE.match(line) for line in lines[last:]):
        return None  # a new input box or agent response makes the old chooser history
    # Only a complete, known one-time label can back the legacy "approve once"
    # action. A cut "Yes, and don't ask again" must never become an implicit Yes.
    once = next((i for i, option in enumerate(options) if re.fullmatch(
        r'Yes(?:,\s*(?:proceed|allow once|allow this (?:time|request)))?(?:\s*\(y\))?\.?', option['text'], re.I)), None)
    full_text = '\n'.join(lines[begin:last]).strip()
    canonical = re.sub(r'(?m)^\s*[>❯›]?\s*(\d+)\.\s+', r'\1. ', full_text)
    if trust:
        canonical = re.sub(r'(?m)^[ \t]*[>❯›]?[ \t]*((?:Yes|No|Trust|Don.t trust|Do not trust|Exit)\b[^?\n]*)$', r'\1', full_text)
    # Claude Code counts down before it blocks a request in an unattended session ("... block this request in 0:57, ...").
    # The time changes each second, so it stays out of the id: otherwise every tap would find a "changed" approval.
    canonical = COUNTDOWN.sub(r'\1-', canonical)
    return {'id': hashlib.sha256(canonical.encode()).hexdigest(), 'text': text,
            'actionable': sum(c[1] for c in choices) == 1 and numbers == list(range(1, len(numbers) + 1)), 'options': options,
            '_numbers': numbers,
            '_selected': selected, '_once': once,
            '_reject': next(i for i, c in enumerate(choices) if APPROVAL_REJECT.match(c[2]))}


PLAN_MENU_OPTION = detection_rules.pattern('claude', 'PLAN_MENU_OPTION')


def plan_menu(pending):
    """Recognize the exit-plan chooser without mistaking a command approval for a plan."""
    options = (pending or {}).get('options') or []
    return (any(PLAN_MENU_OPTION.search(option['text']) for option in options)
            and any(re.match(r'Yes\b', option['text'], re.I) for option in options))


ATTENTION_DELAY = 3
ATTENTION_GRACE = 10
_attention_seen = {}
NUMBERED_ITEM = detection_rules.pattern('common', 'NUMBERED_ITEM')
CURSOR_ITEM = detection_rules.pattern('common', 'CURSOR_ITEM')
# Chat output that an open menu never has below its cursor row: an agent line (● Checking…), the working spinner, a tool result.
CHAT_LINE = detection_rules.pattern('common', 'CHAT_LINE')


def cursor_in_menu(tail, marker):
    """True when a numbered row with a cursor is part of an open menu. A sent message that starts with a number
    ("❯ 1. Do what is compliant") looks the same in the chat history, but the chat goes on below it: agent lines,
    the spinner, or the empty input box. Below a real cursor row there are only options, details and key hints."""
    rows = [i for i, line in enumerate(tail) if CURSOR_ITEM.match(line)]
    if not rows:
        return False
    # A long option wraps in a narrow pane, and a wrapped line can start with "·" ("· API usage billing"). It sits at
    # or right of the option text. Agent lines and the spinner start left of it.
    label = re.match(r'^\s*[>❯›]\s*\d+[.)]\s+', tail[rows[-1]]).end()
    for line in tail[rows[-1] + 1:]:
        if CHAT_LINE.match(line) and len(line) - len(line.lstrip()) < label:
            return False
        if marker and line.startswith(marker) and not NUMBERED_ITEM.match(line):
            return False  # the input box: "❯ " with or without typed text
    return True
# Claude's /usage, /status, /config and /stats share one Settings dialog. /usage grows past a short pane once its scan ends,
# and then its "Esc to cancel" footer is off screen: the tab row at its top still shows it.
SETTINGS_TABS = detection_rules.pattern('claude', 'SETTINGS_TABS')
INPUT_HINT = detection_rules.pattern('common', 'INPUT_HINT')


RULE_LINE = detection_rules.pattern('common', 'RULE_LINE')
PICKER_CURSOR = detection_rules.pattern('common', 'PICKER_CURSOR', groups=1)
PICKER_MORE = detection_rules.pattern('common', 'PICKER_MORE', groups=2)
PICKER_HINT = detection_rules.pattern('common', 'PICKER_HINT')


PICKER_HINT_START = detection_rules.pattern('common', 'PICKER_HINT_START')
PANEL_ROWS = 30  # how far above its list a picker panel's own rule may be


def cursor_picker(screen):
    """Read an unnumbered cursor picker, bounded by its header and Enter/Esc footer."""
    lines = ANSI_SGR.sub('', screen).rstrip().splitlines()
    # A narrow pane can wrap the footer. Stop as soon as both key instructions appear.
    hint = None
    for index in range(len(lines) - 1, -1, -1):
        if not PICKER_HINT_START.match(lines[index]):
            continue
        for end in range(index + 1, min(index + 6, len(lines)) + 1):
            text = joined_lines(lines[index:end])
            if PICKER_HINT.search(text):
                hint = (index, end, text)
                break
        if hint:
            break
    if hint is None:
        return None
    index, end, hint_text = hint
    # An old picker in chat history must not hide the current input box or reply.
    if any(re.match(r'^\s*[❯›]', line) or CHAT_LINE.match(line) for line in lines[end:]):
        return None
    cursor = next((i for i in range(index - 1, -1, -1) if PICKER_CURSOR.match(lines[i])), None)
    if cursor is None or NUMBERED_ITEM.match(lines[cursor]):
        return None
    if any(CHAT_LINE.match(line) for line in lines[cursor + 1:index]):
        return None
    # Rows align with the cursor's label. A scroll count also marks the list's start.
    column = PICKER_CURSOR.match(lines[cursor]).start(1)
    first = cursor
    for i in range(cursor - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        if PICKER_MORE.match(line):
            first = i
            break
        if RULE_LINE.match(line) or len(line) - len(line.lstrip()) < column:
            break
        first = i
    # The paragraph directly above the list is the panel header. Never take the
    # screen tail: it can include the conversation above this paragraph.
    top = first - 1
    while top >= 0 and not lines[top].strip():
        top -= 1
    bottom = top + 1
    while top >= 0 and lines[top].strip() and not RULE_LINE.match(lines[top]):
        top -= 1
    # A panel under its own rule can hold several paragraphs: the question first, a security note last ("Allow external
    # CLAUDE.md file imports?", Claude Code 2.1.325). Take them all when only blank and plain lines lie between.
    drawn = lambda line: line.strip() and RULE_LINE.match(line)  # RULE_LINE also matches a blank line
    rule = top
    while rule >= max(0, first - PANEL_ROWS) and not drawn(lines[rule]):
        if CHAT_LINE.match(lines[rule]) or re.match(r'^\s*[❯›]', lines[rule]):
            break
        rule -= 1
    if rule >= max(0, first - PANEL_ROWS) and drawn(lines[rule]):
        top = rule
    paragraphs = [[]]
    for line in lines[top + 1:bottom]:
        if line.strip():
            paragraphs[-1].append(line.strip())
        elif paragraphs[-1]:
            paragraphs.append([])
    header = [line for paragraph in paragraphs for line in paragraph]
    if not header or any(CHAT_LINE.match(line) or re.match(r'^[❯›]', line) for line in header):
        return None
    subtitle = '\n'.join(filter(None, [' '.join(paragraphs[0][1:])] + [' '.join(part) for part in paragraphs[1:] if part]))
    items, counts = [], {'↑': 0, '↓': 0}
    for line in lines[first:index]:
        if not line.strip() or RULE_LINE.match(line):
            continue
        if match := PICKER_MORE.match(line):
            counts[match[1]] = int(match[2])
        elif NUMBERED_ITEM.match(line):
            return None  # numbered menus keep their existing parser and response
        else:
            match = PICKER_CURSOR.match(line)
            items.append({'text': match[1].strip() if match else line.strip(), 'selected': bool(match)})
    if not items or sum(item['selected'] for item in items) != 1:
        return None
    return {'title': header[0], 'subtitle': subtitle, 'items': items,
            'more_above': counts['↑'], 'more_below': counts['↓'], 'hint': hint_text,
            'text': '\n'.join(lines[top + 1:end]).strip()}


def menu_lines(lines, limit=80):
    """The lines of the menu at the bottom of the screen. Long options in a narrow pane push the question above
    the last 30 lines, so start at the rule line Claude draws above the question (at most `limit` lines up)."""
    lines = lines[-limit:]
    cursor = next((i for i in range(len(lines) - 1, -1, -1) if CURSOR_ITEM.match(lines[i])), None)
    if cursor is None:
        return lines[-30:]
    # Skip the rule between options ("Type something." / "Chat about this"): the top rule has the question below it.
    after = lambda i: next((line for line in lines[i + 1:] if line.strip()), '')
    top = next((i for i in range(cursor - 1, -1, -1) if lines[i].strip() and RULE_LINE.match(lines[i]) and not NUMBERED_ITEM.match(after(i))), None)
    return lines[top + 1:] if top is not None else lines[-30:]


MENU_HINT_START = detection_rules.pattern('common', 'MENU_HINT_START')


def claude_input_box(lines, row):
    """Distinguish a ruled draft from a menu whose key hint follows its closing rule."""
    if row < 0:
        return False
    above = next((line for line in reversed(lines[:row]) if line.strip()), '')
    if not above or not RULE_LINE.match(above):
        return False
    for index in range(row + 1, len(lines)):
        line = lines[index]
        if INPUT_HINT.search(line) or MENU_HINT_START.match(line):
            return False
        if line.strip() and RULE_LINE.match(line):
            return not any(INPUT_HINT.search(after) for after in lines[index + 1:])
    return False


def menu_question(lines, agent=None):
    """Split a numbered terminal menu into its question and options so a phone can show it as a chat card, not a screen dump."""
    first = next((i for i, line in enumerate(lines) if NUMBERED_ITEM.match(line)), None)
    if first is None:
        return None
    clean = lambda line: re.sub(r'^\s*[│┃]\s*|\s*[│┃]\s*$', '', line).strip()
    above, index = [], first - 1
    while index >= 0 and not clean(lines[index]):  # the paragraph right above the options, not the chat before it
        index -= 1
    while index >= 0 and clean(lines[index]) and not RULE_LINE.match(lines[index]):
        above.insert(0, clean(lines[index]))
        index -= 1
    question = re.sub(r'^Question \d+/\d+:\s*', '', ' '.join(above))
    options, ruled = [], False
    for index, line in enumerate(lines[first:], first):
        match = re.match(r'^\s*([>❯›]?)\s*(\d+)[.)]\s+(.*\S)', line)
        if match:
            text = match[3].strip()
            # Codex lays its lists out in columns: "GPT-6.2-Sol (current)  Latest workhorse model ...". The gap of two
            # or more spaces splits the name from its description. Only Codex: Claude's own-answer row holds the user's
            # text as typed, two spaces and all, and menu_choose erases exactly that text before it types a new answer.
            text, column = (re.split(r'\s{2,}', text, maxsplit=1) + [''])[:2] if agent == 'codex' else (text, '')
            # Claude's own-answer row: "Type something." until the user types, then their text. It sits right above
            # the rule line before "Chat about this". With the cursor on it, digit keys type text instead of choosing.
            if options and ruled and text == 'Chat about this':
                options[-1]['free'] = True
            options.append({'id': int(match[2]), 'text': text, 'detail': column.strip(), 'selected': bool(match[1])})
            if text == 'Type something.':
                options[-1]['free'] = True
            ruled = False
        elif options and (INPUT_HINT.search(line) or MENU_HINT_START.match(line)
                          and INPUT_HINT.search(joined_lines(lines[index:index + 3]))):
            break  # the key hint ends the menu; in a narrow pane it wraps, and a named input border follows
        elif options and clean(line) and RULE_LINE.match(line):
            ruled = True
        elif options and clean(line) in ('↓', '↑', '▼', '▲'):
            continue  # a scroll mark: the list goes on past the screen
        elif options and clean(line) and not INPUT_HINT.search(line):
            options[-1]['detail'] = (options[-1]['detail'] + ' ' + clean(line)).strip()
    return {'question': question, 'options': options} if len(options) >= 2 else None


class MenuChanged(RuntimeError):
    pass


def _agent_version(entry):
    version = entry.get('agent_version')
    if entry['agent'] == 'codex' and not version:
        try:
            with open(entry['transcript'], 'rb') as stream:
                first = json.loads(stream.readline(65536))
            if first.get('type') == 'session_meta':
                version = first['payload'].get('cli_version')
        except (KeyError, AttributeError, OSError, TypeError, ValueError):
            pass
    if isinstance(version, str) and re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-+][\w.-]+)?', version):
        return version
    return None


def menu_refusal(entry, *, helper=False, check=False):
    agent = entry['agent']
    version = _agent_version(entry)
    name, captured = {'claude': ('Claude Code', '2.1.316, 2.1.318, 2.1.329 and 2.1.330'),
                      'codex': ('Codex', '0.169.0 and 0.170.0')}.get(agent, (agent, ''))
    if version:
        detail = f' {name} {version} may have changed the screen.'
    elif captured:
        detail = f' A newer {name} version may have changed the screen. Tether knows version {captured}.'
    elif agent == 'antigravity':
        detail = ' The Antigravity version may use an unfamiliar screen.'
    else:
        detail = ''
    where = 'the helper' if helper else 'the terminal'
    action = 'check' if check else 'review'
    return f'The question changed or is no longer open. Open {where} to {action} it.{detail}'


def _open_claude_ask(entry):
    import agents
    try:
        return agents.open_claude_ask(entry)
    except OSError:
        return None


def verified_numbered_menu(entry, frame):
    """Keep numbered display parsing separate from the evidence required to write."""
    if entry['agent'] == 'codex':
        return choice_state.codex_numbered_menu(frame)
    if entry['agent'] != 'claude' or not (current := choice_state.ask_current(frame)):
        return None
    ask = _open_claude_ask(entry)
    if ask is None or len(ask['questions']) != current['total']:
        return None
    expected = ask['questions'][current['index']]
    options = [{'id': item['id'], 'text': item['label'], 'detail': item['description'],
                'selected': item['selected'], 'kind': item['kind'],
                **({'checked': item['checked']} if 'checked' in item else {}),
                **({'free': True} if item['kind'] == 'free' else {})}
               for item in current['options']]
    normalize = lambda text: ' '.join(text.split())
    headers = [normalize(item['header']) for item in ask['questions']]
    visible_headers = [tab['label'] for tab in current['tabs']]
    # At 22 columns a single chip can be clipped to …, while the question and all options are still visible.
    if (expected['multi'] != current['multi']
            or (headers != visible_headers and visible_headers != ['…'])
            or normalize(expected['question']) != current['question']
            or [(normalize(item['label']), normalize(item['description'])) for item in expected['options']]
            != [(item['text'], item['detail']) for item in options[:-2]]):
        return None
    for item, source in zip(options, expected['options']):
        item.update(text=source['label'], detail=source['description'])
    return {'question': expected['question'], 'options': options, 'multi': current['multi'],
            'menu_identity': [current['index'], current['total'], headers[current['index']], ask['id']],
            'control': current['control'], 'ask': ask}


def menu_digest(menu):
    """Bind the question and options, independent of the cursor, checks and free-answer draft."""
    question = menu.get('question')
    options = menu.get('options')
    if not question or not options or [option['id'] for option in options] != list(range(1, len(options) + 1)):
        return None
    content = [(option['id'], option['text'], option.get('detail')) if not option.get('free')
               else (option['id'], None, None) for option in options]
    return hashlib.sha256(json.dumps([question, content, menu.get('menu_identity')]).encode()).hexdigest()


def menu_choose(entry, choice, label=None, answer=None, run=subprocess.run, sleep=time.sleep, *, digest=None,
                current_prompt=None, revision=None):
    """Answer a numbered menu by moving the cursor, not by a digit key. A digit types text when the cursor sits on
    Claude's own-answer row, and then every later tap types more ("344"). So: read the cursor row, press Up or Down
    until it is on the chosen row (checking the screen after each press), then Enter. answer: text for the own-answer row;
    its old text is removed first. With a digest, verify the full prompt and preserve drafts."""
    def press(*keys):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], *keys], check=True, capture_output=True, timeout=5)

    def look(typed=None, *, writing=False):
        if writing:
            verify(entry, run)
        menu = verified_numbered_menu(entry, capture(entry, run, styled=True))
        if not menu or menu.get('multi') or current_prompt is not None and not current_prompt():
            raise MenuChanged(menu_refusal(entry, helper=digest is not None))
        content = menu_digest(menu)
        if expected is not None and expected != content:
            raise MenuChanged(menu_refusal(entry, helper=digest is not None))
        if digest is not None:
            for option in menu['options']:
                if option.get('free'):
                    answer_text = typed if option['id'] == choice and typed is not None else 'Type something.'
                    current = ' '.join((option['text'] + ' ' + option['detail']).split())
                    if current != answer_text:
                        raise DraftError('The free-answer field has different text. Open the helper to check its draft.')
        return menu['options'], content

    expected = digest or revision
    options, expected = look(writing=True)
    target = next((i for i, option in enumerate(options) if option['id'] == choice), None)
    if target is None:
        raise RuntimeError('That option is gone. Reload and review the question again.')
    free = options[target].get('free', False)
    if answer is None and free:
        raise ValueError('Type your answer in the message box, then send it.')
    if answer is not None and not free:
        raise ValueError('This option takes no text.')
    if label is not None and not free and options[target]['text'] != label:
        raise MenuChanged(menu_refusal(entry))
    for _ in range(len(options) * 2 + 2):
        target = next((i for i, option in enumerate(options) if option['id'] == choice), None)
        if target is None or label is not None and not free and options[target]['text'] != label:
            raise MenuChanged(menu_refusal(entry))
        at = next((i for i, option in enumerate(options) if option['selected']), None)
        if at is None:
            raise RuntimeError('Could not find the cursor in the question. Open the terminal to answer it.')
        if at == target:
            break
        press('Down' if at < target else 'Up')
        for _ in range(20):  # up to 2 s for the redraw: a second press before it lands past the row
            sleep(.1)
            options, _ = look()
            moved = next((i for i, option in enumerate(options) if option['selected']), None)
            if moved != at:
                break
        else:
            raise RuntimeError('The cursor did not move. Open the terminal to answer it.')
        if moved is not None and ((moved > target) if at < target else (moved < target)):
            raise RuntimeError('The cursor jumped past that option. Open the terminal to answer it.')
        options, _ = look(writing=True)
    else:
        raise RuntimeError('Could not move to that option. Open the terminal to answer it.')
    if answer is not None:
        old = options[target]['text'] + (' ' + options[target]['detail'] if options[target]['detail'] else '')
        if old != 'Type something.':
            press('End', *['BSpace'] * (len(old) + 2))
            sleep(.2)
            if look()[0][target]['text'] != 'Type something.':
                raise RuntimeError('Could not clear the old answer. Open the terminal to answer it.')
        buffer = 'tmux-mobile-' + uuid.uuid4().hex
        typed = ' '.join(answer.split())
        try:
            run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=typed.encode(), check=True, capture_output=True, timeout=5)
            if not look(writing=True)[0][target]['selected']:
                raise MenuChanged(menu_refusal(entry))
            run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        finally:
            run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)
        sleep(.3)
    if not look(typed if answer is not None else None, writing=True)[0][target]['selected']:
        raise MenuChanged(menu_refusal(entry))
    press('Enter')


def menu_open(agent, lines):
    """A numbered menu waits at the bottom of the screen: two numbered rows, the cursor in the menu, and for Claude
    not a ruled draft that only looks like one. read_picker() uses the same test. attention() also accepts a Claude question that choice_state.ask_current
    reads whole."""
    tail = lines[-30:]
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}.get(agent)
    cursor_row = next((i for i in reversed(range(len(lines))) if CURSOR_ITEM.match(lines[i])), -1)
    return (sum(1 for line in tail if NUMBERED_ITEM.match(line)) >= 2 and cursor_in_menu(tail, marker)
            and not (agent == 'claude' and claude_input_box(lines, cursor_row)))


SGR_COLOR = re.compile(r'\x1b\[(?:[0-9;]*;)?38;5;(\d+)m')


def claude_picker(screen, styled=None):
    """cursor_picker, with each detail line folded into the entry above it. Claude's Rewind draws "No code changes"
    under each point in the same dim color as the key hint, and its cursor never stops on that line. Without the
    styled frame, or with no dim key hint, nothing is folded: picker_choose then refuses a detail row by itself."""
    picker = cursor_picker(screen)
    if not picker or not styled:
        return picker
    rows = styled.rstrip().splitlines()
    hint = next((SGR_COLOR.findall(row) for row in reversed(rows) if 'Esc to' in prompt_actions.plain(row)), None)
    if not hint:
        return picker
    dim = hint[-1]
    def faint(text):
        row = next((row for row in rows if prompt_actions.plain(row).strip() == text), None)
        colors = row is not None and SGR_COLOR.findall(row)
        return bool(colors) and set(colors) == {dim} and not PICKER_CURSOR.match(prompt_actions.plain(row))
    items = []
    for item in picker['items']:
        if items and not item['selected'] and faint(item['text']):
            items[-1]['detail'] = (items[-1].get('detail', '') + ' ' + item['text']).strip()
        else:
            items.append(dict(item))
    return {**picker, 'items': items}


def read_picker(entry, run=subprocess.run, *, styled=None):
    """The cursor list on screen now, in the order attention() checks: a numbered menu, then an unnumbered picker
    (Claude's Rewind, Codex lists). None when neither is open."""
    if styled is None:
        styled = capture(entry, run, styled=True)
    screen = prompt_actions.plain(styled)
    if menu_open(entry['agent'], screen.rstrip().splitlines()):
        menu = menu_question(menu_lines(screen.rstrip().splitlines()), entry['agent'])
        if menu and sum(1 for option in menu['options'] if option['selected']) == 1:
            choice = choice_state.parse(styled, entry['agent'])
            return {'items': menu['options'], 'question': menu['question'], 'numbered': True,
                    'multi': bool(choice and choice['selection'] == 'multi')}
        return None
    if entry['agent'] == 'codex':
        return choice_state.cursor_picker(styled, 'codex')
    if entry['agent'] == 'claude':
        return claude_picker(screen, styled)
    return None


def picker_row_key(item):
    """One row as the user saw it: its text, and its detail line when it has one. Two entries with the same text
    (Rewind points) differ by their detail. The UI builds the same key (pickRowKey in ChoiceExtras.tsx)."""
    detail = item.get('detail')
    return item['text'] + ('\n' + detail if isinstance(detail, str) and detail else '')


def picker_heading(picker):
    """The name of the list on screen: the question of a numbered menu; else the picker title, else its first line.
    The UI builds the same name (pickHeading in ChoiceExtras.tsx)."""
    if picker.get('numbered'):
        return picker.get('question') or ''
    return picker.get('title') or (picker.get('text') or '').split('\n')[0].strip()


def _wait_for_picker_redraw(look, sleep):
    for _ in range(20):  # up to 2 s for a screen to show the result of a key
        sleep(.1)
        result = look()
        if result is not None:
            return result
    return None


NO_TICKS = 'Tick at least one option, then choose Done.'
REVIEW_OPEN = 'Claude shows your answers to all questions. Open the terminal, check each answer, then choose Submit answers.'


def _wait_for_box(entry, run, sleep, revision, row, checked):
    """Return once the box of `row` shows `checked` on the same question, so the next read of the screen (the
    Dock's refresh) shows the tick the user made. Space was already sent: a box that does not change asks for a check."""
    def changed():
        menu = verified_numbered_menu(entry, capture(entry, run, styled=True))
        if not menu or menu_digest(menu) != revision:
            raise MenuChanged(menu_refusal(entry, check=True))
        if bool(menu['options'][row].get('checked')) == checked:
            return True
    if _wait_for_picker_redraw(changed, sleep) is None:
        raise MenuChanged(menu_refusal(entry, check=True))


def _finish_claude_multi(entry, menu, press, run, sleep):
    index, _, _, ask_id = menu['menu_identity']
    advance = menu['control']['kind'] == 'next'
    ticks = [item['text'] for item in menu['options'] if item['kind'] == 'option' and item.get('checked')]
    press('Enter')

    def look():
        verify(entry, run)
        frame = capture(entry, run, styled=True)
        if advance:
            following = verified_numbered_menu(entry, frame)
            if following and following['menu_identity'][0] == index + 1 and following['menu_identity'][3] == ask_id:
                return True
        elif _open_claude_ask(entry) == menu['ask']:
            return choice_state.ask_review(frame)
        return None

    result = _wait_for_picker_redraw(look, sleep)
    if advance and result:
        return
    if result:
        blocks = result['blocks']
        questions = [' '.join(item['question'].split()) for item in menu['ask']['questions']]
        if (result['selected'] and [question for question, _ in blocks] == questions
                and all(answer for _, answer in blocks) and _review_ticks_match(blocks[index][1], ticks)):
            if len(blocks) > 1:
                # Submit answers sends every answer, and the user saw only this question's ticks: earlier answers
                # can change in the terminal after they were chosen. The review stays open for the user.
                raise MenuChanged(REVIEW_OPEN)
            press('Enter')
            return
    # The screen after Enter did not verify: send nothing more. The user checks it in the terminal.
    raise MenuChanged(menu_refusal(entry, check=True))


def _review_ticks_match(answer, ticks):
    # Claude 2.1.330 joins selected labels with commas and quotes labels that contain a delimiter or a quote.
    # Compare the labels in any order because their order can follow the order of selection.
    parts = re.findall(r'("(?:\\.|[^"\\])*"|(?:(?!, )[^"\n])+)(?:, |$)', answer)
    labels = [json.dumps(label, ensure_ascii=False) if ', ' in label or '"' in label else label for label in ticks]
    return ', '.join(parts) == answer and sorted(parts) == sorted(labels)


def picker_choose(entry, row, labels, then='Enter', run=subprocess.run, sleep=time.sleep, heading=None, *, revision=None,
                  ticked=None):
    """Move the cursor of a picker to one row, then press `then`: Enter chooses the row, Space ticks it in a
    multi-select list, and Up on the first row or Down on the last row shows the rows past the screen.

    Done (`then` Enter, no row) submits the ticked boxes of a numbered multi-select question. Its `labels` are the
    ticked rows the user saw; the boxes on screen must be the same.

    Space on a numbered multi-select question (a revision given) toggles a box, so `ticked` is the state of that box
    the user saw. The box on screen must be in that state.

    `labels` is the whole list the user saw (picker_row_key of each row) and `heading` the name of that list. The list
    on screen must match both before the first key and after each move, so the row index names one entry, never a
    look-alike, and never a row of another list. Numbered multi-select writes also bind the outstanding ask by
    revision at each check. Each move must show on screen before the next key:
    a slow redraw must not queue a second press that lands past the row."""
    if then not in ('Enter', 'Up', 'Down', 'Space'):
        raise ValueError('Invalid key.')
    submit = then == 'Enter' and row is None
    if then == 'Enter' and submit != (revision is not None):
        raise MenuChanged(menu_refusal(entry))
    if submit and (not isinstance(labels, list) or not all(isinstance(label, str) for label in labels)):
        raise MenuChanged(menu_refusal(entry))
    if not submit and (not isinstance(labels, list) or not labels or not all(isinstance(label, str) for label in labels)
                       or not isinstance(row, int) or isinstance(row, bool) or not 0 <= row < len(labels)
                       or not isinstance(heading, str)):
        raise ValueError('Choose a row first.')
    answering = then in ('Space', 'Enter')
    bound = answering and revision is not None
    if bound and then == 'Space' and not isinstance(ticked, bool):
        raise MenuChanged(menu_refusal(entry))
    if not bound:
        verify(entry, run)

    def press(key):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], key], check=True, capture_output=True, timeout=5)

    def look():
        menu = None
        target = row
        if bound:
            verify(entry, run)
        frame = capture(entry, run, styled=True)
        if bound:
            menu = verified_numbered_menu(entry, frame)
            if not menu or not menu.get('multi') or menu_digest(menu) != revision:
                raise MenuChanged(menu_refusal(entry))
            items = menu['options']
            control = menu.get('control')
            if submit:
                if not control:
                    raise MenuChanged(menu_refusal(entry))
                boxes = [picker_row_key(item) for item in items if item['kind'] == 'option' and item.get('checked')]
                if not boxes:
                    # Claude would go on, or show a review with "Some questions are unanswered".
                    raise MenuChanged(NO_TICKS)
                if sorted(boxes) != sorted(labels):
                    # The ticks changed in the terminal after the user looked: the revision names the question only.
                    raise MenuChanged(menu_refusal(entry))
                target = len(items) - 1  # the unnumbered control sits between the free and chat rows
            elif (menu['question'] != heading or [picker_row_key(item) for item in items] != labels
                  or items[row]['kind'] != 'option'
                  # The ticks changed in the terminal after the user looked: Space would invert the wrong state.
                  or then == 'Space' and bool(items[row].get('checked')) != ticked):
                raise MenuChanged(menu_refusal(entry))
            if control:
                items = [*items[:-1], control, items[-1]]
        else:
            if answering and entry['agent'] == 'claude' and (choice_state.ask_current(frame) or {}).get('multi'):
                raise MenuChanged(menu_refusal(entry))
            picker = read_picker(entry, run, styled=frame)
            if picker is None:
                raise RuntimeError('The list is no longer open.')
            if picker.get('numbered') and answering:
                if picker.get('multi'):
                    raise MenuChanged(menu_refusal(entry))
                raise ValueError('Choose a numbered option with its own button.')
            items = picker['items']
            if [picker_row_key(item) for item in items] != labels or picker_heading(picker) != heading:
                raise RuntimeError('The list changed. Review it again.')
        at = [index for index, item in enumerate(items) if item['selected']]
        if len(at) != 1:
            if submit:
                raise MenuChanged(menu_refusal(entry))
            raise RuntimeError(f'Could not find the cursor in the list. Open the terminal to choose ({entry["agent"]} may have a new screen layout).')
        return at[0], target, len(items), menu

    def moved_cursor():
        result = look()
        if result[0] != at:
            return result

    at, target, count, menu = look()
    for _ in range(count + 1):
        if at == target:
            if submit:
                _finish_claude_multi(entry, menu, press, run, sleep)
            elif bound and then == 'Space':
                press(then)
                _wait_for_box(entry, run, sleep, revision, row, not ticked)
            else:
                press(then)
            return
        key = 'Down' if at < target else 'Up'
        press(key)

        moved = _wait_for_picker_redraw(moved_cursor, sleep)
        if moved is None:
            if submit:
                raise MenuChanged(menu_refusal(entry))
            raise RuntimeError(f'The cursor did not move. Open the terminal to choose ({entry["agent"]} may have a new screen layout).')
        at, target, _, menu = moved
        if (at > target) if key == 'Down' else (at < target):
            if submit:
                raise MenuChanged(menu_refusal(entry))
            # The cursor jumped over the row: it is a line of the entry above, not a place the cursor stops.
            raise RuntimeError('That line is part of the entry above it. Choose the entry itself.')
    if submit:
        raise MenuChanged(menu_refusal(entry))
    raise RuntimeError(f'Could not move to that row. Open the terminal to choose it ({entry["agent"]} may have a new screen layout).')


def login_link(lines, agent=None):
    """A sign-in screen (Claude's /login, codex login): the sign-in URL, joined back from its wrapped lines, and whether
    the screen asks for a code. A wrapped URL is one link only on its first line, so a click opens a broken address."""
    # An earlier login in scrollback must never own the current input box.
    start = max((i + 1 for i, line in enumerate(lines) if re.match(r'^[❯›>]\s', line)), default=0)
    # Use the latest login link, including when history holds an earlier attempt.
    for index in range(len(lines) - 1, start - 1, -1):
        if 'https://' not in lines[index]:
            continue
        screen = '\n'.join(lines[index:])
        for name in (('claude', 'codex') if agent is None else (agent,)):
            if name not in login_flow.HOSTS:
                continue
            parsed = login_flow.parse_screen(name, screen)
            url = parsed['url']
            if url and (urlsplit(url).hostname == 'auth.openai.com' or '/oauth/' in url):
                code = name == 'claude' and bool(re.search(r'paste\s+code|authentication code|enter (?:the )?code', ' '.join(screen.split()), re.I))
                found = {'url': url, 'code': code}
                if parsed['user_code']:
                    found['user_code'] = parsed['user_code']
                if login_flow.CODE_ERROR.search(screen):
                    found['message'] = 'Sign-in code failed. Check the code and try again.'
                return found
    return None


def login_capture(entry, screen, run=subprocess.run):
    """A resized pane can push the start of its URL into history. Only a live login field permits recovery."""
    if entry['agent'] not in login_flow.HOSTS or not re.search(r'Pastecodehereifprompted>|one-timecode', re.sub(r'\s+', '', screen), re.I):
        return screen
    return run([paths.TMUX, 'capture-pane', '-p', '-J', '-S', '-200', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout


_login_progress = {}


def login_progress(entry, screen, now=None):
    """Short-lived native login feedback. Store no URL, code, transcript, or credential."""
    key = (entry['id'], entry.get('owner_pid'), entry.get('process_start'), entry.get('thread'))
    link = login_link(screen.splitlines(), entry['agent'])
    if not link and key not in _login_progress:
        return None
    now = time.monotonic() if now is None else now
    for old_key, value in list(_login_progress.items()):
        if value['expires'] <= now:
            _login_progress.pop(old_key, None)
    fingerprint = hashlib.sha256(link['url'].encode()).hexdigest() if link else None
    state = _login_progress.get(key)
    if link and (not state or state['fingerprint'] != fingerprint):
        if len(_login_progress) >= 64:
            _login_progress.pop(next(iter(_login_progress)))
        state = {'fingerprint': fingerprint, 'phase': 'waiting', 'message': '', 'expires': now + login_flow.LIMIT}
        _login_progress[key] = state
    if not state:
        return None
    # Only inspect the live end of the screen, not old chat messages above a login.
    tail = '\n'.join(screen.rstrip().splitlines()[-15:])
    phase = 'done' if re.search(r'(?im)^\s*(?:[✓✔]\s*)?(?:Login successful|Successfully logged in|Logged in successfully|Authentication successful|Signed in successfully)\b', tail) else 'failed' if login_flow.CODE_ERROR.search(tail) else None
    if phase:
        if state['phase'] != phase:
            state['expires'] = now + 30
        state.update(phase=phase, message='Signed in.' if phase == 'done' else 'Sign-in failed. Try again.')
    elif not link and state['phase'] == 'waiting':
        state.update(phase='unknown', message='Sign-in screen closed. Check the account in Plan usage.')
    return {key: state[key] for key in ('phase', 'message')} if state['phase'] != 'waiting' else None


def hint_rows(lines, agent):
    """The last 8 rows, where a key hint ("press Enter", "Esc to cancel") means the screen waits on the user.
    Claude Code hides its input box while a prompt or dialog waits. With the box on screen nothing waits on the user:
    rows above it are chat (the reply, a ✦ Heads up card, a notice) and rows under it are the footer (status line,
    mode, and notices such as "Continuing automatically at 9:30pm · Esc to cancel"). So no row counts then."""
    import footer  # footer imports this module
    if agent == 'claude' and footer.input_anchor(lines) is not None:
        return []
    return lines[-8:]


def attention(entry, screen, working, now=None, login_screen=None, styled_screen=None, dialog=None):
    """Catch-all for any screen that waits on the user but is not a recognized approval.

    Returns the visible screen once the same situation has lasted ATTENTION_DELAY seconds, so redraws and
    transient states never flash a panel.
    """
    now = time.monotonic() if now is None else now
    lines = screen.rstrip().splitlines()
    tail = lines[-30:]
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}.get(entry['agent'])
    reason = None
    picker = None
    current = None
    link = login_link(login_screen.splitlines() if login_screen is not None else lines, entry['agent'])
    tabs = next((index for index, line in enumerate(lines) if SETTINGS_TABS.match(line)), None) if entry['agent'] == 'claude' else None
    permissions = permissions_dialog.parse(screen, styled_screen) if entry['agent'] == 'claude' else None
    if link:
        reason = 'prompt'
    elif permissions is not None:
        tab_row = max(index for index, line in enumerate(lines) if permissions_dialog.TAB_ROW.match(line))
        reason, tail = 'permissions', lines[tab_row:]
    elif tabs is not None and dialog is not None:
        reason, tail = 'dialog', dialog['text'].splitlines()
    elif tabs is not None:
        # Claude's Settings dialog (/usage, /status ...) that claude_dialog could not read: show its text from the tab row.
        reason, tail = 'panel', lines[tabs:]
    elif (menu_open(entry['agent'], lines)
          # Claude's own question, read whole by the strict parser. menu_open misses it with the cursor on the
          # Submit or Next row, and in a narrow pane whose key hint wraps to a row that starts with "· ".
          or entry['agent'] == 'claude' and (current := choice_state.ask_current(styled_screen or screen))):
        reason = 'menu'
    elif ((entry['agent'] == 'claude' and (picker := claude_picker(screen, styled_screen)))
          or (entry['agent'] == 'codex' and (picker := choice_state.cursor_picker(styled_screen or screen, 'codex')))):
        reason, tail = 'picker', picker['text'].splitlines()
    elif dialog is not None:
        # A slash-command dialog (/mcp, /resume, /config ...), read as data: see claude_dialog.
        reason, tail = 'dialog', dialog['text'].splitlines()
    elif INPUT_HINT.search(joined_lines(hint_rows(lines, entry['agent']))):
        reason = 'prompt'
    elif marker and not working and lines and not any(line.startswith(marker) for line in lines[-15:]):
        reason = 'no-input-box'
    key = entry['id']
    seen = _attention_seen.get(key)
    if not reason:
        # A redraw can drop the panel for one capture. Keep a shown panel's record a little while so it returns at once.
        if seen and not (seen['shown'] and now - seen['last'] < ATTENTION_GRACE):
            _attention_seen.pop(key, None)
        return None
    # The cursor moves while the user answers; it must not restart the delay.
    text = '\n'.join(tail).strip('\n')
    fingerprint = hashlib.sha256(('permissions' if reason == 'permissions' else 'dialog\0' + dialog['title'] if reason == 'dialog' else
                                  reason + re.sub(r'[>❯›]|\s+', '', text)).encode()).hexdigest()
    # Another dialog is another card; a panel or menu that redraws keeps its record.
    if (not seen or seen['reason'] != reason or seen['fingerprint'] != fingerprint and (not seen['shown'] or reason == 'dialog')):
        seen = _attention_seen[key] = {'fingerprint': fingerprint, 'first': now, 'last': now, 'reason': reason, 'shown': False, 'id': fingerprint}
        if reason != 'dialog':
            return None
    # A dialog shows at once: only an open dialog has its rule row and its indented rows, so a redraw cannot look like one.
    if reason != 'dialog' and not seen['shown'] and now - seen['first'] < ATTENTION_DELAY:
        return None
    # Once shown, a panel that redraws (a /usage panel fills in, a clock ticks) stays up and keeps its id.
    # A menu takes the new id, because a new question needs a fresh card.
    seen.update(fingerprint=fingerprint, last=now, shown=True, id=fingerprint if reason in ('menu', 'picker') else seen['id'])
    found = {'id': seen['id'], 'text': text, 'reason': reason}
    if reason == 'menu':
        menu = verified_numbered_menu(entry, styled_screen or screen)
        revision = menu_digest(menu) if menu else None
        found['menu_verified'] = revision or False
        if revision:
            found.update(question=menu['question'], options=menu['options'])
        elif parsed := menu_question(menu_lines(lines), entry['agent']):
            found['question'] = parsed['question']
    elif reason == 'picker':
        found.update(picker)
    elif reason == 'panel':
        found['requiresResponse'] = False
    elif reason == 'dialog':
        found['requiresResponse'] = False
        found['dialog'] = dialog
    elif reason == 'permissions':
        found['requiresResponse'] = False
        found['permissions'] = permissions
    elif link:
        found['login'] = link
    if reason == 'picker' or found.get('menu_verified'):
        choice = choice_state.parse(styled_screen or screen, entry['agent'])
        if choice is not None:
            found['choice'] = choice
    if entry['agent'] == 'claude' and (current := current or choice_state.ask_current(styled_screen or screen)):
        found['ask_current'] = current
    return found


_busy = {}  # transcript path -> ((size, mtime), result)


def transcript_busy(path, window=262144):
    """True when the last lifecycle event in a Codex rollout file is a started turn."""
    try:
        info = os.stat(path)
        if path in _busy and _busy[path][0] == (info.st_size, info.st_mtime_ns):
            return _busy[path][1]
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - window))
            tail = stream.read().decode(errors='ignore')
    except (OSError, TypeError):
        return False
    last = None
    for line in tail.splitlines():
        for kind in ('task_started', 'task_complete', 'turn_aborted'):
            if '"type":"' + kind + '"' in line.replace(' ', ''):
                last = kind
    if len(_busy) > 64:
        _busy.clear()
    _busy[path] = ((info.st_size, info.st_mtime_ns), last == 'task_started')
    return last == 'task_started'


def reflow_prompt(text):
    """Antigravity breaks its permission request at the pane width; join each paragraph back into one line and drop the
    rule lines and the lone 'Command' heading."""
    paragraphs, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or RULE_LINE.match(stripped):
            if current:
                paragraphs.append(' '.join(current))
                current = []
        elif stripped != 'Command':
            current.append(stripped)
    if current:
        paragraphs.append(' '.join(current))
    return '\n'.join(paragraphs)


# Seconds a "waiting" status must hold. Longer than ATTENTION_DELAY, so a menu the screen parser knows wins,
# and a prompt just answered does not flash a card.
REGISTRY_SETTLE = ATTENTION_DELAY + 1


def waiting_approval(registry, now=None):
    """True when Claude Code's own session file says it waits on a permission: "permission prompt" in 2.1.x."""
    waiting_for = registry.get('waitingFor')
    if registry.get('status') != 'waiting' or not isinstance(waiting_for, str):
        return False
    if waiting_for not in ('permission prompt', 'sandbox request', 'worker request') and not waiting_for.startswith('approve'):
        return False
    changed = registry.get('statusUpdatedAt')
    now = time.time() if now is None else now
    return isinstance(changed, (int, float)) and now - changed / 1000 >= REGISTRY_SETTLE


def live_status(entry, run=subprocess.run):
    if not entry.get('owner_pid') or not entry.get('writable'):
        return {'state': 'Unavailable', 'approval': None}
    if not detection_rules.available(entry['agent']):
        return {'state': 'Unknown', 'approval': None}
    styled_screen = capture(entry, run, styled=True)
    screen = prompt_actions.plain(styled_screen)
    login_screen = login_capture(entry, screen, run)
    status = _live_status(entry, run, screen, login_screen=login_screen, styled_screen=styled_screen)
    if entry['agent'] == 'claude':
        import ask_bridge
        status = ask_bridge.overlay(entry, status)
    progress = login_progress(entry, login_screen)
    if progress:
        status['login_result'] = progress
    if entry['agent'] == 'claude':
        import footer  # footer imports this module
        # Background work from Claude's footer line ("1 shell", "2 local agents"), so the session list can show it.
        status['tasks'] = footer.parse(screen)['tasks']
        import claude_hooks
        # Scheduled jobs (CronCreate), so the tab list can mark a tab that wakes itself up.
        status['crons'] = [{key: job[key] for key in ('id', 'label', 'every', 'next') if key in job} for job in claude_hooks.crons(entry)]
    return status


WORKING = detection_rules.pattern('common', 'WORKING')


def _live_status(entry, run, screen, login_screen=None, styled_screen=None):
    if styled_screen is None:  # callers that pass one frame may pass it styled
        styled_screen, screen = screen, prompt_actions.plain(screen)
    if entry['agent'] == 'antigravity':
        capture_notices(entry, screen)
    else:
        capture_quota_notices(entry, screen)
    if entry['agent'] == 'claude':
        import side_question
        # The /btw overlay covers the prompt area. Its answer is free text, so no other screen rule may read it.
        side = side_question.observe(entry, screen, styled_screen)
        if side:
            prompt_actions.ledger.forget(entry['id'])
            return {'state': 'Working' if WORKING.search(screen) else 'Idle', 'approval': None, 'attention': side}
    pending = approval(screen)
    if pending:
        prompt_actions.ledger.forget(entry['id'])
        if entry['agent'] == 'antigravity':
            pending['text'] = reflow_prompt(pending['text'])
        return {'state': 'Approval', 'approval': {k: v for k, v in pending.items() if not k.startswith('_')}}
    live = claude_hooks.codex_live(entry) if entry['agent'] == 'codex' else None
    # No hook reports an answered approval, so a screen that shows Codex working again overrides "waiting".
    if live is not None and live['waiting'] and WORKING.search(screen):
        live = dict(live, waiting=False, busy=True)
    if live is not None and live['waiting']:
        prompt_actions.ledger.forget(entry['id'])
        detail = live.get('approval') or {}
        text = '\n\n'.join(part for part in ('Codex is waiting for your approval.', detail.get('tool'),
                          detail.get('text'), '\n'.join(screen.rstrip().splitlines()[-20:])) if part)
        return {'state': 'Approval', 'approval': {'id': None, 'text': text.strip(), 'actionable': False}}
    working = live['busy'] and not live['waiting'] if live is not None else bool(WORKING.search(screen))
    if entry['agent'] == 'codex' and live is None:
        working = working or transcript_busy(entry.get('transcript'))
    registry = {}
    if entry['agent'] == 'claude' and entry.get('registry'):  # a Claude with no conversation yet has no registry
        try:
            registry = json.loads(Path(entry['registry']).read_text())
            working = working or registry.get('status') == 'busy'
        except (OSError, ValueError):
            pass
    elif entry['agent'] == 'antigravity':
        try:
            metrics = json.loads((AGY_CACHE / (entry['thread'] + '.json')).read_text())
            working = working or metrics.get('agent_state') in ('thinking', 'working', 'tool_use', 'running')
            if metrics.get('tool_confirmation_pending'):
                return {'state': 'Approval', 'approval': {'id': None, 'text': screen.strip(), 'actionable': False}}
        except (OSError, ValueError):
            pass
    info = None
    # Check chrome before any extra tmux call. The action route brackets its
    # single fresh styled capture with metadata reads under SEND_LOCK.
    if prompt_actions.panel(entry, styled_screen, ('', '', '', '1', '0', '0', '', '')):
        info = prompt_actions.ledger.observe(entry, styled_screen, prompt_actions.metadata(entry, run))
    else:
        prompt_actions.ledger.forget(entry['id'])
    dialog = None
    if entry['agent'] == 'claude' and not info:
        import claude_dialog
        dialog = claude_dialog.read(entry, styled_screen, run)
    needed = info or attention(entry, screen, working, login_screen=login_screen, styled_screen=styled_screen, dialog=dialog)
    if needed and needed.get('requiresResponse') is False:  # navigation does not ask for an answer
        return {'state': 'Working' if working else 'Idle', 'approval': None, 'attention': needed}
    if needed:
        return {'state': 'Input needed', 'approval': None, 'attention': needed}
    if waiting_approval(registry) and (_attention_seen.get(entry['id']) or {}).get('reason') != 'menu':
        # The screen parser missed this prompt. Show it read-only: the option keys are not known.
        reason = {'permission prompt': 'Claude is waiting for your permission.', 'sandbox request': 'Claude is waiting for sandbox access.',
                  'worker request': 'A worker agent is waiting for your permission.'}.get(registry['waitingFor'], registry['waitingFor'])
        text = reason + '\n\n' + '\n'.join(screen.rstrip().splitlines()[-20:])
        return {'state': 'Approval', 'approval': {'id': None, 'text': text.strip(), 'actionable': False}}
    status = {'state': 'Working' if working else 'Idle', 'approval': None}
    if entry['agent'] == 'claude' and not working and (reply := suggestion(entry, run)):
        status['suggestion'] = reply
    return status


class ConversationChanged(RuntimeError):
    pass


def verify(entry, run=subprocess.run):
    if not detection_rules.available(entry['agent']):
        raise RuntimeError('Tether could not load its prompt rules for this agent. Check the server log.')
    pid = entry['owner_pid']
    proc = Path(procinfo.ROOT)
    valid_comm = {'claude': ('claude',), 'codex': ('codex',), 'antigravity': ('agy', 'exe')}[entry['agent']]
    process_comm = procinfo.comm(pid, proc)
    if process_start(pid) != entry['process_start'] or process_comm not in valid_comm:
        raise ConversationChanged('The agent restarted. Reload its tab before sending.')
    result = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                  '#{pane_pid}\t#{pane_current_command}\t#{pane_in_mode}'],
                 check=True, capture_output=True, text=True, timeout=5)
    root, command, mode = result.stdout.strip().split('\t')
    valid_command = command in {'claude': ('claude',), 'codex': ('node', 'codex'), 'antigravity': ('agy', 'exe')}[entry['agent']]
    if procinfo.DARWIN and entry['agent'] == 'claude' and re.fullmatch(r'\d+\.\d+\.\d+', command):
        valid_command = True
    if mode != '0' or not valid_command:
        raise RuntimeError('Return to the agent prompt in Termius before sending.')
    ancestor = pid
    while ancestor != int(root) and ancestor > 1:
        ancestor = procinfo.parent(ancestor, proc)
    if ancestor != int(root):
        raise ConversationChanged('The agent no longer belongs to this tmux pane.')
    if entry['agent'] == 'claude':
        if not entry.get('pending_conversation'):
            registry = json.loads(Path(entry['registry']).read_text())
            if registry.get('sessionId') != entry['thread'] or registry.get('pid') != pid:
                raise ConversationChanged('The Claude conversation changed. Reload its tab.')
    elif entry['agent'] == 'antigravity':
        if not entry.get('pending_conversation'):
            lock = AGY_ROOT / 'presence' / (entry['thread'] + '.lock')
            if lock.exists():
                if not any(target == lock for target in procinfo.resolved_fds(pid, proc)):
                    raise ConversationChanged('The Antigravity conversation changed. Reload its tab.')
            else:
                cache_file = AGY_CACHE / (entry['thread'] + '.json')
                if cache_file.is_file():
                    try:
                        cached = json.loads(cache_file.read_text())
                        if cached.get('conversation_id') != entry['thread']:
                            raise ConversationChanged('The Antigravity conversation changed. Reload its tab.')
                    except (OSError, ValueError):
                        pass
    elif entry['agent'] == 'codex':
        verify_codex(entry, run)


def verify_codex(entry, run):
    """Send only when the pane still shows the conversation of its tab, by the same binding as discovery. A new session
    has no conversation until its first message: it must still have none."""
    from agents import codex_database, codex_recheck, open_rollout
    try:
        fresh = codex_recheck(entry, run)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Without the process table only an in-process rollout still proves the conversation.
        db = codex_database()
        try:
            held = open_rollout(db, entry['owner_pid'])
        finally:
            db.close()
        if held and not entry.get('pending_conversation') and held[0] == entry['thread']:
            return
        raise RuntimeError('Tether could not check the Codex conversation. Try again.') from None
    if fresh and fresh.get('codex_binding') != 'open' and not fresh.get('codex_title_read'):
        # The title is the only word from a daemon client itself; startup arguments and folder guesses go stale.
        raise RuntimeError('Tether could not read the Codex tab title. Try again. A custom tui.terminal_title must '
                           'show "thread-name" and "project-name", or "thread-id".')
    if not fresh or fresh['thread'] != entry['thread'] or bool(fresh.get('pending_conversation')) != bool(entry.get('pending_conversation')):
        raise ConversationChanged('The Codex conversation changed. Reload its tab.')
    if fresh.get('codex_unresolved'):
        raise RuntimeError('Tether cannot tell which Codex conversation this tab shows. '
                           'Give it a unique name with /rename in Codex, then reload its tab.')
    if not fresh.get('pending_conversation') and fresh.get('codex_binding') not in ('open', 'title'):
        # A startup argument or a folder/time guess is good enough to show a chat, never to type into one: the pane
        # may show another conversation of the same folder. Codex names a thread after its first reply.
        raise RuntimeError('Tether cannot confirm which Codex conversation this tab shows. Wait for Codex to name it, '
                           'or name it with /rename in Codex, then reload its tab.')


class DraftError(RuntimeError):
    """The agent input box holds text, so sending would mix it with the new message."""


DRAFT_QUIET = 10
_quiet_drafts = {}


def stash_quiet_draft(entry, run=subprocess.run, now=time.monotonic):
    """Stash only Claude drafts that have stayed unchanged for ten seconds."""
    if entry.get('agent') != 'claude':
        return None
    pane = entry['id']
    try:
        text = '\n'.join(prompt_draft(entry, run))
        if not text.strip():
            _quiet_drafts.pop(pane, None)
            return None
        first_text, first_seen = _quiet_drafts.get(pane, (None, None))
        seen = now()
        if text != first_text:
            _quiet_drafts[pane] = (text, seen)
            raise DraftError('There is an unsent draft. Wait for it to stay unchanged before review delivery.')
        if seen - first_seen < DRAFT_QUIET:
            raise DraftError('There is an unsent draft. Wait for it to stay unchanged before review delivery.')
        verify(entry, run)
        run([paths.TMUX, 'send-keys', '-t', pane, 'C-s'],
            check=True, capture_output=True, timeout=5)
        for attempt in range(11):
            if not any(prompt_draft(entry, run)):
                _quiet_drafts.pop(pane, None)
                return text
            if attempt < 10:
                time.sleep(.1)
        raise DraftError('Could not stash the unsent draft.')
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Could not stash the unsent draft.') from exc


def restore_check(entry, stashed, run=subprocess.run, sleep=time.sleep):
    """Claude restores its stash on submit; toggle it only if the box stays empty."""
    try:
        for attempt in range(21):
            text = ''.join(''.join(prompt_draft(entry, run)).split())
            if text:
                return
            if attempt < 20:
                sleep(.1)
        verify(entry, run)
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'C-s'],
            check=True, capture_output=True, timeout=5)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        # Delivery already succeeded. A failed check must not cause a duplicate send.
        return


def clear_unsent(entry, message, run=subprocess.run):
    """Remove only the review message left in the box by a failed submit."""
    try:
        prefix = ''.join(message.split())[:30]
        text = ''.join(''.join(prompt_draft(entry, run)).split())
        if prefix and text.startswith(prefix):
            clear_draft(entry, run)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Could not clear the unsent review message.') from exc


class PaneTooSmall(RuntimeError):
    """The pane cannot show enough of the input or control to act safely."""


# A visible, readable input always wins. Diagnose pane size only when the input is missing.
MIN_INPUT_ROWS = 8
MIN_INPUT_COLS = 40
SIZE_HELP = 'This pane is too small to read safely. Zoom it in Terminals, or make it at least 40 columns by 8 rows, then try again.'


def small_pane_reason(entry, run=subprocess.run):
    """Explain a hidden input box using read-only tmux queries; None if size is not the cause or queries fail."""
    try:
        result = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                      '#{pane_width}\t#{pane_height}\t#{window_width}\t#{window_height}\t'
                      '#{session_name}\t#{window_name}\t#{window-size}'],
                     check=True, capture_output=True, text=True, timeout=5)
        if result.returncode:
            return None
        pw, ph, ww, wh, session, window, sizing = result.stdout.rstrip('\n').split('\t')
        pw, ph, ww, wh = map(int, (pw, ph, ww, wh))
        if min(pw, ph, ww, wh) <= 0 or not session or not sizing or (ph >= MIN_INPUT_ROWS and pw >= MIN_INPUT_COLS):
            return None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return SIZE_HELP


# Codex's footer under its input: "? for shortcuts", or the status line with its context figure (footer.CODEX_CONTEXT;
# footer.py imports this module, so the pattern lives here too).
CODEX_INPUT_FOOTER = re.compile(r'\?\s+for\s+shortcuts\b|\bContext\s+\d+(?:\.\d+)?%\s+(?:left|used)\b', re.I)


def codex_input_box(lines, end):
    """True when the Codex input ends at a blank line and only its footer follows (wrapped lines are joined)."""
    if end >= len(lines) or lines[end].strip():
        return False
    rest = [line.strip() for line in lines[end + 1:] if line.strip()]
    return 0 < len(rest) <= 6 and CODEX_INPUT_FOOTER.search(' '.join(rest)) is not None


def _unverified_restart_prompt(entry):
    name = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity'}[entry['agent']]
    version = _agent_version(entry)
    changed = f'{name} {version}' if version else f'{"An" if name[0] in "AEIOU" else "A"} {name} update'
    return RuntimeError(f'Tether cannot verify the {name} input box, so it did not restart the tab. '
                        f'Open the tab to check. {changed} may have changed its screen.')


def prompt_draft(entry, run=subprocess.run, *, read_only=False, _size_retry=True):
    # One styled snapshot binds text and attributes to the same screen redraw.
    styled = capture(entry, run, styled=True)
    screen = ANSI_SGR.sub('', styled)
    if approval(screen):
        raise RuntimeError('Respond to the pending approval before sending a message.')
    if entry['agent'] == 'claude' and (picker := cursor_picker(screen)):
        raise RuntimeError(f'Claude has the {picker["title"]} picker open. Choose an item or press Esc in the card.')
    marker = {'claude': '❯', 'codex': '›', 'antigravity': '>'}[entry['agent']]
    lines = screen.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.startswith(marker)]
    if not prompts:
        if not _size_retry:
            raise PaneTooSmall(SIZE_HELP)
        reason = small_pane_reason(entry, run)
        if reason:
            if read_only:
                raise PaneTooSmall(reason)
            # Tether owns agent window sizes (window_size.py); a small client may have shrunk this one.
            window_size.apply(run)
            time.sleep(.1)  # Let the agent redraw its input after SIGWINCH.
            return prompt_draft(entry, run, _size_retry=False)
        name = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity'}[entry['agent']]
        raise RuntimeError(f'Cannot find the {name} input box on screen. '
                           'Open the pane in Terminals to see what it shows.')
    start = prompts[-1]
    claude_box = entry['agent'] == 'claude' and claude_input_box(lines, start)
    if (not claude_box
            and (re.match(r'\s*\d+[.)]\s', lines[start][1:]) and menu_question(lines[start:])
                 or re.match(r'^[>❯›]\s+\S', lines[start]) and INPUT_HINT.search(joined_lines(lines[start + 1:])))):
        # Codex marks the chosen row of a menu ("› 1. Review hooks") the same way as its input line.
        name = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity'}[entry['agent']]
        raise RuntimeError(f'{name} is asking you to pick an option. Answer it above, then send again.')
    if read_only and entry['agent'] == 'claude' and not claude_box:
        raise _unverified_restart_prompt(entry)
    draft = [lines[start][1:].strip()]
    end = start + 1
    for line in lines[end:]:
        if line.lstrip().startswith('─') or (entry['agent'] == 'codex' and not line.strip()):
            break
        draft.append(line.strip())
        end += 1
    colour = 109 if entry['agent'] == 'antigravity' else None
    has_text = any(draft)
    if read_only and entry['agent'] == 'codex' and not codex_input_box(lines, end):
        raise _unverified_restart_prompt(entry)
    if has_text and placeholder_text(styled, marker, start=start, end=end, colour=colour) is not None:
        return []
    if read_only and entry['agent'] != 'claude':
        if not has_text:
            raise _unverified_restart_prompt(entry)
        return draft
    # Older unstyled captures have no attributes. Retain only their exact known hints.
    if not ANSI_SGR.search(styled) and ((entry['agent'] == 'codex' and draft == ['Ask Codex to do anything'])
                                      or (entry['agent'] == 'claude' and draft == ['Press up to edit queued messages'])):
        return []
    return draft


ANSI_SGR = re.compile(r'\x1b\[[0-9;]*m')
AGY_MODE_HINT = detection_rules.pattern('antigravity', 'AGY_MODE_HINT')  # a narrow pane cuts the hint short


def placeholder_text(styled, marker, *, start=None, end=None, colour=None):
    """Input text only if every non-space character is faint or in the known hint colour.

    SGR state crosses rows. Extended colour parameters are data, not attributes:
    the 2 in 38;2;r;g;b does not mean faint, and a zero colour does not reset it.
    None means at least one character is typed text, or no input marker exists.
    """
    rows, dim, foreground = [[]], False, None
    for part in re.split(r'(\x1b\[[0-9;]*m)', styled):
        if ANSI_SGR.fullmatch(part):
            codes = [int(code or 0) for code in part[2:-1].split(';')]
            at = 0
            while at < len(codes):
                code = codes[at]
                if code in (38, 48, 58) and at + 1 < len(codes):
                    count = 2 if codes[at + 1] == 5 else 4 if codes[at + 1] == 2 else 0
                    if code == 38:
                        foreground = codes[at + 2] if count == 2 and at + 2 < len(codes) else None
                    at += count + 1
                    continue
                if code == 0:
                    dim, foreground = False, None
                elif code == 2:
                    dim = True
                elif code == 22:
                    dim = False
                elif code == 39 or 30 <= code <= 37 or 90 <= code <= 97:
                    foreground = None
                at += 1
        else:
            for char in part:
                if char == '\n':
                    rows.append([])
                else:
                    rows[-1].append((char, dim or colour is not None and foreground == colour))
    if start is None:
        candidates = [i for i, row in enumerate(rows) if row and row[0][0] == marker]
        if not candidates:
            return None
        start = candidates[-1]
    result = []
    for i in range(start, end if end is not None else start + 1):
        row = rows[i][1:] if i == start else rows[i]
        if any(not char.isspace() and not hint for char, hint in row):
            return None
        result.append(''.join(char for char, _ in row).strip())
    return '\n'.join(result)


def hint_prompt_text(styled, marker):
    return placeholder_text(styled, marker, colour=109) or ''


def dim_prompt_text(styled):
    """Text of Claude's input line when every visible character is dimmed (a placeholder or suggested reply)."""
    return placeholder_text(styled, '❯') or ''


def suggestion(entry, run=subprocess.run):
    """A reply Claude has prepared for the user, shown dimmed in the input box."""
    text = dim_prompt_text(capture(entry, run, styled=True))
    return '' if text == 'Press up to edit queued messages' or text.startswith('Try ') else text


AGY_PANEL = detection_rules.pattern('antigravity', 'AGY_PANEL')


def close_agy_panel(entry, run=subprocess.run):
    """Antigravity's /usage, /help ... open full-screen panels that swallow typing: close one (Esc) before sending anything."""
    if entry.get('agent') != 'antigravity':
        return
    for _ in range(3):
        screen = capture(entry, run)
        lines = screen.splitlines()
        if not AGY_PANEL.search(joined_lines(lines[-40:])):
            return
        # Panel hints quoted in chat history must not send Esc into a live draft
        # or a chooser. The real information panels replace the input box.
        if approval(screen) or any(CURSOR_ITEM.match(line) for line in lines[-30:]):
            return
        if any(line.startswith('>') and i and lines[i - 1].lstrip().startswith('─')
               and any(row.lstrip().startswith('─') for row in lines[i + 1:]) for i, line in enumerate(lines)):
            return
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Escape'], check=True, capture_output=True, timeout=5)
        time.sleep(.5)
    if AGY_PANEL.search(joined_lines(capture(entry, run).splitlines()[-40:])):
        raise RuntimeError('An Antigravity panel is open. Close it in Termius, then send again.')


PANEL_CLOSE = detection_rules.pattern('claude', 'PANEL_CLOSE')


PARTIAL_PICKER_HINT = detection_rules.pattern('common', 'PARTIAL_PICKER_HINT')


def close_info_panel(entry, run=subprocess.run):
    """A read-only panel such as /usage, /status or /cost covers Claude's input box. Sending a message means the person
    is done with it, so press Esc once. A menu, picker or approval asks something, so it is never closed here."""
    if entry.get('agent') != 'claude':
        return False
    screen = capture(entry, run)
    lines = screen.rstrip().splitlines()
    footer = joined_lines(lines[-8:])
    panel = PANEL_CLOSE.search(footer) or any(SETTINGS_TABS.match(line) for line in lines)
    partial_picker = PARTIAL_PICKER_HINT.search(footer) and any(PICKER_CURSOR.match(line) for line in lines[-30:])
    if approval(screen) or cursor_picker(screen) or partial_picker or not panel or any(CURSOR_ITEM.match(line) for line in lines[-30:]):
        return False
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Escape'], check=True, capture_output=True, timeout=5)
    time.sleep(.4)
    return True


_escape_seen = {}


def drop_double_escape(entry, run=subprocess.run, now=None):
    """Suppress repeated Esc requests only at Claude's visible, empty input box."""
    if entry.get('agent') != 'claude':
        return False
    now = time.monotonic() if now is None else now
    identity = (entry['id'], entry.get('thread'), entry.get('owner_pid'), entry.get('process_start'))
    previous = _escape_seen.get(identity)
    _escape_seen[identity] = now
    if previous is None or not 0 <= now - previous < 1.0:
        return False
    try:
        return not any(prompt_draft(entry, run))
    except RuntimeError:
        return False  # a picker, approval or hidden input box must still receive Esc


def stop_turn(entry, run=subprocess.run, sleep=time.sleep):
    """Interrupt Claude and return text it restores into a previously empty input."""
    if entry.get('agent') != 'claude':
        raise ValueError('Only Claude turns can be stopped with draft restoration.')
    verify(entry, run)
    try:
        before = [line for line in prompt_draft(entry, run) if line]
    except RuntimeError:
        before = None
    if not drop_double_escape(entry, run):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Escape'],
            check=True, capture_output=True, timeout=5)
    if before != []:
        return {'restored': ''}
    for attempt in range(10):
        sleep(.2)
        try:
            draft = [line for line in prompt_draft(entry, run) if line]
            if draft:
                sleep(.2)
                draft = [line for line in prompt_draft(entry, run) if line]
        except RuntimeError:
            continue  # Claude may temporarily hide its input while it redraws.
        if draft:
            clear_draft(entry, run)
            return {'restored': '\n'.join(draft)}
        if attempt >= 4 and not WORKING.search(capture(entry, run)):
            break
    return {'restored': ''}


def empty_prompt(entry, run=subprocess.run, *, read_only=False):
    if not read_only:
        close_agy_panel(entry, run)
    try:
        draft = prompt_draft(entry, run, read_only=read_only)
    except PaneTooSmall:
        raise
    except RuntimeError as exc:
        if read_only or 'input box' not in str(exc) or not close_info_panel(entry, run):
            raise
        draft = prompt_draft(entry, run)
    if any(draft):
        raise DraftError('There is an unsent draft in the agent input. Clear it, then send again.')


def login_code_draft(screen):
    prompts = list(re.finditer(r'Paste\s+code\s+here\s+if\s+prompted\s*>', screen, re.I))
    return re.split(r'\bEsc\s+to\s+cancel\b', screen[prompts[-1].end():], maxsplit=1, flags=re.I)[0].strip() if prompts else None


def type_text(entry, text, enter=False, run=subprocess.run, login_url=None):
    if not isinstance(text, str) or not 0 < len(text) <= 500 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Enter up to 500 characters of text.')
    verify(entry, run)
    if login_url is not None:
        screen = capture(entry, run)
        if login_code_draft(screen):
            raise DraftError('The sign-in field has text. Check the terminal before sending.')
        screen = login_capture(entry, screen, run)
        lines = screen.splitlines()
        link = login_link(lines, entry['agent'])
        if entry['agent'] != 'claude' or not link or not link['code'] or link['url'] != login_url:
            raise RuntimeError('The sign-in prompt changed. Reload and try again.')
        if login_code_draft(screen) != '':
            raise DraftError('The sign-in field has text or is not visible. Check the terminal before sending.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], '-l', '--', text], check=True, capture_output=True, timeout=5)
    if enter:
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)


def card_key(entry, key, run=subprocess.run, sleep=time.sleep):
    """Press one key of a ✦ card. Claude takes a card's hotkeys only while the card holds the keyboard. It moves the focus
    there by itself only when Claude is idle and the input box is empty; otherwise the digit is typed into the box, which then
    blocks every later send. So: refuse when the box has text. If the digit lands as text, remove it, focus the card with
    Ctrl+X Tab (Claude's abovePrompt:focus key) and press it once more. If it lands as text again, remove it and give up."""
    verify(entry, run)
    draft = [line for line in prompt_draft(entry, run) if line]
    if draft:
        raise DraftError(f'Claude’s input box has text (“{" ".join(draft)[:40]}”). Clear it, then tap again.')
    def press(*keys):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], *keys], check=True, capture_output=True, timeout=5)

    def typed():
        return [line for line in prompt_draft(entry, run) if line] == [key]
    press(key)
    sleep(.3)
    if not typed():
        return
    press('BSpace')
    press('C-x', 'Tab')
    sleep(.2)
    press(key)
    sleep(.3)
    if typed():
        press('BSpace')
        raise RuntimeError('Claude did not take the key, so the card is not active now. Open the terminal to answer it.')


def clear_draft(entry, run=subprocess.run):
    verify(entry, run)
    if not any(prompt_draft(entry, run)):
        return
    keys = lambda *k: run([paths.TMUX, 'send-keys', '-t', entry['id'], *k], check=True, capture_output=True, timeout=5)
    keys('C-e')
    # Ctrl+U clears to the start of the line; Backspace joins the previous line of a multi-line draft.
    for _ in range(40):
        keys('C-u')
        time.sleep(.05)
        if not any(prompt_draft(entry, run)):
            return
        keys('BSpace')
        if not any(prompt_draft(entry, run)):
            return
    raise DraftError('Could not clear the draft. Clear it in Termius.')


QUEUE_HINT = 'Press up to edit queued messages'


def unqueue(entry, run=subprocess.run, sleep=time.sleep, *, _size_retry=True):
    """Take back what waits in Claude's queue: Up pulls every queued message into its input (Claude's own "edit queued"),
    then the input is cleared. Returns the text so the page can put it back in its own message box."""
    if entry.get('agent') != 'claude':
        raise ValueError('Only Claude queues can be cancelled from here.')
    verify(entry, run)
    styled = capture(entry, run, styled=True)
    lines = ANSI_SGR.sub('', styled).splitlines()
    prompts = [line for line in lines if line.startswith('❯')]
    if not prompts:
        if not _size_retry:
            raise PaneTooSmall(SIZE_HELP)
        reason = small_pane_reason(entry, run)
        if reason:
            window_size.apply(run)
            sleep(.1)
            return unqueue(entry, run, sleep, _size_retry=False)
    if not prompts or QUEUE_HINT not in prompts[-1]:
        raise RuntimeError('Nothing is queued. Claude may already have taken the message.')
    if ANSI_SGR.search(styled) and placeholder_text(styled, '❯') != prompts[-1][1:].strip():
        raise DraftError('There is an unsent draft in the agent input. Clear it, then send again.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Up'], check=True, capture_output=True, timeout=5)
    for _ in range(10):
        sleep(.15)
        draft = [line for line in prompt_draft(entry, run) if line]
        if draft:
            break
    else:
        raise RuntimeError('Claude did not hand back the queued message. Check the terminal.')
    text = '\n'.join(draft)
    clear_draft(entry, run)
    return text


AGY_BUSY = detection_rules.pattern('antigravity', 'AGY_BUSY')


def confirm_delivery(entry, sent, run):
    """Antigravity sometimes drops a message right after it starts or refreshes, with an empty box and no error. Its screen
    then never shows the text, so a message missing from the screen after a moment is reported instead of silently lost."""
    if entry.get('agent') != 'antigravity' or sent.startswith('/') or len(sent) < 3:
        return
    for _ in range(8):
        time.sleep(.5)
        screen = capture_screen(entry, 200, run)
        # Taken if the text is on screen (the pane can cut or rewrap it, so only its start is compared), or if the agent is
        # visibly busy: a message sent while it works may be queued out of sight.
        if sent[:15] in ''.join(screen.split()) or AGY_BUSY.search(joined_lines(screen.splitlines()[-8:])):
            return
    raise RuntimeError('Antigravity did not take the message. Send it again.')


def leave_scrollback(entry, run=subprocess.run):
    """With tmux mouse mode on, a wheel scroll in the terminal puts the pane in copy mode, and every send was then refused.
    Sending a message means the person is done reading back, so leave copy mode (or view mode) first. Other modes stay."""
    mode = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'], '#{pane_in_mode}\t#{pane_mode}'],
               check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    if mode in ('1\tcopy-mode', '1\tview-mode'):
        run([paths.TMUX, 'send-keys', '-t', entry['id'], '-X', 'cancel'], check=True, capture_output=True, timeout=5)


CLAUDE_PASTE_PLACEHOLDER = re.compile(r'^\[(?:Image #\d+\]|Pasted text #\d+)')


def submit(entry, message, run=subprocess.run):
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
        raise RuntimeError('The message contains terminal control characters.')
    leave_scrollback(entry, run)
    verify(entry, run)
    empty_prompt(entry, run)
    parts = [message]
    codex_drafts = ()
    if entry['agent'] == 'codex':
        # Codex can consume a whole paste as an image. Give each upload its own paste and keep the prose separate.
        image = rf'(?<!\S)({re.escape(str(uploads.ROOT))}/[A-Za-z0-9._-]+\.(?i:png|jpe?g|gif|webp))(?=\s|$)'
        separated = re.split(image, message)
        if len(separated) > 1:
            parts = [part for part in separated if part]
            visible = ''.join(f'[Image #{index // 2 + 1}]' if index % 2 else part for index, part in enumerate(separated))
            codex_drafts = (' '.join(message.split()), ' '.join(visible.split()))
    buffer = 'tmux-mobile-' + uuid.uuid4().hex
    try:
        for index, part in enumerate(parts):
            run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=part.encode(), check=True, capture_output=True, timeout=5)
            verify(entry, run)
            if not index:
                empty_prompt(entry, run)
            elif approval(capture(entry, run)):
                raise RuntimeError('An approval appeared during delivery. Check Termius before retrying.')
            run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        verify(entry, run)
        if approval(capture(entry, run)):
            raise RuntimeError('An approval appeared during delivery. Check Termius before retrying.')
        # The agent can swallow an Enter that arrives right behind a bracketed paste, leaving the
        # message sitting in the input box; confirm the box emptied and press Enter again if not.
        sent = ''.join(message.split())
        for attempt in range(4):
            time.sleep(.2)
            if attempt and codex_drafts:
                # The terminal may have accepted Enter after the previous capture. Recheck the pane and draft
                # while excluding any unrelated prompt that has since appeared.
                verify(entry, run)
                current = prompt_draft(entry, run)
                if not any(current):
                    return
                if ' '.join(' '.join(current).split()) not in codex_drafts:
                    raise DraftError('The agent input changed during delivery. Check the terminal before retrying.')
            run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
            time.sleep(.3)
            try:
                draft = prompt_draft(entry, run)
            except PaneTooSmall:
                raise
            except RuntimeError as exc:
                if 'approval' in str(exc):
                    raise
                return  # no input box on screen any more: a menu or picker took the Enter
            if not any(draft):
                confirm_delivery(entry, sent, run)
                return
            # Agents mark picker rows (/model ...) like the input line. A mismatch means a menu opened, except for
            # Claude paste placeholders: the box was empty before our paste, so these still hold our unsent message.
            draft_text = ' '.join(draft)
            if (not codex_drafts and (entry.get('_review') or '\n' not in message.strip())
                    and not ''.join(draft_text.split()).startswith(sent[:30])
                    and not (entry['agent'] == 'claude' and CLAUDE_PASTE_PLACEHOLDER.match(draft_text))):
                return
        raise DraftError('The message reached the agent input but was not submitted. Clear it, then send again.')
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)


AGENT_TITLES = {'claude': 'Claude', 'codex': 'Codex'}


def rename_agent(entry, name, run=subprocess.run):
    """Send `/rename NAME` so the agent's own session name matches the Tether name. Claude and Codex both take the name inline.
    Only when the agent is idle with an empty input box. Returns '' when sent, else why it was skipped."""
    title = AGENT_TITLES.get(entry.get('agent'))
    if not title or not entry.get('writable'):
        return ''
    try:
        if live_status(entry, run)['state'] != 'Idle':
            return f'{title} is busy, so its own name did not change. Rename again when it is idle.'
        submit(entry, '/rename ' + ' '.join(name.split()), run)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        return f'{title}\'s own name did not change: {exc}'
    return ''


def respond_approval(entry, identifier, action=None, run=subprocess.run, choice=None, feedback=None, *, on_answer=None):
    if choice is None and action not in ('approve', 'reject'):
        raise ValueError('Invalid approval action.')
    verify(entry, run)
    pending = approval(capture(entry, run))
    if not pending or pending['id'] != identifier or not pending['actionable']:
        raise RuntimeError('The approval changed. Reload and review it again.')
    if choice is not None:
        if type(choice) is not int or choice not in pending['_numbers']:
            raise ValueError('Choose one of the displayed approval options.')
        target = pending['_numbers'].index(choice)
    else:
        target = pending['_once'] if action == 'approve' else pending['_reject']
    if target is None:
        raise RuntimeError('That approval choice is unavailable. Review it again.')
    if feedback is not None:
        if (entry.get('agent') != 'claude' or not plan_menu(pending)
                or not re.match(r'No,?\s+keep planning\b', pending['options'][target]['text'], re.I)):
            raise ValueError('Feedback requires the displayed Keep planning option.')
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 10000 or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in feedback):
            raise ValueError('Enter feedback without terminal control characters (up to 10000 characters).')
    delta = target - pending['_selected']
    expected = pending['_selected']
    for _ in range(abs(delta)):
        verify(entry, run)
        current = approval(capture(entry, run))
        if not current or current['id'] != identifier:
            raise RuntimeError('The approval changed. Review it again.')
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Down' if delta > 0 else 'Up'], check=True, capture_output=True, timeout=5)
        expected += 1 if delta > 0 else -1
        deadline = time.monotonic() + .5
        while True:
            current = approval(capture(entry, run))
            if not current or current['id'] != identifier:
                raise RuntimeError('The approval changed. Review it again.')
            if current['_selected'] == expected:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not confirm the approval selection. Check Termius.')
            time.sleep(.03)
    verify(entry, run)
    current = approval(capture(entry, run))
    if not current or current['id'] != identifier or current['_selected'] != target:
        raise RuntimeError('Could not confirm the selected approval choice. Review it in Termius.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    if on_answer is not None:
        label = pending['options'][target]['text']
        if APPROVAL_REJECT.match(label) or (plan_menu(pending) and PLAN_MENU_OPTION.search(label)):
            on_answer('deny')
        elif re.match(r'(?:Yes|Trust|Allow|Approve|Proceed)\b', label, re.I):
            on_answer('approve')
    if feedback is not None:
        deadline = time.monotonic() + 2
        while True:
            verify(entry, run)
            screen = capture(entry, run)
            if not approval(screen) and not menu_question(screen.splitlines()[-30:]):
                try:
                    empty_prompt(entry, run)
                    break
                except DraftError:
                    raise
                except RuntimeError:
                    pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Keep planning was selected, but feedback was not sent. Check the terminal input and send your feedback again.')
            time.sleep(.05)
        submit(entry, feedback.strip(), run=run)


def shell_prompt(entry, run=subprocess.run):
    if entry.get('agent') != 'shell':
        raise RuntimeError('An agent is already running in this pane.')
    if process_start(entry['pane_pid']) != entry['pane_start']:
        raise RuntimeError('This tmux pane changed. Reload its tab.')
    proc = Path(procinfo.ROOT)
    fields = procinfo.stat_fields(entry['pane_pid'], proc)
    if procinfo.comm(entry['pane_pid'], proc) not in ('bash', 'zsh', 'fish') or fields[2] != fields[5]:
        raise RuntimeError('Wait for the foreground program to finish before starting an agent.')
    state = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'],
                 '#{pane_pid}\t#{pane_current_command}\t#{pane_in_mode}\t#{cursor_y}\t#{cursor_x}\t#{pane_width}'],
                check=True, capture_output=True, text=True, timeout=5).stdout.strip().split('\t')
    if len(state) != 6 or state[0] != str(entry['pane_pid']) or state[1] not in ('bash', 'zsh', 'fish') or state[2] != '0':
        raise RuntimeError('This pane is not at an idle shell prompt.')
    lines = run([paths.TMUX, 'capture-pane', '-p', '-N', '-t', entry['id']],
                check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
    row, column, width = int(state[3]), int(state[4]), int(state[5])
    if row >= len(lines):
        # A prompt that fills the last row puts the cursor on a new, empty row that capture-pane drops.
        lines += [''] * (row + 1 - len(lines))
    start = row
    while start > 0 and len(lines[start - 1]) == width:
        start -= 1
    prefix = ''.join(lines[start:row])  # Keep spaces at wrap boundaries until the logical line is complete.
    if lines[row][column:].strip():
        return (prefix + lines[row]).rstrip()  # text after the cursor: a draft, which the caller refuses
    before = (prefix + lines[row][:column]).rstrip()
    if not before and row > 0:
        return lines[row - 1].rstrip()  # the prompt filled the row above and the cursor wrapped
    return before


def start_agent(entry, agent, run=subprocess.run, profile=None):
    """Type the agent's launch command at the pane's idle shell prompt. profile: a Codex profile (`codex -p foundry`); None = the plan."""
    command = LAUNCHERS.get(agent)
    if not command:
        raise ValueError('Choose a supported agent.')
    if profile is not None:
        if agent != 'codex' or not PROFILE_NAME.fullmatch(profile):
            raise ValueError('Choose a supported Codex provider.')
        command += ' -p ' + profile
    prompt = shell_prompt(entry, run)
    if not re.search(r'[$#>%❯➜]$', prompt):
        raise RuntimeError('There is terminal input or an unrecognized shell prompt. Clear it before starting an agent.')
    buffer = 'tmux-mobile-launch-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=command.encode(), check=True, capture_output=True, timeout=5)
        if shell_prompt(entry, run) != prompt:
            raise RuntimeError('The shell input changed. Try again after clearing its draft.')
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 2
        while True:
            current = shell_prompt(entry, run)
            tail = current[len(prompt):].strip() if current.startswith(prompt) else current.strip()
            # zsh-autosuggestions can draw grey text after the pasted command (e.g. "claude --resume"); Enter runs only what was typed.
            if current == prompt + ' ' + command or current == prompt + command or tail == command or current.rstrip().endswith(command) or tail.startswith(command + ' '):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Could not confirm the launch command. Check the terminal before retrying.')
            time.sleep(.05)
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)


def shell_snapshot(entry, run=subprocess.run):
    # -J joins lines the pane wrapped, so a long URL stays one line that the phone can open or copy.
    text = run([paths.TMUX, 'capture-pane', '-p', '-J', '-S', '-1000', '-t', entry['id']],
               check=True, capture_output=True, text=True, timeout=5).stdout.rstrip()
    return {'messages': [{'id': 'terminal', 'role': 'assistant', 'kind': 'terminal', 'text': text}],
            'thread': None, 'agent': 'shell', 'writable': True, 'session': entry['id'], 'usage': {}, 'busy': False}


def foreground_program(entry):
    """True when a program (gcloud auth, a y/n script) runs in front of the pane's shell and reads the keyboard."""
    if entry.get('agent') != 'shell' or process_start(entry['pane_pid']) != entry['pane_start']:
        return False
    try:
        return procinfo.foreground_differs(entry['pane_pid'], Path(procinfo.ROOT))
    except OSError:
        return False


def answer_program(entry, message, run=subprocess.run):
    """Type one line into the running program, then Enter: a verification code, a password prompt answer, "y"."""
    if '\n' in message.strip():
        raise RuntimeError('A program is running in this pane. Send one line at a time.')
    state = run([paths.TMUX, 'display-message', '-p', '-t', entry['id'], '#{pane_pid}\t#{pane_in_mode}'],
                check=True, capture_output=True, text=True, timeout=5).stdout.strip().split('\t')
    if state != [str(entry['pane_pid']), '0']:
        raise RuntimeError('The terminal is scrolled or changed. Check it, then send again.')
    run([paths.TMUX, 'send-keys', '-t', entry['id'], '-l', message.strip()], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)


def shell_submit(entry, message, run=subprocess.run):
    if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
        raise RuntimeError('The command contains terminal control characters.')
    if foreground_program(entry):
        return answer_program(entry, message, run)
    prompt = shell_prompt(entry, run)
    if not re.search(r'[$#>%❯➜]$', prompt):
        raise RuntimeError('Clear the existing terminal draft before sending a command.')
    buffer = 'tmux-mobile-shell-' + uuid.uuid4().hex
    try:
        run([paths.TMUX, 'load-buffer', '-b', buffer, '-'], input=message.encode(), check=True, capture_output=True, timeout=5)
        if shell_prompt(entry, run) != prompt:
            raise RuntimeError('The shell input changed. Try again after clearing its draft.')
        run([paths.TMUX, 'paste-buffer', '-p', '-d', '-b', buffer, '-t', entry['id']], check=True, capture_output=True, timeout=5)
        time.sleep(.1)
        shell_prompt(entry, run)  # Recheck the idle foreground shell before Enter.
        run([paths.TMUX, 'send-keys', '-t', entry['id'], 'Enter'], check=True, capture_output=True, timeout=5)
    finally:
        run([paths.TMUX, 'delete-buffer', '-b', buffer], check=False, capture_output=True, timeout=5)
