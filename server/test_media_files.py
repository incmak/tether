"""Which files the phone may open from a path in chat, and video byte ranges."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import media_files as media


class MediaFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        for name in ('proj/shot.png', 'proj/.work-notes/clip.mp4', 'proj/notes.txt', '.ssh/key.png', '.config/x/a.png', 'proj/.git/a.png'):
            (self.home / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / name).write_bytes(b'x' * 10)
        (self.home / 'proj/link.png').symlink_to(self.home / '.ssh/key.png')

    def tearDown(self):
        self.temp.cleanup()

    def open(self, name, folder='proj'):
        return media.resolve(name, self.home / folder, home=self.home)

    def test_relative_absolute_and_work_notes(self):
        self.assertEqual(self.open('shot.png'), (self.home / 'proj/shot.png', 'image/png'))
        self.assertEqual(self.open(str(self.home / 'proj/.work-notes/clip.mp4'))[1], 'video/mp4')

    def test_text_files_open_as_plain_text(self):
        for name in ('notes.txt', 'src/App.tsx', 'Makefile'):
            (self.home / 'proj' / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / 'proj' / name).write_text('<script>x</script>')
            with self.subTest(name=name):
                self.assertEqual(self.open(name)[1], media.TEXT)

    def test_secret_and_large_text_refused(self):
        for name in ('.env', '.env.local', 'server.key', 'cert.pem', 'id_ed25519', 'credentials.json', 'api-token.txt', 'client_secret.json'):
            (self.home / 'proj' / name).write_text('x')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.open(name)
        (self.home / 'proj/big.log').write_bytes(b'x' * (media.TEXT_LIMIT + 1))
        with self.assertRaises(ValueError):
            self.open('big.log')

    def test_other_types_download(self):
        for name in ('Asset-Inventory-v0.2.xlsx', 'brief.docx', 'logs.zip', 'tool.bin'):
            (self.home / 'proj' / name).write_bytes(b'PK')
            with self.subTest(name=name):
                self.assertEqual(self.open(name), (self.home / 'proj' / name, media.DOWNLOAD))
        for name in ('secrets.xlsx', 'vault.kdbx', '.ssh/keys.zip', '.config/x/a.xlsx'):
            (self.home / 'proj' / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / 'proj' / name).write_bytes(b'PK')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.open(name)

    def test_refused(self):
        (self.home / 'proj/folder.xlsx').mkdir()
        for name, folder in (('folder.xlsx', 'proj'), ('.ssh/key.png', '.'), ('.config/x/a.png', '.'), ('.git/a.png', 'proj'),
                             ('link.png', 'proj'), ('/etc/hostname', 'proj'), ('../../x.png', 'proj'), ('missing.png', 'proj'), ('', 'proj')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.open(name, folder)

    def test_openable(self):
        (self.home / 'proj/api-token.txt').write_text('secret')
        (self.home / 'proj/escape.txt').symlink_to('/etc/hostname')
        names = ['shot.png', 'missing.png', '../.ssh/key.png', 'api-token.txt',
                 'link.png', 'escape.txt', 'notes.txt', None, 42, {}, []]
        with patch.object(Path, 'open', side_effect=AssertionError('Must not read contents')):
            self.assertEqual(media.openable(names, self.home / 'proj', self.home), {
                'shot.png': True, 'missing.png': False, '../.ssh/key.png': False,
                'api-token.txt': False, 'link.png': False, 'escape.txt': False, 'notes.txt': True,
            })

    def test_byte_range(self):
        self.assertIsNone(media.byte_range(None, 100))
        self.assertEqual(media.byte_range('bytes=0-', 100), (0, 99))
        self.assertEqual(media.byte_range('bytes=10-19', 100), (10, 19))
        self.assertEqual(media.byte_range('bytes=90-500', 100), (90, 99))
        self.assertEqual(media.byte_range('bytes=-10', 100), (90, 99))
        for bad in ('bytes=100-', 'bytes=5-2', 'bytes=x-'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                media.byte_range(bad, 100)


if __name__ == '__main__':
    unittest.main()
