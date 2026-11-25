#!/usr/bin/env python3
"""Require the live review approvals before a protected push, deploy or auto-merge."""

import argparse
import fcntl
from functools import cache
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

MERGE_FORWARD = True  # Mak, 2026-11-22: a clean merge of main into the approved commit needs no new review
MAX_MERGES = 20
GIT_TIMEOUT = 20
PANE = re.compile(r'%[0-9]{1,6}')
HASH = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
MAIN = 'refs/heads/main'
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_DEFAULT = object()
_HOOK_START = b'# >>> Tether review gate'
_HOOK_END = b'# <<< Tether review gate <<<\n'
_HOOK_SUFFIXES = (b'', b' (added newline)', b' (new hook)')
_APPROVAL_STEP = ' Ask for approval of this commit or stop the review link with Mak.'


class GateRefused(Exception):
    """The gate could not establish that the operation is allowed."""


def _refusal(commit, reason):
    briefly = f'Commit {commit[:12]}. ' if isinstance(commit, str) and HASH.fullmatch(commit) else ''
    return GateRefused(f'{briefly}{reason}')


def _pane(value):
    if not isinstance(value, str) or not PANE.fullmatch(value):
        raise GateRefused('TMUX_PANE is invalid. Run from the correct Tether tab.')
    return value


def pane_identity(environ=os.environ, proc=Path('/proc')):
    if 'TMUX_PANE' in environ:
        return _pane(environ['TMUX_PANE'])
    pid = os.getpid()
    tmux = False
    try:
        for _ in range(64):
            if pid == 1:
                break
            # /proc encloses comm in parentheses. The name itself can contain parentheses and spaces.
            head, separator, fields = (proc / str(pid) / 'stat').read_bytes().rpartition(b') ')
            fields = fields.split()
            if not separator or len(fields) < 2 or not fields[1].isdigit():
                raise ValueError
            tmux = tmux or head.partition(b' (')[2].startswith(b'tmux')
            pid = int(fields[1])
            if pid == 1:
                break  # host init is the boundary and its environment is normally privileged
            if pid <= 0:
                raise ValueError
            try:
                environ_bytes = (proc / str(pid) / 'environ').read_bytes()
            except PermissionError:
                # sshd, cron and the systemd user manager hide their environment. A tab's own processes do not: the
                # pane shell under the tmux server carries TMUX_PANE, so passing a tmux server without a pane refuses.
                continue
            for entry in environ_bytes.split(b'\0'):
                key, _, value = entry.partition(b'=')
                if key == b'TMUX_PANE':
                    return _pane(os.fsdecode(value))
        if pid != 1:
            raise ValueError
        if tmux:
            raise ValueError
        if (proc / '1' / 'comm').read_bytes().strip() in (b'systemd', b'init'):
            return None
    except (OSError, ValueError):
        raise GateRefused('Tether could not read the tab identity. Run the push or deploy from the correct tab outside '
                          'the sandbox.') from None
    raise GateRefused('Tether cannot tell which tab runs this (a sandbox hides it). Run the push or deploy outside the '
                      'sandbox.')


def _store_path():
    # Keep path discovery and the server's review machinery out of the hook import path.
    import paths
    return paths.CACHE / 'review-links.json'


def _row_kind(row):
    # Match review_links._is_link, _is_cap and _saved without importing the watcher and its dependencies.
    if not isinstance(row, dict):
        return None
    has_worker = (isinstance(row.get('worker'), str) and
                  (row.get('worker_thread') is None or isinstance(row['worker_thread'], str)))
    if has_worker and row.get('cap') is True:
        return 'cap'
    if isinstance(row.get('saved_at'), (int, float)):
        return 'saved'
    if has_worker and isinstance(row.get('reviewer'), str):
        return 'link'
    return None


def _bad_json_constant(value):
    raise ValueError(value)


def _unique_json_object(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError('Duplicate keys')
    return row


def links_for(pane, store=None):
    """Read the store once. A missing or unreadable store cannot establish that a pane has no links."""
    store = _store_path() if store is None else Path(store)
    try:
        rows = json.loads(store.read_text(encoding='utf-8'), parse_constant=_bad_json_constant,
                          object_pairs_hook=_unique_json_object)
    except (OSError, ValueError, RecursionError):
        raise GateRefused('Tether could not read the review store. Ask Mak to check the review links.') from None
    if not isinstance(rows, list):
        raise GateRefused('The review store must contain a list. Ask Mak to check the review links.')
    links = []
    for row in rows:
        kind = _row_kind(row)
        if kind is None:
            raise GateRefused('The review store has an invalid row. Ask Mak to check the review links.')
        if kind == 'link':
            if 'approved_commit' in row and not _full_hash(row['approved_commit']):
                raise GateRefused('The review store has an invalid approved commit. Ask Mak to check the review links.')
            if row['worker'] == pane:
                links.append(row)
    return links


def _full_hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _git(repo, *args, env=None, allowed=(0,)):
    env = {**(os.environ if env is None else env), 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_NO_LAZY_FETCH': '1', 'GIT_GRAFT_FILE': os.path.join(os.devnull, 'grafts')}
    try:
        result = subprocess.run(['git', '-C', os.fspath(repo), *args], env=env, timeout=GIT_TIMEOUT,
                                capture_output=True, encoding='utf-8', errors='surrogateescape')
    except subprocess.TimeoutExpired:
        raise GateRefused(f'Git {args[0]} timed out. Try again when Git is available.') from None
    except (OSError, ValueError):
        raise GateRefused(f'Git {args[0]} could not start. Check the repository.') from None
    if result.returncode not in allowed:
        raise GateRefused(f'Git {args[0]} could not verify the repository state. Check the repository and commit.')
    return result


def _resolve(repo, revision):
    if not isinstance(revision, str) or not revision:
        raise GateRefused('The commit is missing. Supply the commit to check.')
    value = _git(repo, 'rev-parse', '--verify', '--end-of-options', f'{revision}^{{commit}}').stdout.strip()
    if not _full_hash(value):
        raise GateRefused('Git did not return a full commit hash. Check the repository and commit.')
    return value


def _ancestor(repo, older, newer, env=None):
    return _git(repo, 'merge-base', '--is-ancestor', older, newer, env=env, allowed=(0, 1)).returncode == 0


def _commit_allowed(repo, commit, approved, base):
    return commit == approved or (MERGE_FORWARD and merge_forward_allowed(repo, commit, approved, base))


def commit_allowed(repo, commit, approved, base=None):
    return _full_hash(approved) and _commit_allowed(repo, _resolve(repo, commit), approved, base)


def merge_forward_allowed(repo, commit, approved, base):
    if not all(_full_hash(value) for value in (commit, approved, base)):
        return False
    # Use source objects in a bare repository without local or user configuration and attributes.
    objects = _git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'objects').stdout.removesuffix('\n')
    if not Path(objects).is_absolute() or '\n' in objects:
        raise GateRefused('Git did not return a usable object directory. Check the repository.')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_ATTR_NOSYSTEM='1',
               GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.attributesFile', GIT_CONFIG_VALUE_0=os.devnull)
    object_format = 'sha1' if len(commit) == 40 else 'sha256'
    try:
        with tempfile.TemporaryDirectory(prefix='tether-review-git-') as scratch:
            _git(scratch, 'init', '--bare', '--template=', f'--object-format={object_format}', env=env)
            (Path(scratch) / 'objects/info/alternates').write_bytes(os.fsencode(objects) + b'\n')

            @cache
            def allowed(candidate, remaining):
                if candidate == approved:
                    return True
                if remaining == 0:
                    return False
                parents = _git(scratch, 'show', '-s', '--format=%P', candidate, env=env).stdout.split()
                if len(parents) != 2:
                    return False
                first, second = parents
                if not any(_ancestor(scratch, other, base, env) and allowed(parent, remaining - 1)
                           for parent, other in ((first, second), (second, first))):
                    return False
                result = _git(scratch, 'merge-tree', '--write-tree', '--no-messages', first, second, env=env,
                              allowed=(0, 1))
                tree = _git(scratch, 'rev-parse', '--verify', f'{candidate}^{{tree}}', env=env).stdout.strip()
                return result.returncode == 0 and result.stdout.strip() == tree

            return allowed(commit, MAX_MERGES)
    except OSError:
        raise GateRefused('Tether could not prepare the merge check. Check access to the temporary directory.') from None


def check(repo, commit, *, pane=_DEFAULT, links=_DEFAULT, base=None):
    """Return the common approved hash for a linked pane, or None for an ungated operation."""
    try:
        pane = pane_identity() if pane is _DEFAULT else pane
        if pane is None:
            return None
        _pane(pane)
        links = links_for(pane) if links is _DEFAULT else links
        if not links:
            return None
        approvals = [link.get('approved_commit') for link in links]
        if None in approvals:
            raise GateRefused('The review is not approved by every linked reviewer.' + _APPROVAL_STEP)
        if not all(_full_hash(value) for value in approvals):
            raise GateRefused('The review store has an invalid approved commit.')
        approved = approvals[0]
        if any(value != approved for value in approvals):
            raise GateRefused('The linked reviewers approved different commits.' + _APPROVAL_STEP)
        commit = _resolve(repo, commit)
        if not _commit_allowed(repo, commit, approved, base):
            raise GateRefused(f'The reviewers approved {approved[:12]}.' + _APPROVAL_STEP)
        return approved
    except GateRefused as error:
        raise _refusal(commit, error) from None


def pre_push(repo, lines):
    protected = []
    for line in lines:
        # Git separates fields with ASCII spaces. Other whitespace can be part of a ref name.
        line = line.removesuffix('\n')
        fields = line.split(' ')
        if len(fields) != 4 or not all(fields) or _CONTROL.search(line):
            raise GateRefused('Git sent an invalid pre-push line. Run git push again from this repository.')
        _, local, remote_ref, remote = fields
        if not _full_hash(local) or not _full_hash(remote) or len(local) != len(remote):
            raise GateRefused('Git sent an invalid pre-push hash. Run git push again from this repository.')
        if not remote_ref.startswith('refs/') or remote_ref.endswith('/'):
            raise GateRefused('Git sent an invalid pre-push destination. Run git push again from this repository.')
        if remote_ref == MAIN or remote_ref.startswith('refs/tags/'):
            protected.append((local, remote_ref, remote))
    if not protected:
        return
    try:
        pane = pane_identity()
        links = links_for(pane) if pane is not None else []
    except GateRefused as error:
        raise _refusal(protected[0][0], error) from None
    if not links:
        return
    for local, ref, remote in protected:
        zero = '0' * len(local)
        try:
            if local == zero:
                raise GateRefused(f'A linked tab cannot delete {ref}. Stop the review link with Mak before deleting it.')
            if ref != MAIN and remote != zero:
                raise GateRefused(f'A linked tab cannot replace tag {ref}. Use a new tag for the approved commit.')
            if ref == MAIN and remote != zero and _git(repo, 'cat-file', '-e', f'{remote}^{{commit}}',
                                                       allowed=(0, 1, 128)).returncode != 0:
                raise GateRefused(f'This repository does not have the remote main {remote[:12]}. Run git fetch, '
                                  'merge origin/main, and push again.')
            if ref == MAIN and (remote == zero or not _ancestor(repo, remote, local)):
                raise GateRefused('The main push must be a fast-forward of existing main. Update the branch and get it '
                                  'approved before pushing again.')
        except GateRefused as error:
            raise _refusal(remote if local == zero else local, error) from None
        check(repo, local, pane=pane, links=links, base=remote if ref == MAIN else None)


def _hook_block(gate, python, suffix):
    command = f'{shlex.quote(str(python))} -B {shlex.quote(str(gate))}'
    return (_HOOK_START + suffix + b' >>>\n' + f'''_tether_review_refs=$(mktemp) || exit 1
if ! cat > "$_tether_review_refs" || ! {command} pre-push "$@" < "$_tether_review_refs"; then
    rm -f "$_tether_review_refs"
    exit 1
fi
exec < "$_tether_review_refs"
rm -f "$_tether_review_refs" || exit 1
unset _tether_review_refs
'''.encode() + _HOOK_END)


def _hook_contents(original, gate, python, uninstall):
    if original is None:
        return None if uninstall else b'#!/bin/sh\n' + _hook_block(gate, python, b' (new hook)')
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(_HOOK_START)]
    ends = [i for i, line in enumerate(lines) if line == _HOOK_END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise GateRefused('The pre-push hook has invalid review gate markers. Check the hook before changing it.')
        start, end = starts[0], ends[0]
        marker = lines[start].removeprefix(_HOOK_START).removesuffix(b' >>>\n')
        if marker not in _HOOK_SUFFIXES:
            raise GateRefused('The pre-push hook has an invalid review gate marker. Check the hook before changing it.')
        before, after = b''.join(lines[:start]), b''.join(lines[end + 1:])
        if not uninstall:
            return before + _hook_block(gate, python, marker) + after
        if marker == b' (added newline)' and not after:
            before = before.removesuffix(b'\n')
        if marker == b' (new hook)' and not after and before == b'#!/bin/sh\n':
            return None
        return before + after
    if uninstall:
        return original
    if original.startswith(b'#!'):
        shebang, newline, rest = original.partition(b'\n')
        shells = {b'sh', b'bash', b'dash', b'ash', b'ksh', b'mksh', b'zsh'}
        words = shebang[2:].split()
        interpreter = words[0].rsplit(b'/', 1)[-1] if words else b''
        if interpreter not in shells and not (interpreter == b'env' and shells.intersection(words[1:])):
            raise GateRefused('The pre-push hook uses a different language. Ask Mak to add the review gate to the hook.')
        suffix = b'' if newline else b' (added newline)'
        return shebang + b'\n' + _hook_block(gate, python, suffix) + rest
    return _hook_block(gate, python, b'') + original


def _initialize_store(store):
    if os.path.lexists(store):
        return
    store.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=store.parent) as seed:
        os.fchmod(seed.fileno(), 0o600)
        seed.write(b'[]\n')
        seed.flush()
        try:
            # A watcher may publish the real store while the installer prepares the empty one.
            os.link(seed.name, store)
        except FileExistsError:
            pass


def install_hook(repo, uninstall=False, gate=None, python=None, *, store=None):
    hooks = Path(_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks').stdout.removesuffix('\n'))
    if not hooks.is_absolute():
        raise GateRefused('Git did not return an absolute hooks directory. Check the repository.')
    hook = hooks / 'pre-push'
    gate = Path(__file__).resolve() if gate is None else Path(gate).absolute()
    python = Path(sys.executable if python is None else python).absolute()
    hooks.mkdir(parents=True, exist_ok=True)
    with (hooks / '.tether-pre-push.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateRefused('Another installer holds the pre-push hook lock. Try again after it finishes.') from None
        try:
            info = hook.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise GateRefused('The pre-push hook must be a regular file. Check the hook path before changing it.')
        if info is None and uninstall:
            return
        original = hook.read_bytes() if info is not None else None
        contents = _hook_contents(original, gate, python, uninstall)
        if not uninstall:
            _initialize_store(_store_path() if store is None else Path(store))
        mode = stat.S_IMODE(info.st_mode) if info is not None else 0o755
        if contents is None:
            hook.unlink()
        elif info is None or contents != original or (not uninstall and not mode & stat.S_IXUSR):
            with tempfile.NamedTemporaryFile(dir=hooks, prefix='.pre-push.', delete=False) as staged:
                temporary = Path(staged.name)
                try:
                    staged.write(contents)
                    os.fchmod(staged.fileno(), mode if uninstall else mode | stat.S_IXUSR)
                    staged.close()
                    os.replace(temporary, hook)
                finally:
                    temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    push = commands.add_parser('pre-push')
    push.add_argument('remote')
    push.add_argument('url')
    check_cmd = commands.add_parser('check')
    check_cmd.add_argument('--commit', required=True)
    check_cmd.add_argument('--repo', type=Path, default=Path('.'))
    check_cmd.add_argument('--base')
    hook = commands.add_parser('install-hook')
    hook.add_argument('--repo', type=Path, default=Path('.'))
    hook.add_argument('--uninstall', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'pre-push':
            pre_push(Path('.'), sys.stdin)
        elif args.action == 'check':
            check(args.repo, args.commit, base=args.base)
        else:
            install_hook(args.repo, args.uninstall)
    except GateRefused as error:
        print(f'review gate: {error}', file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print('review gate: Tether could not read or write the required files. Check the repository and review store.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
