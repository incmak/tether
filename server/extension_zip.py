"""Build the Chrome extension download from HEAD or an installed release."""
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import zipfile

import files


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 200
MAX_BYTES = 5 * 1024 * 1024
_CACHE_SIZE = 4
_cache = {}
_cache_lock = threading.Lock()
_directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ExtensionZipError(Exception):
    """A build failure with a message safe to send to the user."""


def _git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.run(['git', '-C', root, '--no-replace-objects', *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=10, check=True).stdout


def _check_count(count):
    if count > MAX_FILES:
        raise ExtensionZipError('The extension contains more than 200 files.')


def _check_size(size, remaining):
    if size > remaining:
        raise ExtensionZipError('The extension files exceed 5 MB in total.')


def _committed_files(root, commit):
    tree = _git(root, 'ls-tree', '-r', '-z', '--full-tree', commit, '--', 'extension/')
    blobs = []
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, _, path = entry.partition(b'\t')
        mode, kind, oid = metadata.split()
        prefix, _, name = path.partition(b'/')
        if prefix != b'extension' or any(part in (b'', b'.', b'..') for part in name.split(b'/')):
            raise ExtensionZipError('The extension contains an invalid path.')
        if kind == b'blob' and mode in (b'100644', b'100755'):
            blobs.append((name, int(mode, 8), oid))
            _check_count(len(blobs))

    entries = {}
    remaining = MAX_BYTES
    for name, mode, oid in blobs:
        # Check the uncompressed size before asking Git to send the blob.
        size = int(_git(root, 'cat-file', '-s', oid))
        _check_size(size, remaining)
        entries[name] = (mode, _git(root, 'cat-file', 'blob', oid))
        remaining -= size
    return entries


def _scan_release(directory, entries, remaining, prefix=b''):
    # Keep directories open and resolve each child relative to its parent descriptor.
    with os.scandir(directory) as children:
        for child in children:
            name = prefix + os.fsencode(child.name)
            if child.is_dir(follow_symlinks=False):
                subdirectory = os.open(child.name, _directory_flags, dir_fd=directory)
                try:
                    remaining = _scan_release(subdirectory, entries, remaining, name + b'/')
                finally:
                    os.close(subdirectory)
            elif child.is_file(follow_symlinks=False):
                _check_count(len(entries) + 1)
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                with os.fdopen(os.open(child.name, flags, dir_fd=directory), 'rb') as stream:
                    mode = os.fstat(stream.fileno()).st_mode
                    if not stat.S_ISREG(mode):
                        continue
                    data = stream.read(remaining + 1)
                _check_size(len(data), remaining)
                entries[name] = (0o100755 if mode & 0o111 else 0o100644, data)
                remaining -= len(data)
    return remaining


def _release_files(folder):
    if folder.is_symlink():
        raise ExtensionZipError('The extension folder is a symbolic link.')
    try:
        directory = os.open(folder, _directory_flags)
    except FileNotFoundError:
        raise ExtensionZipError('manifest.json is missing.') from None
    try:
        entries = {}
        _scan_release(directory, entries, MAX_BYTES)
        return entries
    finally:
        os.close(directory)


def _reject_json_constant(_constant):
    raise ValueError  # JSON does not allow NaN or Infinity.


def _archive(entries):
    if b'manifest.json' not in entries:
        raise ExtensionZipError('manifest.json is missing.')
    try:
        manifest = json.loads(entries[b'manifest.json'][1], parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        raise ExtensionZipError('manifest.json is not valid JSON.') from None
    version = manifest.get('version') if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r'\d+(\.\d+){0,3}', version):
        raise ExtensionZipError('manifest.json must contain a valid version.')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, (mode, data) in sorted(entries.items()):
            try:
                path = name.decode('utf-8')
            except UnicodeDecodeError:
                raise ExtensionZipError('The extension contains a path that is not UTF-8.') from None
            info = zipfile.ZipInfo('tether-extension/' + path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return f'tether-extension-{version}.zip', buffer.getvalue()


def build(root=REPO_ROOT, use_cache=True):
    """Return the attachment name and zip bytes for this copy of Tether."""
    if not (root / '.git').exists():
        return _archive(_release_files(root / 'extension'))

    commit = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    if use_cache:
        with _cache_lock:
            if commit in _cache:
                return _cache[commit]
    result = _archive(_committed_files(root, commit))
    if use_cache:
        with _cache_lock:
            _cache[commit] = result
            if len(_cache) > _CACHE_SIZE:
                del _cache[next(iter(_cache))]
    return result


def send(handler):
    try:
        filename, data = build()
    except ExtensionZipError as exc:
        reason = str(exc)
    except OSError:
        reason = 'The extension files could not be read.'
    except subprocess.SubprocessError:
        reason = 'Git could not read the committed extension.'
    else:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/zip')
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Content-Disposition', files.disposition(filename))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers()
        handler.wfile.write(data)
        return
    handler.respond(500, {'error': 'Could not build the extension zip. ' + reason})
