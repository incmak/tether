"""Preview an HTML page an agent made (a Claude artifact's local file, a prototype) inside Tether.

POST /mobile/api/preview {session, path} checks the file with the rules of media_files.resolve and returns a link
/mobile/preview/<token>/<name>. The token opens the page's folder for a set time, so the page's own scripts, styles and
images load by their relative paths. Each file is checked again with the same rules: home folder only, no private
folders, no files that may hold secrets. One more folder is allowed: Claude Code's own scratchpad folder
(/tmp/claude-<uid>), where Claude writes most of the pages it publishes as artifacts.

The token is the only key: a sandboxed frame has an opaque origin, so the browser does not send the SameSite=Strict
Tether cookie with the page's requests. Every response carries `Content-Security-Policy: sandbox`, so the page runs in
an opaque origin even when it is opened in its own browser tab: it cannot read Tether's cookies, storage or API."""
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

import media_files

PREFIX = '/mobile/preview/'
LIFETIME = 12 * 3600
MAX_TOKENS = 200
MAX_BYTES = 50 * 1024 * 1024
PAGE_SUFFIXES = {'.html', '.htm'}
TYPES = {
    '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.cjs': 'text/javascript; charset=utf-8',
    '.json': 'application/json', '.map': 'application/json', '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
    '.csv': 'text/csv; charset=utf-8', '.xml': 'application/xml', '.svg': 'image/svg+xml', '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
    '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf',
    '.wasm': 'application/wasm', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
    '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.pdf': 'application/pdf',
}
# The page may run scripts, forms, pop-ups and downloads, but never in Tether's origin.
SANDBOX = 'sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads'
HEADERS = {'Content-Security-Policy': SANDBOX, 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
           'Cache-Control': 'no-store'}


def scratch_root():
    """Claude Code's temporary folder for this user, when this user owns it."""
    if not hasattr(os, 'getuid'):
        return None
    root = Path('/tmp/claude-%d' % os.getuid())
    try:
        root = root.resolve(strict=True)
        return root if root.stat().st_uid == os.getuid() else None
    except OSError:
        return None


class Previews:
    def __init__(self, clock=time.time, home=None, scratch=scratch_root):
        self.clock = clock
        self.home = home
        self.scratch = scratch
        self.lock = threading.Lock()
        self.tokens = {}  # token -> (folder, expires)

    def resolve(self, name, folder):
        """media_files.resolve, with Claude's scratchpad folder as a second home."""
        root = self.scratch()
        if root:
            try:
                path = Path(os.path.expanduser(name))
                path = (path if path.is_absolute() else Path(folder or os.path.expanduser('~')) / path).resolve(strict=True)
                if root in path.parents:
                    return media_files.resolve(str(path), folder, root, text_limit=None)
            except (OSError, RuntimeError):
                pass
        return media_files.resolve(name, folder, self.home, text_limit=None)

    def issue(self, name, folder):
        """A preview link for the HTML file `name` (relative to the tab's folder). ValueError with a message for the phone."""
        path, _ = self.resolve(name, folder)
        if path.suffix.lower() not in PAGE_SUFFIXES:
            raise ValueError('Tether previews HTML pages only.')
        now = self.clock()
        with self.lock:
            self.tokens = {key: value for key, value in self.tokens.items() if value[1] > now}
            # The same folder keeps its token, so the drawer and "Open in browser" share one link.
            token = next((key for key, value in self.tokens.items() if value[0] == path.parent), None)
            if token is None:
                if len(self.tokens) >= MAX_TOKENS:
                    oldest = min(self.tokens, key=lambda key: self.tokens[key][1])
                    del self.tokens[oldest]
                token = secrets.token_urlsafe(24)
            self.tokens[token] = (path.parent, now + LIFETIME)
        return {'url': PREFIX + token + '/' + quote(path.name), 'name': path.name, 'path': str(path)}

    def find(self, url_path):
        """(file, content type) for a GET under PREFIX. LookupError: no such link. ValueError: a refused file."""
        rest = url_path[len(PREFIX):]
        token, _, relative = rest.partition('/')
        with self.lock:
            entry = self.tokens.get(token)
        if not entry or entry[1] <= self.clock():
            raise LookupError('This preview link has expired. Open the preview again from Tether.')
        folder = entry[0]
        relative = unquote(relative)
        if not relative or relative.endswith('/'):
            relative += 'index.html'
        if '\0' in relative or relative.startswith('/') or any(part == '..' for part in relative.split('/')):
            raise ValueError('That file is outside the preview folder.')
        path, _ = self.resolve(str(folder / relative), str(folder))
        if path != folder and folder not in path.parents:
            raise ValueError('That file is outside the preview folder.')
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('That file is too large to preview.')
        return path, TYPES.get(path.suffix.lower(), 'application/octet-stream')


PREVIEWS = Previews()


def serve(handler, previews=None):
    """Answer GET /mobile/preview/<token>/<path>. Needs no cookie: the token is the key."""
    previews = previews or PREVIEWS
    path = handler.path.split('?', 1)[0].split('#', 1)[0]
    try:
        found, kind = previews.find(path)
        data = found.read_bytes()
        status = 200
    except LookupError as exc:
        status, data, kind = 410, page(str(exc)), 'text/html; charset=utf-8'
    except (ValueError, OSError) as exc:
        status, data, kind = 404, str(exc).encode(), 'text/plain; charset=utf-8'
    handler.send_response(status)
    handler.send_header('Content-Type', kind)
    handler.send_header('Content-Length', str(len(data)))
    for name, value in HEADERS.items():
        handler.send_header(name, value)
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(data)


def page(message):
    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Preview expired</title><body style="font:16px system-ui;margin:2rem;color:#333">'
            '<p>%s</p></body>' % message.replace('&', '&amp;').replace('<', '&lt;')).encode()

