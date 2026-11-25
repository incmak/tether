"""Check that standalone unittest modules isolate their fixtures from the account home."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_IMPORT = 'import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)'
STANDALONE_MODULES = (
    ('server', 'test_claude_usage'),
    ('server', 'test_media_files'),
    ('scripts', 'test_deploy_ui'),
)
LOADER_DRIVER = """
import json
import os
import sys
import tempfile
import unittest

suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])
print(json.dumps({'HOME': os.environ['HOME'], 'TMPDIR': os.environ['TMPDIR'],
                  'tempfile.gettempdir()': tempfile.gettempdir()}), flush=True)
result = unittest.TextTestRunner(verbosity=0).run(suite)
sys.exit(not result.wasSuccessful())
"""


class UnittestIsolationTest(unittest.TestCase):
    def test_bootstrap_is_first_in_every_test_module(self):
        expected = ast.dump(ast.parse(BOOTSTRAP_IMPORT).body[0])
        for folder in ('server', 'scripts'):
            for path in sorted((ROOT / folder).glob('test_*.py')):
                with self.subTest(path=path.relative_to(ROOT)):
                    tree = ast.parse(path.read_bytes(), filename=str(path))
                    index = int(ast.get_docstring(tree) is not None)
                    while index < len(tree.body):
                        node = tree.body[index]
                        if not isinstance(node, ast.ImportFrom) or node.module != '__future__':
                            break
                        index += 1
                    first = ast.dump(tree.body[index]) if index < len(tree.body) else None
                    self.assertEqual(first, expected, f'{path.relative_to(ROOT)}: add {BOOTSTRAP_IMPORT}')

    def run_with_fake_home(self, folder, *args):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            tmp = home / 'tmp'
            tmp.mkdir()
            result = subprocess.run(
                [sys.executable, *args], cwd=ROOT / folder,
                env={'PATH': os.environ['PATH'], 'HOME': str(home), 'TMPDIR': str(tmp)},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(sorted(home.iterdir()), [tmp])
            self.assertEqual(list(tmp.iterdir()), [])
            return home, result.stdout

    def test_standalone_runs_leave_account_home_empty(self):
        for folder, module in STANDALONE_MODULES:
            with self.subTest(folder=folder, module=module):
                self.run_with_fake_home(folder, '-m', 'unittest', '-q', module)

    def test_loader_isolates_before_tests_run(self):
        for folder, module in STANDALONE_MODULES:
            with self.subTest(folder=folder, module=module):
                home, stdout = self.run_with_fake_home(folder, '-c', LOADER_DRIVER, module)
                paths = json.loads(stdout.splitlines()[0])
                inside_home = {name: path for name, path in paths.items()
                               if Path(path).resolve().is_relative_to(home)}
                self.assertEqual(inside_home, {}, 'Paths under the fake home before tests ran')
