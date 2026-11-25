import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agents


class CodexNamesTests(unittest.TestCase):
    def test_last_name_wins_and_file_changes_are_read(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(agents.paths, 'CODEX', Path(folder)):
            agents._codex_names.update(key=None, names={})
            index = Path(folder) / 'session_index.jsonl'
            self.assertEqual(agents.codex_names(), {})
            index.write_text('\n'.join([json.dumps({'id': 'a', 'thread_name': 'First  name'}), 'not json',
                                        json.dumps({'id': 'b', 'thread_name': 'Other'}), json.dumps({'id': 'a', 'thread_name': 'Renamed'})]) + '\n')
            self.assertEqual({key: value[0] for key, value in agents.codex_names().items()}, {'a': 'Renamed', 'b': 'Other'})
            with index.open('a') as lines:
                lines.write(json.dumps({'id': 'b', 'thread_name': ' Fix   the build '}) + '\n')
                lines.write(json.dumps({'id': 'c', 'thread_name': 'Timed', 'updated_at': '2026-11-01T14:12:08.30724454Z'}) + '\n')
            names = agents.codex_names()
            self.assertEqual(names['b'], ('Fix the build', 0))
            self.assertEqual(names['c'], ('Timed', 1793542328307))

    def test_iso_ms(self):
        self.assertEqual(agents.iso_ms('2026-11-01T14:12:08Z'), 1793542328000)
        self.assertEqual(agents.iso_ms('2026-11-01T16:12:08.5+02:00'), 1793542328500)
        self.assertEqual((agents.iso_ms(None), agents.iso_ms('soon')), (0, 0))


if __name__ == '__main__':
    unittest.main()
