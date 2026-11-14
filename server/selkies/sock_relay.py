"""Runs inside the Selkies container (as user abc): relays a Unix socket in the Tether-mounted folder to the
container's own nginx (HTTP and WebSocket bytes, unchanged). Tether reaches the Selkies web client only through it."""
import os
import socket
import sys
import threading

PATH, PORT = sys.argv[1], int(sys.argv[2])


def pump(source, sink):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    for end in (source, sink):
        try:
            end.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def serve(client):
    try:
        upstream = socket.create_connection(('127.0.0.1', PORT), timeout=10)
    except OSError:
        client.close()
        return
    upstream.settimeout(None)
    threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
    pump(client, upstream)


try:
    os.unlink(PATH)
except FileNotFoundError:
    pass
old = os.umask(0o177)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(PATH)
os.umask(old)
server.listen(16)
while True:
    connection, _ = server.accept()
    threading.Thread(target=serve, args=(connection,), daemon=True).start()
