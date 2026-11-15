"""Trust a folder for Codex, so Codex can edit files there (backlog B82).

Codex runs read-only in a folder that has no [projects."<path>"] entry with a trust_level in $CODEX_HOME/config.toml
(Codex 0.166 core/src/config/permissions.rs). Tether then starts it without --add-dir (project_launch.codex_writable).

  POST /mobile/api/codex-trust {op: 'status', mode, session?, cwd?, worktree?}
      mode 'new': a new Codex tab in the folder of tab `session`, in `cwd`, or in the default session folder;
                  with worktree: true, in a new worktree of that repository.
      mode 'running': the Codex tab `session`.
      -> {state, folder, target, target_id?}. state: 'trusted' (nothing to do), 'untrusted' (trusting `target` makes Codex
         writable), 'restart' (the folder is trusted, but the running tab started read-only), 'other' (Codex is
         read-only or unknown for another reason, such as a profile, or Tether cannot tell).
         target_id (when untrusted): the identity (st_dev:st_ino) of the folder `target` names.
  POST {op: 'trust', ...the same fields, target, target_id, approval}
      Writes trust_level = "trusted" for `target`, only with a passkey approval (approval.py): agents hold Tether's token,
      so a plain call must never let an agent trust a folder itself. The approval binds target and target_id. After
      the approval Tether checks again that the same folder and the same Codex folder are there, then writes.

The key is the one the Codex 0.166 TUI writes (tui/src/config_update.rs, config/src/loader/mod.rs project_trust_key):
an existing entry for the folder (canonical spelling first), else one for its repository root, else the canonical
repository root (for a linked worktree, the main checkout: git-utils/src/trust.rs), else the canonical folder.
The edit changes only that one table and keeps every other line, comment and the file mode."""
import copy
import fcntl
import json
import os
import re
import secrets
import stat
from pathlib import Path

import approval
import codex_provider
import paths
import procinfo
from project_launch import _canon, _repo_root, _toml, codex_writable, toml_string

PATH = '/mobile/api/codex-trust'
BACKUP = 'config.toml.bak-tether-trust'
TRUSTED = 'trust_level = "trusted"'
WRITABLE = ('workspace-write', 'danger-full-access', 'external-sandbox')


class TrustError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _identity(path, follow=True):
    """st_dev:st_ino of a directory, or None. follow=False: None for a symbolic link."""
    try:
        info = os.stat(path, follow_symlinks=follow)
    except (OSError, ValueError):
        return None
    return f'{info.st_dev}:{info.st_ino}' if stat.S_ISDIR(info.st_mode) else None


def handles(path):
    return path.split('?', 1)[0] == PATH


def home():
    return Path(os.environ.get('CODEX_HOME') or paths.CODEX)


def target_key(folder, config):
    """The [projects] key Codex itself would write when the user trusts `folder`."""
    known = config.get('projects') if isinstance(config.get('projects'), dict) else {}
    real = _canon(folder)
    for key in dict.fromkeys([real, folder]):
        if key and key in known:
            return key
    root = _repo_root(folder)
    for key in dict.fromkeys([_canon(root), root]):
        if key and key in known:
            return key
    return _canon(root) or real


def last_turn(transcript):
    """(cwd, sandbox type) from the last turn_context record of a Codex rollout, or (None, None)."""
    try:
        with open(transcript, 'rb') as handle:
            size = handle.seek(0, 2)
            window = 262144
            while True:  # a long tool output can push the last turn record far from the end
                handle.seek(max(0, size - window))
                lines = [line for line in handle.read().split(b'\n') if b'"type":"turn_context"' in line]
                if lines or window >= size or window >= 33554432:
                    break
                window *= 4
        for line in reversed(lines):
            try:
                payload = json.loads(line)['payload']
            except (ValueError, KeyError, TypeError):
                continue  # the first line of a window can be cut
            policy = payload.get('sandbox_policy') if isinstance(payload, dict) else None
            if isinstance(policy, dict) and isinstance(policy.get('type'), str):
                return payload.get('cwd') if isinstance(payload.get('cwd'), str) else None, policy['type']
    except (OSError, TypeError):
        pass
    return None, None


def _subject(body, choose, agent_folder, chat_folder, session_folder):
    """(Codex args, start folder, sandbox of the running tab or None) for the request."""
    mode, session = body.get('mode'), body.get('session')
    if mode not in ('new', 'running') or (session is not None and not isinstance(session, str)):
        raise ValueError('Choose a Codex tab or a folder.')
    if not isinstance(body.get('worktree', False), bool):
        raise ValueError('Choose a Codex tab or a folder.')
    if mode == 'running':
        entry = choose(session) if session else None
        if not entry or entry.get('agent') != 'codex' or not entry.get('owner_pid'):
            raise TrustError(409, 'That tab does not run Codex.')
        try:
            args = [arg.decode(errors='surrogateescape') for arg in procinfo.argv(entry['owner_pid'])][1:]
            folder = procinfo.cwd(entry['owner_pid'])
        except OSError:
            raise TrustError(409, 'That tab has ended. Select another tab.') from None
        args = args[:-1] if args and args[-1] == '' else args  # /proc cmdline ends with a NUL
        cwd, sandbox = (None, None) if entry.get('pending_conversation') else last_turn(entry.get('transcript'))
        return args, cwd or folder, sandbox
    if session:
        folder = agent_folder(choose(session))
    elif body.get('cwd') is not None:
        folder = chat_folder(body['cwd'])
    else:
        folder = session_folder()
    if body.get('worktree'):
        folder = _repo_root(folder) if folder else None  # a new linked worktree trusts as its main checkout
    try:
        profile = codex_provider.launch_profile()
    except RuntimeError:
        profile = codex_provider.PROFILE
    return (['-p', profile] if profile else []), folder, None


def status(body, **context):
    args, folder, sandbox = _subject(body, **context)
    if not isinstance(folder, str) or not os.path.isdir(folder):
        return {'state': 'other', 'folder': folder, 'target': None}
    config = _toml(home() / 'config.toml')
    if config is None:
        return {'state': 'other', 'folder': folder, 'target': None}
    target = target_key(folder, config)
    if sandbox in WRITABLE:
        state = 'trusted'
    elif codex_writable(args, folder):
        state = 'restart' if sandbox == 'read-only' else 'trusted'
    elif target and codex_writable(args, folder, assume=target):
        state = 'untrusted'
    else:
        state = 'other'
    if state != 'untrusted':
        return {'state': state, 'folder': folder, 'target': None}
    return {'state': state, 'folder': folder, 'target': target, 'target_id': _identity(target)}


def _header(line):
    """The key path of a [table] header line, or None. tomllib parses the line, so quoting and comments are exact.
    A line inside a multi-line string can look like a header: the check in edit() then refuses the result."""
    if not line.lstrip().startswith('[') or line.lstrip().startswith('[['):
        return None
    try:
        import tomllib
        parsed = tomllib.loads(line)
    except ValueError:
        return None
    keys = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key, parsed = next(iter(parsed.items()))
        keys.append(key)
    return keys if parsed == {} and keys else None


# One trust_level line with a one-line TOML string (basic with escapes, literal, or either multi-line form on one line),
# then only blanks and an optional comment. Anything else is refused, so the comment after the value is never lost.
_VALUE = re.compile(r'(\s*trust_level\s*=\s*)('
                    r'"""(?:[^"\\\n]|\\[^\n]|"(?!""))*"""'
                    r"|'''(?:[^'\n]|'(?!''))*'''"
                    r'|"(?:[^"\\\n]|\\[^\n])*"'
                    r"|'[^'\n]*')"
                    r'([ \t]*(?:#[^\n]*)?\r?\n?)$')


def edit(text, key):
    """config.toml text with trust_level = "trusted" for [projects.<key>], every other byte kept.
    Raises TrustError when the result would differ in any other value (dotted or inline forms, odd layouts)."""
    import tomllib
    try:
        before = tomllib.loads(text)
    except ValueError:
        raise TrustError(409, 'Codex config.toml is not valid TOML. Fix it, then try again.') from None
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _header(line) == ['projects', key]), None)
    if start is None:
        tail = '' if not text or text.endswith('\n') else newline
        gap = newline if text.strip() else ''
        result = text + tail + gap + f'[projects.{toml_string(key)}]' + newline + TRUSTED + newline
    else:
        end = next((i for i in range(start + 1, len(lines)) if _header(lines[i]) is not None
                    or lines[i].lstrip().startswith('[[')), len(lines))
        found = next((i for i in range(start + 1, end) if re.match(r'\s*trust_level\s*=', lines[i])), None)
        if found is None:
            lines.insert(start + 1, TRUSTED + newline)
        else:
            value = _VALUE.match(lines[found])
            if not value:  # a value over several lines, or not a string: Tether cannot keep its comment exactly
                raise TrustError(409, CANNOT_EDIT)
            lines[found] = value.group(1) + '"trusted"' + value.group(3)  # only the value changes: a comment stays
        result = ''.join(lines)
    expected = copy.deepcopy(before)
    projects = expected.setdefault('projects', {})
    if not isinstance(projects, dict) or not isinstance(projects.setdefault(key, {}), dict):
        raise TrustError(409, CANNOT_EDIT)
    projects[key]['trust_level'] = 'trusted'
    try:
        after = tomllib.loads(result)
    except ValueError:
        raise TrustError(409, CANNOT_EDIT) from None
    if after != expected:
        raise TrustError(409, CANNOT_EDIT)
    return result


CANNOT_EDIT = ('Tether cannot change this form of the [projects] table in Codex config.toml safely, so it changed nothing. '
               'Add the entry in Codex: start codex in the folder and choose to trust it.')


def _read_regular(name, directory):
    """(bytes, mode) of the regular, non-symlink file `name` in the open directory `directory`, or (None, None)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        if stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise TrustError(409, 'Codex config.toml is a symbolic link. Tether does not change it.') from exc
        raise
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise TrustError(409, 'Codex config.toml is not a regular file. Tether does not change it.')
        return handle.read(), stat.S_IMODE(info.st_mode)


def _open_home(folder, expect):
    """An open fd of the Codex folder. Refuses a symbolic link, and a folder other than `expect` (st_dev:st_ino)."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if folder.is_symlink():
            raise TrustError(409, 'The Codex folder is a symbolic link. Tether does not change it.') from exc
        raise
    info = os.fstat(fd)
    if expect is not None and f'{info.st_dev}:{info.st_ino}' != expect:
        os.close(fd)
        raise TrustError(409, 'The Codex folder changed after you approved. Nothing changed. Try again.')
    return fd


def write_trust(key, folder=None, expect=None):
    """Add or set [projects.<key>] trust_level = "trusted". Every step works in one open fd of the Codex folder, so a
    swapped folder cannot redirect the write. The lock is shared with codex_provider; a one-time backup; atomic."""
    directory = _open_home(Path(folder or home()), expect)
    try:
        with codex_provider.config_lock(directory):
            old, mode = _read_regular('config.toml', directory)
            try:
                text = (old or b'').decode('utf-8')
            except UnicodeDecodeError:
                raise TrustError(409, 'Codex config.toml is not UTF-8 text. Tether does not change it.') from None
            new = edit(text, key)
            if old is not None:
                try:
                    backup = os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
                except FileExistsError:
                    pass  # the first backup stays: it is the config before Tether ever changed trust
                else:
                    with os.fdopen(backup, 'wb') as handle:
                        handle.write(old)
                        os.fchmod(handle.fileno(), mode)
            temp = f'.config.toml.tether-{secrets.token_hex(8)}'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(new.encode('utf-8'))
                    os.fchmod(handle.fileno(), 0o600 if mode is None else mode)
                    handle.flush()
                    os.fsync(handle.fileno())
                if _read_regular('config.toml', directory)[0] != old:  # Codex has no lock: refuse when it wrote
                    raise TrustError(409, 'Codex config.toml changed while Tether wrote it. Try again.')
                os.replace(temp, 'config.toml', src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temp, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.fsync(directory)
    finally:
        os.close(directory)


def _body(handler):
    size = int(handler.headers.get('Content-Length', '0') or 0)
    if not 0 < size <= 16384:
        raise TrustError(413, 'Invalid request.')
    try:
        body = json.loads(handler.rfile.read(size))
    except (ValueError, UnicodeError):
        raise TrustError(400, 'Invalid request.') from None
    if not isinstance(body, dict):
        raise TrustError(400, 'Invalid request.')
    return body


def post(handler, **context):
    """context: choose(id) -> tab entry, agent_folder(entry), chat_folder(path), session_folder() (server.py)."""
    try:
        body = _body(handler)
        if body.get('op') == 'status':
            handler.respond(200, status(body, **context))
            return
        if body.get('op') != 'trust':
            raise ValueError('Choose status or trust.')
        payload = {key: value for key, value in body.items() if key != 'approval'}
        now = status(body, **context)
        if now['state'] != 'untrusted':
            raise TrustError(409, 'Codex can already edit in this folder.' if now['state'] in ('trusted', 'restart')
                             else 'Trusting this folder does not let Codex edit here. Nothing changed.')
        if body.get('target') != now['target'] or not now['target_id'] or body.get('target_id') != now['target_id']:
            raise TrustError(409, 'The folder changed. Check it again, then trust it.')
        real, codex_home = _canon(now['target']), _identity(home(), follow=False)
        approval.require(handler, 'codex-trust', payload, body.get('approval'))
        # The approval can take a while: the folder must still be the same directory, with the same canonical path.
        again = status(body, **context)
        if (again['state'], again['target'], again.get('target_id'), _canon(now['target'])) != \
                ('untrusted', now['target'], now['target_id'], real):
            raise TrustError(409, 'The folder changed after you approved. Nothing changed. Check it again, then trust it.')
        write_trust(now['target'], expect=codex_home)
        result = status(body, **context)
        if body.get('mode') == 'running' and result['state'] == 'trusted':
            result['state'] = 'restart'  # a tab with no turn yet started before the trust, so it still runs read-only
        handler.respond(200, result)
    except approval.ApprovalError as exc:
        approval.error(handler, exc)
    except TrustError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Tether could not change Codex config.toml.'})
