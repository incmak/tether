"""Make small copies of a backdrop photo: a WebP for the page, a thumbnail and a tiny placeholder.

Pillow is used when it is installed (install with --images). Otherwise ffmpeg is used when it is
on PATH and has the libwebp encoder. Without either, shrink() returns None and the caller keeps
the original file. The core server stays free of third-party Python dependencies.
"""
import base64
import functools
import io
import shutil
import struct
import subprocess

MAX_SIDE = 2560
THUMB_WIDTH = 480
PLACEHOLDER_WIDTH = 32
QUALITY = 80
THUMB_QUALITY = 70
PLACEHOLDER_QUALITY = 50
TIMEOUT = 30


def webp_size(data):
    """Width and height of a WebP file, from its header; None if the header is not known."""
    if len(data) < 30 or data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        return None
    chunk = data[12:16]
    if chunk == b'VP8X':
        width = 1 + int.from_bytes(data[24:27], 'little')
        height = 1 + int.from_bytes(data[27:30], 'little')
    elif chunk == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a':
        width, height = (value & 0x3fff for value in struct.unpack('<HH', data[26:30]))
    elif chunk == b'VP8L' and data[20] == 0x2f:
        bits = int.from_bytes(data[21:25], 'little')
        width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
    else:
        return None
    return (width, height) if width and height else None


def _result(image, thumb, placeholder):
    size = webp_size(image)
    if not size or not thumb or not placeholder:
        return None
    return {'image': image, 'thumb': thumb, 'width': size[0], 'height': size[1],
            'placeholder': 'data:image/webp;base64,' + base64.b64encode(placeholder).decode('ascii')}


def _pillow(data):
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            picture = ImageOps.exif_transpose(source)
        # A palette or grey PNG can mark a transparent colour in info instead of an alpha band.
        alpha = 'A' in picture.getbands() or 'transparency' in picture.info
        picture = picture.convert('RGBA' if alpha else 'RGB')

        def encode(width, quality):
            copy = picture.copy()
            if width:
                copy.thumbnail((width, max(1, round(width * picture.height / picture.width))), Image.LANCZOS)
            else:
                copy.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            out = io.BytesIO()
            copy.save(out, 'WEBP', quality=quality, method=4)
            return out.getvalue()

        return _result(encode(None, QUALITY), encode(THUMB_WIDTH, THUMB_QUALITY), encode(PLACEHOLDER_WIDTH, PLACEHOLDER_QUALITY))
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


@functools.lru_cache(maxsize=1)
def _ffmpeg():
    path = shutil.which('ffmpeg')
    if not path:
        return None
    try:
        encoders = subprocess.run([path, '-hide_banner', '-encoders'], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return path if any(line.split()[1:2] == ['libwebp'] for line in encoders.splitlines()) else None


def _ffmpeg_encode(path, data, scale, quality):
    command = [path, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0', '-frames:v', '1',
               '-vf', scale, '-c:v', 'libwebp', '-quality', str(quality), '-f', 'webp', 'pipe:1']
    try:
        done = subprocess.run(command, input=data, capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 and webp_size(done.stdout) else None


def _with_ffmpeg(data):
    path = _ffmpeg()
    if not path:
        return None
    fit = f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease"
    image = _ffmpeg_encode(path, data, fit, QUALITY)
    thumb = image and _ffmpeg_encode(path, data, f'scale={THUMB_WIDTH}:-2', THUMB_QUALITY)
    placeholder = thumb and _ffmpeg_encode(path, data, f'scale={PLACEHOLDER_WIDTH}:-2', PLACEHOLDER_QUALITY)
    return _result(image, thumb, placeholder) if placeholder else None


def shrink(data):
    """Return {'image', 'thumb', 'placeholder', 'width', 'height'}, or None when no encoder works."""
    return _pillow(data) or _with_ffmpeg(data)


def encoder():
    """The encoder shrink() would use now: 'pillow', 'ffmpeg' or None."""
    try:
        import PIL  # noqa: F401
        return 'pillow'
    except ImportError:
        return 'ffmpeg' if _ffmpeg() else None
