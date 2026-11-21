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
Observe records the decision and merges nothing. When available, Enforce will make one merge commit (parents: the main
branch, the approved commit) and push it with a plain fast-forward push, never a forced one. `git revert -m 1 <merge>` undoes it.
A merge is not a deploy.

The trust ladder gives each area a limit on changed lines. The limit widens after clean merges that Tether made and
shrinks on a revert. Tether checks the history against the decision log and saves each area's level.

  auto_merge.py check [--cwd DIR] [--approved SHA]   print the decision for a branch. It never merges.
"""
import argparse
import difflib
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
# Agents on this OS account can write the trust and passkey files. Enforce needs its own identity.
ENFORCE_AVAILABLE = False
ENFORCE_UNAVAILABLE = ('Enforce is not available yet. Auto-merge needs its own identity first, so that agents on '
                       'this account cannot change its trust or approve it.')
RULES_FILE = 'docs/auto-merge.json'
REMOTE = 'origin'
TARGET = 'main'
TARGET_REF = f'refs/heads/{TARGET}'
SHA = re.compile(r'[0-9a-f]{40}')
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
AREA_NAME = re.compile(r'[a-z][a-z0-9-]{0,23}')
MAX_AREAS = 8
MAX_PATTERNS = 40
MAX_FILES = 300          # a larger diff waits for the user
MODES_OK = ('100644', '100755')
MAX_BLOB = 512 * 1024
GIT_TIMEOUT = 15
NET_TIMEOUT = 45
TARGET_TTL = 300        # seconds Settings can reuse the push repository's main
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
_target_cache = {}
_target_errors = {}  # root -> (time, message): Settings does not ask a failing remote again for TARGET_TTL
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
    if mode == 'enforce':
        if not ENFORCE_AVAILABLE:
            return default
        if _grant != since:
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
        temp.write_text(text, encoding='utf-8', errors='surrogateescape')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_mode(mode):
    """Save the mode and when it began. Enforce merges only approvals that arrive after that time."""
    global _grant
    if mode not in MODE_NAMES:
        raise AutoMergeError(400, 'Choose Off, Observe or Enforce.')
    if mode == 'enforce' and not ENFORCE_AVAILABLE:
        raise AutoMergeError(409, ENFORCE_UNAVAILABLE)
    with _mode_lock:
        # Reviews record whole seconds. The current second may contain approvals from before this grant.
        since = int(time.time()) + (mode == 'enforce')
        if mode != 'enforce':
            _grant = None
        _write(mode_path(), json.dumps({'mode': mode, 'since': since}) + '\n')
        if mode == 'enforce':
            _grant = since


# ---- git ----

def _git_env(helpers=(), *, worktree_attributes=False):
    """The same Git settings for local commands, network commands and the checklist."""
    full = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS',
                'GIT_ATTR_SOURCE', 'GIT_EXTERNAL_DIFF', 'GIT_DIFF_OPTS'):
        full.pop(key, None)
    # Git warns when the graft file exists, even if it is empty. The checklist treats stderr as an error.
    full.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
                GIT_GRAFT_FILE=os.path.join(os.devnull, 'grafts'), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                GIT_ATTR_NOSYSTEM='1')
    settings = [('core.fsmonitor', 'false'), ('core.untrackedCache', 'false'), ('fetch.prune', 'false'),
                ('fetch.pruneTags', 'false'), ('fetch.recurseSubmodules', 'false'), ('push.recurseSubmodules', 'no'),
                ('push.followTags', 'false'), ('submodule.recurse', 'false')]
    if not worktree_attributes:
        settings.append(('core.attributesFile', os.devnull))
    settings.extend(helpers)
    full['GIT_CONFIG_COUNT'] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        full[f'GIT_CONFIG_KEY_{index}'] = key
        full[f'GIT_CONFIG_VALUE_{index}'] = value
    return full


CREDENTIAL_KEY = re.compile(r'credential\.(?:.+\.)?(helper|usehttppath|username)', re.I | re.S)


def _credential_helpers(root):
    """[(key, value)] for the user's own credential settings, in Git's order: credential.helper and the settings for
    one URL, such as credential.https://github.com.helper.

    Git itself lists them, in the repository `root`, so both global files, their includes and the conditions of
    `includeIf` apply as they do for the user. Only entries with the scope "global" pass: nothing comes from the
    repository, the system file, the command line or the environment. This lookup alone uses the user's environment.
    The output is read as bytes: a name can contain a carriage return, and text mode turns that into a line break."""
    try:
        result = RUN(['git', '-C', str(root), 'config', '--includes', '--null', '--show-scope', '--get-regexp',
                      r'^credential\.'], stdin=subprocess.DEVNULL, capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError('Could not read global Git credential helpers.') from exc
    if result.returncode == 1:
        return ()
    fields = result.stdout.split(b'\0')
    if result.returncode or fields[-1] or len(fields) % 2 != 1:
        raise GitError('Could not read global Git credential helpers.')
    found = []
    for scope, entry in zip(fields[:-1:2], fields[1:-1:2]):
        if scope != b'global':
            continue
        key, separator, value = (os.fsdecode(part) for part in entry.partition(b'\n'))
        match = CREDENTIAL_KEY.fullmatch(key)
        if match is None:
            continue
        if not separator:
            # Git credential fill requires a value even for useHttpPath.
            raise GitError(f'The global Git setting {key!r} has no value.')
        found.append((key, value))
    return tuple(found)


def _git(folder, *args, **kwargs):
    return _run_git(('-C', str(folder)), *args, **kwargs)


def _run_git(prefix, *args, network=False, check=True, env=None, binary=False):
    """Run git with a clean environment. Raise GitError on a failure, a timeout or a missing git."""
    decoding = {} if binary else {'encoding': 'utf-8', 'errors': 'surrogateescape'}
    try:
        result = RUN(['git', *prefix, *args], stdin=subprocess.DEVNULL, capture_output=True,
                     env={**_git_env(), 'GIT_NO_LAZY_FETCH': '1'} if env is None else env,
                     timeout=NET_TIMEOUT if network else GIT_TIMEOUT, **decoding)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f'git {args[0]} did not run: {type(exc).__name__}') from exc
    if check and result.returncode:
        stderr = os.fsdecode(result.stderr) if binary else result.stderr
        lines = stderr.strip().splitlines()
        raise GitError(f'git {args[0]} failed: ' + (lines[-1][:200] if lines else f'exit {result.returncode}'))
    return result


def _object_env(network=None, identity=None):
    env = _git_env(_credential_helpers(network) if network else ())
    for key in ('GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_SHALLOW_FILE'):
        env.pop(key, None)
    # A push URL may select a built-in transport, but not an arbitrary remote helper.
    env.update(GIT_ATTR_SOURCE=EMPTY_TREE, GIT_NO_LAZY_FETCH='1',
               GIT_ALLOW_PROTOCOL='file:git:http:https:ssh')
    env.update(identity or {})
    return env


def _object_git(scratch, *args, network=None, check=True, identity=None, binary=False, env=None):
    """Run git in the scratch repository. `network` is the user's repository for a command that uses the remote:
    its credential settings apply. `env`: an environment from _object_env, prepared before a last check."""
    env = _object_env(network, identity) if env is None else env
    return _git(scratch, *args, network=bool(network), check=check, env=env, binary=binary)


def _commit_sha(result, revision):
    text = result.stdout.strip()
    if not SHA.fullmatch(text):
        raise GitError(f'{revision} is not a commit.')
    return text


def _sha(scratch, revision):
    return _commit_sha(_object_git(scratch, 'rev-parse', '--verify', '--quiet', revision + '^{commit}'), revision)


def _head(folder):
    return _commit_sha(_git(folder, 'rev-parse', '--verify', 'HEAD'), 'HEAD')


def _git_dir(folder, option):
    output = _git(folder, 'rev-parse', '--path-format=absolute', option).stdout
    path = output.removesuffix('\n')
    if not output.endswith('\n') or '\n' in path or '\r' in path or not Path(path).is_absolute():
        raise GitError('Git did not report one absolute repository path.')
    return Path(path)


def main_checkout(folder):
    common = _git_dir(folder, '--git-common-dir')
    return str(common.parent if common.name == '.git' else common)


def _worker_checkout(folder):
    git_dir = _git_dir(folder, '--git-dir')
    if git_dir == _git_dir(folder, '--git-common-dir'):
        return None
    # A linked worktree's gitdir points back to the .git file at the top of the worktree.
    try:
        marker = git_dir / os.fsdecode((git_dir / 'gitdir').read_bytes()).removesuffix('\n')
        checkout = marker.parent.resolve()
        if marker.name == '.git' and Path(folder).resolve().is_relative_to(checkout):
            return checkout
    except (OSError, RuntimeError, ValueError) as exc:
        raise GitError('Could not read the linked worktree path.') from exc
    raise GitError('Git did not report the linked worktree path.')


def _scratch(root):
    """Prepare the private bare repository before using any objects from it."""
    objects = _git_dir(root, '--git-common-dir') / 'objects'
    scratch = paths.CACHE / 'auto-merge-git' / (hashlib.sha256(os.fsencode(root)).hexdigest()[:16] + '.git')
    config = scratch / 'config'
    # Check the init settings directly. Asking Git to read an unchecked config could follow includes.
    core = (rb'\[core\]\n\trepositoryformatversion = 0\n\tfilemode = (true|false)\n\tbare = true\n'
            rb'(?:\t(?:ignorecase|precomposeunicode) = (?:true|false)\n)*')
    with _lock:
        try:
            scratch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories = (scratch / 'objects', scratch / 'objects/info', scratch / 'info', scratch / 'hooks')
            if scratch.is_symlink():
                scratch.unlink()
            elif scratch.exists() and (not config.is_file() or config.is_symlink()
                                       or not re.fullmatch(core, config.read_bytes())
                                       or any(path.is_symlink() for path in directories)):
                shutil.rmtree(scratch)
            if not scratch.exists():
                scratch.mkdir(mode=0o700)
                _object_git(scratch, 'init', '--bare', '--template=')
                _target_cache.pop(root, None)
            scratch.chmod(0o700)
            attributes = scratch / 'info/attributes'
            (scratch / 'info').mkdir(exist_ok=True)
            attributes.unlink(missing_ok=True)
            (scratch / 'hooks').mkdir(exist_ok=True)
            for hook in (scratch / 'hooks').iterdir():
                if not hook.name.endswith('.sample'):
                    if hook.is_dir() and not hook.is_symlink():
                        shutil.rmtree(hook)
                    else:
                        hook.unlink()
            _write(scratch / 'objects/info/alternates', str(objects) + '\n')
        except (OSError, UnicodeError) as exc:
            raise GitError('Could not prepare the auto-merge scratch repository.') from exc
    return scratch


def _check_push_url(folder, url):
    if not url or url.startswith(('-', '+')) or '\n' in url:
        raise GitError(f'{REMOTE} does not have a usable push URL.')
    config = _git(folder, 'config', '--null', '--get-regexp',
                  r'^(remote\..*|url\..*\.(insteadof|pushinsteadof))$', check=False)
    if config.returncode not in (0, 1):
        raise GitError('Could not check Git URL rewrites.')
    for item in config.stdout.split('\0')[:-1]:
        key, _, prefix = item.partition('\n')
        section, name = key.split('.', 1)
        name = name.rpartition('.')[0]
        if section == 'remote' and name == url:
            raise GitError(f'The push URL for {REMOTE} must name a repository directly.')
        if section == 'url' and url.startswith(prefix) and name != prefix:
            raise GitError(f'Git would rewrite the push URL for {REMOTE} again.')


def _push_url(root):
    for key in ('pushurl', 'url'):
        result = _git(root, 'config', '--local', '--null', '--get-all', f'remote.{REMOTE}.{key}', check=False)
        if result.returncode == 1:
            continue
        if result.returncode == 0:
            values = result.stdout.split('\0')
            if len(values) == 2 and not values[-1]:
                return values[0]
        break
    raise GitError(f'{REMOTE} must have exactly one push URL.')


def _fetch_target(root, scratch):
    try:
        url = _push_url(root)
        _check_push_url(root, url)
        # Git treats a colon before the first slash as a transport or an SSH host.
        if not re.match(r'^[^/]*:', url):
            url = str(Path(root) / url)
        ref = _object_git(scratch, 'ls-remote', url, TARGET_REF, network=root).stdout
        match = re.fullmatch(r'([0-9a-f]{40})\t' + TARGET_REF + r'\n', ref)
        if match is None:
            raise GitError(f'The push repository has no unique {TARGET} branch.')
        sha = match[1]
        _object_git(scratch, 'fetch', '--no-tags', '--no-write-fetch-head', url, TARGET_REF, network=root)
        _object_git(scratch, 'cat-file', '-e', sha + '^{commit}')
    except GitError:
        _target_cache.pop(root, None)
        raise
    _target_cache[root] = time.monotonic(), (url, sha)
    _target_errors.pop(root, None)
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
    shown = _object_git(folder, 'show', f'{revision}:{RULES_FILE}', check=False)
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
    raw = _object_git(folder, 'ls-tree', '-r', '-z', '--full-tree', revision, binary=True).stdout
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


DIRTY = 'The worktree has uncommitted or untracked files.'
UNSUPPORTED = ('The worktree uses a checkout filter (for example Git LFS) that Tether does not run, '
               'so it cannot confirm that the tree is clean.')
SUBMODULE_DEPTH = 5
STATUS_CORE = {
    'autocrlf', 'eol', 'safecrlf', 'filemode', 'symlinks', 'ignorecase', 'precomposeunicode', 'trustctime',
    'checkstat', 'excludesfile', 'attributesfile', 'sparsecheckout', 'sparsecheckoutcone', 'splitindex',
}


def _status_env():
    """Read config and attributes from the worktree as the user's Git sees them."""
    env = _git_env(worktree_attributes=True)
    for key in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM', 'GIT_ATTR_NOSYSTEM'):
        if key in os.environ:
            env[key] = os.environ[key]  # the server's own setting, as the user's git sees it
        else:
            env.pop(key, None)
    return env


def _index_paths(folder, env):
    raw = _git(folder, 'ls-files', '-s', '-z', env=env, binary=True).stdout
    paths, links = [], []
    for line in raw.split(b'\0')[:-1]:
        meta, sep, path = line.partition(b'\t')
        if not sep or not path:
            raise GitError('git ls-files printed an entry Tether does not know.')
        (links if meta.startswith(b'160000 ') else paths).append(path)
    return paths, links


def _checked_out_submodules(folder, links):
    for link in dict.fromkeys(links):
        inner = folder / os.fsdecode(link)
        if os.path.lexists(inner / '.git'):
            yield inner


def _has_filter(folder, env, depth=0):
    """True when a tracked path, here or in a checked-out submodule, has a filter attribute."""
    paths, links = _index_paths(folder, env)
    if paths:
        try:
            result = RUN(['git', '-C', str(folder), 'check-attr', '-z', '--stdin', 'filter'], input=b'\0'.join(paths) + b'\0',
                         capture_output=True, env=env, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitError(f'git check-attr did not run: {type(exc).__name__}') from exc
        fields = result.stdout.split(b'\0')
        if result.returncode or fields[-1] or (len(fields) - 1) != 3 * len(paths):
            raise GitError('git check-attr gave no clear answer.')
        if any(value not in (b'unspecified', b'unset') for value in fields[2::3]):
            return True
    for inner in _checked_out_submodules(folder, links):
        if depth >= SUBMODULE_DEPTH or _has_filter(inner, env, depth + 1):
            return True
    return False


def _shadow_config(checkout):
    raw = _git(checkout, 'config', '--null', '--list', env=_status_env()).stdout
    entries = raw.split('\0')
    if entries[-1]:
        raise GitError('git config did not finish its last entry.')
    settings = {'core.repositoryformatversion': '0', 'core.bare': 'false', 'core.untrackedcache': 'false'}
    object_format = 'sha1'
    active = []
    for entry in entries[:-1]:
        key, sep, value = entry.partition('\n')
        # Git normalizes sections and variable names. Keep the spelling of submodule names.
        lower = key.lower()
        if lower == 'extensions.objectformat':
            object_format = value
        elif lower.startswith('extensions.') and lower != 'extensions.worktreeconfig':
            return None
        elif lower == 'submodule.active':
            if not sep:
                raise GitError('git config gave no pathspec for submodule.active.')
            active.append((key, value))
        elif ((lower.startswith('core.') and lower[5:] in STATUS_CORE)
              or re.fullmatch(r'submodule\..+\.(url|active|ignore)', lower)):
            if not sep and (lower in ('core.eol', 'core.checkstat', 'core.excludesfile', 'core.attributesfile')
                            or lower.endswith(('.url', '.ignore'))):
                raise GitError(f'git config gave no value for {key}.')
            settings[key] = value if sep else 'true'
    return [*settings.items(), *active] if object_format == 'sha1' else None


def _shadow(checkout, gitdir, common):
    """Build a private Git directory, or return None for repository extensions it cannot mirror."""
    settings = _shadow_config(checkout)
    if settings is None:
        return None
    head = _head(checkout)
    objects = os.fsencode(common / 'objects')
    if b'\n' in objects:
        raise GitError('The Git object directory path contains a newline.')
    cache = (paths.CACHE / 'auto-merge-status').absolute()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    shadow = Path(tempfile.mkdtemp(dir=cache))
    try:
        (shadow / 'HEAD').write_text(head + '\n', encoding='ascii')
        # Preserve the index timestamp so Git can detect a file with a racy stat entry.
        try:
            shutil.copy2(gitdir / 'index', shadow / 'index')
        except FileNotFoundError as exc:
            raise GitError('The worktree has no Git index.') from exc
        for shared in gitdir.glob('sharedindex.*'):
            shutil.copy2(shared, shadow / shared.name)
        (shadow / 'objects/info').mkdir(parents=True)
        (shadow / 'objects/info/alternates').write_bytes(objects + b'\n')
        (shadow / 'refs').mkdir()
        (shadow / 'info').mkdir()
        for info in (common / 'info/exclude', common / 'info/attributes', gitdir / 'info/sparse-checkout'):
            try:
                shutil.copyfile(info, shadow / 'info' / info.name)
            except FileNotFoundError:
                pass
        env = _git_env()
        for key, value in settings:
            _run_git((), 'config', '--file', str(shadow / 'config'), '--add', '--', key, value, env=env)
    except BaseException:
        shutil.rmtree(shadow)
        raise
    return shadow


def _index_digest(gitdir):
    """A hash of the live index (and the shared index files of a split index). A missing index is an error."""
    digest = hashlib.sha256()
    try:
        for path in (gitdir / 'index', *sorted(gitdir.glob('sharedindex.*'))):
            digest.update(os.fsencode(path.name) + b'\0' + path.read_bytes() + b'\0')
    except FileNotFoundError as exc:
        raise GitError('The worktree has no Git index.') from exc
    return digest.digest()


def _shadow_status(checkout, env, depth=0):
    if depth > SUBMODULE_DEPTH:
        return UNSUPPORTED
    gitdir = _git_dir(checkout, '--absolute-git-dir')
    common = _git_dir(checkout, '--git-common-dir')
    try:
        before = _index_digest(gitdir)
        shadow = _shadow(checkout, gitdir, common)
        if shadow is None:
            return UNSUPPORTED
        try:
            # Git compares submodule commits in-process. Check each submodule's files in its own shadow.
            status = _run_git((f'--git-dir={shadow}', f'--work-tree={checkout}'), 'status', '--porcelain=v2', '-z',
                              '--ignore-submodules=dirty', '--untracked-files=all',
                              env=_git_env(worktree_attributes=True), binary=True)
        finally:
            shutil.rmtree(shadow)
    except OSError as exc:
        raise GitError(f'Could not prepare or remove the shadow Git directory: {exc.strerror}') from exc
    if status.stderr:
        raise GitError('git status printed a warning, so the tree is not confirmed clean.')
    answer = DIRTY if status.stdout else ''
    if not answer:
        # The status used a copy of the index. A clean answer counts only when the live index did not change during
        # the check and still equals HEAD (blob ids only: this comparison runs no filter).
        try:
            changed_index = _index_digest(gitdir) != before
        except OSError as exc:
            raise GitError(f'Could not read the Git index: {exc.strerror}') from exc
        staged = _git(checkout, 'diff-index', '--cached', '--quiet', 'HEAD', check=False, env=env)
        if staged.returncode not in (0, 1):
            raise GitError('git diff-index could not compare the index with HEAD.')
        if changed_index or staged.returncode:
            answer = DIRTY
    _, links = _index_paths(checkout, env)
    for inner in _checked_out_submodules(checkout, links):
        result = _shadow_status(inner, env, depth + 1)
        if result == UNSUPPORTED:
            return UNSUPPORTED
        answer = answer or result
    return answer


def _dirty(folder):
    """Ask Git for changes using private Git directories that define no filters."""
    checkout = Path(folder).absolute()
    env = _status_env()
    if _has_filter(checkout, env):
        return UNSUPPORTED
    result = _shadow_status(checkout, env)
    # With a filter attribute that appeared meanwhile, Git compared raw bytes: its answer does not count.
    if _has_filter(checkout, env):
        return UNSUPPORTED
    return result


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
    size = _object_git(folder, 'cat-file', '-s', sha).stdout.strip()
    if not size.isdecimal():
        raise GitError('git did not report the size of a blob.')
    if int(size) > MAX_BLOB:
        return None
    raw = _object_git(folder, 'cat-file', 'blob', sha, binary=True).stdout
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
                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT,
                     env={**_git_env(), 'GIT_ATTR_SOURCE': base})
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


def _history(scratch, tip):
    log = _object_git(scratch, 'log', '--first-parent', f'-n{HISTORY}', '-z', '--no-notes', '--no-decorate',
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


def _ancestor(scratch, before, after):
    result = _object_git(scratch, 'merge-base', '--is-ancestor', before, after, check=False)
    if result.returncode not in (0, 1):
        raise GitError('git could not read the commit ancestry.')
    return result.returncode == 0


def _reverted_areas(scratch, rules, sha, reverted):
    try:
        message = _object_git(scratch, 'log', '-1', '--format=%B', reverted).stdout
        if REVERT.search(message) or not _ancestor(scratch, reverted, sha):
            return set()
        paths = _tree_diff(scratch, reverted + '^1', reverted)
    except GitError:
        return {area['name'] for area in rules}
    return {area_of(rules, path) for path in paths} - {None}


def _history_events(root, scratch, rules, target_sha):
    commits = _history(scratch, target_sha)
    window = {sha for sha, *_ in commits}
    logged = {(item['merge'], item['head']) for item in _records()
              if item.get('root') == root and item['outcome'] == 'merged'
              and _hash(item.get('merge')) and _hash(item.get('head'))}
    added = _object_git(scratch, 'log', '--first-parent', '--full-history', '--diff-merges=first-parent', '--no-renames',
                       '--diff-filter=A', '--no-patch', '--format=%H', target_sha, '--', RULES_FILE).stdout.split()
    epoch = added[-1] if added else None

    # Follow the first-parent order for commits that share a timestamp.
    events = []
    for sha, when, parents, subject, body in reversed(commits):
        if len(parents) == 2 and (sha, parents[1]) in logged \
                and '(Tether auto-merge)' in subject and f'Approved commit: {parents[1]}' in body.splitlines():
            areas, problems = classify(rules, changed(scratch, parents[0], sha))
            events.append((when, 'merge', sha, () if problems else areas))
        reverted = REVERT.findall(body)
        if reverted and epoch is not None and _ancestor(scratch, epoch, sha):
            touched = set()
            for commit in reverted:
                touched.update(_reverted_areas(scratch, rules, sha, commit))
            events.append((when, 'revert', sha, touched))
    return window, sorted(events, key=lambda event: event[0])


def _apply_event(level, kind, sha, when):
    if kind == 'merge':
        level['merges'] += 1
        level['streak'] += 1
        if level['streak'] >= CLEAN_MERGES and 0 < level['rung'] < len(RUNGS) - 1:
            level['rung'] += 1
            level['streak'] = 0
    else:
        level['rung'] = max(0, level['rung'] - 1)
        level['streak'] = 0
        level['reverts'] += 1
        level['last_revert'] = {'commit': sha, 'at': when}


def _restored_at(scratch, target_sha, saved, names):
    restored = {}
    if isinstance(saved, dict):
        for name in names:
            sha = saved.get(name)
            if _hash(sha):
                try:
                    if _sha(scratch, sha) == sha and _ancestor(scratch, sha, target_sha):
                        restored[name] = sha
                except GitError:
                    pass  # A saved hash can name a commit Git does not have.
    return restored


def ladder(root, scratch, rules, target_sha):
    """Keep saved reductions, with trust capped by a replay of the current first-parent history."""
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
        replayed = {name: {'rung': START_RUNG, 'streak': 0, 'merges': 0, 'reverts': 0, 'last_revert': None}
                    for name in names}
        for name, level in replayed.items():
            entry['areas'].setdefault(name, level.copy())
        tip = entry.get('tip')
        # A concurrent fetch may have saved a newer tip. Do not prune or replace its saved reductions.
        stale = False
        if tip and tip != target_sha:
            stale = _ancestor(scratch, target_sha, tip)
            if not stale and not _ancestor(scratch, tip, target_sha):
                raise GitError('The main branch no longer contains the saved trust history.')
        window, events = _history_events(root, scratch, rules, target_sha)
        seen = {kind: set(entry[f'{kind}s_seen']) & window for kind in ('merge', 'revert')}
        restored = _restored_at(scratch, target_sha, entry.get('restored'), names)
        for when, kind, sha, areas in events:
            for name in areas:
                if not stale and sha not in seen[kind]:
                    _apply_event(entry['areas'][name], kind, sha, when)
                if name not in restored or not _ancestor(scratch, sha, restored[name]):
                    _apply_event(replayed[name], kind, sha, when)
            seen[kind].add(sha)
        if not stale:
            entry.update(tip=target_sha, merges_seen=sorted(seen['merge']), reverts_seen=sorted(seen['revert']))
            if entry != stored:
                data[root] = entry
                try:
                    _write(trust_path(), json.dumps(data) + '\n')
                except OSError as exc:
                    raise GitError('Could not save the trust ladder.') from exc
        levels = {}
        for name in names:
            level = min(entry['areas'][name], replayed[name], key=lambda level: level['rung'])
            levels[name] = {**level, 'limit': RUNGS[level['rung']]}
        _ladder_cache[root] = key(), levels
        return levels


def restore(root, name):
    """The user puts an area that fell to rung 0 back on the first rung."""
    if not isinstance(root, str) or not isinstance(name, str) or not AREA_NAME.fullmatch(name):
        raise AutoMergeError(400, 'Choose an area.')
    try:
        if main_checkout(root) != root:
            raise AutoMergeError(400, 'Choose a repository.')
        scratch = _scratch(root)
        _, target_sha = _fetch_target(root, scratch)
        rules = rules_at(scratch, target_sha)
        with _lock:
            levels = ladder(root, scratch, rules, target_sha) if rules else {}
            if name not in levels or levels[name]['rung'] != 0:
                raise AutoMergeError(409, 'Only an area at zero trust can be restored.')
            data = _trust_data()
            entry = data[root]
            entry['areas'][name].update(rung=START_RUNG, streak=0)
            restored = entry.get('restored')
            entry['restored'] = {**(restored if isinstance(restored, dict) else {}), name: target_sha}
            _write(trust_path(), json.dumps(data) + '\n')
            _ladder_cache.pop(root, None)
    except GitError as exc:
        raise AutoMergeError(400, str(exc)) from exc


# ---- the decision ----

def _trial_merge(scratch, target_sha, head):
    trial = _object_git(scratch, 'merge-tree', '--write-tree', '--no-messages', target_sha, head, check=False)
    tree = trial.stdout.split('\n', 1)[0].strip()
    if trial.returncode == 1:
        raise GitError(f'The branch conflicts with {REMOTE}/{TARGET}. Rebase it, then ask for a new review.')
    if trial.returncode or not _hash(tree):
        raise GitError('git could not try the merge.')
    return tree


def evaluate(folder, approved, tab=None):
    """(decision, plan) for the branch in `folder`. Git can write objects during fetch and trial merge.

    tab: the worker tab's own folder. When given (B368), `folder` is resolved once to its real path and must be a
    checkout of the tab's repository (one `--git-common-dir`), checked before any fetch. A refused folder is not
    logged as `root`: Settings reads trust from each logged root. decision: the record for the log and the Settings
    page. plan: what merge() needs, or None when a check failed."""
    decision = {'at': int(time.time()), 'folder': str(folder), 'approved': approved if isinstance(approved, str) else None,
                'checks': [], 'areas': {}, 'outcome': 'wait'}
    checks = decision['checks']

    def check(key, ok, text):
        checks.append({'key': key, 'ok': bool(ok), 'text': text})
        return bool(ok)

    try:
        own_root = None
        if tab is not None:
            decision['root'] = None
            try:
                own = _git_dir(tab, '--git-common-dir').resolve()
            except GitError as exc:
                raise GitError(f"The tab's own folder is not in a Git repository: {exc}") from exc
            own_root = str(own.parent if own.name == '.git' else own)
            decision.update(root=own_root, repo=Path(own_root).name)
            try:
                real = Path(folder).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise GitError(f'The reviewed folder {folder} is gone.') from exc
            if _git_dir(real, '--git-common-dir').resolve() != own:
                raise GitError(f"The reviewed folder {folder} is not a checkout of the tab's repository {own_root}.")
            folder = real  # every later read uses this one path, so a moved symlink cannot switch repositories
        root = main_checkout(folder)
        if own_root is not None and root != own_root:
            raise GitError(f"The reviewed folder {folder} is not a checkout of the tab's repository {own_root}.")
        head = _head(folder)
        branch = _git(folder, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip()
        decision.update(root=root, repo=Path(root).name, head=head, branch=branch, target=f'{REMOTE}/{TARGET}')
        scratch = _scratch(root)
        url, target_sha = _fetch_target(root, scratch)
        if _ancestor(scratch, head, target_sha):
            decision['outcome'] = 'already'
            return decision, None
        base = _object_git(scratch, 'merge-base', target_sha, head).stdout.strip()
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
        if not branch or branch == TARGET:
            check('branch', False, 'The tab is not on a worker branch.')
        elif (checkout := _worker_checkout(folder)) is None:
            check('branch', False, 'The tab works in the main checkout, not in a worktree of its own.')
        elif (dirty := _dirty(checkout)):
            check('branch', False, dirty)
        else:
            check('branch', True, f'Worker branch {branch}, with no uncommitted changes.')
    except GitError as exc:
        check('branch', False, str(exc))

    rules = None
    try:
        rules = rules_at(scratch, target_sha)
    except GitError:
        pass
    check('rules', rules is not None,
          f'The areas come from {RULES_FILE} on {REMOTE}/{TARGET}.' if rules is not None
          else f'{REMOTE}/{TARGET} has no valid {RULES_FILE}, so no area may auto-merge.')

    items = None
    subject = ''
    try:
        message = _object_git(scratch, 'log', '-1', '--format=%B', head).stdout
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
        tree = _trial_merge(scratch, target_sha, head)
        files = changed(scratch, target_sha, tree)
        reviewed = _tree(scratch, head)
        if any(item['new'] != reviewed.get(item['path']) for item in files):
            merge_problem = 'The main branch changed the same files. Rebase the branch, then ask for a new review.'
    except GitError as exc:
        merge_problem = str(exc)
    if rules is None:
        check('scope', False, 'Without the rules file, every file is outside the auto-merge areas.')
    else:
        branch_files = []
        try:
            branch_files = changed(scratch, base, head)
            areas, problems = classify(rules, branch_files)
            if files is not None:
                _, merge_problems = classify(rules, files)
                problems.extend(problem for problem in merge_problems if problem not in problems)
            elif tree is not None:
                problems.append(merge_problem)
        except GitError as exc:
            areas, problems = {}, [str(exc)]
        decision['areas'] = areas
        decision['files'] = len(branch_files)
        labels = {area['name']: area['label'] for area in rules}
        try:
            levels = ladder(root, scratch, rules, target_sha)
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

    rows = checklist_rows(scratch, target_sha, head)
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


class MainMoved(GitError):
    """The main branch is not at the commit that passed the checks. Nothing is retried and nothing is forced."""


def _push_argv(url, made):
    """The one push Tether makes: a plain push of one commit to main. Git refuses it unless it is a fast-forward."""
    argv = ['push', url, f'{made}:{TARGET_REF}']
    if any(item.startswith(('-', '+')) for item in argv[1:]):
        raise GitError('The push arguments must not contain an option or a forced refspec.')
    return argv


def _fast_forward_push(root, scratch, url, checked, made, confirm=None):
    """Push the merge commit with a plain `git push`. No --force and no --force-with-lease, here or anywhere.

    Before the push: the merge commit must contain the checked main commit as its first parent, and the remote main
    must still be at that commit. A remote that moved, went back or lost the branch is "main moved". Git itself
    refuses the push when main moved after that look to a commit the merge does not contain. confirm() runs after
    that look, right before the push, and raises to stop it."""
    if not _hash(checked) or not _hash(made):
        raise GitError('The push needs two commit hashes.')
    if not _ancestor(scratch, checked, made):
        raise GitError('The merge commit does not contain the checked main commit.')
    if _sha(scratch, made + '^1') != checked:
        raise GitError('The merge commit does not have the checked main commit as its first parent.')
    seen = _object_git(scratch, 'ls-remote', url, TARGET_REF, network=root).stdout
    if seen != f'{checked}\t{TARGET_REF}\n':
        raise MainMoved(f'{REMOTE}/{TARGET} moved after the checks. Tether pushed nothing.')
    push_env = _object_env(root)  # the credential lookup runs git too: do it before the last check
    if confirm is not None:
        confirm()
    try:
        _object_git(scratch, *_push_argv(url, made), network=root, env=push_env)
    except GitError as exc:
        raise MainMoved(f'{REMOTE}/{TARGET} did not accept a plain push, so it moved or refused it. Nothing is forced: {exc}') from exc


def merge(plan, reviewer, job):
    """Make the merge commit and push it. Return its hash. Raise GitError when anything is in doubt.

    The push is a plain fast-forward push; a main branch that moved since the checks stops it. Working trees and local refs stay
    untouched."""
    if not ENFORCE_AVAILABLE:
        raise NotAuthorized(ENFORCE_UNAVAILABLE)
    folder = plan['folder']
    root = plan['root']
    scratch = _scratch(root)
    stamp = merge_stamp(plan['items'], reviewer, plan['head'])
    areas = ', '.join(f'{name} {lines} lines' for name, lines in plan['areas'].items())
    message = (f"Merge branch '{plan['branch']}' into {TARGET} (Tether auto-merge)\n\n"
               f"{plan['subject']}\n\n"
               'Tether merged this branch without the user: every auto-merge check passed.\n'
               f"Approved commit: {plan['head']}\n"
               f'Areas: {areas}\n'
               'Undo: git revert -m 1 <this commit>\n\n'
               f'Verified: {stamp}\n')
    name, email = [_git(folder, 'config', '--local', '--null', '--get-all', key, check=False).stdout.split('\0')[-2:-1]
                   for key in ('user.name', 'user.email')]
    identity = {}
    for who in ('AUTHOR', 'COMMITTER'):
        for field, values, default in (('NAME', name, 'Tether auto-merge'), ('EMAIL', email, 'tether@localhost')):
            key = f'GIT_{who}_{field}'
            identity[key] = os.environ.get(key, values[0]) if name and email else default
    made = _object_git(scratch, 'commit-tree', plan['tree'], '-p', plan['target_sha'], '-p', plan['head'], '-m', message,
                       identity=identity).stdout.strip()
    if not SHA.fullmatch(made):
        raise GitError('git made no merge commit.')
    if _object_git(scratch, 'rev-parse', '--verify', made + '^{tree}').stdout.strip() != plan['tree']:
        raise GitError('The merge commit does not contain the planned tree.')
    _check_push_url(root, plan['url'])
    with _mode_lock:
        def confirm():
            problem = _authorization_problem(read_setting(), job.get('approved_at'))
            if problem:
                raise NotAuthorized(problem)
            try:
                if job.get('tab') is not None and str(Path(job['folder']).resolve(strict=True)) != str(folder):
                    raise NotAuthorized('The reviewed folder now points to another worktree.')
            except (OSError, RuntimeError, TypeError) as exc:
                raise NotAuthorized('The reviewed folder is gone.') from exc
            try:
                head = _head(folder)
                branch = _git(folder, 'symbolic-ref', '--quiet', '--short', 'HEAD').stdout.strip()
                checkout = _worker_checkout(folder)
            except GitError as exc:
                raise NotAuthorized(str(exc)) from exc
            if checkout is None or head != plan['head'] or branch != plan['branch']:
                raise NotAuthorized('The worker branch changed before the push.')
            try:
                dirty = _dirty(checkout)
            except GitError as exc:
                raise NotAuthorized(str(exc)) from exc
            if dirty:
                raise NotAuthorized(dirty)
            current = job.get('current')
            try:
                live = current().get(job.get('worker'), {}).get(job.get('reviewer')) if callable(current) else None
                matches = live == {'commit': head, 'at': job['approved_at'], 'folder': job['folder']}
            except Exception as exc:
                raise NotAuthorized('Could not read the current reviewer approval.') from exc
            if not matches:
                raise NotAuthorized('The reviewer approval changed or is no longer available.')

        # Once before the look at the remote main and once after it, right before the push.
        confirm()
        _fast_forward_push(root, scratch, plan['url'], plan['target_sha'], made, confirm)
    return made


_merge_lock = threading.Lock()


def process(job, alert=None):
    """Decide one approved commit and, in enforce mode, merge it. Returns the recorded decision, or None.

    job: {'folder' (the approved worktree, or None), 'tab' (the tab's own folder), 'commit', 'approved_at', 'worker', 'name', 'reviewer', 'reviewer_agent', 'current'}.
    The mode is read here, when the work runs, not when the approval was seen."""
    setting = read_setting()
    if setting['mode'] == 'off':
        return None
    if job.get('folder'):
        decision, plan = evaluate(job['folder'], job.get('commit'), tab=job.get('tab'))
    else:
        # B368: the approval does not name the worktree it checked (saved before B368). Do not guess a folder.
        decision = {'at': int(time.time()), 'folder': None, 'approved': job.get('commit'), 'areas': {},
                    'outcome': 'wait', 'checks': [{'key': 'approval', 'ok': False, 'text':
                    'The approval does not name the folder it reviewed. Ask the reviewer to review the commit again.'}]}
        plan = None
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
    elif not ENFORCE_AVAILABLE or setting['mode'] != 'enforce':
        decision['outcome'] = 'would_merge'
    elif (problem := _authorization_problem(setting, at)) is not None:
        decision['outcome'] = 'would_merge'
        decision['note'] = problem
    else:
        with _merge_lock:
            try:
                # One attempt. When main moved, the branch stays for a new approval or for the user.
                decision['merge'] = merge(plan, job.get('reviewer_agent'), job)
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


def observe(entries, approvals, folder_of, alert=None, start=True, current=None):
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
                tab = folder_of(entry)
            except (OSError, KeyError, ValueError, TypeError):
                tab = None
            if not tab:
                continue
            # B368: merge the worktree the approval checked; the tab's own folder only names the repository.
            folder = approval.get('folder')
            _seen.add(key)
            jobs.append({'folder': folder if isinstance(folder, str) and folder else None, 'tab': tab, 'commit': commit, 'approved_at': at, 'worker': worker,
                         'name': str(entry.get('name') or '')[:80], 'reviewer': reviewer,
                         'reviewer_agent': (by_pane.get(reviewer) or {}).get('agent'), 'current': current})
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
    failed = _target_errors.get(root)
    if failed and time.monotonic() - failed[0] < TARGET_TTL:
        entry['error'] = failed[1]
        return entry if known else None
    try:
        scratch = _scratch(root)
        cached = _target_cache.get(root)
        _, target_sha = cached[1] if cached and time.monotonic() - cached[0] < TARGET_TTL else _fetch_target(root, scratch)
        _target_errors.pop(root, None)
        entry['target'] = f'{REMOTE}/{TARGET}'
        rules = rules_at(scratch, target_sha)
        if rules is None:
            entry['error'] = f'{REMOTE}/{TARGET} has no valid {RULES_FILE}. Nothing auto-merges in this repository.'
            return entry if known else None
        levels = ladder(root, scratch, rules, target_sha)
    except GitError as exc:
        # A repository Tether cannot read is listed only when it has decisions: other open repositories are not
        # part of auto-merge, and their remote errors do not belong on the Settings page.
        entry['error'] = str(exc)
        _target_errors[root] = time.monotonic(), entry['error']
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
    return {**setting, 'enforce_available': ENFORCE_AVAILABLE,
            'modes': [{'mode': key, 'name': name, 'summary': summary} for key, name, summary in MODES
                      if ENFORCE_AVAILABLE or key != 'enforce'],
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
        if body.get('mode') == 'enforce' and not ENFORCE_AVAILABLE:
            raise AutoMergeError(409, ENFORCE_UNAVAILABLE)
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
    args = parser.parse_args(argv)
    decision, plan = evaluate(args.cwd, args.approved)
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
