"""Run the status-line recorder with isolated homes and caches."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.env = dict(os.environ, HOME=self.temp.name, MOBILE_TMUX_CACHE=self.temp.name)

    def run_recorder(self, raw, *args):
        return subprocess.run([sys.executable, str(Path(__file__).with_name('claude_usage.py')), *args],
                              input=raw, capture_output=True, env=self.env, timeout=15)

    def test_then_preserves_stdin_and_captures_model(self):
        raw = b'{"session_id":"s-1","model":{"display_name":"Opus"}}'
        result = self.run_recorder(raw, '--then', 'cat')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, raw)
        saved = json.loads((self.cache / 'usage/s-1.json').read_text())
        self.assertEqual(saved['model'], {'display_name': 'Opus'})

    def test_then_returns_command_exit_code(self):
        result = self.run_recorder(b'{}', '--then', 'exit 3')
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_then_runs_after_invalid_json(self):
        result = self.run_recorder(b'invalid JSON', '--then', 'printf ok')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'ok')

    def test_no_args_captures_without_output(self):
        raw = b'{"session_id":"s-1","model":{"display_name":"Opus"}}'
        result = self.run_recorder(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertTrue((self.cache / 'usage/s-1.json').exists())


if __name__ == '__main__':
    unittest.main()
