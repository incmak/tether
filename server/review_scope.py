"""What a review request covers (B205): the commit in the worker's folder, read from git.

A review is bound to a commit hash. The request names the hash; an approval counts only while the worker's folder is
still at that hash. All reads are bounded and read-only. An unreadable folder gives None: the caller then sends an
unbound request, as before B205.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

RULES = 'origin/main:REVIEW.md'
TIMEOUT = 5
HASH = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')  # SHA-1 or SHA-256 repositories
UI_PATH = re.compile(
    r'^ui/(src|public)/|^ui/(index\.html|vite\.config\.ts|package\.json|package-lock\.json)$'
    r'|^server/(index\.html|app\.js|ui_manifest\.json)$')
# The lock file too: `npm ci` builds from it. server/ui_manifest.json: ui/src/layouts.ts imports it.
UI_SPECS = ('ui', 'server/index.html', 'server/app.js', 'server/ui_manifest.json')
BASE_REF = ('rev-parse', '--verify', '--quiet', 'origin/main')  # Verify the ref before trying to read its commit.
CHECKER = Path(__file__).resolve().with_name('reliability_checklist.py')


def git_result(folder, *args):
    """The result of a read-only git command, or None if it could not run."""
    if not folder:
        return None
    try:
        return subprocess.run(['git', *args], cwd=folder, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
                              capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def git(folder, *args, complete=False):
    """stdout of a read-only git command in `folder`, or None when it fails. complete: also None when git wrote a
    warning: it can exit with 0 after it skipped a path that it could not read, and the output is then partial."""
    result = git_result(folder, *args)
    if result is None or result.returncode != 0 or complete and result.stderr:
        return None
    return result.stdout.decode('utf-8', 'replace')


def head(folder):
    """The full hash of HEAD in `folder`, or None."""
    if not folder:
        return None
    found = (git(folder, 'rev-parse', '--verify', 'HEAD^{commit}') or '').strip()
    return found if HASH.fullmatch(found) else None


def checklist(folder):
    """Run the checker with a deadline for regular expressions from the repository."""
    empty = {'rows': [], 'paths': []}
    base = git_result(folder, *BASE_REF)
    if base is not None and base.returncode == 1 and not base.stderr:
        return {'status': 'absent', **empty, 'reason': 'origin/main is not a readable commit'}
    if base is None or base.returncode != 0 or base.stderr:
        return {'status': 'unchecked', **empty, 'reason': 'could not check origin/main'}
    try:
        result = subprocess.run([sys.executable, str(CHECKER), '--json', '--repo', str(folder)],
                                capture_output=True, timeout=15, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        if result.returncode in (0, 1, 2):
            data = json.loads(result.stdout)
            if isinstance(data, dict) and data.get('status') in ('clear', 'hit', 'absent', 'unchecked'):
                return data
        reason = 'checker returned an invalid result'
    except subprocess.TimeoutExpired:
        reason = 'checker timed out'
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, RecursionError):
        reason = 'checker could not run or its result could not be read'
    return {'status': 'unchecked', **empty, 'reason': reason}


def _names(result):
    return [path for path in result.stdout.decode('utf-8', 'replace').split('\0') if path]


def ui_paths(folder):
    """The changed UI paths, or None when they are not known (git failed, or there is no base to compare with
    in a repository that has UI files)."""
    root = git_result(folder, 'rev-parse', '--show-toplevel')
    if root is None or root.returncode != 0 or root.stderr:
        return None
    root = os.fsdecode(root.stdout.removesuffix(b'\n'))
    base = git_result(root, *BASE_REF)
    if base is None or base.stderr or base.returncode not in (0, 1):
        return None
    start = None
    if base.returncode == 0:
        start = (git(root, 'merge-base', base.stdout.decode('ascii', 'replace').strip(), 'HEAD') or '').strip()
        if not start:
            return None
    # Without origin/main the committed work cannot be compared. The uncommitted and untracked files still can.
    commands = [('diff', '--name-only', '-z', '--no-renames', '--no-relative', start or 'HEAD'),
                ('diff', '--cached', '--name-only', '-z', '--no-renames', '--no-relative', start or 'HEAD'),
                ('ls-files', '--others', '--exclude-standard', '-z'),
                # The commit under review itself: a staged or uncommitted revert must not hide what it holds.
                ('diff', '--name-only', '-z', '--no-renames', '--no-relative', start, 'HEAD') if start
                else ('ls-files', '-z')]  # no base: does this repository have UI files at all?
    found = []
    for command in commands:
        result = git_result(root, *command, '--', *UI_SPECS)
        if result is None or result.returncode != 0 or result.stderr:
            return None
        found.append({path for path in _names(result) if UI_PATH.search(path)})
    changed = sorted(set().union(*(found if start else found[:3])))
    if start is None and not changed and found[3]:
        return None  # UI files exist, and commits that are not on a known base may have changed them
    return changed


def fingerprint(folder):
    """A digest of everything a scope depends on, or None when git cannot read it: HEAD, origin/main, the status,
    the tracked and the staged diff, and the size, the modification time and the change time of each untracked file. The same digest means the
    same scope, so a request that is retried on each monitor tick does not run the checker again."""
    root = git(folder, 'rev-parse', '--show-toplevel')
    if not root:
        return None
    root = root.removesuffix('\n')
    digest = hashlib.sha256()
    base = git_result(root, *BASE_REF)
    if base is None:
        return None
    digest.update(base.stdout + bytes([base.returncode & 255]))
    for command in (('rev-parse', 'HEAD'), ('status', '--porcelain', '-z', '--untracked-files=all'),
                    # As the checker reads them: no text conversion, or an edit could leave the digest as it was.
                    ('diff', '--no-ext-diff', '--no-textconv', '--text', 'HEAD'),
                    ('diff', '--no-ext-diff', '--no-textconv', '--text', '--cached', 'HEAD'),
                    ('ls-files', '--others', '--exclude-standard', '-z')):
        result = git_result(root, *command)
        if result is None or result.returncode != 0 or result.stderr:
            return None
        digest.update(result.stdout + b'\0\0')
    for name in result.stdout.split(b'\0'):
        if name:
            try:
                found = os.lstat(os.path.join(os.fsencode(root), name))
                # The change time too: a tool that keeps the modification time (cp -p, touch -r) cannot keep this one.
                digest.update(b'%d:%d:%d:%d;' % (found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_ino))
            except OSError:
                digest.update(b'unreadable;')
    return digest.hexdigest()


def scope(folder):
    """{'commit', 'dirty', 'folder', 'rules', 'checklist', 'ui'} for a review request, or None when the folder has no
    readable commit. checklist: the reliability checklist result. ui: the changed UI paths, or None when not known.
    dirty: True when tracked or untracked changes exist, False when the tree is clean, None when the status cannot be
    read. Only False counts as clean. The commit and the status come from one git command, so they describe the same
    moment: a commit made between two reads cannot pair one commit with the status of another.
    rules: origin/main has a REVIEW.md that the reviewer can read with `git show`."""
    # --untracked-files: the repository's own settings (status.showUntrackedFiles=no) must not hide untracked files.
    status = git(folder, 'status', '--porcelain=v2', '--branch', '--untracked-files=normal', complete=True) \
        if folder else None
    commit, dirty = None, None
    if status is not None:
        lines = status.splitlines()
        found = [line.split(' ', 2)[2] for line in lines if line.startswith('# branch.oid ')]
        commit = found[0] if len(found) == 1 and HASH.fullmatch(found[0]) else None
        dirty = any(line and not line.startswith('#') for line in lines) if commit else None
    if commit is None:
        commit = head(folder)  # the status failed or is slow: the review can still be bound, the tree is unknown
    if commit is None:
        return None
    return {'commit': commit, 'dirty': dirty, 'folder': str(folder),
            'rules': git(folder, 'cat-file', '-e', RULES) is not None,
            'checklist': checklist(folder), 'ui': ui_paths(folder)}
