#!/usr/bin/env python3
"""Auto-merge by risk (B213) and the trust ladder (B214).

Tether merges a worker branch into the main branch on origin without the user only when every check passes:

  approval   the reviewer approved exactly the commit at the head of the branch
  branch     the tab works in a linked worktree, on a branch, with no uncommitted changes
  rules      the main branch has docs/auto-merge.json, which names the areas that may auto-merge
  stamp      the head commit has a "Verified:" stamp with a tests(...) item
  scope      every changed file is a regular text file in an area, and each area is within its trust limit
  checklist  scripts/checklist.py on this server finds no reliability-checklist row for the diff
  merge      the commit merges into the main branch with no conflict

Each unknown fails closed: a git error, a timeout, a missing file or a missing approval means "wait for the user".

Modes (paths.CONFIG/auto-merge): "off", "observe" (the default, and what a missing or bad file means) and "enforce".
Observe records the decision and merges nothing. Enforce makes one merge commit (parents: the main branch, the approved
commit) and pushes it with a normal push, never a forced one. `git revert -m 1 <merge>` undoes it. A merge is not a
deploy.

The trust ladder gives each area a limit on changed lines. The limit widens after clean merges that Tether made and
shrinks on a revert. It is computed from the decision log and the history of the main branch.

  auto_merge.py check [--cwd DIR] [--approved SHA]   print the decision for a branch. It never merges.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import paths  # noqa: E402

MODES = (
    ('off', 'Off', 'Tether does not check branches.'),
    ('observe', 'Observe', 'Tether records what it would merge. It merges nothing.'),
    ('enforce', 'Enforce', 'Tether merges a branch into the main branch when every check passes.'),
)
MODE_NAMES = tuple(key for key, _, _ in MODES)
DEFAULT_MODE = 'observe'
RULES_FILE = 'docs/auto-merge.json'
REMOTE = 'origin'
SHA = re.compile(r'[0-9a-f]{40}')
AREA_NAME = re.compile(r'[a-z][a-z0-9-]{0,23}')
MAX_AREAS = 8
MAX_PATTERNS = 40
MAX_FILES = 300          # a larger diff waits for the user
MODES_OK = ('100644', '100755')
GIT_TIMEOUT = 15
NET_TIMEOUT = 45
HISTORY = 3000           # commits of the main branch the ladder reads
LOG_KEEP = 2000          # decision records kept
SHOWN = 30               # decision records the Settings page gets
APPROVAL_HOURS = 24      # an older approval no longer merges: the branch needs a new review

# The trust ladder. A rung is the most changed lines (added + deleted) one merge may have in an area. The top rung
# stays at the size where the reviewer rules call a change critical (about 300 lines).
RUNGS = (0, 60, 150, 300)
START_RUNG = 1
CLEAN_MERGES = 5         # clean merges in an area that move it up one rung
REVERT = re.compile(r'This reverts commit ([0-9a-f]{40})')

_lock = threading.Lock()
_ladder_cache = {}
RUN = subprocess.run     # tests replace this to prove that observe mode never writes


class AutoMergeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GitError(Exception):
    pass


# ---- the mode ----

def mode_path():
    return paths.CONFIG / 'auto-merge'


def log_path():
    return paths.CACHE / 'auto-merge.jsonl'


def trust_path():
    return paths.CONFIG / 'auto-merge-trust.json'


def read_setting():
    """{'mode', 'since'}. A missing, unreadable or malformed file means observe. It never means enforce."""
    try:
        data = json.loads(mode_path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return {'mode': DEFAULT_MODE, 'since': 0}
    since = data.get('since') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('mode') not in MODE_NAMES or isinstance(since, bool) \
            or not isinstance(since, (int, float)) or since < 0:
        return {'mode': DEFAULT_MODE, 'since': 0}
    return {'mode': data['mode'], 'since': int(since)}


def read_mode():
    return read_setting()['mode']


def _write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_mode(mode):
    """Save the mode and when it began. Enforce merges only approvals that arrive after that time."""
    if mode not in MODE_NAMES:
        raise AutoMergeError(400, 'Choose Off, Observe or Enforce.')
    if read_setting()['mode'] != mode or not mode_path().exists():
        _write(mode_path(), json.dumps({'mode': mode, 'since': int(time.time())}) + '\n')


# ---- git ----

def _git(folder, *args, timeout=GIT_TIMEOUT, check=True, env=None):
    """Run git with a clean environment. Raise GitError on a failure, a timeout or a missing git."""
    full = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        full.pop(key, None)
    full.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C', GIT_OPTIONAL_LOCKS='0')
    full.update(env or {})
    try:
        result = RUN(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                     env=full, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f'git {args[0]} did not run: {type(exc).__name__}') from exc
    if check and result.returncode:
        lines = (result.stderr or '').strip().splitlines()
        raise GitError(f'git {args[0]} failed: ' + (lines[-1][:200] if lines else f'exit {result.returncode}'))
    return result


def _sha(folder, revision):
    text = _git(folder, 'rev-parse', '--verify', '--quiet', revision + '^{commit}').stdout.strip()
    if not SHA.fullmatch(text):
        raise GitError(f'{revision} is not a commit.')
    return text


def main_checkout(folder):
    common = Path(_git(folder, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    return str(common.parent if common.name == '.git' else common)


def target_branch(folder):
    """The main branch on origin: origin's HEAD when git knows it, else main, else master."""
    head = _git(folder, 'symbolic-ref', '--quiet', '--short', f'refs/remotes/{REMOTE}/HEAD', check=False)
    names = [head.stdout.strip()[len(REMOTE) + 1:]] if head.returncode == 0 and head.stdout.strip().startswith(REMOTE + '/') else []
    for name in (*names, 'main', 'master'):
        if _git(folder, 'show-ref', '--verify', '--quiet', f'refs/remotes/{REMOTE}/{name}', check=False).returncode == 0:
            return name
    raise GitError(f'The repository has no {REMOTE}/main branch.')


# ---- the rules file on the main branch ----

def _pattern(text):
    """A path pattern as a regular expression: ** crosses folders, * and ? stay inside one name."""
    out = []
    index = 0
    while index < len(text):
        if text.startswith('**', index):
            out.append('.*')
            index += 2
        elif text[index] == '*':
            out.append('[^/]*')
            index += 1
        elif text[index] == '?':
            out.append('[^/]')
            index += 1
        else:
            out.append(re.escape(text[index]))
            index += 1
    return re.compile(''.join(out))


def parse_rules(text):
    """[{'name', 'label', 'paths': [regex]}] from docs/auto-merge.json, or raise ValueError."""
    data = json.loads(text)
    areas = data.get('areas') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(areas, list) or not 0 < len(areas) <= MAX_AREAS:
        raise ValueError('bad rules')
    found = []
    for area in areas:
        if not isinstance(area, dict):
            raise ValueError('bad area')
        name, label, patterns = area.get('name'), area.get('label'), area.get('paths')
        if not isinstance(name, str) or not AREA_NAME.fullmatch(name) or any(item['name'] == name for item in found):
            raise ValueError('bad area name')
        if not isinstance(label, str) or not 0 < len(label) <= 40 or not label.isprintable():
            raise ValueError('bad area label')
        if not isinstance(patterns, list) or not 0 < len(patterns) <= MAX_PATTERNS or not all(
                isinstance(item, str) and 0 < len(item) <= 200 and not item.startswith(('/', '*')) and '..' not in item.split('/')
                for item in patterns):
            raise ValueError('bad area paths')
        found.append({'name': name, 'label': label, 'paths': [_pattern(item) for item in patterns]})
    return found


def rules_at(folder, revision):
    """The areas from the rules file at a commit of the main branch. Never from the worker branch."""
    shown = _git(folder, 'show', f'{revision}:{RULES_FILE}', check=False)
    if shown.returncode:
        return None
    try:
        return parse_rules(shown.stdout)
    except (ValueError, re.error):
        return None


def area_of(rules, path):
    for area in rules:
        if any(pattern.fullmatch(path) for pattern in area['paths']):
            return area['name']
    return None


# ---- the diff ----

def changed(folder, base, head):
    """[{'path', 'old_mode', 'new_mode', 'lines'}] for base..head. A rename gives both sides. lines is None for a
    binary file."""
    raw = _git(folder, 'diff', '--raw', '-z', '--no-renames', '--no-abbrev', base, head).stdout.split('\0')
    files = {}
    index = 0
    while index + 1 < len(raw):
        meta, path = raw[index], raw[index + 1]
        index += 2
        parts = meta.split(' ')
        if len(parts) != 5 or not meta.startswith(':'):
            raise GitError('git diff printed a line Tether does not know.')
        files[path] = {'path': path, 'old_mode': parts[0][1:], 'new_mode': parts[1], 'lines': None}
    counts = _git(folder, 'diff', '--numstat', '-z', '--no-renames', base, head).stdout.split('\0')
    for item in counts:
        if not item:
            continue
        parts = item.split('\t', 2)
        if len(parts) != 3 or parts[2] not in files:
            raise GitError('git diff printed a count Tether does not know.')
        if parts[0].isdigit() and parts[1].isdigit():
            files[parts[2]]['lines'] = int(parts[0]) + int(parts[1])
    return list(files.values())


def classify(rules, files):
    """({area: lines}, [problem text]) for a diff. Any problem means the diff is out of scope."""
    areas = {}
    problems = []
    if not files:
        return areas, ['The branch changes no files.']
    if len(files) > MAX_FILES:
        return areas, [f'The diff changes {len(files)} files. The limit is {MAX_FILES}.']
    outside = []
    for item in files:
        area = area_of(rules, item['path'])
        if area is None:
            outside.append(item['path'])
        elif item['new_mode'] not in (*MODES_OK, '000000') or item['old_mode'] not in (*MODES_OK, '000000'):
            problems.append(f"{item['path']} is a link or a submodule, not a regular file.")
        elif '000000' not in (item['old_mode'], item['new_mode']) and item['old_mode'] != item['new_mode']:
            problems.append(f"{item['path']} changes its file mode.")
        elif item['new_mode'] == '100755' and item['old_mode'] == '000000':
            problems.append(f"{item['path']} is a new executable file.")
        elif item['lines'] is None:
            problems.append(f"{item['path']} is a binary file.")
        else:
            areas[area] = areas.get(area, 0) + item['lines']
    if outside:
        more = f' and {len(outside) - 3} more' if len(outside) > 3 else ''
        problems.insert(0, 'Files outside the auto-merge areas: ' + ', '.join(outside[:3]) + more + '.')
    return areas, problems[:6]


# ---- the stamp ----

def _verification():
    """scripts/verification.py, which owns the stamp format. None when this install does not have it."""
    path = HERE.parent / 'scripts' / 'verification.py'
    try:
        spec = importlib.util.spec_from_file_location('tether_verification', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, AttributeError, SyntaxError):
        return None


def stamp_items(message, verification=None):
    """{kind: detail} from the last "Verified:" line of a commit message. None for no stamp, a bad one or "none"."""
    verification = verification or _verification()
    if verification is None:
        return None
    found = verification.FOUND.findall(message or '')
    if not found:
        return None
    try:
        stamp = verification.valid(found[-1])
    except ValueError:
        return None
    if stamp.startswith('none'):
        return None
    return dict(re.findall(r'([a-z][a-z-]*)\(([^()\n]+)\)', stamp))


def merge_stamp(items, reviewer, head, verification=None):
    """The stamp of the merge commit: the head commit's items, with the review that Tether itself recorded."""
    verification = verification or _verification()
    agent = reviewer if isinstance(reviewer, str) and re.fullmatch(r'[a-z]{1,20}', reviewer) else 'reviewer'
    kept = [f'{kind}({detail})' for kind, detail in items.items() if kind != 'review']
    return verification.valid(' '.join([*kept, f'review({agent} APPROVED {head[:12]})']))


# ---- the reliability checklist (B209) ----

def checklist_rows(folder, base, head):
    """The checklist rows the diff hits, as texts. None when the checklist cannot answer: that counts as a hit."""
    script = HERE.parent / 'scripts' / 'checklist.py'
    if not script.is_file():
        return None
    try:
        result = RUN([sys.executable, str(script), 'hits', '--repo', str(folder), '--json', f'{base}..{head}'],
                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        rows = json.loads(result.stdout) if result.returncode in (0, 1) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return [str(row.get('title') or row.get('id') or 'row')[:120] if isinstance(row, dict) else str(row)[:120] for row in rows]


# ---- the decision log ----

def _records():
    try:
        lines = log_path().read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get('outcome'), str):
            found.append(record)
    return found


def records():
    with _lock:
        return _records()


def record(decision):
    """Add one decision. A repeat of the last decision for the same commit is not added again."""
    key = ('root', 'head', 'mode', 'outcome')
    marks = [item['ok'] for item in decision.get('checks', [])]
    with _lock:
        existing = _records()
        last = next((item for item in reversed(existing)
                     if item.get('root') == decision.get('root') and item.get('head') == decision.get('head')), None)
        if last is not None and all(last.get(name) == decision.get(name) for name in key) \
                and [item.get('ok') for item in last.get('checks', []) if isinstance(item, dict)] == marks \
                and decision['outcome'] in ('wait', 'would_merge'):
            return False
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(existing) >= LOG_KEEP + 200:
            # Merges feed the trust ladder, so they stay. A lost merge can only lower trust.
            cut = len(existing) - LOG_KEEP
            kept = [item for index, item in enumerate(existing) if index >= cut or item.get('outcome') == 'merged']
            _write(path, ''.join(json.dumps(item) + '\n' for item in kept))
        with open(path, 'ab') as stream:
            stream.write((json.dumps(decision) + '\n').encode())
        return True


# ---- the trust ladder (B214) ----

def _resets():
    try:
        data = json.loads(trust_path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stat(path):
    try:
        found = path.stat()
        return found.st_mtime_ns, found.st_size
    except OSError:
        return None


def ladder(root, rules, target_sha):
    """{area: {'rung', 'limit', 'streak', 'merges', 'reverts', 'last_revert'}} for one repository.

    Every area starts at START_RUNG. In time order: a merge that Tether made and that is on the main branch adds one
    to the area's count, and CLEAN_MERGES of them move the area up one rung. A revert on the main branch of a commit
    that touched the area moves it down one rung and clears the count. At rung 0 only the user restores the area.
    Commits that agents push with their own stamps never count: a stamp is a claim, not evidence."""
    names = [area['name'] for area in rules]
    key = (root, target_sha, tuple(names), tuple(tuple(p.pattern for p in area['paths']) for area in rules),
           _stat(log_path()), _stat(trust_path()))
    with _lock:
        cached = _ladder_cache.get(root)
        if cached and cached[0] == key:
            return cached[1]
    state = {name: {'rung': START_RUNG, 'streak': 0, 'merges': 0, 'reverts': 0, 'last_revert': None} for name in names}
    log = _git(root, 'log', '--first-parent', f'-n{HISTORY}', '--format=%H%x1f%ct%x1f%B%x1e', target_sha).stdout
    commits = []
    for item in log.split('\x1e'):
        parts = item.strip('\n').split('\x1f')
        if len(parts) == 3 and SHA.fullmatch(parts[0]) and parts[1].isdigit():
            commits.append((parts[0], int(parts[1]), parts[2]))
    on_main = {sha: when for sha, when, _ in commits}
    added = _git(root, 'log', '--first-parent', '--diff-filter=A', '--format=%ct', target_sha, '--', RULES_FILE).stdout.split()
    epoch = int(added[-1]) if added and added[-1].isdigit() else None
    events = []
    for item in records():
        merged = item.get('merge')
        if item.get('root') == root and item.get('outcome') == 'merged' and merged in on_main \
                and isinstance(item.get('areas'), dict):
            events.append((on_main[merged], 0, 'merge', [name for name in item['areas'] if name in state], merged))
    for sha, when, body in commits:
        if epoch is None or when < epoch:
            continue
        for reverted in REVERT.findall(body):
            try:
                if _git(root, 'merge-base', '--is-ancestor', reverted, sha, check=False).returncode:
                    continue
                if REVERT.search(_git(root, 'log', '-1', '--format=%B', reverted).stdout):
                    continue  # a revert of a revert puts the work back: it says nothing new about the area
                touched = {area_of(rules, file['path']) for file in changed(root, reverted + '^1', reverted)} - {None}
            except GitError:
                touched = set(names)  # unknown: every area loses a rung
            if touched:
                events.append((when, 1, 'revert', sorted(touched), sha))
    resets = _resets().get(root)
    for name, times in (resets.items() if isinstance(resets, dict) else ()):
        if name in state and isinstance(times, list):
            events += [(when, 2, 'reset', [name], None) for when in times
                       if isinstance(when, (int, float)) and not isinstance(when, bool)]
    for when, _, kind, areas, sha in sorted(events, key=lambda event: event[:2]):
        for name in areas:
            area = state[name]
            if kind == 'merge':
                area['merges'] += 1
                area['streak'] += 1
                if area['streak'] >= CLEAN_MERGES and 0 < area['rung'] < len(RUNGS) - 1:
                    area['rung'] += 1
                    area['streak'] = 0
            elif kind == 'revert':
                area['rung'] = max(0, area['rung'] - 1)
                area['streak'] = 0
                area['reverts'] += 1
                area['last_revert'] = {'commit': sha, 'at': when}
            elif area['rung'] == 0:
                area['rung'] = START_RUNG
                area['streak'] = 0
    for area in state.values():
        area['limit'] = RUNGS[area['rung']]
    with _lock:
        _ladder_cache[root] = (key, state)
    return state


def restore(root, name):
    """The user puts an area that fell to rung 0 back on the first rung."""
    if not isinstance(root, str) or not isinstance(name, str) or not AREA_NAME.fullmatch(name):
        raise AutoMergeError(400, 'Choose an area.')
    try:
        if main_checkout(root) != root:
            raise AutoMergeError(400, 'Choose a repository.')
        target = target_branch(root)
        target_sha = _sha(root, f'refs/remotes/{REMOTE}/{target}')
        rules = rules_at(root, target_sha)
        levels = ladder(root, rules, target_sha) if rules else {}
    except GitError as exc:
        raise AutoMergeError(400, str(exc)) from exc
    if name not in levels or levels[name]['rung'] != 0:
        raise AutoMergeError(409, 'Only an area at zero trust can be restored.')
    with _lock:
        data = _resets()
        data.setdefault(root, {}).setdefault(name, []).append(int(time.time()))
        data[root][name] = data[root][name][-50:]
        _write(trust_path(), json.dumps(data) + '\n')


# ---- the decision ----

def evaluate(folder, approved, *, fetch=True):
    """(decision, plan) for the branch checked out in `folder`. It writes nothing but the objects of a trial merge.

    decision: the record for the log and the Settings page. plan: what merge() needs, or None when a check failed."""
    decision = {'at': int(time.time()), 'folder': str(folder), 'approved': approved if isinstance(approved, str) else None,
                'checks': [], 'areas': {}, 'outcome': 'wait'}
    checks = decision['checks']

    def check(key, ok, text):
        checks.append({'key': key, 'ok': bool(ok), 'text': text})
        return bool(ok)

    try:
        root = main_checkout(folder)
        head = _sha(folder, 'HEAD')
        branch = _git(folder, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip()
        target = target_branch(folder)
        decision.update(root=root, repo=Path(root).name, head=head, branch=branch, target=f'{REMOTE}/{target}')
        fetched = None
        if fetch:
            try:
                _git(folder, 'fetch', '--no-tags', REMOTE, target, timeout=NET_TIMEOUT)
            except GitError as exc:
                fetched = str(exc)
        target_sha = _sha(folder, f'refs/remotes/{REMOTE}/{target}')
        if _git(folder, 'merge-base', '--is-ancestor', head, target_sha, check=False).returncode == 0:
            decision['outcome'] = 'already'
            return decision, None
        base = _git(folder, 'merge-base', target_sha, head).stdout.strip()
        if not SHA.fullmatch(base):
            raise GitError('The branch shares no history with the main branch.')
    except GitError as exc:
        check('git', False, str(exc))
        return decision, None
    if fetched:
        check('git', False, f'Could not fetch {REMOTE}: {fetched}')

    if not isinstance(approved, str) or not SHA.fullmatch(approved):
        check('approval', False, 'No reviewer approval is tied to a commit of this branch.')
    elif approved != head:
        check('approval', False, f'The reviewer approved {approved[:12]}, but the branch is at {head[:12]}.')
    else:
        check('approval', True, f'The reviewer approved commit {head[:12]}.')

    try:
        linked = _git(folder, 'rev-parse', '--path-format=absolute', '--git-dir').stdout.strip() != \
            _git(folder, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
        dirty = _git(folder, 'status', '--porcelain', '--untracked-files=no').stdout.strip()
        if not branch or branch == target:
            check('branch', False, 'The tab is not on a worker branch.')
        elif not linked:
            check('branch', False, 'The tab works in the main checkout, not in a worktree of its own.')
        elif dirty:
            check('branch', False, 'The worktree has uncommitted changes.')
        else:
            check('branch', True, f'Worker branch {branch}, with no uncommitted changes.')
    except GitError as exc:
        check('branch', False, str(exc))

    rules = None
    try:
        rules = rules_at(folder, target_sha)
    except GitError:
        pass
    check('rules', rules is not None,
          f'The areas come from {RULES_FILE} on {REMOTE}/{target}.' if rules is not None
          else f'{REMOTE}/{target} has no valid {RULES_FILE}, so no area may auto-merge.')

    items = None
    subject = ''
    try:
        message = _git(folder, 'log', '-1', '--format=%B', head).stdout
        subject = message.strip().splitlines()[0][:200] if message.strip() else ''
        items = stamp_items(message)
    except GitError:
        pass
    decision['subject'] = subject
    if items is None:
        check('stamp', False, 'The head commit has no valid "Verified:" stamp.')
    elif 'tests' not in items:
        check('stamp', False, 'The "Verified:" stamp of the head commit has no tests(...) item.')
    else:
        check('stamp', True, f"The stamp names tests({items['tests']}). The review is the approval above.")

    files = None
    try:
        files = changed(folder, base, head)
    except GitError as exc:
        check('scope', False, str(exc))
    if files is not None and rules is None:
        check('scope', False, 'Without the rules file, every file is outside the auto-merge areas.')
    elif files is not None:
        areas, problems = classify(rules, files)
        decision['areas'] = areas
        decision['files'] = len(files)
        labels = {area['name']: area['label'] for area in rules}
        try:
            levels = ladder(root, rules, target_sha)
            for name, lines in areas.items():
                limit = levels[name]['limit']
                if limit == 0:
                    problems.append(f'{labels[name]}: trust is at zero after a revert. The user merges this area.')
                elif lines > limit:
                    problems.append(f'{labels[name]}: {lines} changed lines. The trust limit for this area is {limit}.')
        except GitError as exc:
            problems.append(f'The trust ladder could not be read: {exc}')
        if problems:
            check('scope', False, ' '.join(problems[:6]))
        else:
            check('scope', True, 'Every file is in an area within its trust limit: '
                  + ', '.join(f'{labels[name]} {lines} lines' for name, lines in areas.items()) + '.')

    rows = checklist_rows(folder, base, head)
    if rows is None:
        check('checklist', False, 'The reliability checklist gave no answer on this server, so the diff counts as a hit.')
    elif rows:
        check('checklist', False, 'Reliability checklist rows hit: ' + '; '.join(rows[:4])
              + (f'; and {len(rows) - 4} more' if len(rows) > 4 else '') + '.')
    else:
        check('checklist', True, 'The diff hits no reliability checklist row.')

    tree = None
    try:
        trial = _git(folder, 'merge-tree', '--write-tree', '--no-messages', target_sha, head, check=False)
        first = trial.stdout.split('\n', 1)[0].strip()
        if trial.returncode == 0 and SHA.fullmatch(first):
            tree = first
            check('merge', True, f'The commit merges into {REMOTE}/{target} with no conflict.')
        elif trial.returncode == 1:
            check('merge', False, f'The branch conflicts with {REMOTE}/{target}. Rebase it, then ask for a new review.')
        else:
            check('merge', False, 'git could not try the merge.')
    except GitError as exc:
        check('merge', False, str(exc))

    if not all(item['ok'] for item in checks):
        return decision, None
    decision['outcome'] = 'pass'
    return decision, {'folder': str(folder), 'root': root, 'head': head, 'branch': branch, 'target': target,
                      'target_sha': target_sha, 'tree': tree, 'items': items, 'subject': subject,
                      'paths': {item['path'] for item in files}, 'areas': decision['areas']}


# ---- the merge (enforce mode only) ----

def merge(plan, reviewer):
    """Make the merge commit and push it. Return its hash. Raise GitError when anything is in doubt.

    The push is a normal one: git refuses it when the main branch moved, and nothing is forced. No working tree and
    no local branch changes, so other sessions' uncommitted work in the main checkout is never touched."""
    folder = plan['folder']
    stamp = merge_stamp(plan['items'], reviewer, plan['head'])
    areas = ', '.join(f'{name} {lines} lines' for name, lines in plan['areas'].items())
    message = (f"Merge branch '{plan['branch']}' into {plan['target']} (Tether auto-merge)\n\n"
               f"{plan['subject']}\n\n"
               'Tether merged this branch without the user: every auto-merge check passed.\n'
               f"Approved commit: {plan['head']}\n"
               f'Areas: {areas}\n'
               'Undo: git revert -m 1 <this commit>\n\n'
               f'Verified: {stamp}\n')
    identity = {}
    if _git(folder, 'config', 'user.name', check=False).returncode or _git(folder, 'config', 'user.email', check=False).returncode:
        identity = {'GIT_AUTHOR_NAME': 'Tether auto-merge', 'GIT_AUTHOR_EMAIL': 'tether@localhost',
                    'GIT_COMMITTER_NAME': 'Tether auto-merge', 'GIT_COMMITTER_EMAIL': 'tether@localhost'}
    made = _git(folder, 'commit-tree', plan['tree'], '-p', plan['target_sha'], '-p', plan['head'], '-m', message,
                env=identity).stdout.strip()
    if not SHA.fullmatch(made):
        raise GitError('git made no merge commit.')
    landed = {item['path'] for item in changed(folder, plan['target_sha'], made)}
    if not landed or not landed <= plan['paths']:
        raise GitError('The merge would change files that the reviewed diff does not.')
    if read_mode() != 'enforce':
        raise GitError('Auto-merge left enforce mode before the push.')
    _git(folder, 'push', REMOTE, f"{made}:refs/heads/{plan['target']}", timeout=NET_TIMEOUT)
    return made


_merge_lock = threading.Lock()


def process(job, alert=None):
    """Decide one approved commit and, in enforce mode, merge it. Returns the recorded decision, or None.

    job: {'folder', 'commit', 'approved_at', 'worker', 'name', 'reviewer', 'reviewer_agent'}. The mode is read here,
    when the work runs, not when the approval was seen."""
    setting = read_setting()
    if setting['mode'] == 'off':
        return None
    decision, plan = evaluate(job['folder'], job.get('commit'))
    if decision['outcome'] == 'already':
        return None
    decision.update(mode=setting['mode'], worker=job.get('worker'), name=job.get('name'), reviewer=job.get('reviewer'))
    # A folder that git could not read has no repository and no head: the log then names the folder and the approval.
    decision.setdefault('root', decision.pop('folder'))
    decision.setdefault('head', job.get('commit'))
    decision.pop('folder', None)
    at = job.get('approved_at')
    timed = isinstance(at, (int, float)) and not isinstance(at, bool)
    if plan is None:
        decision['outcome'] = 'wait'
    elif setting['mode'] != 'enforce':
        decision['outcome'] = 'would_merge'
    elif not timed or at < setting['since']:
        decision['outcome'] = 'would_merge'
        decision['note'] = 'The approval is older than enforce mode. Only a new approval can merge.'
    elif time.time() - at > APPROVAL_HOURS * 3600:
        decision['outcome'] = 'would_merge'
        decision['note'] = f'The approval is older than {APPROVAL_HOURS} hours. Only a new approval can merge.'
    else:
        with _merge_lock:
            try:
                try:
                    decision['merge'] = merge(plan, job.get('reviewer_agent'))
                except GitError:
                    # The main branch may have moved between the fetch and the push. Decide again, once.
                    again, plan = evaluate(job['folder'], job.get('commit'))
                    if plan is None:
                        raise
                    decision['checks'] = again['checks']
                    decision['merge'] = merge(plan, job.get('reviewer_agent'))
                decision['outcome'] = 'merged'
            except GitError as exc:
                decision['outcome'] = 'failed'
                decision['error'] = str(exc)
    added = record(decision)
    if added and alert is not None and decision['outcome'] in ('merged', 'failed'):
        name = decision.get('name') or decision.get('branch') or 'A branch'
        try:
            if decision['outcome'] == 'merged':
                alert('Auto-merged', f"“{name}”: Tether merged {decision['head'][:12]} into {decision['target']} as "
                      f"{decision['merge'][:12]}. It is not deployed. Undo: git revert -m 1 {decision['merge'][:12]}",
                      decision.get('worker'))
            else:
                alert('Auto-merge failed', f"“{name}”: every check passed, but the merge did not happen: "
                      f"{decision['error']} Merge it yourself.", decision.get('worker'))
        except Exception:  # a failed notification must not lose the decision
            pass
    return decision


# ---- the watcher: one call per monitor tick ----

_seen = set()
_queue = []
_wake = threading.Condition()
_thread = None


def _work(alert):
    while True:
        with _wake:
            while not _queue:
                _wake.wait()
            job = _queue.pop(0)
        try:
            process(job, alert)
        except Exception as exc:  # one odd repository must never stop the next decision
            print(f'auto-merge failed: {exc!r}', file=sys.stderr, flush=True)


def observe(entries, approvals, folder_of, alert=None, start=True):
    """Queue each approved commit once. approvals: review_links.approved_commits(). Git runs on another thread, so
    the monitor never waits for it. Returns the jobs it queued."""
    global _thread
    if not approvals or read_mode() == 'off':
        return []
    by_pane = {entry.get('id'): entry for entry in entries}
    jobs = []
    for worker, reviewers in approvals.items():
        entry = by_pane.get(worker)
        if entry is None or not isinstance(reviewers, dict):
            continue
        for reviewer, approval in reviewers.items():
            commit = approval.get('commit') if isinstance(approval, dict) else None
            if not isinstance(commit, str) or not SHA.fullmatch(commit) or (worker, reviewer, commit) in _seen:
                continue
            try:
                folder = folder_of(entry)
            except (OSError, KeyError, ValueError, TypeError):
                folder = None
            if not folder:
                continue
            _seen.add((worker, reviewer, commit))
            jobs.append({'folder': folder, 'commit': commit, 'approved_at': approval.get('at'), 'worker': worker,
                         'name': str(entry.get('name') or '')[:80], 'reviewer': reviewer,
                         'reviewer_agent': (by_pane.get(reviewer) or {}).get('agent')})
    if jobs and start:
        with _wake:
            if _thread is None:
                _thread = threading.Thread(target=_work, args=(alert,), daemon=True, name='auto-merge')
                _thread.start()
            _queue.extend(jobs)
            del _queue[:-50]
            _wake.notify()
    return jobs


# ---- HTTP (/mobile/api/auto-merge). server.py checks sign-in, Origin and X-Codex-Mobile first. ----

PATH = '/mobile/api/auto-merge'


def handles(path):
    return path.split('?', 1)[0] == PATH


def _trust(root, known):
    """One repository's ladder for the Settings page. known: the repository appears in the decision log."""
    entry = {'root': root, 'repo': Path(root).name, 'areas': [], 'error': ''}
    try:
        target = target_branch(root)
        target_sha = _sha(root, f'refs/remotes/{REMOTE}/{target}')
        entry['target'] = f'{REMOTE}/{target}'
        rules = rules_at(root, target_sha)
        if rules is None:
            entry['error'] = f'{REMOTE}/{target} has no valid {RULES_FILE}. Nothing auto-merges in this repository.'
            return entry if known else None
        levels = ladder(root, rules, target_sha)
    except GitError as exc:
        entry['error'] = str(exc)
        return entry if known else None
    for area in rules:
        level = levels[area['name']]
        entry['areas'].append({'name': area['name'], 'label': area['label'], 'rung': level['rung'], 'limit': level['limit'],
                               'streak': level['streak'], 'need': CLEAN_MERGES, 'top': level['rung'] == len(RUNGS) - 1,
                               'merges': level['merges'], 'reverts': level['reverts'], 'last_revert': level['last_revert']})
    return entry


def state(roots=()):
    setting = read_setting()
    found = records()
    shown = [{key: item.get(key) for key in ('at', 'mode', 'outcome', 'repo', 'root', 'branch', 'head', 'target', 'name',
                                              'subject', 'checks', 'areas', 'files', 'merge', 'error', 'note')}
             for item in reversed(found[-SHOWN:])]
    logged = []
    for item in reversed(found):
        if isinstance(item.get('root'), str) and item['root'] not in logged:
            logged.append(item['root'])
    trust = []
    for root in [*logged[:4], *[root for root in roots if root not in logged][:4]]:
        entry = _trust(root, root in logged)
        if entry is not None:
            trust.append(entry)
    return {'mode': setting['mode'], 'since': setting['since'],
            'modes': [{'mode': key, 'name': name, 'summary': summary} for key, name, summary in MODES],
            'decisions': shown, 'trust': trust, 'rungs': list(RUNGS), 'rules_file': RULES_FILE}


def get(handler, roots=()):
    handler.respond(200, state(roots))


def post(handler, roots=()):
    """{mode} sets the mode. {restore: {root, area}} puts an area at zero trust back on the first rung."""
    try:
        if handler.headers.get('X-Tether-Hook'):
            # Agent tools sign in with the hook credential. The mode and the trust are the user's to change.
            raise AutoMergeError(403, 'Only the user changes auto-merge, in Settings.')
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 4096:
            raise AutoMergeError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise AutoMergeError(400, 'Invalid request.')
        if 'restore' in body:
            item = body['restore'] if isinstance(body['restore'], dict) else {}
            restore(item.get('root'), item.get('area'))
        else:
            set_mode(body.get('mode'))
        handler.respond(200, state(roots))
    except AutoMergeError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Could not save the auto-merge setting.'})


# ---- the command line ----

def main(argv=None):
    parser = argparse.ArgumentParser(description='Show the auto-merge decision for a branch. It never merges.')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='print each check for the branch in --cwd')
    check.add_argument('--cwd', default=os.getcwd())
    check.add_argument('--approved', help='the commit the reviewer approved (full hash). Default: none.')
    check.add_argument('--no-fetch', action='store_true', help='use the origin branch as last fetched')
    args = parser.parse_args(argv)
    decision, plan = evaluate(args.cwd, args.approved, fetch=not args.no_fetch)
    if decision['outcome'] == 'already':
        print(f"{decision['head'][:12]} is already on {decision['target']}.")
        return 0
    for item in decision['checks']:
        print(('ok    ' if item['ok'] else 'WAIT  ') + f"{item['key']}: {item['text']}")
    print(f"Mode: {read_mode()}. " + ('Every check passes: Tether would merge this commit.' if plan is not None
                                      else 'The branch waits for the user.'))
    return 0 if plan is not None else 3


if __name__ == '__main__':
    sys.exit(main())
