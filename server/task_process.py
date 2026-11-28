"""Find and stop a Claude background shell or monitor through its process, not the terminal screen.

Claude Code starts each background shell and monitor as a direct child process in its own process
group. A shell writes to tasks/<id>.output, so its open file names it exactly. A monitor writes to a
socket, so it is matched by its command and start time. This does not depend on how a Claude Code
version draws /tasks.
"""
from collections import namedtuple
import json
import math
import os
import signal
import subprocess
import threading
import time

import procinfo
from paths import CACHE

LEDGER = CACHE / 'task-stops.json'
LEDGER_LIMIT = 500
# The start record is written after the process starts; allow for a slow transcript write.
START_BEFORE, START_AFTER = 15, 3
_lock = threading.Lock()
_memo = [None, {}]  # (path, mtime_ns), stop records
# A pid alone can name a new process after the task exits; start ticks pin the exact process.
Process = namedtuple('Process', 'pid ticks')


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


def _child_of(pid, owner_pid):
    try:
        return procinfo.parent(pid) == owner_pid
    except (OSError, ValueError, KeyError, IndexError):
        return False


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
    """Return the task's process group leader as Process(pid, ticks), or None when it is gone or not exact.

    task: {'kind', 'command'}; output: resolved tasks/<id>.output path or None; started: epoch or None."""
    # Ticks are read first and compared after every check, the parent check included. Equal ticks
    # mean one process passed all the checks; a reused pid would show new ticks.
    ticks = {}
    for pid in (children(owner_pid) if pids is None else pids):
        try:
            ticks[pid] = procinfo.start_ticks(pid)
        except (OSError, ValueError, KeyError, IndexError):
            continue
    pids = [pid for pid in ticks if _child_of(pid, owner_pid) and _group_leader(pid)]
    chosen = None
    if output is not None:
        exact = [pid for pid in pids if _writes_to(pid, str(output))]
        if len(exact) > 1:
            return None
        chosen = exact[0] if exact else None
    if chosen is None:
        command = task.get('command')
        if task.get('kind') != 'monitor' or not isinstance(command, str) or not command.strip() or started is None:
            return None
        matched = [pid for pid in pids if _runs(pid, command, started)]
        if len(matched) != 1:
            return None
        chosen = matched[0]
    process = Process(chosen, ticks[chosen])
    return process if same(process) else None


def running(owner_pid, task, started, pids=None):
    """True when at least one live process runs this monitor's command near its start time.

    For the listing only, never for a signal. Same-command twins all count as running: the
    listing reads a finished monitor's state from Claude's own end notification first, so only
    monitors without one get here. Showing a live twin as finished would hide its Stop button."""
    command = task.get('command')
    if not isinstance(command, str) or not command.strip() or started is None:
        return False
    candidates = children(owner_pid) if pids is None else pids
    return any(_child_of(pid, owner_pid) and _group_leader(pid) and _runs(pid, command, started) for pid in candidates)


def _alive(pid, ticks):
    try:
        fields = procinfo.stat_fields(pid)
        return fields[19] == ticks and fields[0] not in ('Z', 'X')
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return None


def exited(process):
    """Confirm exit of an earlier binding, even if a live process can close its output or change group."""
    return _alive(process.pid, process.ticks) is False


def same(process):
    """True while process is still the same live group leader that find named."""
    try:
        return _alive(process.pid, process.ticks) is True and os.getpgid(process.pid) == process.pid
    except (OSError, ValueError, KeyError):
        return False


def stop(process, *, sleep=time.sleep, kill=os.killpg):
    """SIGTERM the task's process group, then SIGKILL after 3 s. Returns True when it is gone.

    process comes from find. Each signal goes only while the pid still has the start ticks find
    read, so a pid that a new process reused is never signalled."""
    if not same(process):
        return exited(process)
    kill(process.pid, signal.SIGTERM)
    for attempt in range(31):
        if exited(process):
            return True
        if attempt == 30:
            break
        sleep(0.1)
    if same(process):
        kill(process.pid, signal.SIGKILL)
        for _ in range(10):
            sleep(0.1)
            if exited(process):
                return True
    return exited(process)


def _stop_time(record):
    return record.get('ended') if isinstance(record, dict) else record


def _read():
    try:
        data = json.loads(LEDGER.read_text())
        if isinstance(data, dict):
            return {key: record for key, record in data.items()
                    if type(stamp := _stop_time(record)) in (int, float) and math.isfinite(stamp)}
    except (OSError, ValueError, OverflowError):
        pass
    return {}


def record(key, *, confirmed=False):
    """Record a bound exit. Older numeric entries only recorded a signal attempt."""
    with _lock:
        data = _read()
        now = time.time()
        data[key] = {'ended': now} if confirmed else now
        for old in sorted(data, key=lambda key: _stop_time(data[key]))[:-LEDGER_LIMIT]:
            del data[old]
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        temporary = LEDGER.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        os.replace(temporary, LEDGER)


def stopped():
    """Read the stop records once per ledger change for the listing poll."""
    with _lock:
        try:
            stamp = (str(LEDGER), LEDGER.stat().st_mtime_ns)
        except OSError:
            return {}
        if _memo[0] != stamp:
            _memo[:] = [stamp, _read()]
        return _memo[1]


def key(tasks_dir, identifier):
    return f'{tasks_dir}/{identifier}'
