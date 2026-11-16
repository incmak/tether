import tempfile
import unittest
from pathlib import Path
from unittest import mock

import prefs_sync


class PrefsHostsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(prefs_sync, 'FILE', Path(self.folder.name) / 'prefs.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)

    def test_hosts_round_trip(self):
        hosts = [{'name': 'MacBook Pro M5', 'url': 'https://m5.example.com'}]
        stored = prefs_sync.save({'hosts': hosts})
        self.assertEqual(stored['prefs']['hosts'], hosts)
        self.assertEqual(prefs_sync.load(), stored)

    def test_host_origins_for_frame_ancestors(self):
        prefs_sync.save({'hosts': [{'name': 'Mac', 'url': 'https://mac.example.com/'}, {'name': 'Box', 'url': 'http://10.0.0.2:7683'},
                                   {'name': 'Upper', 'url': 'https://Mac.Example.com'}]})
        # Mixed case is not a plain origin: it never reaches a CSP header.
        self.assertEqual(prefs_sync.host_origins(), ['https://mac.example.com', 'http://10.0.0.2:7683'])
        prefs_sync.save({'hosts': []})
        self.assertEqual(prefs_sync.host_origins(), [])

    def test_empty_hosts_replace_previous_list(self):
        prefs_sync.save({'hosts': [{'name': 'Host', 'url': 'https://x'}]})
        stored = prefs_sync.save({'hosts': []})
        self.assertEqual(stored['prefs']['hosts'], [])
        self.assertEqual(prefs_sync.load(), stored)

    def test_rejects_invalid_hosts(self):
        bad = [
            [{'name': 'Host', 'url': 'https://x%s' % index} for index in range(21)],
            [{'name': 'Host', 'url': 'https://x', 'extra': True}],
        ]
        bad.extend([{'name': name, 'url': 'https://x'}] for name in ('', ' ', 'n' * 41, 'Host\n', 'Host\x7f'))
        bad.extend([{'name': 'Host', 'url': url}] for url in (
            'javascript:alert(1)', 'ftp://x', 'https://', 'https://u:p@x',
            'https://x/chat/', 'https://x?a=1', 'https://x#a', 'https://x:99999',
            'https://' + 'x' * 193,
        ))
        bad.extend([
            [{'name': 'One', 'url': 'https://x'}, {'name': 'Two', 'url': 'https://x/'}],
            [None],
        ])
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r'^Invalid preference: hosts\.$'):
                    prefs_sync.save({'hosts': value})
        self.assertEqual(prefs_sync.load()['version'], 0)


if __name__ == '__main__':
    unittest.main()
