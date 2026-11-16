import json
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
        self.assertEqual(app_identity.load(), {'name': 'Tether', 'color': 'clay'})
        self.assertIsNone(app_identity.serve('manifest.webmanifest', PUBLIC / 'manifest.webmanifest'))

    def test_save_validates_name_and_colour(self):
        self.assertEqual(app_identity.save({'name': '  Work   box ', 'color': 'blue'})['name'], 'Work box')
        self.assertEqual(app_identity.save({'color': 'green'}), app_identity.state())
        self.assertEqual(app_identity.load(), {'name': 'Work box', 'color': 'green'})
        for body in ({'name': ''}, {'name': 'x' * 31}, {'name': 'a\x07b'}, {'name': 5}, {'color': 'pink'}, []):
            with self.subTest(body=body), self.assertRaises(app_identity.Bad):
                app_identity.save(body)
        self.assertEqual(app_identity.load(), {'name': 'Work box', 'color': 'green'})

    def test_bad_file_falls_back_to_default(self):
        paths.CONFIG.mkdir(parents=True)
        (paths.CONFIG / 'app-identity.json').write_text('{"name": "", "color": "nope"}')
        self.assertEqual(app_identity.load(), {'name': 'Tether', 'color': 'clay'})

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
                width, height, kind, channels, rows = app_identity._decode(out)
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


if __name__ == '__main__':
    unittest.main()
