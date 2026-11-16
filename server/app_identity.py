"""App name and icon for this server, so two installed Tether apps (one per server) look different.

  paths.CONFIG/app-identity.json   {"name": "Work", "color": "blue", "icon": "color"|"custom"}.
                                   Missing or bad: "Tether", clay, colour icon (the default).
  paths.CONFIG/app-icon/art.png    the custom icon art: 512x512, opaque, without the badge.
  paths.CONFIG/app-icon/draft.png  a made or uploaded art that waits for "Use this icon".

The installed app reads its name and icon from /chat/manifest.webmanifest. serve() answers that file and the icon files
with the saved name and icon, so a change reaches a new install at once. Chrome on Android checks the manifest of an
installed app at most once a day, on launch, and applies a change when the app is closed and the phone charges on Wi-Fi.

A colour icon is recoloured, not redrawn: each pixel of the built PNG is a blend of the clay background and the cream
glyph, so it gets the same blend of the new colour and the cream. A custom icon is the art with the Tether mark as a
badge in the lower-right corner, always, so the icon still says Tether. The badge stays inside the circle that Android
keeps of a maskable icon (radius 40% of the side). Everything needs only zlib; ffmpeg, when present, reads JPEG and WebP
uploads."""
import base64
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib

import backgrounds
import paths

PATH = '/mobile/api/app-identity'
DEFAULT_NAME = 'Tether'
NAME_LIMIT = 30
PROMPT_LIMIT = 300
UPLOAD_LIMIT = 12 * 1024 * 1024
TIMEOUT = 300
SIDE = 512
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
DEFAULT = {'name': DEFAULT_NAME, 'color': DEFAULT_COLOR, 'icon': 'color'}
SOURCE = (0xC9, 0x64, 0x42)
GLYPH = (0xFB, 0xF7, 0xF1)
PNG_SIZES = {'icon-192.png': 192, 'icon-512.png': 512, 'icon-maskable-512.png': 512, 'apple-touch-icon.png': 180}
ICON_FILES = ('icon.svg', 'icon-maskable.svg') + tuple(PNG_SIZES)
SERVED = ('manifest.webmanifest', 'index.html') + ICON_FILES
# The badge: the Tether mark (0.22 of the side) on a cream ring, lower right. Its far corner is 0.39 of the side from
# the centre, inside Android's 0.40 safe circle.
BADGE, RING, BADGE_END = 112, 6, 404
_lock = threading.Lock()
_store = threading.RLock()  # one change at a time to app-identity.json and the icon files
_cache = {}  # key -> bytes
_jobs = {}  # job id -> {'state': 'running'|'done'|'failed', 'error'?}


class Bad(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _file():
    return paths.CONFIG / 'app-identity.json'


def _folder():
    return paths.CONFIG / 'app-icon'


def _art():
    return _folder() / 'art.png'


def _draft():
    return _folder() / 'draft.png'


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
    icon = 'custom' if saved.get('icon') == 'custom' and _art().is_file() else 'color'
    return {'name': name, 'color': color, 'icon': icon}


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


def _write(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix='.' + target.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save(body):
    if not isinstance(body, dict):
        raise Bad('Send a JSON object.')
    with _store:
        return _save(body)


def _save(body):
    current = load()
    name = clean_name(body['name']) if 'name' in body else current['name']
    color = body.get('color', current['color'])
    if color not in HEX:
        raise Bad('Choose one of the colours.')
    icon = body.get('icon', current['icon'])
    if icon not in ('color', 'custom'):
        raise Bad('Choose a colour icon or your own icon.')
    if body.get('useDraft') is True or body.get('discardDraft') is True:
        # The draft the page showed: another page or the AI job may have replaced it since.
        if not _draft().is_file():
            raise Bad('Make or upload an icon first.', 409)
        if body.get('draft') != _stamp(_draft()):
            raise Bad('The picture changed on another device. Check the new one first.', 409)
        if body.get('useDraft') is True:
            os.replace(_draft(), _art())
            icon = 'custom'
        else:
            _draft().unlink()
    if icon == 'custom' and not _art().is_file():
        raise Bad('Make or upload an icon first.')
    _write(_file(), json.dumps({'name': name, 'color': color, 'icon': icon}).encode())
    return state()


def _stamp(target):
    try:
        return str(target.stat().st_mtime_ns)
    except OSError:
        return None


def state():
    with _lock:
        running = next((ident for ident, job in _jobs.items() if job['state'] == 'running'), None)
    return dict(load(), default=DEFAULT, art=_stamp(_art()), draft=_stamp(_draft()), job=running,
                colors=[{'id': key, 'label': label, 'hex': value} for key, label, value in COLORS])


# ---- AI art and uploads ----

def _icon_prompt(prompt):
    template = (Path(__file__).parent / 'prompts' / 'app_icon.md').read_text()
    return (template.replace('{{prompt}}', prompt) + '\n\nGenerate one image and save it as bg.png in the working folder. '
            'If the image tool saves elsewhere, copy the image to bg.png. Report the saved image path.')


def _run_job(ident, prompt):
    try:
        data = backgrounds.codex_image(_icon_prompt(prompt), time.monotonic() + TIMEOUT, 'tether-app-icon-')
        image = encode_png(SIDE, SIDE, square_art(data), 6)
        with _store:
            _write(_draft(), image)
        result = {'state': 'done'}
    except subprocess.TimeoutExpired:
        result = {'state': 'failed', 'error': 'The icon took too long. Please try again.'}
    except (Bad, backgrounds.Bad) as error:
        result = {'state': 'failed', 'error': str(error)}
    except (OSError, ValueError, zlib.error):
        result = {'state': 'failed', 'error': 'Could not make or save that icon. Please try again.'}
    with _lock:
        _jobs[ident] = result


def generate(body):
    prompt = body.get('prompt') if isinstance(body, dict) else None
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_LIMIT:
        raise Bad('Describe the icon in 1 to %d characters.' % PROMPT_LIMIT)
    with _lock:
        if any(job['state'] == 'running' for job in _jobs.values()):
            raise Bad('An icon is already being made. Please wait.', 409)
        while len(_jobs) >= 20:
            del _jobs[next(iter(_jobs))]
        ident = secrets.token_hex(16)
        _jobs[ident] = {'state': 'running'}
    try:
        threading.Thread(target=_run_job, args=(ident, prompt.strip()), daemon=True, name='app-icon-' + ident).start()
    except RuntimeError:
        with _lock:
            _jobs[ident] = {'state': 'failed', 'error': 'Could not start the icon job. Please try again.'}
    return {'job': ident}


def upload(data, kind):
    if kind not in ('image/png', 'image/jpeg', 'image/webp'):
        raise Bad('Choose a PNG, JPEG or WebP image.', 415)
    image = encode_png(SIDE, SIDE, square_art(data), 6)
    with _store:
        _write(_draft(), image)
        return state()


# ---- HTTP ----

def handles(path):
    path = path.split('?', 1)[0]
    return path == PATH or path.startswith(PATH + '/')


def get(handler):
    try:
        url = handler.path.split('?', 1)
        path, query = url[0], url[1] if len(url) > 1 else ''
        if path == PATH:
            handler.respond(200, state())
        elif path == PATH + '/generate':
            ident = dict(part.partition('=')[::2] for part in query.split('&') if part).get('job', '')
            with _lock:
                job = _jobs.get(ident)
            if job is None:
                raise Bad('No such icon job. Please start again.', 404)
            handler.respond(200, job)
        elif path in (PATH + '/preview/draft.png', PATH + '/preview/art.png'):
            source = _draft() if path.endswith('draft.png') else _art()
            if not source.is_file():
                raise Bad('No icon to show.', 404)
            handler.respond(200, _custom_png(source, 192, _badge_source()), 'image/png', 'no-store')
        else:
            raise Bad('Not found.', 404)
    except Bad as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (OSError, ValueError, zlib.error):
        handler.respond(503, {'error': 'Could not read the icon.'})


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        is_upload = handler.path == PATH + '/upload'
        if not 0 < size <= (UPLOAD_LIMIT if is_upload else 4096):
            handler.close_connection = True
            raise Bad('That image is empty or larger than 12 MB.' if is_upload else 'Send a small JSON body.', 413)
        data = handler.rfile.read(size)
        if len(data) != size:
            raise Bad('The upload did not finish. Please try again.')
        if is_upload:
            handler.respond(200, upload(data, handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()))
            return
        body = json.loads(data)
        if handler.path == PATH:
            handler.respond(200, save(body))
        elif handler.path == PATH + '/generate':
            handler.respond(202, generate(body))
        else:
            raise Bad('Not found.', 404)
    except Bad as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, zlib.error):
        handler.respond(400, {'error': 'Send a valid JSON object or image.'})
    except OSError:
        handler.respond(503, {'error': 'Could not save the app icon. Try again.'})


# ---- serving the built files ----

def _badge_source():
    """The built Tether mark: ui/dist in production, ui/public as a fallback."""
    for folder in (Path(os.environ.get('TMUX_CHAT_DIST', '')) if os.environ.get('TMUX_CHAT_DIST') else None,
                   Path(__file__).resolve().parent.parent / 'ui' / 'dist',
                   Path(__file__).resolve().parent.parent / 'ui' / 'public'):
        if folder and (folder / 'icon-512.png').is_file():
            return folder / 'icon-512.png'
    raise OSError('The Tether icon is missing.')


def serve(name, target):
    """The bytes to send for a built UI file that carries the name or icon, or None to send the file as built."""
    if name not in SERVED:
        return None
    identity = load()
    if identity == DEFAULT:
        return None
    custom = identity['icon'] == 'custom'
    key = (str(target), target.stat().st_mtime_ns, identity['name'], identity['color'], custom and _stamp(_art()))
    with _lock:
        if key in _cache:
            return _cache[key]
    data = target.read_bytes()
    color = HEX[identity['color']]
    if name == 'manifest.webmanifest':
        data = manifest(data, identity)
    elif name == 'index.html':
        data = page(data, identity)
    elif custom and name.endswith('.svg'):
        # The page's favicon: the custom icon inside an SVG, so index.html keeps its link.
        inner = base64.b64encode(_custom_png(_art(), 192, _badge_source())).decode()
        data = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><image width="192" height="192" '
                'href="data:image/png;base64,%s"/></svg>' % inner).encode()
    elif name.endswith('.svg'):
        data = re.sub(rb'#C96442', color.encode(), data, flags=re.IGNORECASE)
    elif custom:
        data = _custom_png(_art(), PNG_SIZES[name], target.parent / 'icon-512.png')
    else:
        data = recolor_png(data, _rgb(color))
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = data
    return data


def _version(identity):
    if identity['icon'] == 'custom':
        return 'a' + format(zlib.crc32((_stamp(_art()) or '').encode()), 'x')
    return identity['color']


def manifest(data, identity):
    built = json.loads(data)
    built['name'] = identity['name']
    built['short_name'] = identity['name']
    # A new icon address per icon, so Chrome and any cache see a new icon, not the old bytes.
    version = _version(identity)
    for icon in built.get('icons', []):
        if isinstance(icon, dict) and icon.get('src', '').split('?', 1)[0].split('/')[-1] in ICON_FILES:
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


CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _decode(data):
    """8-bit, non-interlaced PNG -> (width, height, colour type, channels, rows, palette, transparency)."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    position, raw, header, palette, alpha = 8, [], None, b'', b''
    while position < len(data):
        length, kind = struct.unpack('>I4s', data[position:position + 8])
        chunk = data[position + 8:position + 8 + length]
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', chunk)
        elif kind == b'PLTE':
            palette = chunk
        elif kind == b'tRNS':
            alpha = chunk
        elif kind == b'IDAT':
            raw.append(chunk)
        elif kind == b'IEND':
            break
        position += length + 12
    if header is None:
        raise ValueError('not a PNG')
    width, height, depth, kind, _, _, interlace = header
    if depth != 8 or kind not in CHANNELS or interlace or not 0 < width <= 4096 or not 0 < height <= 4096:
        raise ValueError('unsupported PNG')
    channels = CHANNELS[kind]
    stride = width * channels
    # Read no more than the image needs, so a small file cannot expand to gigabytes.
    expected = (stride + 1) * height
    inflater = zlib.decompressobj()
    stream = inflater.decompress(b''.join(raw), expected + 1)
    if len(stream) != expected:
        raise ValueError('PNG data does not match its size')
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
    return width, height, kind, channels, rows, palette, alpha


def decode_rgba(data):
    """Any 8-bit non-interlaced PNG -> (width, height, RGBA bytearray)."""
    width, height, kind, channels, rows, palette, alpha = _decode(data)
    out = bytearray(width * height * 4)
    at = 0
    # tRNS for grey and RGB names one colour, as 16-bit samples, that is fully transparent.
    key = tuple(alpha[index + 1] for index in range(0, len(alpha), 2)) if kind in (0, 2) and alpha else None
    for line in rows:
        if kind == 6:
            out[at:at + width * 4] = line
            at += width * 4
            continue
        for i in range(0, len(line), channels):
            if kind == 2:
                rgb = (line[i], line[i + 1], line[i + 2])
                pixel = (*rgb, 0 if rgb == key else 255)
            elif kind == 0:
                pixel = (line[i], line[i], line[i], 0 if (line[i],) == key else 255)
            elif kind == 4:
                pixel = (line[i], line[i], line[i], line[i + 1])
            else:
                index = line[i]
                pixel = (*palette[index * 3:index * 3 + 3], alpha[index] if index < len(alpha) else 255)
            out[at:at + 4] = bytes(pixel)
            at += 4
    return width, height, out


def _chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF)


def encode_png(width, height, rgba, kind=6):
    """RGBA pixels -> PNG; kind 2 drops the alpha (for the opaque Apple icon)."""
    step = 4 if kind == 6 else 3
    out = bytearray()
    for y in range(height):
        out.append(0)
        row = rgba[y * width * 4:(y + 1) * width * 4]
        if kind == 6:
            out += row
        else:
            for x in range(0, len(row), 4):
                out += row[x:x + 3]
    assert step  # keeps the colour type and step together for readers
    header = struct.pack('>IIBBBBB', width, height, 8, kind, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(bytes(out), 9))
            + _chunk(b'IEND', b''))


def recolor_png(data, color):
    width, height, kind, channels, rows, _, _ = _decode(data)
    if kind not in (2, 6):
        raise ValueError('unsupported icon PNG')
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


# ---- pixels ----

def _weights(source, target, start=0.0, span=None):
    """Area weights: for each target pixel, the source pixels it covers and how much of each."""
    span = source if span is None else span
    scale = span / target
    out = []
    for index in range(target):
        left, right = start + index * scale, start + (index + 1) * scale
        parts, first = [], int(left)
        for pixel in range(first, min(source, math.ceil(right))):
            cover = min(right, pixel + 1) - max(left, pixel)
            if cover > 0:
                parts.append((pixel, cover / scale))
        out.append(parts)
    return out


def resize(width, height, rgba, size, crop=None):
    """Area-average RGBA pixels to size x size. crop = (x, y, side) picks a square of the source first."""
    x0, y0, side = crop or (0, 0, width)
    across = _weights(width, size, x0, side)
    down = _weights(height, size, y0, side if crop else height)
    middle = []  # one row of size*4 floats per source row that is needed
    needed = sorted({pixel for parts in down for pixel, _ in parts})
    rows = {}
    for y in needed:
        base = y * width * 4
        row = [0.0] * (size * 4)
        for x, parts in enumerate(across):
            r = g = b = a = 0.0
            for pixel, weight in parts:
                at = base + pixel * 4
                alpha = rgba[at + 3] * weight
                r += rgba[at] * alpha
                g += rgba[at + 1] * alpha
                b += rgba[at + 2] * alpha
                a += alpha
            row[x * 4:x * 4 + 4] = (r, g, b, a)
        rows[y] = row
    out = bytearray(size * size * 4)
    for y, parts in enumerate(down):
        for x in range(size):
            r = g = b = a = 0.0
            for pixel, weight in parts:
                source = rows[pixel]
                r += source[x * 4] * weight
                g += source[x * 4 + 1] * weight
                b += source[x * 4 + 2] * weight
                a += source[x * 4 + 3] * weight
            at = (y * size + x) * 4
            if a > 0:
                out[at:at + 4] = bytes((min(255, round(r / a)), min(255, round(g / a)), min(255, round(b / a)),
                                        min(255, round(a))))
    del middle
    return out


def _flatten(rgba, background=GLYPH):
    """Put the art on cream, so a transparent upload still gives the opaque square a maskable icon needs."""
    for at in range(0, len(rgba), 4):
        alpha = rgba[at + 3]
        if alpha != 255:
            for channel in range(3):
                rgba[at + channel] = round((rgba[at + channel] * alpha + background[channel] * (255 - alpha)) / 255)
            rgba[at + 3] = 255
    return rgba


def _ffmpeg_square(data):
    path = shutil.which('ffmpeg')
    if not path:
        return None
    command = [path, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0', '-frames:v', '1', '-vf',
               "crop='min(iw,ih)':'min(iw,ih)',scale=%d:%d:flags=area" % (SIDE, SIDE),
               '-pix_fmt', 'rgba', '-f', 'rawvideo', 'pipe:1']
    try:
        done = subprocess.run(command, input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return bytearray(done.stdout) if done.returncode == 0 and len(done.stdout) == SIDE * SIDE * 4 else None


def square_art(data):
    """Any PNG, JPEG or WebP -> SIDE x SIDE opaque RGBA, cut to the centre square."""
    pixels = _ffmpeg_square(data)
    if pixels is None:
        if data[:8] != b'\x89PNG\r\n\x1a\n':
            raise Bad('This server can read PNG images only. Use a PNG, or install ffmpeg for JPEG and WebP.', 415)
        try:
            width, height, rgba = decode_rgba(data)
        except (ValueError, IndexError, struct.error, zlib.error):
            raise Bad('Could not read that image. Use another PNG, JPEG or WebP.') from None
        side = min(width, height)
        pixels = resize(width, height, rgba, SIDE, ((width - side) / 2, (height - side) / 2, side))
    return _flatten(pixels)


def _cover(px, py, left, top, size, radius):
    """How much of pixel (px, py) a rounded square covers, 0 to 1, with a one-pixel soft edge."""
    half = size / 2
    qx = abs(px + 0.5 - (left + half)) - (half - radius)
    qy = abs(py + 0.5 - (top + half)) - (half - radius)
    distance = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius
    return min(1.0, max(0.0, 0.5 - distance))


def _over(canvas, side, x, y, color, alpha):
    if alpha <= 0:
        return
    at = (y * side + x) * 4
    for channel in range(3):
        canvas[at + channel] = round(canvas[at + channel] * (1 - alpha) + color[channel] * alpha)


def _badge(badge_png):
    key = ('badge', str(badge_png), badge_png.stat().st_mtime_ns)
    with _lock:
        if key in _cache:
            return _cache[key]
    width, height, rgba = decode_rgba(badge_png.read_bytes())
    small = resize(width, height, rgba, BADGE)
    with _lock:
        _cache[key] = small
    return small


def compose(art, badge_png):
    """The art with the Tether badge on a cream ring in the lower-right corner, at SIDE x SIDE."""
    canvas = bytearray(art)
    ring = BADGE + 2 * RING
    left = BADGE_END - ring
    for y in range(left, BADGE_END):
        for x in range(left, BADGE_END):
            _over(canvas, SIDE, x, y, GLYPH, _cover(x, y, left, left, ring, ring * 112 / 512))
    badge = _badge(badge_png)
    for y in range(BADGE):
        for x in range(BADGE):
            at = (y * BADGE + x) * 4
            _over(canvas, SIDE, left + RING + x, left + RING + y, badge[at:at + 3], badge[at + 3] / 255)
    return canvas


def _custom_png(art_png, size, badge_png):
    key = ('custom', str(art_png), _stamp(art_png), size)
    with _lock:
        if key in _cache:
            return _cache[key]
    width, height, art = decode_rgba(art_png.read_bytes())
    if (width, height) != (SIDE, SIDE):
        raise ValueError('bad icon art')
    canvas = compose(art, badge_png)
    if size != SIDE:
        canvas = resize(SIDE, SIDE, canvas, size)
    data = encode_png(size, size, canvas, 2 if size == 180 else 6)
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = data
    return data
