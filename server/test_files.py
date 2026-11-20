"""Files explorer (files.py) against a temporary root. No network, no tmux."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import files
import prefs_sync


class FakeHandler:
    def __init__(self, path='/', headers=None, body=b''):
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.close_connection = False
        self.status = None
        self.sent = {}
        self.answer = None

    def respond(self, status, body, content_type='application/json', cache='no-store'):
        self.status, self.answer = status, body

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent[key] = value

    def end_headers(self):
        pass


class Reader:
    """A request body that records how much each read asked for."""

    def __init__(self, size, stop_at=None):
        self.left, self.reads, self.stop_at, self.given = size, [], stop_at, 0

    def read(self, count):
        self.reads.append(count)
        if self.stop_at is not None and self.given >= self.stop_at:
            return b''
        count = min(count, self.left, self.stop_at - self.given if self.stop_at is not None else count)
        self.left -= count
        self.given += count
        return b'x' * count


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.root = base / 'home'
        self.outside = base / 'elsewhere'
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / 'loot.txt').write_text('outside')
        (self.root / 'proj').mkdir()
        (self.root / 'proj' / 'app.py').write_text('print(1)\n')
        (self.root / 'proj' / '.env').write_text('KEY=1\n')
        (self.root / 'proj' / 'api_token.txt').write_text('t')
        (self.root / 'proj' / 'sub').mkdir()
        (self.root / 'proj' / 'sub' / 'id_rsa').write_text('k')
        (self.root / 'proj' / 'sub' / 'notes.md').write_text('# hi\n')
        (self.root / '.ssh').mkdir()
        (self.root / '.ssh' / 'config').write_text('Host x\n')
        (self.root / '.bashrc').write_text('alias x=y\n')
        (self.root / 'escape').symlink_to(self.outside)
        (self.root / 'proj-link').symlink_to(self.root / 'proj')
        (self.root / 'ssh-link').symlink_to(self.root / '.ssh')
        self.now = 1_000_000.0
        patches = [mock.patch.object(files, 'clock', lambda: self.now),
                   mock.patch.object(prefs_sync, 'FILE', base / 'prefs.json'),
                   mock.patch.dict(os.environ, {'MOBILE_TMUX_FILES_ROOT': str(self.root)})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        files.set_secrets(False)

    def tearDown(self):
        files.set_secrets(False)
        self.temp.cleanup()

    def assertCode(self, code, call, *args):
        with self.assertRaises(files.FilesError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def names(self, path='~', hidden=True):
        return [entry['name'] for entry in files.list_folder(path, hidden=hidden)['entries']]

    # ---- root ----

    def test_root_escape_with_dotdot(self):
        self.assertCode('outside', files.list_folder, '~/..')
        self.assertCode('outside', files.list_folder, '~/proj/../../elsewhere')
        self.assertCode('outside', files.stat_path, str(self.outside / 'loot.txt'))
        self.assertCode('outside', files.make_folder, '~/..', 'x')
        self.assertCode('outside', files.upload, Reader(1), 1, '~/../elsewhere', 'a.txt')
        self.assertFalse((self.outside / 'x').exists())

    def test_root_escape_with_symlink(self):
        self.assertNotIn('escape', self.names())  # links that leave the root are left out
        self.assertIn('proj-link', self.names())
        self.assertCode('outside', files.list_folder, '~/escape')
        self.assertCode('outside', files.preview, '~/escape/loot.txt')
        self.assertCode('outside', files.send_raw, FakeHandler(), '~/escape/loot.txt', True, self.root)
        self.assertCode('outside', files.upload, Reader(1), 1, '~/escape', 'a.txt')
        self.assertCode('outside', files.transfer, ['~/proj/app.py'], '~/escape', False)
        self.assertCode('outside', files.size_of, ['~/escape'])
        self.assertEqual(sorted(os.listdir(self.outside)), ['loot.txt'])

    def test_writes_refuse_a_symlink_as_the_item(self):
        self.assertCode('link', files.rename, '~/proj-link', 'other')
        self.assertCode('link', files.delete, ['~/proj-link'])
        self.assertCode('link', files.transfer, ['~/proj-link'], '~/proj/sub', True)
        self.assertTrue((self.root / 'proj-link').is_symlink())

    def test_open_dir_does_not_follow_a_swapped_symlink(self):
        # A folder swapped for a symlink after the root check: the component-by-component open refuses it.
        self.assertCode('changed', files.open_dir, self.root, self.root / 'proj-link')
        self.assertCode('changed', files.open_dir, self.root, self.root / 'escape')

    def test_paths_come_back_in_home_style(self):
        listing = files.list_folder('~/proj')
        self.assertEqual((listing['path'], listing['parent']), ('~/proj', '~'))
        self.assertEqual(files.list_folder('~')['parent'], None)
        self.assertEqual(files.list_folder(str(self.root / 'proj'))['path'], '~/proj')
        self.assertIn('~/proj/app.py', [entry['path'] for entry in listing['entries']])

    # ---- secrets ----

    def test_secrets_hidden_by_default_even_with_hidden_on(self):
        self.assertNotIn('.ssh', self.names('~', hidden=True))
        self.assertNotIn('ssh-link', self.names('~', hidden=True))
        self.assertIn('.bashrc', self.names('~', hidden=True))
        self.assertNotIn('.bashrc', self.names('~', hidden=False))
        self.assertEqual(sorted(self.names('~/proj', hidden=True)), ['app.py', 'sub'])
        self.assertNotIn('id_rsa', self.names('~/proj/sub'))
        for call in (files.list_folder, files.stat_path, files.preview):
            self.assertCode('secret', call, '~/.ssh/config' if call is not files.list_folder else '~/.ssh')
        self.assertCode('secret', files.preview, '~/proj/.env')
        self.assertCode('secret', files.list_folder, '~/ssh-link')
        hits = files.search('~', 'i', hidden=True)['hits']
        self.assertFalse([hit for hit in hits if hit['secret'] or hit['name'] in ('id_rsa', '.env', 'api_token.txt', 'config')])

    def test_secret_window_opens_for_ten_minutes(self):
        state = files.set_secrets(True)
        self.assertEqual(state, {'on': True, 'expires_at': self.now + 600})
        listing = files.list_folder('~', hidden=True)
        self.assertEqual(listing['secrets'], state)
        ssh = next(entry for entry in listing['entries'] if entry['name'] == '.ssh')
        self.assertTrue(ssh['secret'])
        self.assertIn('.env', self.names('~/proj'))
        self.assertEqual(files.list_folder('~/.ssh')['entries'][0]['name'], 'config')
        self.assertTrue(files.stat_path('~/proj/.env')['secret'])
        self.assertEqual(files.preview('~/proj/.env')['lines'], ['KEY=1'])
        self.assertEqual(files.places()['secrets'], state)
        self.now += 599
        self.assertIn('.env', self.names('~/proj'))
        self.now += 2  # past 10 minutes: the server closes the window by itself
        self.assertNotIn('.env', self.names('~/proj'))
        self.assertCode('secret', files.stat_path, '~/proj/.env')
        self.assertEqual(files.secrets_state(), {'on': False, 'expires_at': None})

    def test_secret_window_can_be_closed(self):
        files.set_secrets(True)
        self.assertEqual(files.set_secrets(False), {'on': False, 'expires_at': None})
        self.assertCode('secret', files.preview, '~/proj/.env')
        self.assertCode('invalid', files.set_secrets, 'yes')

    def test_secret_paths_stay_blocked_for_raw_zip_and_writes_inside_the_window(self):
        files.set_secrets(True)
        handler = FakeHandler()
        self.assertCode('secret', files.send_raw, handler, '~/proj/.env', True, self.root)
        self.assertCode('secret', files.send_raw, handler, '~/.ssh/config', False, self.root)
        self.assertIsNone(handler.status)
        self.assertCode('secret', files.size_of, ['~/proj/.env'])
        self.assertCode('secret', files.zip_tops, ['~/.ssh'], self.root)
        self.assertEqual(files.preview('~/proj/sub/id_rsa')['kind'], 'text')  # read is fine in the window
        (self.root / '.ssh' / 'shot.png').write_bytes(b'\x89PNG')
        self.assertEqual(files.preview('~/.ssh/shot.png')['reason'], 'secret')  # no raw URL for secret media
        writes = [
            (files.upload, Reader(3), 3, '~/.ssh', 'a.txt'),
            (files.upload, Reader(3), 3, '~/proj', '.env.local'),
            (files.upload, Reader(3), 3, '~/proj', 'my-secret.txt'),
            (files.make_folder, '~/.ssh', 'new'),
            (files.make_folder, '~/proj', '.ssh'),
            (files.rename, '~/proj/.env', 'env.txt'),
            (files.rename, '~/proj/app.py', '.env'),
            (files.rename, '~/.ssh', 'ssh'),
            (files.duplicate, '~/proj/.env'),
            (files.transfer, ['~/proj/.env'], '~/proj/sub', False),
            (files.transfer, ['~/proj/app.py'], '~/.ssh', False),
            (files.transfer, ['~/.ssh/config'], '~/proj', True),
            (files.transfer, ['~/proj/app.py'], '~/.ssh', True),
            (files.delete, ['~/proj/.env']),
            (files.delete, ['~/.ssh']),
        ]
        for call, *args in writes:
            with self.subTest(call=call.__name__, args=args[-2:]):
                self.assertCode('secret', call, *args)
        self.assertEqual(sorted(os.listdir(self.root / '.ssh')), ['config', 'shot.png'])
        self.assertEqual(sorted(os.listdir(self.root / 'proj')), ['.env', 'api_token.txt', 'app.py', 'sub'])
        self.assertFalse((self.root / '.local').exists())

    def test_zip_and_size_skip_secrets_inside_a_folder(self):
        files.set_secrets(True)  # the window does not let secrets into a zip
        size = files.size_of(['~/proj'])
        self.assertEqual(size['skipped']['secret'], 3)  # .env, api_token.txt, sub/id_rsa
        self.assertEqual((size['files'], size['folders']), (2, 2))
        self.assertEqual(size['bytes'], len('print(1)\n') + len('# hi\n'))
        self.assertIn('3 secret items will be left out.', size['note'])
        out = io.BytesIO()
        files.write_zip(files._Counting(out), files.zip_tops(['~/proj'], self.root), self.root)
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
            self.assertEqual(sorted(archive.namelist()), ['proj/', 'proj/app.py', 'proj/sub/', 'proj/sub/notes.md'])
            self.assertEqual(archive.read('proj/sub/notes.md'), b'# hi\n')

    def test_zip_streams_to_a_socket_without_seek_and_skips_links(self):
        (self.root / 'proj' / 'out-link').symlink_to(self.outside / 'loot.txt')
        (self.root / 'a.txt').write_text('A')

        class Socketish:
            def __init__(self):
                self.data = bytearray()

            def write(self, data):
                self.data += data
                return len(data)

            def flush(self):
                pass

        handler = FakeHandler('/mobile/api/files/zip?path=~/proj&path=~/a.txt')
        handler.wfile = Socketish()
        files.serve_get(handler, '/mobile/api/files/zip')
        self.assertEqual(handler.status, 200)
        self.assertIn('files.zip', handler.sent['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(bytes(handler.wfile.data))) as archive:
            names = archive.namelist()
        self.assertIn('a.txt', names)
        self.assertFalse([name for name in names if 'loot' in name or 'out-link' in name])
        self.assertEqual(files.size_of(['~/proj'])['skipped']['links'], 1)

    # ---- names ----

    def test_clash_names(self):
        for _ in range(3):
            files.upload(Reader(2), 2, '~/proj', 'report.pdf')
        self.assertTrue({'report.pdf', 'report (2).pdf', 'report (3).pdf'} <= set(os.listdir(self.root / 'proj')))
        self.assertEqual(files.duplicate('~/proj/report (2).pdf')['name'], 'report (4).pdf')
        self.assertEqual(files.make_folder('~/proj', 'sub')['name'], 'sub (2)')
        (self.root / 'dest').mkdir()
        (self.root / 'dest' / 'app.py').write_text('old')
        copied = files.transfer(['~/proj/app.py'], '~/dest', False)
        self.assertEqual(copied['done'], [{'from': '~/proj/app.py', 'path': '~/dest/app (2).py'}])
        moved = files.transfer(['~/proj/app.py'], '~/dest', True)
        self.assertEqual(moved['done'][0]['path'], '~/dest/app (3).py')
        self.assertEqual((self.root / 'dest' / 'app.py').read_text(), 'old')
        self.assertFalse((self.root / 'proj' / 'app.py').exists())
        self.assertEqual(list(files.numbered('.bashrc', False))[1], '.bashrc (2)')

    def test_rename_does_not_overwrite(self):
        (self.root / 'proj' / 'b.py').write_text('b')
        self.assertCode('exists', files.rename, '~/proj/app.py', 'b.py')
        self.assertEqual(files.rename('~/proj/app.py', 'c.py')['path'], '~/proj/c.py')
        self.assertCode('invalid', files.rename, '~/proj/c.py', 'a/b')
        self.assertCode('invalid', files.rename, '~/proj/c.py', '..')
        self.assertCode('invalid', files.rename, '~', 'x')

    def test_named_pipe_does_not_hang_reads_or_copies(self):
        os.mkfifo(self.root / 'proj' / 'pipe')  # opening a FIFO to read blocks until a writer comes
        result = {}

        def run():
            result['dup'] = self.codeOf(files.duplicate, '~/proj/pipe')
            result['raw'] = self.codeOf(files.open_read, files.files_root(), files.files_root() / 'proj' / 'pipe')
        import threading
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'a FIFO blocked the thread')
        self.assertEqual(result, {'dup': 'invalid', 'raw': 'invalid'})

    def codeOf(self, function, *args):
        try:
            function(*args)
        except files.FilesError as exc:
            return exc.code
        return None

    def test_copy_folder_skips_secrets_and_refuses_into_itself(self):
        result = files.duplicate('~/proj')
        self.assertEqual(result['name'], 'proj (2)')
        self.assertEqual(result['skipped'], {'secret': 3})
        self.assertEqual(sorted(os.listdir(self.root / 'proj (2)')), ['app.py', 'sub'])
        self.assertEqual(sorted(os.listdir(self.root / 'proj (2)' / 'sub')), ['notes.md'])
        self.assertCode('invalid', files.transfer, ['~/proj'], '~/proj/sub', True)
        self.assertCode('invalid', files.transfer, ['~/proj'], '~/proj', False)

    def test_move_in_same_folder_keeps_the_item(self):
        result = files.transfer(['~/proj/app.py'], '~/proj', True)
        self.assertEqual(result['done'], [{'from': '~/proj/app.py', 'path': '~/proj/app.py'}])

    def test_batch_is_checked_before_any_change(self):
        (self.root / 'dest').mkdir()
        self.assertCode('missing', files.transfer, ['~/proj/app.py', '~/proj/nope'], '~/dest', True)
        self.assertTrue((self.root / 'proj' / 'app.py').exists())
        self.assertCode('missing', files.delete, ['~/proj/app.py', '~/nope'])
        self.assertTrue((self.root / 'proj' / 'app.py').exists())

    # ---- trash ----

    def test_delete_moves_to_trash_with_info(self):
        (self.root / 'proj' / 'my file é.txt').write_text('bye')
        result = files.delete(['~/proj/my file é.txt'])
        self.assertEqual(result, {'done': [{'from': '~/proj/my file é.txt', 'trash_name': 'my file é.txt'}], 'failed': []})
        trash = self.root / '.local' / 'share' / 'Trash'
        self.assertFalse((self.root / 'proj' / 'my file é.txt').exists())
        self.assertEqual((trash / 'files' / 'my file é.txt').read_text(), 'bye')
        info = (trash / 'info' / 'my file é.txt.trashinfo').read_text().splitlines()
        self.assertEqual(info[0], '[Trash Info]')
        self.assertEqual(info[1], 'Path=' + str(self.root / 'proj').replace(' ', '%20') + '/my%20file%20%C3%A9.txt')
        self.assertRegex(info[2], r'^DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$')
        (self.root / 'proj' / 'my file é.txt').write_text('again')
        self.assertEqual(files.delete(['~/proj/my file é.txt'])['done'][0]['trash_name'], 'my file é (2).txt')
        self.assertTrue((trash / 'info' / 'my file é (2).txt.trashinfo').exists())
        self.assertEqual(files.delete(['~/proj/sub'])['done'][0]['trash_name'], 'sub')
        self.assertTrue((trash / 'files' / 'sub' / 'id_rsa').exists())  # moved with its folder, never read

    # ---- upload ----

    def test_upload_streams_in_chunks(self):
        size = 3 * files.CHUNK + 123
        body = Reader(size)
        result = files.upload(body, str(size), '~/proj', 'big.bin')
        self.assertEqual(result, {'path': '~/proj/big.bin', 'name': 'big.bin', 'size': size})
        self.assertEqual((self.root / 'proj' / 'big.bin').stat().st_size, size)
        self.assertLessEqual(max(body.reads), files.CHUNK)
        self.assertEqual(len(body.reads), 4)

    def test_upload_limit_and_short_body(self):
        body = Reader(10)
        with mock.patch.object(files, 'UPLOAD_LIMIT', 9):
            self.assertCode('too-large', files.upload, body, 10, '~/proj', 'a.bin')
        self.assertEqual(body.reads, [])  # refused before reading
        self.assertEqual(files.UPLOAD_LIMIT, 2 * 1024 ** 3)
        self.assertCode('length', files.upload, Reader(1), None, '~/proj', 'a.bin')
        self.assertCode('invalid', files.upload, Reader(10, stop_at=4), 10, '~/proj', 'cut.bin')
        self.assertFalse((self.root / 'proj' / 'cut.bin').exists())  # the partial file is removed
        self.assertEqual(files.upload(Reader(0), 0, '~/proj', 'empty.txt')['size'], 0)

    def test_upload_percent_encoded_names_through_the_handler(self):
        handler = FakeHandler(headers={'Content-Length': '5', 'X-Folder': '~/proj', 'X-Filename': '%E2%9C%93%20r%C3%A9sum%C3%A9.pdf'},
                              body=b'hello')
        files.serve_post(handler, '/mobile/api/files/upload')
        self.assertEqual((handler.status, handler.answer['name']), (200, '✓ résumé.pdf'))
        self.assertEqual((self.root / 'proj' / '✓ résumé.pdf').read_bytes(), b'hello')
        for bad in ('%FF.txt', 'a%2Fb.txt', '..', ''):
            handler = FakeHandler(headers={'Content-Length': '1', 'X-Folder': '~/proj', 'X-Filename': bad}, body=b'x')
            files.serve_post(handler, '/mobile/api/files/upload')
            self.assertEqual((handler.status, handler.answer['code']), (400, 'invalid'), bad)
            self.assertTrue(handler.close_connection)
        handler = FakeHandler(headers={'Content-Length': str(2 * 1024 ** 3 + 1), 'X-Folder': '~/proj', 'X-Filename': 'a'})
        files.serve_post(handler, '/mobile/api/files/upload')
        self.assertEqual(handler.status, 413)

    # ---- reads ----

    def test_list_sort_and_kinds(self):
        (self.root / 'proj' / 'zeta.png').write_bytes(b'\x89PNG' + b'0' * 100)
        listing = files.list_folder('~/proj', sort='size', order='desc')
        self.assertEqual([entry['name'] for entry in listing['entries']], ['sub', 'zeta.png', 'app.py'])
        self.assertEqual(listing['entries'][1]['kind'], 'image')
        self.assertIsNone(listing['entries'][0]['size'])
        self.assertCode('invalid', files.list_folder, '~/proj', False, 'colour')

    def test_preview_kinds(self):
        (self.root / 'pic.png').write_bytes(b'\x89PNG')
        (self.root / 'blob.bin').write_bytes(b'\0\1\2')
        (self.root / 'long.txt').write_text('line\n' * 3000)
        self.assertEqual(files.preview('~/pic.png')['url'], '/mobile/api/files/raw?path=~/pic.png')
        self.assertEqual(files.preview('~/blob.bin')['reason'], 'binary')
        long = files.preview('~/long.txt')
        self.assertEqual((len(long['lines']), long['truncated']), (2000, True))
        self.assertEqual(files.preview('~/proj')['reason'], 'folder')

    def test_raw_download_headers(self):
        (self.root / 'proj' / 'naïve file.txt').write_text('abc')
        handler = FakeHandler(headers={})
        files.send_raw(handler, '~/proj/naïve file.txt', True, self.root)
        self.assertEqual(handler.wfile.getvalue(), b'abc')
        self.assertIn("filename*=UTF-8''na%C3%AFve%20file.txt", handler.sent['Content-Disposition'])
        self.assertEqual(handler.sent['Content-Security-Policy'], 'sandbox')
        handler = FakeHandler(headers={'Range': 'bytes=1-'})
        files.send_raw(handler, '~/proj/naïve file.txt', False, self.root)
        self.assertEqual((handler.status, handler.wfile.getvalue(), handler.sent['Content-Disposition']), (206, b'bc', 'inline'))
        self.assertEqual(handler.sent['Content-Type'], 'text/plain; charset=utf-8')

    def test_search_limits(self):
        for number in range(30):
            (self.root / 'proj' / ('match-%02d.txt' % number)).write_text('')
        (self.root / 'proj' / 'node_modules').mkdir()
        (self.root / 'proj' / 'node_modules' / 'match-deep.txt').write_text('')
        with mock.patch.object(files, 'SEARCH_HITS', 10):
            result = files.search('~', 'MATCH')
        self.assertEqual((len(result['hits']), result['truncated']), (10, True))
        result = files.search('~', 'match')
        self.assertNotIn('match-deep.txt', [hit['name'] for hit in result['hits']])
        self.assertEqual(result['hits'][0]['path'], '~/proj/match-00.txt')
        self.assertCode('invalid', files.search, '~', '')

    def test_places(self):
        prefs_sync.save({'files.pinned': ['~/proj', '~/gone', '~/.ssh', '~/../elsewhere']})
        rows = [{'session': '%1', 'name': 'one', 'agent': 'claude', 'cwd': str(self.root / 'proj')},
                {'session': '%2', 'name': 'two', 'agent': 'codex', 'cwd': str(self.outside)},
                {'session': '%3', 'name': 'three', 'agent': 'shell', 'cwd': str(self.root / '.ssh')}]
        result = files.places(rows)
        self.assertEqual(result['pinned'], [{'path': '~/proj', 'name': 'proj', 'exists': True},
                                            {'path': '~/gone', 'name': 'gone', 'exists': False}])
        self.assertEqual(result['sessions'], [{'session': '%1', 'name': 'one', 'agent': 'claude', 'cwd': '~/proj'}])
        self.assertGreater(result['disk']['total'], 0)
        with self.assertRaises(ValueError):
            prefs_sync.save({'files.pinned': [1]})

    @unittest.skipUnless(shutil.which('git'), 'git is not installed')
    def test_git_badges(self):
        repo = self.root / 'proj'
        run = lambda *args: subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(repo), *args],
                                           check=True, capture_output=True)
        run('init', '-q')
        run('add', 'app.py', 'sub/notes.md')
        run('commit', '-qm', 'one')
        (repo / 'app.py').write_text('print(2)\n')
        (repo / 'new.py').write_text('x')
        (repo / 'staged.py').write_text('y')
        run('add', 'staged.py')
        (repo / 'fresh').mkdir()
        (repo / 'fresh' / 'f.txt').write_text('z')
        badges = {entry['name']: entry['git'] for entry in files.list_folder('~/proj')['entries']}
        self.assertEqual(badges, {'app.py': 'M', 'new.py': '?', 'staged.py': 'A', 'fresh': '?', 'sub': None})
        (repo / 'sub' / 'notes.md').write_text('changed')
        self.assertEqual(files.list_folder('~/proj')['entries'][1]['git'], 'M')  # sub, after fresh
        self.assertEqual(files.list_folder('~/proj')['repo'], '~/proj')
        self.assertEqual(files.list_folder('~/proj/sub')['entries'][0]['git'], 'M')
        info = files.stat_path('~/proj/app.py')['git']
        self.assertEqual((info['repo'], info['state']), ('~/proj', 'M'))
        self.assertIsNone(files.stat_path('~/proj')['git']['state'])
        self.assertIsNone(files.list_folder('~')['repo'])

    def test_post_routes_answer_json_errors(self):
        handler = FakeHandler(headers={'Content-Length': '30'}, body=json.dumps({'paths': ['~/nope']}).encode().ljust(30))
        files.serve_post(handler, '/mobile/api/files/delete')
        self.assertEqual((handler.status, handler.answer['code']), (404, 'missing'))
        body = json.dumps({'on': True}).encode()
        handler = FakeHandler(headers={'Content-Length': str(len(body))}, body=body)
        files.serve_post(handler, '/mobile/api/files/secrets')
        self.assertEqual(handler.answer, {'on': True, 'expires_at': self.now + 600})


if __name__ == '__main__':
    unittest.main()
