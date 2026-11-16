"""App name and icon colour for this server, so two installed Tether apps (one per server) look different.

  paths.CONFIG/app-identity.json   {"name": "Work", "color": "blue"}. Missing or bad: "Tether" and clay (the default).

The installed app reads its name and icon from /chat/manifest.webmanifest. serve() answers that file and the icon files
with the saved name and colour, so a change reaches a new install at once. Chrome on Android checks the manifest of an
installed app at most once a day, on launch, and applies a change when the app is closed and the phone charges on Wi-Fi.

The icons are recoloured, not redrawn: each pixel of the built PNG is a blend of the clay background and the cream
glyph, so it gets the same blend of the new colour and the cream. This needs only zlib, no image library."""
import json
import os
import re
import struct
import threading
import zlib

import paths

PATH = '/mobile/api/app-identity'
DEFAULT_NAME = 'Tether'
NAME_LIMIT = 30
# Each colour gives the cream glyph (#FBF7F1) at least 3:1 contrast (WCAG 2.2, 1.4.11).
COLORS = (
    ('clay', 'Clay', '#C96442'),
    ('blue', 'Blue', '#2F6BD8'),
    ('green', 'Green', '#2E7D4F'),
    ('teal', 'Teal', '#0E7C86'),
    ('purple', 'Purple', '#7547C9'),
    ('rose', 'Rose', '#C23B6A'),
    ('amber', 'Amber', '#B26A00'),
    ('slate', 'Slate', '#4B5563'),
)
HEX = {key: value for key, _, value in COLORS}
DEFAULT_COLOR = 'clay'
SOURCE = (0xC9, 0x64, 0x42)
GLYPH = (0xFB, 0xF7, 0xF1)
ICON_FILES = ('icon.svg', 'icon-maskable.svg', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png',
              'apple-touch-icon.png')
SERVED = ('manifest.webmanifest', 'index.html') + ICON_FILES
_lock = threading.Lock()
_cache = {}  # (file, mtime_ns, colour) -> bytes


class Bad(ValueError):
    pass


def _file():
    return paths.CONFIG / 'app-identity.json'


def load():
    try:
        saved = json.loads(_file().read_text())
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    try:
        name = clean_name(saved.get('name', DEFAULT_NAME))
    except Bad:
        name = DEFAULT_NAME
    color = saved.get('color') if saved.get('color') in HEX else DEFAULT_COLOR
    return {'name': name, 'color': color}


def clean_name(value):
    if not isinstance(value, str):
        raise Bad('Type a name.')
    name = ' '.join(value.split())
    if not name:
        raise Bad('Type a name.')
    if len(name) > NAME_LIMIT:
        raise Bad('Use %d characters or fewer.' % NAME_LIMIT)
    if any(ord(char) < 32 or 0x7F <= ord(char) < 0xA0 for char in name):
        raise Bad('Use letters, numbers and spaces.')
    return name


def save(body):
    if not isinstance(body, dict):
        raise Bad('Send a JSON object.')
    current = load()
    name = clean_name(body['name']) if 'name' in body else current['name']
    color = body.get('color', current['color'])
    if color not in HEX:
        raise Bad('Choose one of the colours.')
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'name': name, 'color': color}))
    os.replace(temporary, target)
    return state()


def state():
    return dict(load(), default={'name': DEFAULT_NAME, 'color': DEFAULT_COLOR},
                colors=[{'id': key, 'label': label, 'hex': value} for key, label, value in COLORS])


# ---- HTTP ----

def handles(path):
    return path.split('?', 1)[0] == PATH


def get(handler):
    handler.respond(200, state())


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise Bad('Send a small JSON body.')
        body = json.loads(handler.rfile.read(size))
        handler.respond(200, save(body))
    except Bad as exc:
        handler.respond(400, {'error': str(exc)})
    except ValueError:
        handler.respond(400, {'error': 'Send a valid JSON object.'})
    except OSError:
        handler.respond(503, {'error': 'Could not save the app name. Try again.'})


def serve(name, target):
    """The bytes to send for a built UI file that carries the name or colour, or None to send the file as built."""
    if name not in SERVED:
        return None
    identity = load()
    if identity == {'name': DEFAULT_NAME, 'color': DEFAULT_COLOR}:
        return None
    stat = target.stat()
    key = (str(target), stat.st_mtime_ns, identity['name'], identity['color'])
    with _lock:
        if key in _cache:
            return _cache[key]
    data = target.read_bytes()
    color = HEX[identity['color']]
    if name == 'manifest.webmanifest':
        data = manifest(data, identity)
    elif name == 'index.html':
        data = page(data, identity)
    elif name.endswith('.svg'):
        data = re.sub(rb'#C96442', color.encode(), data, flags=re.IGNORECASE)
    else:
        data = recolor_png(data, _rgb(color))
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = data
    return data


def manifest(data, identity):
    built = json.loads(data)
    built['name'] = identity['name']
    built['short_name'] = identity['name']
    # A new icon address per colour, so Chrome and any cache see a new icon, not the old bytes.
    version = identity['color']
    for icon in built.get('icons', []):
        if isinstance(icon, dict) and icon.get('src', '').split('/')[-1] in ICON_FILES:
            icon['src'] = icon['src'].split('?', 1)[0] + '?c=' + version
    return json.dumps(built, ensure_ascii=False, indent=2).encode()


def page(data, identity):
    from html import escape
    text = data.decode()
    title = escape(identity['name'])
    text = text.replace('<title>Tether</title>', '<title>%s</title><meta name="apple-mobile-web-app-title" content="%s" />'
                        % (title, title), 1)
    text = re.sub('#C96442', HEX[identity['color']], text, flags=re.IGNORECASE)
    return text.encode()


def _rgb(value):
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


# ---- PNG ----

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _decode(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    position, raw, header = 8, [], None
    while position < len(data):
        length, kind = struct.unpack('>I4s', data[position:position + 8])
        chunk = data[position + 8:position + 8 + length]
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', chunk)
        elif kind == b'IDAT':
            raw.append(chunk)
        elif kind == b'IEND':
            break
        position += length + 12
    width, height, depth, kind, _, _, interlace = header
    if depth != 8 or kind not in (2, 6) or interlace:
        raise ValueError('unsupported PNG')
    channels = 4 if kind == 6 else 3
    stream = zlib.decompress(b''.join(raw))
    stride = width * channels
    rows, previous, offset = [], bytearray(stride), 0
    for _ in range(height):
        method = stream[offset]
        line = bytearray(stream[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        if method == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif method == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif method == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif method == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                corner = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], corner)) & 0xFF
        rows.append(line)
        previous = line
    return width, height, kind, channels, rows


def _chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF)


def recolor_png(data, color):
    width, height, kind, channels, rows = _decode(data)
    sr, sg, sb = SOURCE
    gr, gg, gb = GLYPH
    dr, dg, db = gr - sr, gg - sg, gb - sb
    span = dr * dr + dg * dg + db * db
    mapped = {}
    out = bytearray()
    for line in rows:
        out.append(0)
        for i in range(0, len(line), channels):
            pixel = (line[i], line[i + 1], line[i + 2])
            new = mapped.get(pixel)
            if new is None:
                r, g, b = pixel
                t = ((r - sr) * dr + (g - sg) * dg + (b - sb) * db) / span
                t = min(1.0, max(0.0, t))
                near = (sr + t * dr - r) ** 2 + (sg + t * dg - g) ** 2 + (sb + t * db - b) ** 2
                # Only blends of clay and cream change; any other pixel keeps its colour.
                new = tuple(round(c + t * (glyph - c)) for c, glyph in zip(color, GLYPH)) if near <= 300 else pixel
                mapped[pixel] = new
            out += bytes(new)
            if channels == 4:
                out.append(line[i + 3])
    header = struct.pack('>IIBBBBB', width, height, 8, kind, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(bytes(out), 9))
            + _chunk(b'IEND', b''))
