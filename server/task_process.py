"""Find and stop a Claude background shell or monitor through its process, not the terminal screen.

Claude Code starts each background shell and monitor as a direct child process in its own process
group. A shell writes to tasks/<id>.output, so its open file names it exactly. A monitor writes to a
socket, so it is matched by its command and start time. This does not depend on how a Claude Code
version draws /tasks.
"""
import json
import os
import signal
import threading
import time

import procinfo
from paths import CACHE

LEDGER = CACHE / 'task-stops.json'
LEDGER_LIMIT = 500
# The start record is written after the process starts; allow for a slow transcript write.
START_BEFORE, START_AFTER = 15, 3
_lock = threading.Lock()
_memo = [None, frozenset()]  # (path, mtime_ns), keys


def quoted(command):
    """Claude Code runs the command as eval '<command>' with POSIX single-quote escaping."""
    return "'" + command.replace("'", "'\\''") + "'"


def children(owner_pid):
    found = []
    for pid in procinfo.pids():
        try:
            if procinfo.parent(pid) == owner_pid:
                found.append(pid)
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return found


def _group_leader(pid):
    try:
        return os.getpgid(pid) == pid and procinfo.uid(pid) == os.getuid()
    except (OSError, ValueError, KeyError):
        return False


def _writes_to(pid, output):
    try:
        for _, target in procinfo.fd_targets(pid):
            try:
                if os.path.realpath(target) == output:
                    return True
            except (OSError, ValueError):
                continue
    except (OSError, ValueError, KeyError):
        pass
    return False


def _runs(pid, command, started):
    try:
        args = b' '.join(procinfo.argv(pid)).decode('utf-8', 'replace')
        begun = procinfo.start_ms(procinfo.start_ticks(pid)) / 1000
    except (OSError, ValueError, KeyError, IndexError):
        return False
    return (f'eval {quoted(command)}' in args
            and (started is None or started - START_BEFORE <= begun <= started + START_AFTER))


def find(owner_pid, task, output, started, pids=None):
    """Return the process group leader for one task, or None when it is gone or not exact.

    task: {'kind', 'command'}; output: resolved tasks/<id>.output path or None; started: epoch or None."""
    pids = [pid for pid in (children(owner_pid) if pids is None else pids) if _group_leader(pid)]
    if output is not None:
        exact = [pid for pid in pids if _writes_to(pid, str(output))]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None
    command = task.get('command')
    if task.get('kind') != 'monitor' or not isinstance(command, str) or not command.strip() or started is None:
        return None
    matched = [pid for pid in pids if _runs(pid, command, started)]
    return matched[0] if len(matched) == 1 else None


def _alive(pid, ticks):
    try:
        return procinfo.start_ticks(pid) == ticks and procinfo.stat_fields(pid)[0] not in ('Z', 'X')
    except (OSError, ValueError, KeyError, IndexError):
        return False


def stop(pid, *, sleep=time.sleep, kill=os.killpg):
    """SIGTERM the task's process group, then SIGKILL after 3 s. Returns True when it is gone."""
    ticks = procinfo.start_ticks(pid)
    kill(pid, signal.SIGTERM)
    for attempt in range(31):
        if not _alive(pid, ticks):
            return True
        if attempt == 30:
            break
        sleep(0.1)
    if _alive(pid, ticks):
        kill(pid, signal.SIGKILL)
        for _ in range(10):
            sleep(0.1)
            if not _alive(pid, ticks):
                return True
    return not _alive(pid, ticks)


def _read():
    try:
        data = json.loads(LEDGER.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(key):
    """Remember that Tether stopped this task, so its listing says stopped, not failed."""
    with _lock:
        data = _read()
        data[key] = time.time()
        for old in sorted(data, key=data.get)[:-LEDGER_LIMIT]:
            del data[old]
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        temporary = LEDGER.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        os.replace(temporary, LEDGER)


def stopped():
    """Keys of tasks Tether stopped. Listing polls call this, so it reads the file only after a change."""
    with _lock:
        try:
            stamp = (str(LEDGER), LEDGER.stat().st_mtime_ns)
        except OSError:
            return frozenset()
        if _memo[0] != stamp:
            _memo[:] = [stamp, frozenset(_read())]
        return _memo[1]


def key(tasks_dir, identifier):
    return f'{tasks_dir}/{identifier}'
