"""Recover backlog changes from a CLI that cannot write to the audit folder."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import agent_audit_cli
import agent_backlog_cli as cli
import audit
import backlog
import paths


INITIAL = '## Build\n- [ ] Fix warnings #B12\n- [x] Old fix #B13\n'
SOURCE = 'codex · tmux $3 · pane %17 · chat 019a'
AGENT = {'source': 'agent-cli', 'pane': '%17'}
UNKNOWN = {'source': 'unknown'}


class BacklogObservedTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(paths, 'TETHER', root / 'tether'))
        self.enterContext(mock.patch.object(paths, 'CONFIG', root / 'config'))
        self.enterContext(mock.patch.object(audit, '_tab_name', return_value=None))
        backlog.folder().mkdir(parents=True)
        self.file = backlog.folder() / 'BACKLOG.md'
        self.file.write_text(INITIAL)
        self.snapshot = audit.folder() / 'backlog-seen.md'

    @contextlib.contextmanager
    def sandbox(self, quiet=True):
        """A Codex sandbox mounts the audit folder read-only: the CLI changes the list and records nothing."""
        folder = audit.folder()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o500)
        try:
            with contextlib.redirect_stderr(io.StringIO()) as warnings:
                yield
        finally:
            folder.chmod(0o700)
        if quiet:
            self.assertEqual(warnings.getvalue(), '')  # no "could not record" noise in the sandboxed tab

    def entries(self):
        return audit.query(kinds=['backlog'])['entries']

    def assert_observed(self, entry, kind, summary, actor):
        self.assertEqual((entry['kind'], entry['summary'], entry['actor']), (kind, summary, actor))
        self.assertIs(entry['detail']['observed'], True)

    def cli_add(self, title):
        with mock.patch.object(cli, 'source', return_value=SOURCE), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['add', '--project', 'Build', '--agent', 'codex', title]), 0)

    def test_first_observe_seeds_snapshot_without_recording_history(self):
        backlog.observe()
        self.assertEqual(self.snapshot.read_text(), INITIAL)
        self.assertEqual(self.entries(), [])

    def test_sandboxed_cli_add_is_observed_once_with_source_pane(self):
        backlog.observe()
        with self.sandbox():
            self.cli_add('Add checks')
        self.assertIn('Add checks', self.file.read_text())
        self.assertEqual(self.snapshot.read_text(), INITIAL)
        self.assertEqual(self.entries(), [])

        backlog.observe()
        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Add checks', AGENT)
        self.assertEqual(entry['project'], 'Build')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(agent_audit_cli.main(['list', '--json']), 0)
        self.assertEqual(json.loads(out.getvalue())['entries'], [entry])

    def test_start_then_done_before_observe_coalesces_to_done(self):
        backlog.observe()
        with self.sandbox():
            backlog.start('B12', 'codex · pane %18')
            backlog.mark_done('B12', result='Fixed', who=SOURCE)

        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.done', 'B12 done: Fix warnings', AGENT)

    def test_add_then_start_before_observe_records_both(self):
        backlog.observe()
        with self.sandbox():
            self.cli_add('Add checks')
            backlog.start('B14', 'codex · pane %18')

        backlog.observe()

        added, started = sorted(self.entries(), key=lambda entry: entry['kind'])  # same millisecond: any order
        self.assert_observed(added, 'backlog.add', 'B14 added: Add checks', AGENT)
        self.assert_observed(started, 'backlog.start', 'B14 started: Add checks', {'source': 'agent-cli', 'pane': '%18'})

    def test_observe_between_start_and_done_records_each_with_its_pane(self):
        backlog.observe()
        with self.sandbox():
            backlog.start('B12', 'codex · pane %18')
        backlog.observe()
        with self.sandbox():
            backlog.mark_done('B12', result='Fixed', who=SOURCE)
        backlog.observe()

        found = self.entries()
        self.assertEqual(len(found), 2)
        entries = {entry['kind']: entry for entry in found}
        self.assert_observed(entries['backlog.start'], 'backlog.start', 'B12 started: Fix warnings',
                             {'source': 'agent-cli', 'pane': '%18'})
        self.assert_observed(entries['backlog.done'], 'backlog.done', 'B12 done: Fix warnings', AGENT)

    def test_unsandboxed_cli_catches_up_before_its_own_write(self):
        backlog.observe()
        with self.sandbox():
            self.cli_add('Sandboxed')
        with audit.acting('agent-cli', pane='%19'):
            self.cli_add('Unsandboxed')

        entries = sorted(self.entries(), key=lambda entry: entry['summary'])
        self.assertEqual(len(entries), 2)
        self.assert_observed(entries[0], 'backlog.add', 'B14 added: Sandboxed', AGENT)
        self.assertEqual((entries[1]['kind'], entries[1]['summary'], entries[1]['actor']),
                         ('backlog.add', 'B15 added: Unsandboxed', {'source': 'agent-cli', 'pane': '%19'}))
        self.assertNotIn('detail', entries[1])
        backlog.observe()
        self.assertCountEqual(self.entries(), entries)

    def test_unsandboxed_add_does_not_repeat_on_observe(self):
        self.cli_add('Add checks')
        entry, = self.entries()
        self.assertNotIn('detail', entry)
        backlog.observe()
        self.assertEqual(self.entries(), [entry])

    def test_silent_write_updates_the_snapshot(self):
        backlog.observe()
        backlog.add_detail('B12', 'Doing: codex · pane %18')
        self.assertEqual(self.snapshot.read_text(), self.file.read_text())

        backlog.observe()

        self.assertEqual(self.entries(), [])

    def test_sandboxed_archive_ignores_items_already_done(self):
        backlog.observe()
        with self.sandbox():
            self.assertEqual(backlog.archive_done()[0], 1)

        backlog.observe()

        self.assertIn('#B13', (backlog.folder() / 'DONE.md').read_text())
        self.assertNotIn('#B13', self.file.read_text())
        self.assertEqual(self.entries(), [])

    def test_sandboxed_save_records_item_done_and_archived_in_one_write(self):
        backlog.observe()
        state = backlog.read()
        edited = state['text'].replace('- [ ] Fix warnings #B12',
                                       '- [x] Fix warnings #B12\n\n  - Doing: codex · pane %17')
        with (self.sandbox(), mock.patch.object(backlog, 'ARCHIVE_AT', 1),
              mock.patch.object(backlog, 'ARCHIVE_TO', 0)):
            backlog.save(edited, state['version'])

        backlog.observe()

        self.assertNotIn('#B12', self.file.read_text())
        self.assertIn('#B12', (backlog.folder() / 'DONE.md').read_text())
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.done', 'B12 done: Fix warnings', AGENT)
        self.assertEqual(entry['project'], 'Build')

    def test_direct_edit_removal_has_unknown_actor(self):
        backlog.observe()
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', ''))

        with audit.acting('ui', device='Chrome on Mac'):
            backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.remove', 'B12 removed: Fix warnings', UNKNOWN)

    def test_observation_with_a_failed_snapshot_write_can_seed_later(self):
        with self.sandbox(quiet=False):
            backlog.observe()
        self.assertFalse(self.snapshot.exists())

        backlog.observe()

        self.assertEqual(self.snapshot.read_text(), INITIAL)
        self.assertEqual(self.entries(), [])

    def test_unchanged_file_skips_lock(self):
        backlog.observe()
        with mock.patch.object(backlog, '_locked', wraps=backlog._locked) as lock:
            backlog.observe()
        lock.assert_not_called()

    def test_watch_makes_one_pass_if_already_stopped(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(backlog, 'observe') as observe:
            backlog.watch(stop)
        observe.assert_called_once_with()
