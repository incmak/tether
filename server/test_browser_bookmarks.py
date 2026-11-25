"""Bookmark storage and HTTP routes, isolated from both browser instances."""
import unittest_isolated  # noqa: F401
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import browser_api
import browser_bookmarks as bookmarks
import paths
from test_browser import FakeHandler


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(paths, 'CONFIG', Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.file = paths.private_file('browser-bookmarks.json')

    def test_add_list_dedupe_and_remove(self):
        self.assertEqual(bookmarks.list('test'), [])
        self.assertFalse(self.file.exists())
        with patch.object(bookmarks.time, 'time', return_value=1234.5):
            first, = bookmarks.add('test', 'https://example.test/', 'Example')
            second, _ = bookmarks.add('test', 'https://example.test/#anchor', 'Anchor')
        self.assertRegex(first['id'], r'^[0-9a-f]{16}$')
        self.assertEqual(first['added'], 1234)
        self.assertEqual(bookmarks.list('test'), [second, first])
        updated = dict(first, title='Updated')
        self.assertEqual(bookmarks.add('test', first['url'], 'Updated'), [second, updated])
        self.assertEqual(bookmarks.list('test'), [second, updated])
        self.assertEqual(bookmarks.remove('test', first['id']), [second])
        self.assertEqual(bookmarks.remove('test', first['id']), [second])

    def test_url_validation(self):
        for url in ('javascript:alert(1)', 'data:text/html,test', 'about:blank', 'ftp://example.test',
                    'https:///path', 'http://:80/path', 'http://[broken', 123, None):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'Only http and https pages can be bookmarked.'):
                bookmarks.add('test', url)
        prefix = 'https://example.test/'
        url = prefix.ljust(2048, 'x')
        self.assertEqual(bookmarks.add('test', url)[0]['url'], url)
        with self.assertRaisesRegex(ValueError, 'This address is too long to bookmark.'):
            bookmarks.add('test', url + 'x')

    def test_title_trims_collapses_and_falls_back(self):
        self.assertEqual(bookmarks.add('test', 'http://example.test/page', ' \tA\n nice\u00a0 title  ')[0]['title'], 'A nice title')
        self.assertEqual(bookmarks.add('test', 'https://example.test:444/page', ' \t ')[0]['title'], 'example.test')
        self.assertEqual(bookmarks.add('test', 'https://other.test/page')[0]['title'], 'other.test')
        self.assertEqual(bookmarks.add('test', 'https://other.test/page', 'x' * 301)[0]['title'], 'x' * 300)
        with self.assertRaises(ValueError):
            bookmarks.add('test', 'https://example.test/page', ['title'])

    def test_limit_allows_update_and_keeps_other_browser_separate(self):
        rows = [{'id': f'{index:016x}', 'url': f'https://example.test/{index}', 'title': 'Example', 'added': index}
                for index in reversed(range(499))]
        self.file.write_text(json.dumps({'test': rows, 'signed': []}), encoding='utf-8')
        full = bookmarks.add('test', 'https://example.test/499')
        self.assertEqual(len(full), 500)
        self.assertEqual(bookmarks.add('test', full[-1]['url'], 'Updated'), [*full[:-1], dict(full[-1], title='Updated')])
        with self.assertRaisesRegex(ValueError, 'You have 500 bookmarks. Remove one first.'):
            bookmarks.add('test', 'https://example.test/500')
        signed, = bookmarks.add('signed', full[0]['url'], 'Signed')
        self.assertNotEqual(signed['id'], full[0]['id'])
        self.assertEqual(bookmarks.list('test')[0], full[0])
        self.assertEqual(bookmarks.remove('test', signed['id'])[0], full[0])
        self.assertEqual(bookmarks.list('signed'), [signed])

    def test_corrupt_file_is_quarantined_and_recovered(self):
        bad = self.file.with_suffix('.json.bad')
        for content in (b'\xff', b'{invalid', b'{"test":[{}],"signed":[]}'):
            with self.subTest(content=content):
                self.file.write_bytes(content)
                self.assertEqual(bookmarks.list('test'), [])
                self.assertEqual(bookmarks.list('signed'), [])
                self.assertFalse(self.file.exists())
                self.assertEqual(bad.read_bytes(), content)
        saved = bookmarks.add('signed', 'https://example.test/')
        self.assertEqual(bookmarks.list('signed'), saved)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertTrue(bad.exists())

    def test_concurrent_writes_keep_all_rows(self):
        urls = [f'https://example.test/{index}' for index in range(20)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = pool.map(lambda url: bookmarks.add('test', url), urls)
            self.assertEqual(len(tuple(results)), len(urls))
        self.assertCountEqual([row['url'] for row in bookmarks.list('test')], urls)

    def request(self, name, body=None):
        class UnusableBrowser:
            def __bool__(self):
                raise AssertionError('Bookmark routes must not select a browser')

            def __getattr__(self, name):
                raise AssertionError(f'Bookmark routes must not call {name}')

        handler = FakeHandler(browser_api.PREFIX + name, body)
        call = browser_api.get if body is None else browser_api.post
        with patch.object(browser_api, '_select', side_effect=AssertionError('Bookmark routes must bypass _select')):
            call(handler, UnusableBrowser(), signed_browser=UnusableBrowser())
        reply, = handler.replies
        return reply

    def test_routes_do_not_select_a_browser(self):
        self.assertEqual(self.request('bookmarks?browser=signed'), (200, {'bookmarks': [], 'browser': 'signed'}))
        status, reply = self.request('bookmarks/add', {'browser': 'signed', 'url': 'https://example.test/'})
        self.assertEqual(status, 200)
        self.assertEqual(reply['browser'], 'signed')
        self.assertEqual(self.request('bookmarks?browser=signed'), (200, reply))
        self.assertEqual(self.request('bookmarks'), (200, {'bookmarks': []}))
        ident = reply['bookmarks'][0]['id']
        self.assertEqual(self.request('bookmarks/remove?browser=signed', {'id': ident}),
                         (200, {'bookmarks': [], 'browser': 'signed'}))
        status, reply = self.request('bookmarks/add', {'url': 'about:blank'})
        self.assertEqual((status, reply), (400, {'error': 'Only http and https pages can be bookmarked.'}))

    def test_routes_refuse_unknown_browser_and_chrome(self):
        for browser in ('other', '', 'chrome', 'mac'):
            expected = {'error': 'Bookmarks are for the Test and Signed-in browsers.'}
            if browser in ('chrome', 'mac'):
                expected['browser'] = 'chrome'
            for name, body in ((f'bookmarks?browser={browser}', None),
                               (f'bookmarks/remove?browser={browser}', {'id': 'missing'}),
                               ('bookmarks/add', {'browser': browser, 'url': 'https://example.test/'})):
                with self.subTest(name=name, browser=browser):
                    self.assertEqual(self.request(name, body), (400, expected))
