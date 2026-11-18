"""What a review request covers (B205): the commit in the worker's folder, read from git.

A review is bound to a commit hash. The request names the hash; an approval counts only while the worker's folder is
still at that hash. All reads are bounded and read-only. An unreadable folder gives None: the caller then sends an
unbound request, as before B205.
"""
import os
import re
import subprocess

RULES = 'origin/main:REVIEW.md'
TIMEOUT = 5
HASH = re.compile(r'[0-9a-f]{40}')


def git(folder, *args):
    """stdout of a read-only git command in `folder`, or None when it fails."""
    try:
        result = subprocess.run(['git', *args], cwd=folder, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
                                capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode('utf-8', 'replace') if result.returncode == 0 else None


def head(folder):
    """The full hash of HEAD in `folder`, or None."""
    if not folder:
        return None
    found = (git(folder, 'rev-parse', '--verify', 'HEAD^{commit}') or '').strip()
    return found if HASH.fullmatch(found) else None


def scope(folder):
    """{'commit', 'dirty', 'folder', 'rules'} for a review request, or None when the folder has no readable commit.
    dirty: tracked or untracked changes exist, so the commit alone does not hold the work.
    rules: origin/main has a REVIEW.md that the reviewer can read with `git show`."""
    commit = head(folder)
    if commit is None:
        return None
    status = git(folder, 'status', '--porcelain')
    if status is None:
        return None
    return {'commit': commit, 'dirty': bool(status.strip()), 'folder': str(folder),
            'rules': git(folder, 'cat-file', '-e', RULES) is not None}
