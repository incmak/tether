import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents


class ClaudeModelTests(unittest.TestCase):
    """The model chip uses Claude's status-line JSON, not the status-line text."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        patcher = patch.object(agents, 'METRICS', self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, thread, payload):
        (self.folder / (thread + '.json')).write_text(json.dumps(payload))

    def test_display_name_then_id(self):
        self.write('a-1', {'model': {'id': 'claude-opus-5-5', 'display_name': 'Opus 5.5'}})
        self.write('b-2', {'model': {'id': 'claude-sonnet-5-5'}})
        self.assertEqual(agents.claude_model('a-1'), 'Opus 5.5')
        self.assertEqual(agents.claude_model('b-2'), 'claude-sonnet-5-5')

    def test_missing_or_bad_data_is_empty(self):
        self.write('c-3', {'model': 'odd'})
        (self.folder / 'd-4.json').write_text('{not json')
        for thread in ('c-3', 'd-4', 'none', None, '', '../x'):
            self.assertEqual(agents.claude_model(thread), '')


if __name__ == '__main__':
    unittest.main()
