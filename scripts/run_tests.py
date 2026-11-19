#!/usr/bin/env python3
"""Run a folder's unittest suite. With --label-flaky, say which failures are flaky (B206).

  python3 scripts/run_tests.py server                 the same tests as `cd server && python3 -m unittest discover -q`
  python3 scripts/run_tests.py server --label-flaky   also run each failed test once more, alone, in a new process

The second run only labels a failure in the report:
  flaky          the test failed in the full run and passed alone. It depends on timing, load or another test.
  failed         the test failed both times.
  skipped alone  the rerun reported a skip or ran zero tests.
The second run never changes the result. The exit code is 1 whenever a test failed in the full run. A flaky test is
a failure: fix its cause, or delete it and give the reason in the commit message (AGENTS.md, "Flaky tests")."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import unittest

RERUN_SECONDS = 600
RERUN_SKIPPED = 5  # Child exit code for a skipped or empty result.


def test_id(test):
    """The dotted name of a test (the parent for a subtest), or None for an import, setUpClass or setUpModule error."""
    if isinstance(test, unittest.case._SubTest):
        test = test.test_case
    if not isinstance(test, unittest.TestCase) or type(test).__module__.startswith('unittest.'):
        return None
    return test.id()


def first_run(folder, pattern, verbosity, stream):
    """Run the suite in this process. [(name, test id or None)] for each failure, error and unexpected success."""
    os.chdir(folder)
    sys.path.insert(0, str(folder))
    suite = unittest.defaultTestLoader.discover('.', pattern=pattern)
    result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
    found, seen = [], set()
    for test in [test for test, _ in result.failures + result.errors] + list(result.unexpectedSuccesses):
        target = test_id(test)
        name = target or str(test)
        if name not in seen:
            seen.add(name)
            found.append((name, target))
    return result.testsRun, found


def rerun_exit_code(name):
    """Run one named test in the child and classify its unittest result."""
    sys.path.insert(0, os.getcwd())
    sys.argv[1:] = ['-q', name]
    result = unittest.main(module=None, exit=False).result
    if not result.wasSuccessful():
        return 1
    if result.skipped or result.testsRun == 0:
        return RERUN_SKIPPED
    return 0


def rerun(folder, name):
    """Return the label for one test rerun in a fresh process."""
    try:
        command = [sys.executable, str(Path(__file__).resolve()), '--rerun-one', name]
        ran = subprocess.run(command, cwd=folder, capture_output=True,
                             timeout=RERUN_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return 'failed'
    return {0: 'flaky', RERUN_SKIPPED: 'skipped alone'}.get(ran.returncode, 'failed')


def report(found, labels):
    lines = ['', 'Flaky-test report: each named test that failed in the full run was rerun alone.']
    for name, _ in found:
        label = labels[name]
        note = {'flaky': 'failed in the full run, passed alone', 'failed': 'failed in the full run and alone',
                'skipped alone': 'failed in the full run, rerun reported a skip or ran zero tests',
                'not rerun': 'not one test (import, setUpClass or setUpModule): fix it first'}[label]
        lines.append(f'  {label:<13}  {name}  ({note})')
    flaky = sum(label == 'flaky' for label in labels.values())
    lines.append(f'Result: FAILED. {len(found)} failed in the full run; {flaky} of them flaky. '
                 'A flaky test is a failure: fix its cause, or delete it with a reason in the commit message.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folder', help='the folder to discover tests in, for example server or scripts')
    parser.add_argument('--label-flaky', action='store_true',
                        help='rerun each failed test alone to report whether it passed, failed or skipped')
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        parser.exit(2, f'{args.folder} is not a folder.\n')
    ran, found = first_run(folder, args.pattern, 2 if args.verbose else 0, sys.stderr)
    if not found:
        return 0 if ran else 5  # like unittest: no tests ran is not a pass
    if args.label_flaky:
        labels = {name: 'not rerun' if target is None else rerun(folder, target) for name, target in found}
        print(report(found, labels), file=sys.stderr)
    return 1


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--rerun-one':
        sys.exit(rerun_exit_code(sys.argv[2]))
    sys.exit(main())
