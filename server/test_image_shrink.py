"""image_shrink: header parsing, encoder order and real encodes when an encoder is installed."""
import base64
import io
import struct
import unittest
import zlib
from unittest import mock

import image_shrink


def png(width, height):
    """A plain RGB gradient PNG built with the standard library."""
    rows = b''.join(b'\x00' + b''.join(bytes((x * 255 // width, y * 255 // height, 128)) for x in range(width))
                    for y in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def has_pillow():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


class WebpSizeTests(unittest.TestCase):
    def test_reads_vp8x_vp8_and_vp8l_headers(self):
        vp8x = b'RIFF\x00\x00\x00\x00WEBPVP8X' + bytes(8) + (2559).to_bytes(3, 'little') + (1439).to_bytes(3, 'little')
        self.assertEqual(image_shrink.webp_size(vp8x), (2560, 1440))
        vp8 = b'RIFF\x00\x00\x00\x00WEBPVP8 ' + bytes(4) + bytes(3) + b'\x9d\x01\x2a' + struct.pack('<HH', 480, 270)
        self.assertEqual(image_shrink.webp_size(vp8), (480, 270))
        bits = (32 - 1) | ((18 - 1) << 14)
        vp8l = b'RIFF\x00\x00\x00\x00WEBPVP8L' + bytes(4) + b'\x2f' + bits.to_bytes(4, 'little') + bytes(5)
        self.assertEqual(image_shrink.webp_size(vp8l), (32, 18))

    def test_rejects_other_data(self):
        for data in (b'', b'\x89PNG' + bytes(40), b'RIFF\x00\x00\x00\x00WEBPVP8 ' + bytes(20)):
            self.assertIsNone(image_shrink.webp_size(data))


class OrderTests(unittest.TestCase):
    def test_pillow_first_then_ffmpeg_then_none(self):
        result = {'image': b'x'}
        with mock.patch.object(image_shrink, '_pillow', return_value=result), \
                mock.patch.object(image_shrink, '_with_ffmpeg') as ffmpeg:
            self.assertIs(image_shrink.shrink(b'data'), result)
        ffmpeg.assert_not_called()
        with mock.patch.object(image_shrink, '_pillow', return_value=None), \
                mock.patch.object(image_shrink, '_with_ffmpeg', return_value=result):
            self.assertIs(image_shrink.shrink(b'data'), result)
        with mock.patch.object(image_shrink, '_pillow', return_value=None), \
                mock.patch.object(image_shrink, '_with_ffmpeg', return_value=None):
            self.assertIsNone(image_shrink.shrink(b'data'))

    def test_ffmpeg_without_libwebp_is_not_used(self):
        image_shrink._ffmpeg.cache_clear()
        self.addCleanup(image_shrink._ffmpeg.cache_clear)
        listing = mock.Mock(stdout=' V....D libx264   H.264\n V..... png   PNG\n')
        with mock.patch.object(image_shrink.shutil, 'which', return_value='/fake/ffmpeg'), \
                mock.patch.object(image_shrink.subprocess, 'run', return_value=listing):
            self.assertIsNone(image_shrink._ffmpeg())
            self.assertIsNone(image_shrink._with_ffmpeg(b'data'))


class RealEncodeMixin:
    def check(self, result, width, height):
        self.assertIsNotNone(result)
        self.assertEqual((result['width'], result['height']), (width, height))
        self.assertEqual(image_shrink.webp_size(result['thumb'])[0], image_shrink.THUMB_WIDTH)
        prefix = 'data:image/webp;base64,'
        self.assertTrue(result['placeholder'].startswith(prefix))
        tiny = base64.b64decode(result['placeholder'][len(prefix):])
        self.assertEqual(image_shrink.webp_size(tiny)[0], image_shrink.PLACEHOLDER_WIDTH)
        self.assertLess(len(result['placeholder']), 2000)

    def test_large_image_fits_2560_and_keeps_its_shape(self):
        self.check(self.encode(png(3200, 1800)), 2560, 1440)

    def test_small_image_keeps_its_size(self):
        self.check(self.encode(png(800, 600)), 800, 600)

    def test_garbage_returns_none(self):
        self.assertIsNone(self.encode(b'\x89PNG\r\n\x1a\nnot really'))


@unittest.skipUnless(has_pillow(), 'Pillow is not installed')
class PillowTests(RealEncodeMixin, unittest.TestCase):
    def encode(self, data):
        return image_shrink._pillow(data)

    def test_palette_png_keeps_its_transparent_colour(self):
        from PIL import Image
        source = Image.new('P', (64, 36), 0)
        source.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
        source.paste(1, (0, 0, 32, 36))
        out = io.BytesIO()
        source.save(out, 'PNG', transparency=0)
        result = self.encode(out.getvalue())
        with Image.open(io.BytesIO(result['image'])) as page:
            self.assertEqual(page.mode, 'RGBA')
            self.assertEqual(page.getpixel((60, 18))[3], 0)


@unittest.skipUnless(image_shrink._ffmpeg(), 'ffmpeg with libwebp is not installed')
class FfmpegTests(RealEncodeMixin, unittest.TestCase):
    def encode(self, data):
        return image_shrink._with_ffmpeg(data)


if __name__ == '__main__':
    unittest.main()
