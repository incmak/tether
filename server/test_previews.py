"""HTML previews: which pages and files a preview link opens, and the headers that keep the page out of Tether's origin."""
from pathlib import Path
import io
import tempfile
import unittest

import previews
import tool_details


class FakeHandler:
    def __init__(self, path):
        self.path = path
        self.command = 'GET'
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / 'home'
        self.scratch = base / 'claude-1000'
        files = {'home/proj/site/index.html': b'<h1>Hi</h1>', 'home/proj/site/app.js': b'x' * (3 * 1024 * 1024),
                 'home/proj/site/.env': b'SECRET=1', 'home/proj/site/sub/style.css': b'body{}', 'home/proj/notes.txt': b'n',
                 'home/proj/other.html': b'<p>', 'home/.ssh/page.html': b'<p>', 'claude-1000/s/scratchpad/report.html': b'<p>r',
                 'outside/page.html': b'<p>'}
        for name, data in files.items():
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_bytes(data)
        (self.home / 'proj/site/key.html').symlink_to(self.home / '.ssh/page.html')
        self.now = [1000.0]
        self.previews = previews.Previews(clock=lambda: self.now[0], home=self.home, scratch=lambda: self.scratch)
        self.base = base

    def tearDown(self):
        self.temp.cleanup()

    def issue(self, name, folder='proj'):
        return self.previews.issue(name, str(self.home / folder))

    def get(self, url):
        handler = FakeHandler(url)
        previews.serve(handler, self.previews)
        return handler

    def test_page_and_its_relative_files_load(self):
        link = self.issue('site/index.html')
        self.assertTrue(link['url'].startswith(previews.PREFIX) and link['url'].endswith('/index.html'))
        page = self.get(link['url'])
        self.assertEqual((page.status, page.wfile.getvalue()), (200, b'<h1>Hi</h1>'))
        self.assertEqual(page.headers['Content-Type'], 'text/html; charset=utf-8')
        folder = link['url'].rsplit('/', 1)[0]
        self.assertEqual(self.get(folder + '/sub/style.css').headers['Content-Type'], 'text/css; charset=utf-8')
        # A script bundle over the 2 MB text limit of the chat file viewer still loads.
        self.assertEqual(self.get(folder + '/app.js?v=2').status, 200)
        self.assertEqual(self.get(folder + '/').status, 200)

    def test_every_answer_is_sandboxed(self):
        link = self.issue('site/index.html')
        for url in (link['url'], link['url'].rsplit('/', 1)[0] + '/.env', previews.PREFIX + 'nope/index.html'):
            headers = self.get(url).headers
            self.assertTrue(headers['Content-Security-Policy'].startswith('sandbox '), url)
            self.assertNotIn('allow-same-origin', headers['Content-Security-Policy'])
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_refused_files(self):
        folder = self.issue('site/index.html')['url'].rsplit('/', 1)[0]
        for name in ('.env', '../notes.txt', '..%2Fnotes.txt', 'key.html', 'missing.html', '%00.html'):
            self.assertEqual(self.get(folder + '/' + name).status, 404, name)

    def test_only_html_pages_in_allowed_folders(self):
        for name in ('notes.txt', str(self.home / '.ssh/page.html'), str(self.base / 'outside/page.html'), 'missing.html'):
            with self.assertRaises(ValueError, msg=name):
                self.issue(name)

    def test_claude_scratchpad_pages(self):
        link = self.issue(str(self.scratch / 's/scratchpad/report.html'))
        self.assertEqual(self.get(link['url']).wfile.getvalue(), b'<p>r')
        self.previews.scratch = lambda: None
        with self.assertRaises(ValueError):
            self.issue(str(self.scratch / 's/scratchpad/report.html'))

    def test_links_expire_and_folders_share_one_token(self):
        first = self.issue('site/index.html')
        self.assertEqual(first['url'].rsplit('/', 1)[0], self.issue('site/index.html')['url'].rsplit('/', 1)[0])
        self.assertNotEqual(first['url'].rsplit('/', 1)[0], self.issue('other.html')['url'].rsplit('/', 1)[0])
        self.now[0] += previews.LIFETIME + 1
        expired = self.get(first['url'])
        self.assertEqual(expired.status, 410)
        self.assertIn(b'expired', expired.wfile.getvalue())
        self.assertEqual(self.get(previews.PREFIX).status, 410)


class ArtifactToolTests(unittest.TestCase):
    def mark(self, details, name='Artifact', result=None):
        messages = [{'tool': {'id': 't1', 'name': name, 'title': '', 'is_error': False, 'status': 'pending'}}]
        tool_details.mark_result(messages, 't1', result or {'content': 'Published'}, details=details)
        return messages[0]['tool']

    def test_published_page_gets_a_card(self):
        tool = self.mark({'url': 'https://claude.ai/artifact/2NztzX68PNVFZAxenMFTY3', 'path': '/tmp/claude-1000/a/scratchpad/plan.html', 'title': ' CP2   Decisions '})
        self.assertEqual(tool['artifact'], {'url': 'https://claude.ai/artifact/2NztzX68PNVFZAxenMFTY3',
                                            'path': '/tmp/claude-1000/a/scratchpad/plan.html', 'title': 'CP2 Decisions'})
        self.assertEqual(self.mark({'url': 'https://claude.ai/code/artifact/0b2b7011-02b0-42ec-817b-bbd70717b700', 'path': '/x/a.htm'})['artifact']['title'], 'a.htm')

    def test_other_results_get_none(self):
        for details in (None, 'text', {'quickstart': {}}, {'url': 'https://evil.example/artifact/abcdefg', 'path': '/x/a.html'},
                        {'url': 'https://claude.ai/artifact/abcdefg', 'path': '/x/a.md'}, {'url': 'javascript:alert(1)', 'path': '/x/a.html'}):
            self.assertNotIn('artifact', self.mark(details), details)
        good = {'url': 'https://claude.ai/artifact/2NztzX68PNVFZAxenMFTY3', 'path': '/x/a.html'}
        self.assertNotIn('artifact', self.mark(good, name='Write'))
        self.assertNotIn('artifact', self.mark(good, result={'content': 'failed', 'is_error': True}))


if __name__ == '__main__':
    unittest.main()
