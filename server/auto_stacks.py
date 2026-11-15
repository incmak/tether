"""Auto stacks: one stack per main session. agent_tabs.py applies the rules under groups.LOCK.

- A main session is a tab that no other agent tab started. Every helper (worker, reviewer, advisor) has a root: follow
  the helper records (helper_tabs.py, owner_pane) up to the main session. A helper of a helper has the same root. Each
  new helper record also stores root_pane, so the root stays known after a tab in between closes.
- The first helper of a root makes the stack and moves the root and the helper into it. Later helpers of that root, at
  any depth, join the root's stack. A main session with no helpers stays in Unstacked.
- --worktree does not make a stack of its own. --stack NAME only names the root's stack when it is made.
- Stack name: the root's goal in Title Case (stack_name). Never a branch, folder, bare B-number or prompt fragment.
- Tether marks the stacks it makes (groups.py "auto") and removes them when they are empty. A user rename clears it.
- A tab the user placed by hand (@group_by user) never moves."""
import re
import subprocess
import time

import groups
import helper_tabs
import paths

ROLE_PREFIX = re.compile(r'^(?:advisor(?: 2)?|review|reviewer)\s*:\s*', re.I)
ROLE_SUFFIX = re.compile(r'\s+·\s+(?:review|advisor(?: 2)?)$', re.I)
TICKET = re.compile(r'^[A-Z]{1,3}[0-9]+\b[\s:·-]*', re.I)  # "B45 advisor log", "B45: x"
SENTENCE = re.compile(r'[.?!;:,]\s|[?!]$')
WORDS = 3
# A title that starts with one of these, or speaks to "you", is a request ("Can you fix this"), not a goal name.
REQUEST = frozenset('can could would will should shall please pls how what why when where who which is are am do does '
                    'did i i\'m im we let\'s lets hey hi hello help ok okay yes no thanks'.split())
SPEAKER = frozenset('you your you\'re yourself u'.split())
AGENTS = frozenset('claude codex antigravity agent shell zsh bash'.split())
ROLES = frozenset('review reviewer reviews advisor helper worker'.split())  # "B76 review" is no goal
SMALL = frozenset('a an and as at but by for in of on or the to vs via with'.split())
CHARACTERS = 32
TAB_LIMIT = 40  # the pane name limit (server.py pane-name)
FALLBACK_GOAL = 'Work'
FALLBACK_TASK = 'Task'
NAME_BY = '@chat_name_by'  # "user" after the user named the tab in Tether (server.py pane-name)
ROLE_WORDS = {'advisor': 'advisor', 'second-advisor': 'advisor 2'}
ROOT_DEPTH = 16
SWEEP_EVERY = 30


def short_name(text):
    """A clean name from a title, or None: 1 to 3 words, no cut-off ("…"), sentence or request. A slug ("desktop-nav",
    "feature/tab-icons") reads as words ("Desktop nav", "Tab icons"). The first letter is upper case."""
    if not isinstance(text, str):
        return None
    text = ROLE_SUFFIX.sub('', ROLE_PREFIX.sub('', ' '.join(text.split()))).strip()
    if not text or text.endswith(('…', '...')) or SENTENCE.search(text):
        return None
    if ' ' not in text:
        text = ' '.join(part for part in re.split(r'[-_]+', text.rsplit('/', 1)[-1]) if part)
    text = text.rstrip('.').strip()
    words = text.lower().split()
    if not words or len(words) > WORDS or len(text) > CHARACTERS:
        return None
    if words[0] in REQUEST or SPEAKER.intersection(words) or set(words) <= AGENTS | ROLES:
        return None
    if any(ord(char) < 32 or char == '\x7f' for char in text):
        return None
    return text[0].upper() + text[1:]


def title_case(text):
    """ "header swipe" -> "Header Swipe". A word with a capital or a digit inside ("BriskBot", "B45", "UI") stays."""
    words = text.split()
    out = []
    for index, word in enumerate(words):
        if word != word.lower() or any(char.isdigit() for char in word):
            out.append(word)
        elif index and word in SMALL:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return ' '.join(out)


def goal(text):
    """A stack name from one source, or None: short_name in Title Case, without a leading ticket ("B45 advisor log" ->
    "Advisor Log"). A bare ticket ("B45") gives None."""
    if not isinstance(text, str):
        return None
    text = ROLE_SUFFIX.sub('', ROLE_PREFIX.sub('', ' '.join(text.split()))).strip()
    text = TICKET.sub('', text, count=1) if TICKET.match(text) and ' ' in text else ('' if TICKET.fullmatch(text) else text)
    name = short_name(text)
    return title_case(name) if name else None


def _path_like(name, root):
    """True when name only repeats the root's branch or folder ("tether-wt" -> "Tether wt")."""
    plain = name.lower().replace(' ', '')
    for value in (root.get('worktree'), root.get('folder'), root.get('cwd')):
        if isinstance(value, str) and value:
            base = value.rstrip('/').rsplit('/', 1)[-1]
            if re.sub(r'[-_\s]+', '', base.lower()) == plain:
                return True
    return False


def stack_name(root, stack=None, task=None):
    """The name of a new stack for a root (main session) entry. Sources in order: the user's own name for the root
    (/rename, or a rename in Tether: root['named_by_user']), the --stack name, a short name of the root's goal (its tab
    name), then the first helper's task. root may have 'folder' and 'worktree', which never name a stack."""
    user = [root.get('agent_name')] + ([root.get('pane_name')] if root.get('named_by_user') else [])
    for candidate in user + [stack, root.get('pane_name'), task]:
        name = goal(candidate)
        if name and not (candidate is not stack and _path_like(name, root)):
            return name
    return FALLBACK_GOAL


def tab_task(entry):
    """The task a tab works on, from its name, for its helpers' names ("B45 advisor log · review" -> "B45 Advisor Log")."""
    for candidate in (entry.get('pane_name'), entry.get('agent_name')):
        name = short_name(candidate)
        if name:
            return title_case(name)
    return None


def _fit(task, suffix):
    """task + suffix in TAB_LIMIT characters: drop whole words from the end of task first."""
    words = task.split()
    while len(words) > 1 and len(' '.join(words)) + len(suffix) > TAB_LIMIT:
        words.pop()
    return ' '.join(words)[:TAB_LIMIT - len(suffix)].rstrip() + suffix


def helper_tab_name(*, role=None, review=False, name=None, branch=None, stack=None, message='', caller=None,
                    stack_goal=None):
    """The visible name of a new helper tab. Worker: its task in Title Case ("Header Swipe"). Reviewer: "<task> · review".
    Advisor: "<task or stack goal> · advisor". Never a bare agent name or a prompt fragment."""
    if role or review:
        task = tab_task(caller or {}) or stack_goal or FALLBACK_TASK
        return _fit(task, ' · ' + (ROLE_WORDS.get(role, 'advisor') if role else 'review'))
    task = short_name(name)
    if task:
        return title_case(task)
    if isinstance(name, str) and name.strip() and name.strip().lower() not in AGENTS:
        return ' '.join(name.split())[:TAB_LIMIT]  # the agent chose it; keep it rather than guess
    first_line = message.strip().split('\n', 1)[0] if isinstance(message, str) else ''
    for candidate in (branch, first_line, stack):
        task = short_name(candidate)
        if task:
            return title_case(task)
    return stack_goal or FALLBACK_TASK


def root_of(pane, thread_of, run=subprocess.run):
    """The main session of a tab: follow helper records to the tab no other tab started. A record whose owner now holds
    another conversation (thread_of(pane) differs from owner_thread) is a broken link: the chain stops there.
    Returns a pane id. It may be a closed tab when the root has closed."""
    current, seen = pane, {pane}
    for _ in range(ROOT_DEPTH):
        exists, record = helper_tabs.load(current, run)
        if not exists or record is None:
            return current
        root = record.get('root_pane')
        if isinstance(root, str) and groups.PANE.fullmatch(root) and root != current:
            return root
        owner = record['owner_pane']
        known = thread_of(owner)
        if owner in seen or (record.get('owner_thread') and known and known != record['owner_thread']):
            return current
        seen.add(owner)
        current = owner
    return current


def named_by_user(pane, run=subprocess.run):
    shown = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{' + NAME_BY + '}'],
                capture_output=True, text=True, timeout=5)
    return shown.returncode == 0 and shown.stdout.strip() == 'user'


def watch(run=subprocess.run, sleep=time.sleep, restoring=lambda: False):
    """Service timer: remove Tether-made stacks whose last tab closed. It waits while auto-resume reopens tabs, whose
    stacks are back before their tabs are."""
    while True:
        try:
            if not restoring():
                groups.drop_empty_auto(run)
        except Exception:  # a tmux hiccup must not stop the timer
            pass
        sleep(SWEEP_EVERY)
