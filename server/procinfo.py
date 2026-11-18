"""Platform process information and the raw readers used by injectable process scans."""
import ctypes
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

ROOT = '/proc'
DARWIN = sys.platform == 'darwin'
_DARWIN_LOCK = threading.Lock()
_DARWIN_SNAPSHOT = None
_DARWIN_SNAPSHOT_AT = 0


def _parse_ps(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 6:
            continue
        pid, ppid, pgid, tpgid = map(int, fields[:4])
        result[pid] = {'ppid': ppid, 'pgid': pgid, 'tpgid': tpgid,
                       'stat': fields[4], 'uid': int(fields[5])}
    return result


def _darwin_ps():
    return subprocess.run(['ps', '-ax', '-o', 'pid=,ppid=,pgid=,tpgid=,stat=,uid='],
                          check=True, capture_output=True, text=True, timeout=1).stdout


def _darwin_snapshot(pid=None, fresh=False):
    global _DARWIN_SNAPSHOT, _DARWIN_SNAPSHOT_AT
    with _DARWIN_LOCK:
        refresh = fresh or _DARWIN_SNAPSHOT is None or time.monotonic() - _DARWIN_SNAPSHOT_AT >= 1
        if refresh or (pid is not None and pid not in _DARWIN_SNAPSHOT):
            _DARWIN_SNAPSHOT = _parse_ps(_darwin_ps())
            _DARWIN_SNAPSHOT_AT = time.monotonic()
        if pid is not None and pid not in _DARWIN_SNAPSHOT:
            raise ProcessLookupError(errno.ESRCH, 'No such process', pid)
        return _DARWIN_SNAPSHOT


def _darwin_sysctl(mib):
    libc = ctypes.CDLL(None, use_errno=True)
    sysctl = libc.sysctl
    sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int
    names = (ctypes.c_int * len(mib))(*mib)
    size = ctypes.c_size_t()
    if sysctl(names, len(mib), None, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    data = ctypes.create_string_buffer(size.value)
    if sysctl(names, len(mib), data, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return data.raw[:size.value]


def _parse_procargs(data):
    if len(data) < 4:
        raise OSError(errno.EINVAL, 'Invalid process arguments')
    argc = struct.unpack_from('i', data)[0]
    end = data.find(b'\0', 4)
    if argc < 0 or end < 0:
        raise OSError(errno.EINVAL, 'Invalid process arguments')
    parts = [part for part in data[end + 1:].split(b'\0') if part]
    if len(parts) < argc:
        raise OSError(errno.EINVAL, 'Incomplete process arguments')
    return data[4:end], parts[:argc], parts[argc:]


def _darwin_procargs(pid):
    return _parse_procargs(_darwin_sysctl([1, 49, pid]))


def _parse_timeval(data):
    if not data:
        raise ProcessLookupError(errno.ESRCH, 'No such process')
    if len(data) < struct.calcsize('qi'):
        raise OSError(errno.EINVAL, 'Invalid process time')
    return struct.unpack_from('qi', data)


def _darwin_ucomm(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'ucomm='],
                            capture_output=True, text=True, timeout=1)
    name = result.stdout.strip()
    if not name:
        raise ProcessLookupError(errno.ESRCH, 'No process name', pid)
    return name


def _darwin_name(pid, exe, args):
    for value in [*args[:1], exe]:
        name = os.path.basename(os.fsdecode(value))
        if name:
            return name
    return os.path.basename(_darwin_ucomm(pid))


def _parse_lsof(text):
    files, current, found = {}, None, False
    for line in text.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            found = True
        elif line.startswith('f'):
            current = line[1:]
        elif line.startswith('n') and current is not None:
            files[current] = line[1:]
    if not found:
        raise ProcessLookupError(errno.ESRCH, 'No process files')
    return files


def _darwin_lsof(pid):
    try:
        result = subprocess.run(['lsof', '-nP', '-a', '-p', str(pid), '-F', 'ftn'],
                                capture_output=True, text=True, timeout=1)
    except subprocess.TimeoutExpired as error:  # a busy process: callers treat an OSError as "files unknown"
        raise TimeoutError(errno.ETIMEDOUT, 'lsof timed out') from error
    return _parse_lsof(result.stdout)


def _parse_socket_owner(text, local_port, remote_port):
    names = {'127.0.0.1:%d->127.0.0.1:%d' % (local_port, remote_port),
             '[::1]:%d->[::1]:%d' % (local_port, remote_port)}
    pid = None
    for line in text.splitlines():
        if line.startswith('p'):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith('n') and line[1:] in names:
            return pid
    return None


def read_text(path):
    return Path(path).read_text()


def read_bytes(path):
    return Path(path).read_bytes()


def list_dir(path):
    return os.listdir(path)


def read_link(path):
    return os.readlink(path)


def uid_of(path):
    return os.stat(path).st_uid


def pids(proc=Path('/proc')):
    if DARWIN:
        return list(_darwin_snapshot())
    return [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]


def stat_fields(pid, proc=Path('/proc')):
    if DARWIN:
        row = _darwin_snapshot(pid)[pid]
        # Linux-shaped fields: state, ppid, pgid, tpgid and start at 0, 1, 2, 5 and 19.
        fields = ['0'] * 20
        fields[0], fields[1], fields[2] = row['stat'][0], str(row['ppid']), str(row['pgid'])
        fields[5], fields[19] = str(row['tpgid']), start_ticks(pid)
        return fields
    return (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()


def stat_exists(pid, proc=Path('/proc')):
    if DARWIN:
        try:
            return pid in _darwin_snapshot(pid)
        except ProcessLookupError:
            return False
    return (proc / str(pid) / 'stat').is_file()


def parent(pid, proc=Path('/proc')):
    if DARWIN:
        return _darwin_snapshot(pid)[pid]['ppid']
    return int(stat_fields(pid, proc)[1])


def start_ticks(pid, proc=Path('/proc')):
    if DARWIN:
        sec, usec = _parse_timeval(_darwin_sysctl([1, 14, 1, pid]))
        return '%d.%06d' % (sec, usec)
    return stat_fields(pid, proc)[19]


def uptime(proc=Path('/proc')):
    if DARWIN:
        sec, usec = _parse_timeval(_darwin_sysctl([1, 21]))
        return time.time() - (sec + usec / 1_000_000)
    return float((proc / 'uptime').read_text().split()[0])


def start_ms(ticks, proc=Path('/proc'), now=None):
    if DARWIN:
        return int(float(ticks) * 1000)
    now = time.time() if now is None else now
    # lxcfs can replace /proc/uptime, but start ticks use the kernel boot clock.
    elapsed = time.clock_gettime(time.CLOCK_BOOTTIME) if proc == Path('/proc') else uptime(proc)
    boot = now - elapsed
    return int((boot + int(ticks) / os.sysconf('SC_CLK_TCK')) * 1000)


def comm(pid, proc=Path('/proc')):
    if DARWIN:
        try:
            exe, args, _ = _darwin_procargs(pid)
        except OSError:
            exe, args = b'', []
        return _darwin_name(pid, exe, args)
    return (proc / str(pid) / 'comm').read_text().strip()


def argv(pid, proc=Path('/proc')):
    if DARWIN:
        return _darwin_procargs(pid)[1]
    return (proc / str(pid) / 'cmdline').read_bytes().split(b'\0')


def environ(pid, proc=Path('/proc')):
    if DARWIN:
        return _darwin_procargs(pid)[2] or None
    return (proc / str(pid) / 'environ').read_bytes().split(b'\0')


def uid(pid, proc=Path('/proc')):
    if DARWIN:
        return _darwin_snapshot(pid)[pid]['uid']
    return uid_of(proc / str(pid))


def fd_names(pid, proc=Path('/proc')):
    if DARWIN:
        return [name for name in _darwin_lsof(pid) if name.isdigit()]
    return [entry.name for entry in (proc / str(pid) / 'fd').iterdir()]


def fd_targets(pid, proc=Path('/proc')):
    if DARWIN:
        return [(name, target) for name, target in _darwin_lsof(pid).items() if name.isdigit()]
    targets = []
    for name in fd_names(pid, proc):
        try:
            targets.append((name, fd_target(pid, name, proc)))
        except OSError:
            continue
    return targets


def resolved_fds(pid, proc=Path('/proc')):
    if DARWIN:
        for name, target in _darwin_lsof(pid).items():
            if name.isdigit():
                yield Path(target).resolve()
        return
    # Resolve the fd path itself: relative, broken and disappearing links keep
    # Path.resolve's semantics. Yield so callers can retain partial matches.
    for entry in (proc / str(pid) / 'fd').iterdir():
        yield entry.resolve()


def fd_target(pid, fd, proc=Path('/proc'), *, readlink=None):
    if DARWIN:
        files = _darwin_lsof(pid)
        if not str(fd).isdigit() or str(fd) not in files:
            raise OSError(errno.ENOENT, 'No such file descriptor', fd)
        return files[str(fd)]
    return (os.readlink if readlink is None else readlink)(proc / str(pid) / 'fd' / str(fd))


def cwd(pid, proc=Path('/proc'), *, readlink=None):
    if DARWIN:
        files = _darwin_lsof(pid)
        if 'cwd' not in files:
            raise OSError(errno.ENOENT, 'No process working directory', pid)
        return files['cwd']
    return (os.readlink if readlink is None else readlink)(proc / str(pid) / 'cwd')


def tcp_socket_inode(local_port, remote_port, uid, proc=Path('/proc')):
    for table in ('tcp', 'tcp6'):
        try:
            rows = (proc / 'net' / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[3] == '01':
                local = int(fields[1].rsplit(':', 1)[1], 16)
                remote = int(fields[2].rsplit(':', 1)[1], 16)
                if local == local_port and remote == remote_port and int(fields[7]) == uid:
                    return fields[9]
    return None


def pid_with_socket(inode, uid, proc=Path('/proc')):
    for pid in pids(proc):
        try:
            if uid_of(proc / str(pid)) != uid:
                continue
            for _, target in fd_targets(pid, proc):
                if target == 'socket:[%s]' % inode:
                    return pid
        except OSError:
            continue
    return None


def foreground_differs(pid, proc=Path('/proc')):
    if DARWIN:
        row = _darwin_snapshot(pid)[pid]
        return row['pgid'] != row['tpgid']
    fields = stat_fields(pid, proc)
    return fields[2] != fields[5]


def socket_owner(local_port, remote_port, uid):
    if DARWIN:
        result = subprocess.run(['lsof', '-nP', '-a', '-u', str(uid), '-iTCP:%d' % local_port,
                                 '-sTCP:ESTABLISHED', '-F', 'pn'],
                                capture_output=True, text=True, timeout=1)
        return _parse_socket_owner(result.stdout, local_port, remote_port)
    inode = tcp_socket_inode(local_port, remote_port, uid)
    return pid_with_socket(inode, uid) if inode is not None else None


def table(run=subprocess.run, fresh=False):
    """All processes as {pid: (ppid, name)}. On macOS the ps snapshot is shared for a second; fresh=True reads it
    again (Linux always reads ps)."""
    if DARWIN:
        result = {}
        for pid, row in _darwin_snapshot(fresh=fresh).items():
            try:
                args = argv(pid)
                name = os.path.basename(os.fsdecode(args[0])) if args else ''
                result[pid] = (row['ppid'], name or comm(pid))
            except OSError:
                continue
        return result
    command = ['ps', '-eo', 'pid=,ppid=,comm=']
    ps = run(command, capture_output=True, text=True, timeout=5)
    if ps.returncode:
        raise subprocess.CalledProcessError(ps.returncode, command, output=ps.stdout)
    processes = {}
    for line in ps.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3:
            processes[int(fields[0])] = (int(fields[1]), fields[2])
    return processes
