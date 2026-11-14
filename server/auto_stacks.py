"""Auto stacks: the stack a helper tab (worker, reviewer, advisor) joins when an agent starts it, and that stack's name.

Rules (agent_tabs.py applies them under groups.LOCK):
- `--stack NAME`: the new tab goes in that stack, made if missing. A caller in no stack moves in too, unless the new tab
  is a `--worktree` worker (a separate task) or the user placed the caller by hand.
- A `--worktree` worker without `--stack` is a separate task: it gets its own new stack, named after the task.
- Else a caller in a stack shares it with the new tab.
- Else (caller in no stack) Tether makes a stack named after the caller's goal and moves the caller into it, unless the
  user placed the caller in no stack by hand (groups.BY). Then the new tab stays in no stack too.
A name Tether makes is never a raw prompt snippet: short_name() keeps only short, title-like names."""
import os
import re

import paths

ROLE_PREFIX = re.compile(r'^(?:advisor(?: 2)?|review|reviewer)\s*:\s*', re.I)
SENTENCE = re.compile(r'[.?!;:,]\s|[?!]$')
WORDS = 3
# A title that starts with one of these, or speaks to "you", is a request ("Can you fix this"), not a goal name.
REQUEST = frozenset('can could would will should shall please pls how what why when where who which is are am do does '
                    'did i i\'m im we let\'s lets hey hi hello help ok okay yes no thanks'.split())
SPEAKER = frozenset('you your you\'re yourself u'.split())
CHARACTERS = 32
FALLBACK_GOAL = 'Work'
FALLBACK_TASK = 'Task'


def short_name(text):
    """A clean stack name from a title, or None: 1 to 3 words, no cut-off ("…"), sentence or request. A slug ("desktop-nav",
    "feature/tab-icons") reads as words ("Desktop nav", "Tab icons"). The first letter is upper case."""
    if not isinstance(text, str):
        return None
    text = ROLE_PREFIX.sub('', ' '.join(text.split())).strip()
    if not text or text.endswith(('…', '...')) or SENTENCE.search(text):
        return None
    if ' ' not in text:
        text = ' '.join(part for part in re.split(r'[-_]+', text.rsplit('/', 1)[-1]) if part)
    text = text.rstrip('.').strip()
    words = text.lower().split()
    if not words or len(words) > WORDS or len(text) > CHARACTERS:
        return None
    if words[0] in REQUEST or SPEAKER.intersection(words):
        return None
    if any(ord(char) < 32 or char == '\x7f' for char in text):
        return None
    return text[0].upper() + text[1:]


def goal_name(caller, folder):
    """The name of a new stack for the caller's goal: the caller's tab name, its worktree branch, or its folder."""
    for candidate in (caller.get('agent_name'), caller.get('pane_name'), caller.get('worktree')):
        name = short_name(candidate)
        if name:
            return name
    if folder and os.path.abspath(folder) not in (os.path.abspath(str(paths.HOME)), os.path.abspath(os.sep)):
        name = short_name(os.path.basename(os.path.normpath(folder)))
        if name:
            return name
    return FALLBACK_GOAL


def task_name(body, branch, tab_name):
    """The name of a new stack for a separate task (a --worktree worker): its --name, its branch, or its tab name."""
    for candidate in (body.get('name'), branch, tab_name):
        name = short_name(candidate)
        if name:
            return name
    return FALLBACK_TASK
