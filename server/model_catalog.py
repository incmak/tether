"""Provider-owned model discovery and short-lived release notices."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

import codex_level
import paths

ALIASES = ('fable', 'opus', 'sonnet', 'haiku')
FAMILIES = ALIASES
PREFER = ('mythos', *ALIASES)
# These are product/test strings in the Claude binary, not model families.
DENY_FAMILIES = {'code', 'desktop', 'eval'}
CHUNK_SIZE = 1024 * 1024
OVERLAP = 256
NOTICE_SECONDS = 14 * 24 * 60 * 60
ID = re.compile(rb'claude-([a-z]+)-([0-9]+)(?:-([0-9]{1,2})(?![0-9]))?')
DATED = re.compile(rb'-[0-9]{8}(?![0-9])')
_LOCK = threading.RLock()
REGISTRY = re.compile(rb'\{id:"(claude-[a-z0-9-]+)",family:"([a-z]+)",display_name:"([^"]{1,40})"')
_run = subprocess.run
_PROBING = False
_PENDING = []
_ACTIVE = None
_GENERATION = 0


def registry(binary):
    """Keep enough lookahead to read ranks across chunks and nested objects."""
    found = {}
    with open(binary, 'rb') as stream:
        tail = b''
        while True:
            chunk = stream.read(CHUNK_SIZE)
            data = tail + chunk
            limit = max(0, len(data) - 4200) if chunk else len(data)
            for match in REGISTRY.finditer(data):
                if match.start() >= limit:
                    break
                slug, family, name = (part.decode() for part in match.groups())
                version = re.fullmatch(r'claude-' + re.escape(family) + r'-(\d+)(?:-(\d{1,2}))?', slug)
                if not version:
                    continue
                end = data.find(b'{id:"', match.end(), match.start() + 4000)
                entry = data[match.end():end if end >= 0 else match.start() + 4000]
                rank = re.search(rb'advisor_rank:(\d+)', entry)
                found[slug] = dict(id=slug, family=family, name=name,
                                   rank=int(rank[1]) if rank else None,
                                   version=(int(version[1]), int(version[2]) if version[2] else None))
            if not chunk:
                break
            tail = data[limit:]
    return list(found.values())


def _order(row):
    return (-(row['rank'] if row['rank'] is not None else -1),
            PREFER.index(row['family']) if row['family'] in PREFER else len(PREFER), row['name'])


def _registry_result(entries):
    newest = {}
    for entry in entries:
        family = entry['family']
        if family in DENY_FAMILIES:
            continue
        if family not in newest or _version(entry['version']) > _version(newest[family]['version']):
            newest[family] = entry
    rows = []
    for family, entry in newest.items():
        row = _row(family, entry['version'])
        row.update(name=entry['name'], family=family, rank=entry['rank'])
        rows.append(row)
    rows.sort(key=_order)
    alias_ranks = [r['rank'] for r in rows if r['family'] in ALIASES and r['rank'] is not None]
    top = max(alias_ranks) if alias_ranks else None
    return dict(models=rows, latest=None, source='claude-code',
                new_families=sorted(r['family'] for r in rows if r['family'] not in PREFER
                                    and r['rank'] is not None and top is not None and r['rank'] >= top))


def _stale(probe, key, now=None):
    if not isinstance(probe, dict) or probe.get('key') != key:
        return True
    state, at = probe.get('state'), probe.get('at')
    if state not in ('available', 'unavailable', 'error') or type(at) not in (int, float):
        return True
    return (time.time() if now is None else now) - at >= (7 if state == 'available' else 1) * 86400


def _probe(binary, slug):
    folder = paths.CACHE / 'model-probe'
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        # Never launch with local settings or hooks planted in this directory.
        if any(folder.iterdir()):
            return 'error'
        done = _run([str(binary), '-p', '--setting-sources', 'local', '--no-session-persistence',
                     '--max-turns', '1', '--model', slug, 'Reply with OK only.'],
                    cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        if done.returncode == 0:
            return 'available'
        if 'may not exist or you may not have access' in (done.stdout + done.stderr).lower():
            return 'unavailable'
    except (OSError, subprocess.SubprocessError):
        pass
    return 'error'


def _probe_all(binary, key, slugs, generation=None):
    """Synchronous entry point; tests inject _run and never launch an agent."""
    for slug in slugs:
        state = _probe(binary, slug)
        with _LOCK:
            if generation is not None and generation != _GENERATION:
                continue
            cache = _read()
            before = copy.deepcopy(cache)
            cache.setdefault('probes', {})[slug] = dict(state=state, at=time.time(), key=key)
            if cache != before:
                _write(cache)


def _probe_worker():
    global _PROBING, _ACTIVE
    while True:
        with _LOCK:
            if not _PENDING:
                _ACTIVE = None
                _PROBING = False
                return
            _ACTIVE = _PENDING.pop(0)
            generation, binary, key, slugs = _ACTIVE
        try:
            _probe_all(binary, key, slugs, generation)
        except Exception:
            # A cache failure must not strand the worker flag.
            with _LOCK:
                _ACTIVE = None
                _PROBING = False
            return
        with _LOCK:
            _ACTIVE = None


def _start_probes(binary, key, slugs):
    global _PROBING
    queued = [job for job in [*_PENDING, *([_ACTIVE] if _ACTIVE else [])]
              if job[0] == _GENERATION and job[2] == key]
    slugs = [slug for slug in slugs if not any(slug in job[3] for job in queued)]
    if not slugs:
        return
    job = (_GENERATION, binary, key, slugs)
    if job != _ACTIVE and job not in _PENDING:
        _PENDING.append(job)
    if not _PROBING:
        _PROBING = True
        try:
            threading.Thread(target=_probe_worker, daemon=True).start()
        except Exception:
            _PROBING = False
            raise


def recheck():
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        _PENDING.clear()
        cache = _read()
        before = copy.deepcopy(cache)
        cache.pop('scan', None)
        cache.pop('probes', None)
        if cache != before:
            _write(cache)
    return catalog()


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
    return {'slug': f'claude-{family}-{suffix}', 'name': name, 'efforts': list(delegate.EFFORTS),
            'family': family, 'rank': None}


def _builtin():
    import delegate
    rows = [{'slug': slug, 'name': name, 'efforts': list(delegate.EFFORTS),
             'family': slug.split('-')[1], 'rank': None}
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
        key, binary = None, None
        try:
            binary = shutil.which('claude')
            if binary:
                binary = Path(binary).resolve()
                stat = binary.stat()
                key = [str(binary), stat.st_size, stat.st_mtime_ns]
            saved = cache.get('scan', {})
            if (key is not None and saved.get('key') == key
                    and all('family' in row and 'rank' in row for row in saved.get('result', {}).get('models', []))
                    and 'result' in saved):
                result = copy.deepcopy(saved['result'])
            else:
                entries = registry(binary) if binary else []
                result = (_registry_result(entries) if entries else _discovered(claude_ids(binary))) if binary else _builtin()
                cache['scan'] = {'key': key, 'result': copy.deepcopy(result)}
        except OSError:
            result = _builtin()
        stale = []
        for row in result['models']:
            family = row['family']
            row['launch'] = family if family in ALIASES else row['slug']
            probe = cache.get('probes', {}).get(row['slug'], {})
            valid = not _stale(probe, key)
            # While a stale probe re-runs, show its last result for the same Claude Code build.
            state = probe.get('state') if isinstance(probe, dict) and probe.get('key') == key else None
            row['available'] = True if family in ALIASES else (
                True if state == 'available' else False if state == 'unavailable' else None)
            row['access'] = ('ok' if row['available'] is True else 'no-access' if row['available'] is False
                             else 'error' if state == 'error' else 'checking')
            if family not in ALIASES and not valid and key is not None:
                stale.append(row['slug'])
        if any(row['rank'] is not None for row in result['models']):
            available = sorted((row for row in result['models'] if row['available'] is True), key=_order)
            result['latest'] = ({k: available[0][k] for k in ('launch', 'slug', 'name')} if available else None)
        elif result['latest'] is None:
            aliases = [row for family in ALIASES for row in result['models'] if row['family'] == family]
            result['latest'] = ({k: aliases[0][k] for k in ('launch', 'slug', 'name')} if aliases else None)
        # The release notice is provider state, not part of the cached binary scan.
        result = _notice(cache, 'claude', dict(result))
        if json.dumps(cache, sort_keys=True) != before:
            _write(cache)
        _start_probes(binary, key, stale)
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
