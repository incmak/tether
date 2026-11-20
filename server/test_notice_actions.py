"""Notice chips use the same regex data in Python and the UI."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = json.loads((ROOT / 'ui/src/noticeActions.json').read_text())
FIXTURES = json.loads((ROOT / 'server/fixtures/notice_strings.json').read_text())


class NoticeActionsTest(unittest.TestCase):
    def test_real_notices(self):
        matched = set()
        for fixture in FIXTURES:
            with self.subTest(note=fixture['note'], agent=fixture['agent']):
                matches = [row for row in PATTERNS if fixture['agent'] in row['agents']
                           and re.search(row['pattern'], fixture['note'], re.I if row['flags'] == 'i' else 0)]
                self.assertLessEqual(len(matches), 1)
                self.assertEqual(matches[0]['command'] if matches else None, fixture['command'])
                matched.update(row['command'] for row in matches)
        self.assertEqual(matched, {row['command'] for row in PATTERNS})

    def test_pattern_contract(self):
        for row in PATTERNS:
            with self.subTest(command=row['command']):
                self.assertIn(row['flags'], ('', 'i'))
                self.assertRegex(row['command'], r'^/[a-z-]+(?: [a-z]+)?$')
                self.assertTrue(row['label'])
                self.assertTrue(row['hint'])
                if row['command'] in ('/clear', '/compact', '/rewind'):
                    self.assertTrue(row['confirm'])
                self.assertNotIn(row['command'], ('/login', '/usage-credits'))


if __name__ == '__main__':
    unittest.main()
