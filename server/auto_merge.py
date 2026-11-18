#!/usr/bin/env python3
"""Auto-merge by risk (B213) and the trust ladder (B214).

Tether merges a worker branch into the main branch on origin without the user only when every check passes:

  approval   the reviewer approved exactly the commit at the head of the branch
  branch     the tab works in a linked worktree, on a branch, with no uncommitted changes
  rules      the main branch has docs/auto-merge.json, which names the areas that may auto-merge
  stamp      the head commit has a "Verified:" stamp with a tests(...) item
  scope      every changed file is regular text, in one area and within its trust limit
  checklist  scripts/checklist.py on this server finds no reliability-checklist row for the diff
  merge      the commit merges into the main branch with no conflict

Each unknown fails closed: a git error, a timeout, a missing file or a missing approval means "wait for the user".

Modes (paths.CONFIG/auto-merge): "off", "observe" (the default, and what a missing or bad file means) and "enforce".
Observe records the decision and merges nothing. Enforce makes one merge commit (parents: the main branch, the approved
commit) and pushes it with a normal push, never a forced one. `git revert -m 1 <merge>` undoes it. A merge is not a
deploy.

The trust ladder gives each area a limit on changed lines. The limit widens after clean merges that Tether made and
shrinks on a revert. Tether checks the history against the decision log and saves each area's level.

  auto_merge.py check [--cwd DIR] [--approved SHA]   print the decision for a branch. It never merges.
"""
import argparse
import difflib
import importlib.util
import json
import math
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

import approval  # noqa: E402
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
TARGET = 'main'
TARGET_REF = f'refs/heads/{TARGET}'
TRACKING_REF = f'refs/remotes/{REMOTE}/{TARGET}'
SHA = re.compile(r'[0-9a-f]{40}')
AREA_NAME = re.compile(r'[a-z][a-z0-9-]{0,23}')
MAX_AREAS = 8
MAX_PATTERNS = 40
MAX_FILES = 300          # a larger diff waits for the user
MODES_OK = ('100644', '100755')
MAX_BLOB = 512 * 1024
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

_lock = threading.RLock()
_ladder_cache = {}
_mode_lock = threading.Lock()
_grant = None
RUN = subprocess.run     # tests replace this to prove that observe mode never writes


class AutoMergeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GitError(Exception):
    pass


class NotAuthorized(GitError):
    pass


# ---- the mode ----

def mode_path():
    return paths.CONFIG / 'auto-merge'


def log_path():
    return paths.CACHE / 'auto-merge.jsonl'


def trust_path():
    return paths.CONFIG / 'auto-merge-trust.json'


def read_setting():
    """A missing or malformed file means observe. Only a grant in this process allows enforce."""
    default = {'mode': DEFAULT_MODE, 'since': 0, 'paused': False}
    try:
        data = json.loads(mode_path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError, RecursionError):
        return default
    since = data.get('since') if isinstance(data, dict) else None
    if not _number(since) or since < 0 or (isinstance(since, float) and not since.is_integer()):
        return default
    mode = data.get('mode')
    if mode not in MODE_NAMES:
        return default
    if mode == 'enforce' and _grant != since:
        return {**default, 'paused': True}
    return {'mode': mode, 'since': int(since), 'paused': False}


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


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
    global _grant
    if mode not in MODE_NAMES:
        raise AutoMergeError(400, 'Choose Off, Observe or Enforce.')
    with _mode_lock:
        # Reviews record whole seconds. The current second may contain approvals from before this grant.
        since = int(time.time()) + (mode == 'enforce')
        if mode != 'enforce':
            _grant = None
        _write(mode_path(), json.dumps({'mode': mode, 'since': since}) + '\n')
        if mode == 'enforce':
            _grant = since


# ---- git ----

def _git(folder, *args, network=False, check=True, env=None, binary=False):
    """Run git with a clean environment. Raise GitError on a failure, a timeout or a missing git."""
    full = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT'):
        full.pop(key, None)
    full.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
                GIT_GRAFT_FILE=os.devnull)
    if network:
        # These options can expand the requested refspec even when Git takes a literal URL.
        settings = {'fetch.prune': 'false', 'fetch.pruneTags': 'false', 'fetch.recurseSubmodules': 'false',
                    'push.recurseSubmodules': 'no', 'push.followTags': 'false'}
        full['GIT_CONFIG_COUNT'] = str(len(settings))
        for index, (key, value) in enumerate(settings.items()):
            full[f'GIT_CONFIG_KEY_{index}'] = key
            full[f'GIT_CONFIG_VALUE_{index}'] = value
    else:
        full.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    full.update(env or {})
    decoding = {} if binary else {'encoding': 'utf-8', 'errors': 'surrogateescape'}
    try:
        result = RUN(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL, capture_output=True,
                     env=full, timeout=NET_TIMEOUT if network else GIT_TIMEOUT, **decoding)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f'git {args[0]} did not run: {type(exc).__name__}') from exc
    if check and result.returncode:
        stderr = os.fsdecode(result.stderr) if binary else result.stderr
        lines = stderr.strip().splitlines()
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


def _check_push_url(folder, url):
    if not url or url.startswith(('-', '+')) or '\n' in url:
        raise GitError(f'{REMOTE} does not have a usable push URL.')
    alias = _git(folder, 'remote', 'get-url', '--push', url, check=False)
    if alias.returncode != 2:
        raise GitError(f'The push URL for {REMOTE} must name a repository directly.')
    rewrites = _git(folder, 'config', '--null', '--get-regexp', r'^url\..*\.(insteadof|pushinsteadof)$', check=False)
    if rewrites.returncode not in (0, 1):
        raise GitError('Could not check Git URL rewrites.')
    for item in rewrites.stdout.split('\0')[:-1]:
        key, _, prefix = item.partition('\n')
        if url.startswith(prefix) and key[4:].rsplit('.', 1)[0] != prefix:
            raise GitError(f'Git would rewrite the push URL for {REMOTE} again.')


def _fetch_target(folder):
    url = _git(folder, 'remote', 'get-url', '--push', REMOTE).stdout.rstrip('\n')
    _check_push_url(folder, url)
    ref = _git(folder, 'ls-remote', url, TARGET_REF, network=True).stdout
    match = re.fullmatch(r'([0-9a-f]{40})\t' + TARGET_REF + r'\n', ref)
    if match is None:
        raise GitError(f'The push repository has no unique {TARGET} branch.')
    sha = match[1]
    _git(folder, 'fetch', '--no-tags', '--no-write-fetch-head', url, TARGET_REF, network=True)
    _git(folder, 'cat-file', '-e', sha + '^{commit}')
    return url, sha


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

def _tree(folder, revision):
    """Read the entries from the tree itself, independent of Git's diff and attributes."""
    raw = _git(folder, 'ls-tree', '-r', '-z', '--full-tree', revision, binary=True).stdout
    entries = {}
    for line in raw.split(b'\0')[:-1]:
        meta, sep, path = line.partition(b'\t')
        match = re.fullmatch(rb'([0-7]{6}) (blob|commit|tree) ([0-9a-f]{40})', meta)
        if match is None or not sep or not path:
            raise GitError('git ls-tree printed an entry Tether does not know.')
        entries[os.fsdecode(path)] = tuple(part.decode('ascii') for part in match.groups())
    if raw and not raw.endswith(b'\0'):
        raise GitError('git ls-tree did not finish its last entry.')
    return entries


def _tree_diff(folder, base, head):
    old, new = _tree(folder, base), _tree(folder, head)
    return {path: (old.get(path), new.get(path)) for path in sorted(old.keys() | new.keys())
            if old.get(path) != new.get(path)}


def _regular(entry):
    return entry is None or (entry[0] in MODES_OK and entry[1] == 'blob')


def _blob(folder, entry):
    if entry is None:
        return b''
    sha = entry[2]
    size = _git(folder, 'cat-file', '-s', sha).stdout.strip()
    if not size.isdecimal():
        raise GitError('git did not report the size of a blob.')
    if int(size) > MAX_BLOB:
        return None
    raw = _git(folder, 'cat-file', 'blob', sha, binary=True).stdout
    return None if b'\0' in raw else raw


def changed(folder, base, head):
    """The changed paths and their line counts. A rename gives both sides."""
    entries = _tree_diff(folder, base, head)
    files = []
    for path, (old, new) in entries.items():
        lines = None
        if len(entries) <= MAX_FILES and _regular(old) and _regular(new):
            before, after = _blob(folder, old), _blob(folder, new)
            if before is not None and after is not None:
                diff = difflib.SequenceMatcher(None, before.splitlines(keepends=True), after.splitlines(keepends=True))
                lines = sum(i2 - i1 + j2 - j1 for op, i1, i2, j1, j2 in diff.get_opcodes() if op != 'equal')
        files.append({'path': path, 'old': old, 'new': new, 'lines': lines})
    return files


def classify(rules, files):
    """({area: lines}, [problem text]) for a diff. Any problem means the diff is out of scope."""
    areas = {}
    problems = []
    if not files:
        return areas, ['The branch changes no files.']
    if len(files) > MAX_FILES:
        return areas, [f'The diff changes {len(files)} files. The limit is {MAX_FILES}.']
    outside = []
    touched = set()
    for item in files:
        old, new = item['old'], item['new']
        area = area_of(rules, item['path'])
        if area is not None:
            touched.add(area)
        if area is None:
            outside.append(item['path'])
        elif not _regular(old) or not _regular(new):
            problems.append(f"{item['path']} is a link or a submodule, not a regular file.")
        elif old and new and old[0] != new[0]:
            problems.append(f"{item['path']} changes its file mode.")
        elif old is None and new[0] == '100755':
            problems.append(f"{item['path']} is a new executable file.")
        elif item['lines'] is None:
            problems.append(f"{item['path']} is binary or too large.")
        else:
            areas[area] = areas.get(area, 0) + item['lines']
    if len(touched) > 1:
        labels = sorted(area['label'] for area in rules if area['name'] in touched)
        problems.insert(0, 'The diff touches more than one area: ' + ', '.join(labels) + '. A branch may change one area.')
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
    """The checklist rows that the commits base...head hit, as texts. [] only when the checklist says "clear".
    None when it cannot answer (no script on this server, no checklist on the main branch, an error): that counts
    as a hit. base is the tip of the main branch: scripts/checklist.py reads the checklist from there, never from
    the worker branch."""
    script = HERE.parent / 'scripts' / 'checklist.py'
    if not script.is_file():
        return None
    try:
        result = RUN([sys.executable, str(script), '--repo', str(folder), '--base', base, '--head', head, '--json'],
                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        answer = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    status = answer.get('status') if isinstance(answer, dict) else None
    rows = answer.get('rows') if isinstance(answer, dict) else None
    if status == 'clear' and result.returncode == 0 and rows == []:
        return []
    if status != 'hit' or result.returncode != 1 or not isinstance(rows, list) or not rows:
        return None
    return [' '.join(str(row.get(key) or '') for key in ('id', 'title')).strip()[:120] or 'row' if isinstance(row, dict)
            else 'row' for row in rows]


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

def _trust_data():
    try:
        data = json.loads(trust_path().read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError, RecursionError):
        return {}
    except OSError as exc:
        raise GitError('Could not read the trust ladder.') from exc
    return data if isinstance(data, dict) else {}


def _hash(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _count(value):
    return type(value) is int and value >= 0


def _valid_level(level):
    if not isinstance(level, dict) or set(level) != {'rung', 'streak', 'merges', 'reverts', 'last_revert'}:
        return False
    if not all(_count(level[key]) for key in ('rung', 'streak', 'merges', 'reverts')) or level['rung'] >= len(RUNGS):
        return False
    last = level['last_revert']
    return last is None or (isinstance(last, dict) and _hash(last.get('commit')) and _count(last.get('at')))


def _valid_trust(entry):
    if not isinstance(entry, dict) or (entry.get('tip') is not None and not _hash(entry['tip'])):
        return False
    areas = entry.get('areas')
    if not isinstance(areas, dict) or not all(_valid_level(level) for level in areas.values()):
        return False
    return all(isinstance(entry.get(key), list) and all(_hash(sha) for sha in entry[key])
               for key in ('merges_seen', 'reverts_seen'))


def _stat(path):
    try:
        found = path.stat()
        return found.st_mtime_ns, found.st_size
    except OSError:
        return None


def _history(root, tip):
    log = _git(root, 'log', '--first-parent', f'-n{HISTORY}', '-z', '--no-notes', '--no-decorate',
               '--format=%H%x00%ct%x00%P%x00%s%x00%B', tip).stdout
    fields = log.split('\0')
    if fields[-1] or (len(fields) - 1) % 5:
        raise GitError('git printed an unreadable history.')
    commits = []
    for index in range(0, len(fields) - 1, 5):
        sha, when, parents, subject, body = fields[index:index + 5]
        parents = parents.split()
        if not _hash(sha) or not when.isdecimal() or not all(_hash(parent) for parent in parents):
            raise GitError('git printed an unreadable commit.')
        commits.append((sha, int(when), parents, subject, body))
    return commits


def _ancestor(root, before, after):
    result = _git(root, 'merge-base', '--is-ancestor', before, after, check=False)
    if result.returncode not in (0, 1):
        raise GitError('git could not read the commit ancestry.')
    return result.returncode == 0


def _reverted_areas(root, rules, sha, reverted):
    try:
        message = _git(root, 'log', '-1', '--format=%B', reverted).stdout
        if REVERT.search(message) or not _ancestor(root, reverted, sha):
            return set()
        paths = _tree_diff(root, reverted + '^1', reverted)
    except GitError:
        return {area['name'] for area in rules}
    return {area_of(rules, path) for path in paths} - {None}


def _apply_history(root, rules, target_sha, entry):
    commits = _history(root, target_sha)
    window = {sha for sha, *_ in commits}
    merges_seen = set(entry['merges_seen']) & window
    reverts_seen = set(entry['reverts_seen']) & window
    logged = {(item['merge'], item['head']) for item in _records()
              if item.get('root') == root and item['outcome'] == 'merged'
              and _hash(item.get('merge')) and _hash(item.get('head'))}
    added = _git(root, 'log', '--first-parent', '--full-history', '--diff-merges=first-parent', '--no-renames',
                 '--diff-filter=A', '--no-patch', '--format=%H', target_sha, '--', RULES_FILE).stdout.split()
    epoch = added[-1] if added else None

    # Follow the first-parent order for commits that share a timestamp.
    events = []
    for sha, when, parents, subject, body in reversed(commits):
        if sha not in merges_seen and len(parents) == 2 and (sha, parents[1]) in logged \
                and '(Tether auto-merge)' in subject and f'Approved commit: {parents[1]}' in body.splitlines():
            areas, problems = classify(rules, changed(root, parents[0], sha))
            merges_seen.add(sha)
            if not problems:
                events.append((when, 'merge', sha, areas))
        reverted = REVERT.findall(body)
        if reverted and sha not in reverts_seen and epoch is not None and _ancestor(root, epoch, sha):
            touched = set()
            for commit in reverted:
                touched.update(_reverted_areas(root, rules, sha, commit))
            reverts_seen.add(sha)
            if touched:
                events.append((when, 'revert', sha, touched))

    for when, kind, sha, areas in sorted(events, key=lambda event: event[0]):
        for name in areas:
            area = entry['areas'][name]
            if kind == 'merge':
                area['merges'] += 1
                area['streak'] += 1
                if area['streak'] >= CLEAN_MERGES and 0 < area['rung'] < len(RUNGS) - 1:
                    area['rung'] += 1
                    area['streak'] = 0
            else:
                area['rung'] = max(0, area['rung'] - 1)
                area['streak'] = 0
                area['reverts'] += 1
                area['last_revert'] = {'commit': sha, 'at': when}
    entry.update(tip=target_sha, merges_seen=sorted(merges_seen), reverts_seen=sorted(reverts_seen))


def _levels(entry, names):
    return {name: {**entry['areas'][name], 'limit': RUNGS[entry['areas'][name]['rung']]} for name in names}


def ladder(root, rules, target_sha, *, persist=True):
    """Apply new first-parent events and save each area's trust, including reductions that age out."""
    names = [area['name'] for area in rules]

    def key():
        return (target_sha, HISTORY, tuple(names), tuple(tuple(p.pattern for p in area['paths']) for area in rules),
                _stat(log_path()), _stat(trust_path()))

    with _lock:
        cached = _ladder_cache.get(root)
        if cached and cached[0] == key():
            return cached[1]
        data = _trust_data()
        stored = data.get(root)
        entry = stored if _valid_trust(stored) else {'areas': {}, 'merges_seen': [], 'reverts_seen': []}
        entry = {**entry, 'areas': {name: level.copy() for name, level in entry['areas'].items()}}
        for name in names:
            entry['areas'].setdefault(name, {'rung': START_RUNG, 'streak': 0, 'merges': 0, 'reverts': 0,
                                            'last_revert': None})
        tip = entry.get('tip')
        # Settings and --no-fetch can see an older origin/main. They must not replay or prune that history.
        stale = False
        if tip and tip != target_sha:
            stale = _ancestor(root, target_sha, tip)
            if not stale and not _ancestor(root, tip, target_sha):
                raise GitError('The main branch no longer contains the saved trust history.')
        if not stale:
            _apply_history(root, rules, target_sha, entry)
            if persist and entry != stored:
                data[root] = entry
                try:
                    _write(trust_path(), json.dumps(data) + '\n')
                except OSError as exc:
                    raise GitError('Could not save the trust ladder.') from exc
        levels = _levels(entry, names)
        if persist:
            _ladder_cache[root] = key(), levels
        return levels


def restore(root, name):
    """The user puts an area that fell to rung 0 back on the first rung."""
    if not isinstance(root, str) or not isinstance(name, str) or not AREA_NAME.fullmatch(name):
        raise AutoMergeError(400, 'Choose an area.')
    try:
        if main_checkout(root) != root:
            raise AutoMergeError(400, 'Choose a repository.')
        _, target_sha = _fetch_target(root)
        rules = rules_at(root, target_sha)
        with _lock:
            levels = ladder(root, rules, target_sha) if rules else {}
            if name not in levels or levels[name]['rung'] != 0:
                raise AutoMergeError(409, 'Only an area at zero trust can be restored.')
            data = _trust_data()
            data[root]['areas'][name].update(rung=START_RUNG, streak=0)
            _write(trust_path(), json.dumps(data) + '\n')
            _ladder_cache.pop(root, None)
    except GitError as exc:
        raise AutoMergeError(400, str(exc)) from exc


# ---- the decision ----

def _trial_merge(folder, target_sha, head):
    trial = _git(folder, 'merge-tree', '--write-tree', '--no-messages', target_sha, head, check=False)
    tree = trial.stdout.split('\n', 1)[0].strip()
    if trial.returncode == 1:
        raise GitError(f'The branch conflicts with {REMOTE}/{TARGET}. Rebase it, then ask for a new review.')
    if trial.returncode or not _hash(tree):
        raise GitError('git could not try the merge.')
    return tree


def evaluate(folder, approved, *, fetch=True):
    """(decision, plan) for the branch in `folder`. Git can write objects during fetch and trial merge.

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
        decision.update(root=root, repo=Path(root).name, head=head, branch=branch, target=f'{REMOTE}/{TARGET}')
        url, target_sha = _fetch_target(folder) if fetch else (None, _sha(folder, TRACKING_REF))
        if _ancestor(folder, head, target_sha):
            decision['outcome'] = 'already'
            return decision, None
        base = _git(folder, 'merge-base', target_sha, head).stdout.strip()
        if not SHA.fullmatch(base):
            raise GitError('The branch shares no history with the main branch.')
    except GitError as exc:
        check('git', False, str(exc))
        return decision, None
    if not isinstance(approved, str) or not SHA.fullmatch(approved):
        check('approval', False, 'No reviewer approval is tied to a commit of this branch.')
    elif approved != head:
        check('approval', False, f'The reviewer approved {approved[:12]}, but the branch is at {head[:12]}.')
    else:
        check('approval', True, f'The reviewer approved commit {head[:12]}.')

    try:
        linked = _git(folder, 'rev-parse', '--path-format=absolute', '--git-dir').stdout.strip() != \
            _git(folder, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
        dirty = _git(folder, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none').stdout
        if not branch or branch == TARGET:
            check('branch', False, 'The tab is not on a worker branch.')
        elif not linked:
            check('branch', False, 'The tab works in the main checkout, not in a worktree of its own.')
        elif dirty:
            check('branch', False, 'The worktree has uncommitted or untracked files.')
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
          f'The areas come from {RULES_FILE} on {REMOTE}/{TARGET}.' if rules is not None
          else f'{REMOTE}/{TARGET} has no valid {RULES_FILE}, so no area may auto-merge.')

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

    tree = files = merge_problem = None
    try:
        tree = _trial_merge(folder, target_sha, head)
        files = changed(folder, target_sha, tree)
        reviewed = _tree(folder, head)
        if any(item['new'] != reviewed.get(item['path']) for item in files):
            merge_problem = 'The main branch changed the same files. Rebase the branch, then ask for a new review.'
    except GitError as exc:
        merge_problem = str(exc)
    if rules is None:
        check('scope', False, 'Without the rules file, every file is outside the auto-merge areas.')
    elif files is not None:
        areas, problems = classify(rules, files)
        decision['areas'] = areas
        decision['files'] = len(files)
        labels = {area['name']: area['label'] for area in rules}
        try:
            levels = ladder(root, rules, target_sha, persist=fetch)
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

    rows = checklist_rows(folder, target_sha, head)
    if rows is None:
        check('checklist', False, 'The reliability checklist gave no answer on this server, so the diff counts as a hit.')
    elif rows:
        check('checklist', False, 'Reliability checklist rows hit: ' + '; '.join(rows[:4])
              + (f'; and {len(rows) - 4} more' if len(rows) > 4 else '') + '.')
    else:
        check('checklist', True, 'The diff hits no reliability checklist row.')

    check('merge', merge_problem is None, merge_problem or f'The commit merges into {REMOTE}/{TARGET} with no conflict.')

    if not all(item['ok'] for item in checks):
        return decision, None
    decision['outcome'] = 'pass'
    return decision, {'folder': str(folder), 'root': root, 'head': head, 'branch': branch, 'url': url,
                      'target_sha': target_sha, 'tree': tree, 'items': items, 'subject': subject,
                      'areas': decision['areas']}


# ---- the merge (enforce mode only) ----

def _authorization_problem(setting, approved_at):
    if setting['mode'] != 'enforce' or setting['paused']:
        return 'Auto-merge left enforce mode before the push.'
    if not _number(approved_at) or approved_at < setting['since']:
        return 'The approval is older than enforce mode. Only a new approval can merge.'
    if approved_at < time.time() - APPROVAL_HOURS * 3600:
        return f'The approval is older than {APPROVAL_HOURS} hours. Only a new approval can merge.'
    return None


def merge(plan, reviewer, approved_at):
    """Make the merge commit and push it. Return its hash. Raise GitError when anything is in doubt.

    The push is a normal one: git refuses it when the main branch moved, and nothing is forced. No working tree and
    no local branch changes, so other sessions' uncommitted work in the main checkout is never touched."""
    if plan['url'] is None:
        raise GitError('Auto-merge needs the current main branch from the push repository.')
    folder = plan['folder']
    stamp = merge_stamp(plan['items'], reviewer, plan['head'])
    areas = ', '.join(f'{name} {lines} lines' for name, lines in plan['areas'].items())
    message = (f"Merge branch '{plan['branch']}' into {TARGET} (Tether auto-merge)\n\n"
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
    if _git(folder, 'rev-parse', '--verify', made + '^{tree}').stdout.strip() != plan['tree']:
        raise GitError('The merge commit does not contain the planned tree.')
    _check_push_url(folder, plan['url'])
    with _mode_lock:
        problem = _authorization_problem(read_setting(), approved_at)
        if problem:
            raise NotAuthorized(problem)
        _git(folder, 'push', plan['url'], f'{made}:{TARGET_REF}', network=True)
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
    if plan is None:
        decision['outcome'] = 'wait'
    elif setting['mode'] != 'enforce':
        decision['outcome'] = 'would_merge'
    elif (problem := _authorization_problem(setting, at)) is not None:
        decision['outcome'] = 'would_merge'
        decision['note'] = problem
    else:
        with _merge_lock:
            try:
                try:
                    decision['merge'] = merge(plan, job.get('reviewer_agent'), at)
                except NotAuthorized:
                    raise
                except GitError:
                    # The main branch may have moved between the fetch and the push. Decide again, once.
                    again, plan = evaluate(job['folder'], job.get('commit'))
                    if plan is None:
                        raise
                    decision.update(checks=again['checks'], areas=again['areas'], files=again['files'])
                    decision['merge'] = merge(plan, job.get('reviewer_agent'), at)
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
    """Queue each approval once. approvals: review_links.approved_commits(). Git runs on another thread, so
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
            if not _hash(commit):
                continue
            at = approval.get('at')
            key = (worker, reviewer, commit, at if _number(at) else None)
            if key in _seen:
                continue
            try:
                folder = folder_of(entry)
            except (OSError, KeyError, ValueError, TypeError):
                folder = None
            if not folder:
                continue
            _seen.add(key)
            jobs.append({'folder': folder, 'commit': commit, 'approved_at': at, 'worker': worker,
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
        target_sha = _sha(root, TRACKING_REF)
        entry['target'] = f'{REMOTE}/{TARGET}'
        rules = rules_at(root, target_sha)
        if rules is None:
            entry['error'] = f'{REMOTE}/{TARGET} has no valid {RULES_FILE}. Nothing auto-merges in this repository.'
            return entry if known else None
        levels = ladder(root, rules, target_sha, persist=False)
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
    return {**setting,
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
        if not 0 < size <= 16384:
            raise AutoMergeError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise AutoMergeError(400, 'Invalid request.')
        if 'restore' in body or body.get('mode') == 'enforce':
            payload = {key: value for key, value in body.items() if key != 'approval'}
            approval.require(handler, 'auto-merge', payload, body.get('approval'))
        if 'restore' in body:
            item = body['restore'] if isinstance(body['restore'], dict) else {}
            restore(item.get('root'), item.get('area'))
        else:
            set_mode(body.get('mode'))
        handler.respond(200, state(roots))
    except approval.ApprovalError as exc:
        approval.error(handler, exc)
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
