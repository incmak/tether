"""Runs inside the Selkies container (as user abc). Starts Chromium with --remote-debugging-pipe and serves that
pipe, unchanged (NUL-separated JSON), on a Unix socket in the folder Tether mounts. No TCP debug port opens.

One client at a time: a new connection replaces the old one (Tether reconnects after its own restart).
The script exits when Chromium exits; the image's watchdog (RESTART_APP=true) then starts it again."""
import fcntl
import os
import socket
import sys
import threading

SOCK = os.environ.get('TETHER_CDP_SOCK', '/sock/cdp.sock')


def high_pipe():
    # Both ends above fd 9: dup2(3, 3) would keep close-on-exec, and Chromium then reports
    # "Remote debugging pipe file descriptors are not open".
    read, write = os.pipe()
    high = fcntl.fcntl(read, fcntl.F_DUPFD_CLOEXEC, 10), fcntl.fcntl(write, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(read)
    os.close(write)
    return high


def main(argv):
    to_chrome_r, to_chrome_w = high_pipe()
    from_chrome_r, from_chrome_w = high_pipe()
    devnull = os.open(os.devnull, os.O_RDWR)
    pid = os.posix_spawn(argv[0], argv + ['--remote-debugging-pipe'], os.environ, file_actions=[
        (os.POSIX_SPAWN_DUP2, devnull, 0), (os.POSIX_SPAWN_DUP2, devnull, 1), (os.POSIX_SPAWN_DUP2, devnull, 2),
        (os.POSIX_SPAWN_DUP2, to_chrome_r, 3), (os.POSIX_SPAWN_DUP2, from_chrome_w, 4)])
    os.close(to_chrome_r)
    os.close(from_chrome_w)
    lock = threading.Lock()
    current = [None]

    def from_chrome():
        while True:
            data = os.read(from_chrome_r, 65536)
            if not data:
                os._exit(0)
            with lock:
                client = current[0]
            if client is not None:
                try:
                    client.sendall(data)
                except OSError:
                    pass

    def to_chrome(client):
        while True:
            try:
                data = client.recv(65536)
            except OSError:
                data = b''
            if not data:
                return
            with lock:
                if current[0] is not client:
                    return
            os.write(to_chrome_w, data)

    threading.Thread(target=from_chrome, daemon=True).start()
    threading.Thread(target=lambda: (os.waitpid(pid, 0), os._exit(0)), daemon=True).start()
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK)
    os.umask(old)
    server.listen(1)
    while True:
        client, _ = server.accept()
        with lock:
            if current[0] is not None:
                try:
                    current[0].close()
                except OSError:
                    pass
            current[0] = client
        threading.Thread(target=to_chrome, args=(client,), daemon=True).start()


if __name__ == '__main__':
    main(sys.argv[1:])
