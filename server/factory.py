"""Read-only facts for the Factory view (ui/src/Factory.tsx) that no other endpoint gives.

The Factory view shows every agent tab as work that moves through phases (Inbox, Spec, Build, Review, Verify, Deploy,
Done). The UI already has the tabs (/api/sessions) and the backlog (/api/projects/backlog). This module adds five
things, all read-only:

- tools:    each agent tab's latest tool calls, from the end of its transcript. The UI guesses Spec, Build, Verify and
            Deploy from them (ui/src/factoryModel.ts). Only the last TAIL_BYTES of a transcript are read, and the
            result is kept until the file changes, so a big screen that asks every few seconds costs little.
            The agents' own parsers turn records into tool summaries, so this module has no parser of its own.
- deployed: lines of DEPLOYED.md (the deploy log at the top of the Tether checkout) from the last DEPLOYED_DAYS days.
- commits:  recent commits of the git repositories that the tabs work in, so a task can show the commits that name it.
- sweep:    the last worktree sweep's lines (sweep.py, B210): behind main, failing checks, idle. Read from its file.
- history:  shared phase history (factory_history.py), as reported by the screens.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import factory_history
import paths
import sweep
import tether_zone

PATH = '/mobile/api/factory'
TAIL_BYTES = 262144
TOOLS = 12           # latest tool calls per tab
DEPLOYED_DAYS = 2
COMMIT_HOURS = 48
COMMITS = 300        # per repository
COMMIT_TTL = 30      # seconds
GIT_TIMEOUT = 5
GIT_BUDGET = 3       # seconds of uncached git work per request; the rest fills in on later polls
REPOSITORIES = 12
AGENTS = ('claude', 'codex', 'antigravity')

_tail_cache = {}     # path -> (inode, size, mtime_ns, tools)
_tail_lock = threading.Lock()
_commit_cache = {}   # repository -> (monotonic, commits)
_root_cache = {}     # folder -> (monotonic, repository or None)
_lock = threading.Lock()

REF = re.compile(r'\bB(\d{1,5})\b')
COMMIT = re.compile(r'\b([0-9a-f]{7,40})\b')
MONTHS = {name: number for number, name in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
ISO_DAY = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?')
WORD_DAY = re.compile(r'^(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})(?: (\d{1,2}):(\d{2}))?')
ZONES = {'IST': timedelta(hours=5, minutes=30), 'UTC': timedelta(0), 'GMT': timedelta(0)}
ZONE = re.compile(r'^\s*(IST|UTC|GMT|[+-]\d{2}:?\d{2})\b')
LEAD = re.compile(r'^[\s·:]+')


def deployed_file():
    return Path(os.environ.get('TETHER_DEPLOYED') or Path(__file__).resolve().parent.parent / 'DEPLOYED.md')


def refs(text):
    return sorted({f'B{number}' for number in REF.findall(text or '')}, key=lambda ref: int(ref[1:]))


def parse_deployed_line(line):
    """One DEPLOYED.md line: {at, day, timed, commit, text, refs}, or None for a line with no date. The log has no single
    format: "- 2026-11-17 08:35 IST · 5d59421 ...", "2026-11-17 00:21 IST: b8636f0 ...", "- 09 Nov 2026 11:35 a315cf8 ...".
    at: Unix seconds, using the written offset when known and host time otherwise. For a line with only a date,
    at is host midnight for older clients and day is the written date (timed False)."""
    text = line.strip().lstrip('-*').strip()
    found = ISO_DAY.match(text)
    try:
        if found:
            year, month, day, hour, minute = found.groups()
            when = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
        else:
            found = WORD_DAY.match(text)
            if not found or found[2].lower() not in MONTHS:
                return None
            when = datetime(int(found[3]), MONTHS[found[2].lower()], int(found[1]), int(found[4] or 0), int(found[5] or 0))
    except ValueError:
        return None
    timed = found.group(4) is not None
    rest = text[found.end():]
    zone = ZONE.match(rest)
    if zone and timed:
        token = zone[1]
        if token in ZONES:
            when = when.replace(tzinfo=timezone(ZONES[token]))
        else:
            try:
                when = when.replace(tzinfo=datetime.strptime(token, '%z').tzinfo)
            except ValueError:
                return None
    at = when.timestamp()
    rest = LEAD.sub('', rest[zone.end():] if zone else rest).strip()
    commit = COMMIT.search(rest)
    return {'at': int(at), 'day': when.date().isoformat(), 'timed': timed, 'commit': commit[1] if commit else None,
            'text': rest[:400], 'refs': refs(rest)}


def deployed(now=None, file=None):
    """DEPLOYED.md lines from the last DEPLOYED_DAYS days, newest first. A missing file gives none."""
    now = time.time() if now is None else now
    try:
        lines = (file or deployed_file()).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    today = tether_zone.now(now).date()
    since = today - timedelta(days=DEPLOYED_DAYS)
    first_day, last_day = since.isoformat(), today.isoformat()
    first_at = tether_zone.day_start(since)
    found = []
    for item in map(parse_deployed_line, lines):
        if not item:
            continue
        if item['timed']:
            recent = first_at <= item['at'] <= now + 86400
        else:
            recent = first_day <= item['day'] <= last_day
        if recent:
            found.append(item)
    return sorted(found, key=lambda item: item['at'], reverse=True)


def tail_records(path, size=TAIL_BYTES):
    """The whole JSON records in the last `size` bytes of a JSONL file. The first line may start mid-record: dropped."""
    with open(path, 'rb') as stream:
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
        start = max(0, end - size)
        stream.seek(start)
        data = stream.read()
    lines = data.split(b'\n')
    if start:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def tools_from(records, fresh, consume, limit=TOOLS):
    """The latest tool calls in these records, oldest first: {name, title, at, error, done}."""
    state = fresh()
    for index, record in enumerate(records):
        try:
            consume(state, index, record)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # one odd record must not hide the others
    messages = state['messages'] if isinstance(state, dict) else state
    found = []
    for message in messages:
        tool = message.get('tool') if isinstance(message, dict) else None
        if isinstance(tool, dict):
            found.append({'name': tool.get('name') or 'Tool', 'title': tool.get('title') or '', 'at': tool.get('started') or message.get('time'),
                          'error': bool(tool.get('is_error')), 'done': tool.get('status') == 'complete'})
    return found[-limit:]


def recent_tools(entry, readers):
    """An agent tab's latest tool calls. readers: agent -> (fresh, consume), the agent's own transcript parser."""
    reader = readers.get(entry.get('agent'))
    path = entry.get('transcript')
    if not reader or not path:
        return []
    try:
        info = os.stat(path)
    except (OSError, TypeError):
        return []
    key = (info.st_ino, info.st_size, info.st_mtime_ns)
    with _tail_lock:
        cached = _tail_cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
    try:
        found = tools_from(tail_records(path), *reader)
    except OSError:
        return []
    with _tail_lock:
        if len(_tail_cache) > 200:
            _tail_cache.clear()
        _tail_cache[path] = (key, found)
    return found


def _time_left(deadline):
    """Seconds this request may still spend on git, or None when it has none left."""
    if deadline is None:
        return GIT_TIMEOUT
    left = deadline - time.monotonic()
    return min(GIT_TIMEOUT, left) if left > 0.2 else None


def repository(folder, run=subprocess.run, deadline=None):
    """The main checkout of the git repository a folder belongs to (worktrees share it), or None.
    Past the deadline it answers from the cache only, even an old entry."""
    now = time.monotonic()
    with _lock:
        cached = _root_cache.get(folder)
        if cached and now - cached[0] < 300:
            return cached[1]
    left = _time_left(deadline)
    if left is None:
        return cached[1] if cached else None
    root = None
    try:
        result = run(['git', '-C', folder, 'rev-parse', '--path-format=absolute', '--git-common-dir'], capture_output=True, text=True, timeout=left)
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip())
            root = str(common.parent if common.name == '.git' else common)
    except (OSError, subprocess.SubprocessError):
        root = None
    with _lock:
        _root_cache[folder] = (now, root)
    return root


def commits_of(root, run=subprocess.run, now=None, deadline=None):
    """Commits of the last COMMIT_HOURS hours on every branch of one repository, newest first.
    Past the deadline it answers from the cache only, even an old entry."""
    clock = time.monotonic()
    with _lock:
        cached = _commit_cache.get(root)
        if cached and clock - cached[0] < COMMIT_TTL:
            return cached[1]
    left = _time_left(deadline)
    if left is None:
        return cached[1] if cached else []
    found = []
    try:
        result = run(['git', '-C', root, 'log', '--all', f'--since={COMMIT_HOURS}.hours', f'-n{COMMITS}', '--format=%h%x09%ct%x09%s'],
                     capture_output=True, text=True, timeout=left)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split('\t', 2)
                if len(parts) == 3 and parts[1].isdigit():
                    found.append({'hash': parts[0], 'at': int(parts[1]), 'subject': parts[2][:300], 'refs': refs(parts[2]), 'repo': Path(root).name})
    except (OSError, subprocess.SubprocessError):
        found = []
    with _lock:
        _commit_cache[root] = (clock, found)
    return found


def payload(entries, folder_of, readers, run=subprocess.run, now=None):
    """The Factory view's extra facts. entries: the live tabs (server.sessions()); folder_of(entry): a tab's folder."""
    now = now or time.time()
    agents = [entry for entry in entries if entry.get('agent') in AGENTS]
    tools = {entry['id']: recent_tools(entry, readers) for entry in agents}
    deadline = time.monotonic() + GIT_BUDGET
    roots = []
    for entry in agents:
        if len(roots) >= REPOSITORIES:
            break
        try:
            folder = folder_of(entry)
        except (OSError, KeyError, ValueError, TypeError):
            continue
        root = repository(folder, run, deadline) if folder else None
        if root and root not in roots:
            roots.append(root)
    commits = sorted((commit for root in roots for commit in commits_of(root, run, deadline=deadline)), key=lambda item: item['at'], reverse=True)
    try:
        swept = sweep.public({entry['id'] for entry in entries})
    except Exception:  # noqa: BLE001 - the sweep file must never break the view
        swept = {'at': 0, 'every': sweep.PERIOD, 'lines': []}
    try:
        shared = factory_history.history()
    except OSError:  # an unreadable history file must never break the view; the next write replaces it
        shared = {}
    return {'now': int(now), 'tools': tools, 'deployed': deployed(now), 'commits': commits[:COMMITS],
            'sweep': swept, 'history': shared}
