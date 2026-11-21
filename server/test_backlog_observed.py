"""Recover backlog changes from a CLI that cannot write to the audit folder."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import fcntl
import io
import json
import os
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
        self.archive = backlog.folder() / 'DONE.md'
        self.snapshot = audit.folder() / 'backlog-seen.json'

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

    def seen(self):
        return json.loads(self.snapshot.read_text())

    def assert_observed(self, entry, kind, summary, actor):
        self.assertEqual((entry['kind'], entry['summary'], entry['actor']), (kind, summary + ' (seen in the file)', actor))
        self.assertIs(entry['detail']['observed'], True)

    def cli_add(self, title):
        with mock.patch.object(cli, 'source', return_value=SOURCE), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['add', '--project', 'Build', '--agent', 'codex', title]), 0)

    def test_first_observe_seeds_snapshot_without_recording_history(self):
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.with_name('backlog-seen.md').write_text('- [ ] From the old snapshot #B1\n')
        backlog.observe()
        self.assertEqual(self.seen(), {'text': INITIAL, 'archived': []})
        self.assertEqual(self.entries(), [])

    def test_sandboxed_cli_add_is_observed_once_with_source_pane(self):
        backlog.observe()
        with self.sandbox():
            self.cli_add('Add checks')
        self.assertIn('Add checks', self.file.read_text())
        self.assertEqual(self.seen()['text'], INITIAL)
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
            backlog.mark_done('B12', result='Fixed a warning from pane %99', who='codex · pane %17')

        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.done', 'B12 done: Fix warnings', AGENT)

    def test_result_can_mention_a_pane_without_providing_an_actor(self):
        before = INITIAL + '- [ ] Fix lint #B14\n'
        self.file.write_text(before)
        backlog.observe()
        self.file.write_text(before
                             .replace('- [ ] Fix warnings #B12\n', '- [x] Fix warnings #B12\n'
                                      '  - Done: 2026-11-21 09:00 · Fixed pane %99\n')
                             .replace('- [ ] Fix lint #B14\n', '- [x] Fix lint #B14\n'
                                      '  - Doing: codex · pane %17\n'
                                      '  - Done: 2026-11-21 09:00 · Fixed (reported by pane %99)\n'))

        backlog.observe()

        warnings, lint = sorted(self.entries(), key=lambda entry: entry['summary'])
        self.assert_observed(warnings, 'backlog.done', 'B12 done: Fix warnings', UNKNOWN)
        self.assert_observed(lint, 'backlog.done', 'B14 done: Fix lint', AGENT)

    def test_start_uses_only_a_source_value_from_the_last_doing_line(self):
        backlog.observe()
        with self.sandbox():
            backlog.start('B12', SOURCE)
            backlog.add_detail('B12', 'Doing: ask codex about pane %99')

        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.start', 'B12 started: Fix warnings', UNKNOWN)

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

    def test_snapshot_update_invalidates_the_previous_observation(self):
        backlog.observe()
        self.cli_add('Add checks')
        self.file.write_text(INITIAL)

        backlog.observe()

        added, removed = sorted(self.entries(), key=lambda entry: entry['kind'])
        self.assertEqual(added['summary'], 'B14 added: Add checks')
        self.assert_observed(removed, 'backlog.remove', 'B14 removed: Add checks', UNKNOWN)

    def test_silent_write_updates_the_snapshot(self):
        backlog.observe()
        backlog.add_detail('B12', 'Doing: codex · pane %18')
        self.assertEqual(self.seen()['text'], self.file.read_text())

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

    def test_same_size_edit_with_old_mtime_is_observed(self):
        backlog.observe()
        before = self.file.stat()
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings', '- [x] Fix warnings'))
        os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.file.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.done', 'B12 done: Fix warnings', UNKNOWN)

    def test_unchecked_archive_item_does_not_explain_a_removal(self):
        backlog.observe()
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', ''))
        self.archive.write_text('## Build\n- [ ] Fix warnings #B12\n')

        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.remove', 'B12 removed: Fix warnings', UNKNOWN)
        self.assertEqual(self.seen()['archived'], [])

    def test_add_done_and_archive_without_a_net_change_to_backlog(self):
        backlog.archive_done()
        before = self.file.read_text()
        backlog.observe()
        self.assertEqual(self.seen()['archived'], ['B13'])

        with self.archive.open('a') as archive:
            archive.write('  - Added detail to an old item\n')
        backlog.observe()
        self.assertEqual(self.entries(), [])  # the archived ref was already seen

        with self.sandbox():
            self.cli_add('More checks')
            backlog.mark_done('B14', result='Fixed', who=SOURCE)
            backlog.archive_done()

        self.assertEqual(self.file.read_text(), before)
        backlog.observe()

        added, done = sorted(self.entries(), key=lambda entry: entry['kind'])
        self.assert_observed(added, 'backlog.add', 'B14 added: More checks', AGENT)
        self.assert_observed(done, 'backlog.done', 'B14 done: More checks', AGENT)
        self.assertEqual((added['project'], done['project']), ('Build', 'Build'))
        self.assertEqual(self.seen()['archived'], ['B13', 'B14'])

    def test_in_process_archive_saves_new_archive_refs_without_duplicate_entries(self):
        backlog.observe()
        state = backlog.read()
        text = state['text'] + '- [x] Done already #B14\n'
        with mock.patch.object(backlog, 'ARCHIVE_AT', 1), mock.patch.object(backlog, 'ARCHIVE_TO', 0):
            backlog.save(text, state['version'])

        self.assertEqual(self.seen()['archived'], ['B13', 'B14'])
        backlog.observe()
        entry, = self.entries()
        self.assertEqual(entry['summary'], 'B14 added: Done already')

    def test_partial_append_failure_keeps_snapshot_until_next_observation(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] First #B14\n- [ ] Second #B15\n')
        record = audit.record

        def fail_first(kind, summary, **detail):
            if summary.startswith('B14 '):
                return None
            return record(kind, summary, **detail)

        with mock.patch.object(audit, 'record', side_effect=fail_first):
            backlog.observe()

        self.assertEqual(self.seen()['text'], INITIAL)
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B15 added: Second', UNKNOWN)
        backlog.observe()

        self.assertEqual(self.seen()['text'], self.file.read_text())
        self.assertCountEqual([entry['summary'] for entry in self.entries()],
                              ['B14 added: First (seen in the file)', 'B15 added: Second (seen in the file)',
                               'B15 added: Second (seen in the file)'])

    def test_failed_catch_up_does_not_save_snapshot_after_in_process_write(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        record = audit.record

        def fail_observed(kind, summary, **detail):
            if detail.get('observed'):
                return None
            return record(kind, summary, **detail)

        with mock.patch.object(audit, 'record', side_effect=fail_observed):
            backlog.add('In-process write')

        self.assertEqual(self.seen()['text'], INITIAL)
        self.assertIn('In-process write #B15', self.file.read_text())
        backlog.observe()

        self.assertCountEqual([entry['summary'] for entry in self.entries()],
                              ['B14 added: Direct edit (seen in the file)', 'B15 added: In-process write',
                               'B15 added: In-process write (seen in the file)'])

    def test_failed_in_process_append_leaves_change_for_observation(self):
        backlog.observe()
        with mock.patch.object(audit, 'record', return_value=None):
            backlog.add('In-process write')

        self.assertEqual(self.seen()['text'], INITIAL)
        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: In-process write', UNKNOWN)

    def test_observation_skips_busy_lock_until_it_is_released(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        with open(backlog.folder() / '.lock', 'rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            real_flock = fcntl.flock

            def require_nonblocking(fd, mode):
                self.assertEqual(mode, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return real_flock(fd, mode)

            with mock.patch.object(fcntl, 'flock', side_effect=require_nonblocking) as lock:
                backlog.observe()
            lock.assert_called_once()
            self.assertEqual(self.entries(), [])
            self.assertEqual(self.seen()['text'], INITIAL)

        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Direct edit', UNKNOWN)

    def test_save_succeeds_when_audit_parent_cannot_be_searched(self):
        paths.CONFIG.mkdir()
        paths.CONFIG.chmod(0)
        try:
            backlog.save(INITIAL + '- [ ] Added through save\n', backlog.read()['version'])
        finally:
            paths.CONFIG.chmod(0o700)

        self.assertIn('Added through save #B14', self.file.read_text())
        self.assertEqual(self.entries(), [])

    def test_direct_unnumbered_add_is_observed_once_before_numbering(self):
        backlog.observe()
        self.file.write_text(INITIAL + f'- [ ] Add checks ({SOURCE})\n')

        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'Added backlog item: Add checks', AGENT)
        self.assertIn('#B14', backlog.numbered()['text'])
        backlog.observe()
        self.assertEqual(self.entries(), [entry])

    def test_untouched_unnumbered_duplicates_do_not_absorb_numbered_add(self):
        before = INITIAL + '- [ ] Add checks\n' * 2
        self.file.write_text(before)
        backlog.observe()
        self.file.write_text(before + '- [ ] Add checks #B14\n')

        backlog.observe()

        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Add checks', UNKNOWN)

    def test_only_the_unnumbered_duplicate_that_left_can_match_a_new_id(self):
        self.file.write_text(INITIAL + '- [ ] Add checks\n' * 2)
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Add checks\n- [ ] Add checks #B14\n')

        backlog.observe()

        self.assertEqual(self.entries(), [])

    def test_observation_with_a_failed_snapshot_write_can_seed_later(self):
        with self.sandbox(quiet=False):
            backlog.observe()
        self.assertFalse(self.snapshot.exists())

        backlog.observe()

        self.assertEqual(self.seen()['text'], INITIAL)
        self.assertEqual(self.entries(), [])

    def test_unchanged_files_skip_lock_and_parse(self):
        backlog.observe()
        with (mock.patch.object(fcntl, 'flock', wraps=fcntl.flock) as lock,
              mock.patch.object(backlog, '_audit_items', wraps=backlog._audit_items) as parse):
            backlog.observe()
        lock.assert_not_called()
        parse.assert_not_called()

    def test_watch_makes_one_pass_if_already_stopped(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(backlog, 'observe') as observe:
            backlog.watch(stop)
        observe.assert_called_once_with()
