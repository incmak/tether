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
HASH = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')  # SHA-1 or SHA-256 repositories


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
    dirty: True when tracked or untracked changes exist, False when the tree is clean, None when the status cannot be
    read. Only False counts as clean. The commit and the status come from one git command, so they describe the same
    moment: a commit made between two reads cannot pair one commit with the status of another.
    rules: origin/main has a REVIEW.md that the reviewer can read with `git show`."""
    # --untracked-files: the repository's own settings (status.showUntrackedFiles=no) must not hide untracked files.
    status = git(folder, 'status', '--porcelain=v2', '--branch', '--untracked-files=normal') if folder else None
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
            'rules': git(folder, 'cat-file', '-e', RULES) is not None}
