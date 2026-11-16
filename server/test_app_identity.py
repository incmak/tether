import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import app_identity
import paths

PUBLIC = Path(__file__).resolve().parent.parent / 'ui' / 'public'


class AppIdentityTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = mock.patch.object(paths, 'CONFIG', Path(self.folder.name) / 'config')
        patcher.start()
        self.addCleanup(patcher.stop)
        app_identity._cache.clear()

    def test_default_serves_files_as_built(self):
        self.assertEqual(app_identity.load(), {'name': 'Tether', 'color': 'clay', 'icon': 'color'})
        self.assertIsNone(app_identity.serve('manifest.webmanifest', PUBLIC / 'manifest.webmanifest'))

    def test_save_validates_name_and_colour(self):
        self.assertEqual(app_identity.save({'name': '  Work   box ', 'color': 'blue'})['name'], 'Work box')
        self.assertEqual(app_identity.save({'color': 'green'}), app_identity.state())
        self.assertEqual(app_identity.load(), {'name': 'Work box', 'color': 'green', 'icon': 'color'})
        for body in ({'name': ''}, {'icon': 'custom'}, {'icon': 'photo'}, {'useDraft': True}, {'name': 'x' * 31}, {'name': 'a\x07b'}, {'name': 5}, {'color': 'pink'}, []):
            with self.subTest(body=body), self.assertRaises(app_identity.Bad):
                app_identity.save(body)
        self.assertEqual(app_identity.load(), {'name': 'Work box', 'color': 'green', 'icon': 'color'})

    def test_bad_file_falls_back_to_default(self):
        paths.CONFIG.mkdir(parents=True)
        (paths.CONFIG / 'app-identity.json').write_text('{"name": "", "color": "nope"}')
        self.assertEqual(app_identity.load(), {'name': 'Tether', 'color': 'clay', 'icon': 'color'})

    def test_manifest_carries_name_and_new_icon_addresses(self):
        app_identity.save({'name': 'Home lab', 'color': 'teal'})
        built = json.loads(app_identity.serve('manifest.webmanifest', PUBLIC / 'manifest.webmanifest'))
        self.assertEqual((built['name'], built['short_name']), ('Home lab', 'Home lab'))
        sources = [icon['src'] for icon in built['icons']]
        self.assertIn('/chat/icon-512.png?c=teal', sources)
        self.assertIn('/chat/icon-monochrome-512.png', sources)  # monochrome icons have no colour to change
        self.assertEqual(built['id'], '/chat/')  # the same app: an update, not a second install

    def test_page_title_and_svg(self):
        app_identity.save({'name': 'A <b>', 'color': 'purple'})
        page = Path(self.folder.name) / 'index.html'
        page.write_text('<head><title>Tether</title></head><svg><rect fill="#C96442"/></svg>')
        text = app_identity.serve('index.html', page).decode()
        self.assertIn('<title>A &lt;b&gt;</title>', text)
        self.assertIn('apple-mobile-web-app-title" content="A &lt;b&gt;"', text)
        self.assertIn('#7547C9', text)
        self.assertNotIn('#C96442', app_identity.serve('icon.svg', PUBLIC / 'icon.svg').decode())

    def test_png_recolour_keeps_size_and_alpha(self):
        for name, color in (('icon-512.png', 'blue'), ('icon-maskable-512.png', 'slate'), ('apple-touch-icon.png', 'rose')):
            with self.subTest(name=name):
                source = (PUBLIC / name).read_bytes()
                out = app_identity.recolor_png(source, app_identity._rgb(app_identity.HEX[color]))
                width, height, kind, channels, rows, _, _ = app_identity._decode(out)
                old = app_identity._decode(source)
                self.assertEqual((width, height, kind), old[:3])
                x, y = width // 2, height * 92 // 100  # below the bubble: background
                self.assertEqual(tuple(rows[y][x * channels:x * channels + 3]), app_identity._rgb(app_identity.HEX[color]))
                if channels == 4:
                    self.assertEqual(bytes(rows[0][3::4]), bytes(old[4][0][3::4]))  # transparent corners stay

    def test_unrelated_files_untouched(self):
        app_identity.save({'color': 'blue'})
        self.assertIsNone(app_identity.serve('icon-monochrome-512.png', PUBLIC / 'icon-monochrome-512.png'))
        self.assertIsNone(app_identity.serve('sw.js', PUBLIC / 'sw.js'))


class Handler:
    def __init__(self, path, body=b'', kind='application/json'):
        self.path, self.rfile = path, io.BytesIO(body)
        self.headers = {'Content-Length': str(len(body)), 'Content-Type': kind}
        self.sent = None

    def respond(self, status, body, content_type='application/json', cache='no-store'):
        self.sent = (status, body, content_type)


class CustomIconTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = mock.patch.object(paths, 'CONFIG', Path(self.folder.name) / 'config')
        patcher.start()
        self.addCleanup(patcher.stop)
        app_identity._cache.clear()
        app_identity._jobs.clear()
        # A wide, opaque photo-like PNG: red left half, blue right half.
        width, height = 300, 200
        pixels = bytearray()
        for _ in range(height):
            for x in range(width):
                pixels += bytes((220, 30, 30, 255) if x < width // 2 else (30, 30, 220, 255))
        self.photo = app_identity.encode_png(width, height, pixels)

    def use_photo(self):
        draft = app_identity.upload(self.photo, 'image/png')['draft']
        return app_identity.save({'useDraft': True, 'draft': draft})

    def test_upload_makes_draft_then_use_makes_custom(self):
        state = app_identity.upload(self.photo, 'image/png')
        self.assertTrue(state['draft'])
        self.assertEqual(state['icon'], 'color')  # a draft does not change the installed icon
        self.assertIsNone(app_identity.serve('icon-512.png', PUBLIC / 'icon-512.png'))
        state = app_identity.save({'useDraft': True, 'draft': state['draft']})
        self.assertEqual((state['icon'], state['draft']), ('custom', None))
        width, height, art = app_identity.decode_rgba(app_identity._art().read_bytes())
        self.assertEqual((width, height), (512, 512))
        self.assertEqual(tuple(art[(256 * 512 + 10) * 4:(256 * 512 + 10) * 4 + 4]), (220, 30, 30, 255))  # centre square kept

    def test_custom_icons_carry_badge_at_each_size(self):
        self.use_photo()
        for name, size in app_identity.PNG_SIZES.items():
            with self.subTest(name=name):
                width, height, rgba = app_identity.decode_rgba(app_identity.serve(name, PUBLIC / name))
                self.assertEqual((width, height), (size, size))
                corner = tuple(rgba[0:4])
                self.assertEqual(corner[3], 255)  # opaque, as a maskable icon must be
                x = y = round(size * 340 / 512)  # inside the badge: clay or cream, not the photo blue
                pixel = tuple(rgba[(y * size + x) * 4:(y * size + x) * 4 + 3])
                self.assertNotEqual(pixel, (30, 30, 220))
                self.assertGreater(pixel[0], pixel[2])
        svg = app_identity.serve('icon.svg', PUBLIC / 'icon.svg')
        self.assertIn(b'data:image/png;base64,', svg)

    def test_manifest_version_follows_the_art(self):
        self.use_photo()
        first = json.loads(app_identity.serve('manifest.webmanifest', PUBLIC / 'manifest.webmanifest'))
        source = next(icon['src'] for icon in first['icons'] if 'icon-512.png' in icon['src'])
        self.assertRegex(source, r'\?c=a[0-9a-f]+$')
        app_identity._cache.clear()
        stamp = app_identity._art().stat().st_mtime_ns
        os.utime(app_identity._art(), ns=(stamp + 10**9, stamp + 10**9))
        again = json.loads(app_identity.serve('manifest.webmanifest', PUBLIC / 'manifest.webmanifest'))
        self.assertNotIn(source, [icon['src'] for icon in again['icons']])
        app_identity.save({'icon': 'color', 'color': 'blue'})
        back = json.loads(app_identity.serve('manifest.webmanifest', PUBLIC / 'manifest.webmanifest'))
        self.assertIn('/chat/icon-512.png?c=blue', [icon['src'] for icon in back['icons']])

    def test_missing_art_falls_back_to_colour(self):
        self.use_photo()
        app_identity._art().unlink()
        self.assertEqual(app_identity.load()['icon'], 'color')

    def test_upload_refuses_other_types_and_broken_images(self):
        with self.assertRaises(app_identity.Bad):
            app_identity.upload(b'GIF89a', 'image/gif')
        with mock.patch.object(app_identity.shutil, 'which', return_value=None):
            with self.assertRaises(app_identity.Bad):
                app_identity.upload(b'\xff\xd8\xff', 'image/jpeg')  # JPEG needs ffmpeg
            with self.assertRaises(ValueError):
                app_identity.upload(b'\x89PNG\r\n\x1a\n broken', 'image/png')
            self.assertEqual(app_identity.upload(self.photo, 'image/png')['icon'], 'color')  # pure-Python path

    def test_discard_draft(self):
        draft = app_identity.upload(self.photo, 'image/png')['draft']
        self.assertIsNone(app_identity.save({'discardDraft': True, 'draft': draft})['draft'])

    def test_use_refuses_a_draft_the_page_did_not_show(self):
        shown = app_identity.upload(self.photo, 'image/png')['draft']
        stamp = app_identity._draft().stat().st_mtime_ns + 10**9
        os.utime(app_identity._draft(), ns=(stamp, stamp))  # another device replaced the draft
        for body in ({'useDraft': True, 'draft': shown}, {'discardDraft': True, 'draft': shown}, {'useDraft': True}):
            with self.subTest(body=body), self.assertRaises(app_identity.Bad) as refused:
                app_identity.save(body)
            self.assertEqual(refused.exception.status, 409)
        self.assertEqual(app_identity.load()['icon'], 'color')
        self.assertTrue(app_identity._draft().is_file())
        self.assertEqual(sorted(path.name for path in app_identity._folder().iterdir()), ['draft.png'])  # no temporary files

    def test_decoder_refuses_a_decompression_bomb(self):
        header = app_identity._chunk(b'IHDR', app_identity.struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
        body = app_identity._chunk(b'IDAT', app_identity.zlib.compress(bytes(16 * 1024 * 1024), 9))
        bomb = b'\x89PNG\r\n\x1a\n' + header + body + app_identity._chunk(b'IEND', b'')
        with self.assertRaises(ValueError):
            app_identity.decode_rgba(bomb)
        with mock.patch.object(app_identity.shutil, 'which', return_value=None), self.assertRaises(app_identity.Bad):
            app_identity.upload(bomb, 'image/png')
        zero = b'\x89PNG\r\n\x1a\n' + app_identity._chunk(b'IHDR', app_identity.struct.pack('>IIBBBBB', 0, 1, 8, 6, 0, 0, 0))
        with self.assertRaises(ValueError):
            app_identity.decode_rgba(zero + app_identity._chunk(b'IEND', b''))

    def test_rgb_and_grey_transparency_key(self):
        def png(kind, rows, key):
            header = app_identity._chunk(b'IHDR', app_identity.struct.pack('>IIBBBBB', 2, 1, 8, kind, 0, 0, 0))
            data = app_identity._chunk(b'IDAT', app_identity.zlib.compress(b'\x00' + bytes(rows)))
            return b'\x89PNG\r\n\x1a\n' + header + app_identity._chunk(b'tRNS', key) + data + app_identity._chunk(b'IEND', b'')
        rgb = app_identity.decode_rgba(png(2, (0, 0, 0, 9, 9, 9), b'\x00\x00\x00\x00\x00\x00'))[2]
        self.assertEqual((rgb[3], rgb[7]), (0, 255))
        grey = app_identity.decode_rgba(png(0, (0, 9), b'\x00\x09'))[2]
        self.assertEqual((grey[3], grey[7]), (255, 0))

    def test_generate_runs_one_job_and_saves_a_draft(self):
        with mock.patch.object(app_identity.backgrounds, 'codex_image', return_value=self.photo) as run, \
             mock.patch.object(app_identity.threading, 'Thread') as thread:
            job = app_identity.generate({'prompt': '  a red fox  '})['job']
            with self.assertRaises(app_identity.Bad) as busy:
                app_identity.generate({'prompt': 'again'})
            self.assertEqual(busy.exception.status, 409)
            target, args = thread.call_args.kwargs['target'], thread.call_args.kwargs['args']
            target(*args)
            self.assertIn('a red fox', run.call_args.args[0])
            self.assertIn('lower-right', run.call_args.args[0])
        self.assertEqual(app_identity._jobs[job], {'state': 'done'})
        self.assertTrue(app_identity.state()['draft'])
        for body in ({}, {'prompt': ''}, {'prompt': 'x' * 301}, []):
            with self.subTest(body=body), self.assertRaises(app_identity.Bad):
                app_identity.generate(body)

    def test_generate_failure_is_reported(self):
        failure = app_identity.backgrounds.Bad('Codex image generation is off or unavailable.')
        with mock.patch.object(app_identity.backgrounds, 'codex_image', side_effect=failure):
            app_identity._jobs['j'] = {'state': 'running'}
            app_identity._run_job('j', 'fox')
        self.assertEqual(app_identity._jobs['j'], {'state': 'failed', 'error': 'Codex image generation is off or unavailable.'})
        handler = Handler(app_identity.PATH + '/generate?job=j')
        app_identity.get(handler)
        self.assertEqual(handler.sent[:2], (200, {'state': 'failed', 'error': 'Codex image generation is off or unavailable.'}))
        handler = Handler(app_identity.PATH + '/generate?job=nope')
        app_identity.get(handler)
        self.assertEqual(handler.sent[0], 404)

    def test_http_upload_preview_and_limits(self):
        handler = Handler(app_identity.PATH + '/upload', self.photo, 'image/png')
        app_identity.post(handler)
        self.assertEqual(handler.sent[0], 200)
        handler = Handler(app_identity.PATH + '/preview/draft.png')
        app_identity.get(handler)
        self.assertEqual((handler.sent[0], handler.sent[2]), (200, 'image/png'))
        self.assertEqual(app_identity.decode_rgba(handler.sent[1])[:2], (192, 192))
        handler = Handler(app_identity.PATH + '/preview/art.png')
        app_identity.get(handler)
        self.assertEqual(handler.sent[0], 404)
        handler = Handler(app_identity.PATH + '/upload', b'')
        app_identity.post(handler)
        self.assertEqual(handler.sent[0], 413)
        handler = Handler(app_identity.PATH + '/elsewhere', b'{}')
        app_identity.post(handler)
        self.assertEqual(handler.sent[0], 404)


if __name__ == '__main__':
    unittest.main()
