"""B206: run_tests.py labels a failure as flaky or failed, and never turns it into a pass. Real runs in temp folders."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().with_name('run_tests.py')

PASSES = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n'
# Fails on its first run and passes on the next: the marker file plays the part of load or timing.
FLAKY = '''import unittest
from pathlib import Path

class T(unittest.TestCase):
    def test_sometimes(self):
        marker = Path(__file__).with_name('ran-once')
        first = not marker.exists()
        marker.write_text('x')
        self.assertFalse(first, 'first run')

    def test_error_then_ok(self):
        marker = Path(__file__).with_name('raised-once')
        if not marker.exists():
            marker.write_text('x')
            raise RuntimeError('first run')
'''
ALWAYS = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_never(self):\n        self.fail("always")\n'
SUBTESTS = '''import unittest
from pathlib import Path

class T(unittest.TestCase):
    def test_subtests(self):
        marker = Path(__file__).with_name('runs')
        first = not marker.exists()
        with marker.open('a') as log:
            log.write('run\\n')
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertFalse(first or {always})
'''
SETUP = ('import unittest\n\nclass T(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n'
         '        raise RuntimeError("no setup")\n\n    def test_x(self):\n        pass\n')
SURPRISE = ('import unittest\n\nclass T(unittest.TestCase):\n    @unittest.expectedFailure\n'
            '    def test_surprise(self):\n        pass\n')


class RunTests(unittest.TestCase):
    def run_folder(self, files, *options, folder=None):
        if folder is None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            folder = Path(temp.name)
        for name, text in files.items():
            (folder / name).write_text(text)
        ran = subprocess.run([sys.executable, str(RUNNER), str(folder), *options], capture_output=True, text=True,
                             timeout=120)
        return ran.returncode, ran.stderr

    def test_all_pass(self):
        code, out = self.run_folder({'test_ok.py': PASSES}, '--label-flaky')
        self.assertEqual(code, 0, out)
        self.assertNotIn('Flaky-test report', out)

    def test_flaky_is_labelled_and_still_fails(self):
        code, out = self.run_folder({'test_ok.py': PASSES, 'test_flaky.py': FLAKY}, '--label-flaky')
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r'flaky\s+test_flaky\.T\.test_sometimes\s+\(failed in the full run, passed alone\)')
        self.assertRegex(out, r'flaky\s+test_flaky\.T\.test_error_then_ok')
        self.assertIn('Result: FAILED. 2 failed in the full run; 2 of them flaky.', out)

    def test_a_real_failure_is_labelled_failed(self):
        code, out = self.run_folder({'test_always.py': ALWAYS, 'test_flaky.py': FLAKY}, '--label-flaky')
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r'failed\s+test_always\.T\.test_never\s+\(failed in the full run and alone\)')
        self.assertIn('Result: FAILED. 3 failed in the full run; 2 of them flaky.', out)

    def assert_subtest_label(self, *, always, label):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            code, out = self.run_folder({'test_subtests.py': SUBTESTS.format(always=always)},
                                        '--label-flaky', folder=folder)
            self.assertEqual(code, 1, out)
            self.assertIn('FAILED (failures=2)', out)
            report = out.split('Flaky-test report:', 1)[1]
            self.assertRegex(report, rf'(?m)^  {label} +test_subtests\.T\.test_subtests  \(')
            self.assertEqual(report.count('test_subtests.T.test_subtests'), 1, report)
            self.assertIn('Result: FAILED. 1 failed in the full run;', report)
            self.assertEqual((folder / 'runs').read_text().splitlines(), ['run', 'run'])

    def test_always_failing_subtests_are_rerun_once_and_labelled_failed(self):
        self.assert_subtest_label(always=True, label='failed')

    def test_first_run_subtests_are_rerun_once_and_labelled_flaky(self):
        self.assert_subtest_label(always=False, label='flaky')

    def test_without_the_flag_nothing_runs_again(self):
        code, out = self.run_folder({'test_flaky.py': FLAKY})
        self.assertEqual(code, 1, out)
        self.assertNotIn('Flaky-test report', out)
        self.assertIn('FAILED (failures=1, errors=1)', out)

    def test_setup_and_import_failures_are_not_rerun(self):
        code, out = self.run_folder({'test_setup.py': SETUP, 'test_import.py': 'import no_such_module_b206\n'},
                                    '--label-flaky')
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r'not rerun\s+setUpClass \(test_setup\.T\)')
        self.assertRegex(out, r'not rerun\s+.*test_import')
        self.assertIn('2 failed in the full run; 0 of them flaky.', out)

    def test_unexpected_success_and_no_tests_fail(self):
        code, out = self.run_folder({'test_surprise.py': SURPRISE}, '--label-flaky')
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r'failed\s+test_surprise\.T\.test_surprise')
        self.assertEqual(self.run_folder({'notes.txt': 'x'}, '--label-flaky')[0], 5)
        self.assertEqual(subprocess.run([sys.executable, str(RUNNER), '/nonexistent/folder'], capture_output=True).returncode, 2)


if __name__ == '__main__':
    unittest.main()
