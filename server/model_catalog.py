"""Provider-owned model discovery and short-lived release notices."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

import codex_level
import paths

FAMILIES = ('fable', 'opus', 'sonnet', 'haiku')
# These are product/test strings in the Claude binary, not model families.
DENY_FAMILIES = {'code', 'desktop', 'eval'}
CHUNK_SIZE = 1024 * 1024
OVERLAP = 256
NOTICE_SECONDS = 14 * 24 * 60 * 60
ID = re.compile(rb'claude-([a-z]+)-([0-9]+)(?:-([0-9]{1,2})(?![0-9]))?')
DATED = re.compile(rb'-[0-9]{8}(?![0-9])')
_LOCK = threading.RLock()


def claude_ids(binary):
    """Scan without loading the executable into memory, including split ids."""
    found = set()
    with open(binary, 'rb') as stream:
        tail = b''
        while True:
            chunk = stream.read(CHUNK_SIZE)
            data = tail + chunk
            limit = max(0, len(data) - OVERLAP) if chunk else len(data)
            for match in ID.finditer(data):
                if match.start() >= limit:
                    break
                if DATED.match(data, match.end()):
                    continue
                family, major, minor = match.groups()
                found.add((family.decode(), (int(major), int(minor) if minor is not None else None)))
            if not chunk:
                break
            tail = data[limit:]
    return found


def _version(version):
    return version[0], -1 if version[1] is None else version[1]


def _row(family, version):
    import delegate
    major, minor = version
    suffix = str(major) + (f'-{minor}' if minor is not None else '')
    name = f'{family.title()} {major}' + (f'.{minor}' if minor is not None else '')
    return {'slug': f'claude-{family}-{suffix}', 'name': name, 'efforts': list(delegate.EFFORTS)}


def _builtin():
    import delegate
    rows = [{'slug': slug, 'name': name, 'efforts': list(delegate.EFFORTS)}
            for slug, name in delegate.CLAUDE_MODELS if slug.startswith('claude-')]
    return {'latest': {'launch': 'fable', 'slug': rows[0]['slug'], 'name': rows[0]['name']},
            'models': rows, 'new_families': [], 'source': 'built-in'}


def _discovered(ids):
    newest = {}
    for family, version in ids:
        if family not in newest or _version(version) > _version(newest[family]):
            newest[family] = version
    families = [family for family in FAMILIES if family in newest]
    if not families:
        return _builtin()
    rows = [_row(family, newest[family]) for family in families]
    return {'latest': dict(launch=families[0], slug=rows[0]['slug'], name=rows[0]['name']),
            'models': rows,
            'new_families': sorted(family for family, version in newest.items()
                                   if family not in FAMILIES and family not in DENY_FAMILIES
                                   and version[0] >= newest[families[0]][0]),
            'source': 'claude-code'}


def _read():
    try:
        value = json.loads((paths.CACHE / 'model-catalog.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(value):
    """The cache is optional; publish it atomically with owner-only permissions."""
    try:
        paths.CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix='.model-catalog.', dir=paths.CACHE)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream)
                stream.write('\n')
            os.replace(temp, paths.CACHE / 'model-catalog.json')
        finally:
            Path(temp).unlink(missing_ok=True)
    except OSError:
        pass


def _notice(cache, provider, result):
    now = time.time()
    providers = cache.setdefault('providers', {})
    previous = providers.get(provider, {})
    latest = result['latest']
    changed = previous.get('changed')
    if latest is not None:
        if previous.get('slug') and previous['slug'] != latest['slug']:
            changed = {'from': previous['name'], 'to': latest['name'], 'at': now}
        previous = {'slug': latest['slug'], 'name': latest['name']}
    if changed and now - changed['at'] >= NOTICE_SECONDS:
        changed = None
    providers[provider] = dict(previous, changed=changed)
    result['changed'] = changed
    return result


def claude():
    with _LOCK:
        cache = _read()
        before = json.dumps(cache, sort_keys=True)
        key = None
        try:
            binary = shutil.which('claude')
            if binary:
                binary = Path(binary).resolve()
                stat = binary.stat()
                key = [str(binary), stat.st_size, stat.st_mtime_ns]
            saved = cache.get('scan', {})
            if key is not None and saved.get('key') == key:
                result = saved['result']
            else:
                result = _discovered(claude_ids(binary)) if binary else _builtin()
                cache['scan'] = {'key': key, 'result': result}
        except OSError:
            result = _builtin()
        # The release notice is provider state, not part of the cached binary scan.
        result = _notice(cache, 'claude', dict(result))
        if json.dumps(cache, sort_keys=True) != before:
            _write(cache)
        return result


def codex():
    rows = [{'slug': row['slug'], 'name': row['name'], 'efforts': list(row.get('efforts', []))}
            for row in codex_level.models()]
    result = {'latest': dict(launch=rows[0]['slug'], slug=rows[0]['slug'], name=rows[0]['name']) if rows else None,
              'models': rows, 'new_families': [], 'source': 'codex'}
    with _LOCK:
        cache = _read()
        before = json.dumps(cache, sort_keys=True)
        result = _notice(cache, 'codex', result)
        if json.dumps(cache, sort_keys=True) != before:
            _write(cache)
    return result


def catalog():
    return {'claude': claude(), 'codex': codex()}
