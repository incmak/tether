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
        self.enterContext(mock.patch.object(backlog, '_busy_since', None))
        self.enterContext(mock.patch.object(backlog, '_observed_files', None))

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
            self.assertEqual(lock.call_count, 2)  # the audit lock, then the busy backlog lock
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

    # B303 round 3 review

    def test_archive_entry_added_during_in_process_write_is_still_recorded(self):
        backlog.observe()
        real = backlog._catch_up

        def catch_up_then_archive(text, **kwargs):
            refs = real(text, **kwargs)
            self.archive.write_text('## Build (moved 2026-11-21 10:00)\n- [x] Sneaked in #B40\n')
            return refs
        with mock.patch.object(backlog, '_catch_up', side_effect=catch_up_then_archive):
            self.cli_add('In-process write')
        self.assertNotIn('B40', self.seen()['archived'])
        backlog.observe()
        kinds = sorted((e['kind'], e['summary']) for e in self.entries() if (e.get('detail') or {}).get('observed'))
        self.assertEqual(kinds, [('backlog.add', 'B40 added: Sneaked in (seen in the file)'),
                                 ('backlog.done', 'B40 done: Sneaked in (seen in the file)')])

    def test_replaced_backlog_lock_does_not_let_two_catch_ups_run(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        with backlog._audit_lock():
            lock = backlog.folder() / '.lock'
            lock.unlink()
            lock.write_text('')  # a worker replaces the backlog lock
            backlog.observe()
            self.assertEqual(self.entries(), [])
        backlog.observe()
        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Direct edit', UNKNOWN)

    def test_unreadable_archive_does_not_hold_back_other_changes(self):
        backlog.observe()
        self.archive.mkdir()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        with contextlib.redirect_stderr(io.StringIO()):
            backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Direct edit', UNKNOWN)

    def test_unreadable_archive_counts_a_missing_item_as_removed(self):
        backlog.observe()
        self.archive.mkdir()
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', ''))
        with contextlib.redirect_stderr(io.StringIO()):
            backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.remove', 'B12 removed: Fix warnings', UNKNOWN)

    def test_oversized_archive_is_treated_as_unreadable(self):
        backlog.observe()
        self.archive.write_text('## Build\n- [x] Fix warnings #B12\n')
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', ''))
        with mock.patch.object(backlog, 'ARCHIVE_READ', 10), contextlib.redirect_stderr(io.StringIO()):
            backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.remove', 'B12 removed: Fix warnings', UNKNOWN)

    def test_old_archive_copy_does_not_turn_a_removal_into_done(self):
        self.archive.write_text('## Build (moved 2026-11-20 10:00)\n- [x] Fix warnings #B12\n')
        backlog.observe()
        self.assertEqual(self.seen()['archived'], ['B12'])
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', ''))
        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.remove', 'B12 removed: Fix warnings', UNKNOWN)

    def test_done_result_with_source_shape_and_no_who_gives_no_hint(self):
        backlog.observe()
        with self.sandbox():
            backlog.mark_done('B12', 'Build', 'merged (codex · pane %99)', '')
        self.assertIn('(codex · pane %99) (unknown)', self.file.read_text())
        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.done', 'B12 done: Fix warnings', UNKNOWN)


    # B303 round 4 review

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file modes')
    def test_audit_lock_cannot_be_opened_read_only(self):
        with backlog._audit_lock():
            pass
        lock = audit.folder() / '.backlog-seen.lock'
        self.assertEqual(lock.stat().st_mode & 0o777, 0o200)
        with self.assertRaises(PermissionError):
            os.open(lock, os.O_RDONLY)

    def test_aborted_write_with_a_stale_read_records_no_false_removal(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        backlog.observe()

        def abort():
            raise RuntimeError('version changed')
        with backlog._locked(), self.assertRaises(RuntimeError):
            backlog._replace(INITIAL, INITIAL + '- [ ] Stale write #B15\n', audit_before=INITIAL, before_write=abort)
        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Direct edit', UNKNOWN)

    def test_write_over_a_stale_read_leaves_the_difference_to_observe(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        backlog.observe()
        with backlog._locked():
            backlog._replace(INITIAL, INITIAL + '- [ ] Stale write #B15\n', audit_before=INITIAL)
        backlog.observe()
        kinds = sorted((e['kind'], e['summary']) for e in self.entries())
        self.assertEqual(kinds, [('backlog.add', 'B14 added: Direct edit (seen in the file)'),
                                 ('backlog.add', 'B15 added: Stale write'),
                                 ('backlog.add', 'B15 added: Stale write (seen in the file)'),
                                 ('backlog.remove', 'B14 removed: Direct edit (seen in the file)')])

    def test_archive_read_again_does_not_add_an_item_already_removed(self):
        backlog.observe()
        self.archive.mkdir()
        self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', ''))
        with contextlib.redirect_stderr(io.StringIO()):
            backlog.observe()
        self.archive.rmdir()
        self.archive.write_text('## Build (moved 2026-11-21 10:00)\n- [x] Fix warnings #B12\n')
        backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.remove', 'B12 removed: Fix warnings', UNKNOWN)

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'needs /proc')
    def test_audit_lock_close_error_does_not_fail_a_saved_action(self):
        real_close = os.close

        def close(fd):
            name = os.readlink(f'/proc/self/fd/{fd}')
            real_close(fd)
            if name.endswith('.backlog-seen.lock'):
                raise OSError('close failed')
        with mock.patch.object(backlog.os, 'close', side_effect=close):
            self.cli_add('Saved once')
        self.assertIn('Saved once', self.file.read_text())

    def test_observe_goes_on_without_a_backlog_lock_held_too_long(self):
        backlog.observe()
        self.file.write_text(INITIAL + '- [ ] Direct edit #B14\n')
        clock = iter([100.0, 100.0 + backlog.BUSY_LIMIT])
        with open(backlog.folder() / '.lock', 'rb') as handle, \
                mock.patch.object(backlog.time, 'monotonic', side_effect=lambda: next(clock)):
            fcntl.flock(handle, fcntl.LOCK_EX)
            backlog.observe()
            self.assertEqual(self.entries(), [])
            backlog.observe()
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.add', 'B14 added: Direct edit', UNKNOWN)


    # B303 round 5 review

    def test_unusable_backlog_lock_path_uses_the_busy_fallback(self):
        for make in (lambda lock: lock.mkdir(), lambda lock: lock.symlink_to(self.file)):
            with self.subTest(make=make):
                backlog.observe()
                lock = backlog.folder() / '.lock'
                lock.unlink(missing_ok=True)
                make(lock)
                count = len(self.entries())
                self.file.write_text(self.file.read_text() + f'- [ ] Direct edit {count} #B{20 + count}\n')
                with mock.patch.object(backlog, 'BUSY_LIMIT', 0):
                    backlog.observe()
                self.assertEqual(len(self.entries()), count + 1)
                lock.rmdir() if lock.is_dir() and not lock.is_symlink() else lock.unlink()

    def half_done_archive(self, item):
        """A write archives an item: DONE.md is written, BACKLOG.md not yet. A worker holds the backlog lock."""
        backlog.observe()
        handle = self.enterContext(open(backlog.folder() / '.lock', 'rb'))
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.enterContext(mock.patch.object(backlog, 'BUSY_LIMIT', 0))
        self.archive.write_text(f'## Build (moved 2026-11-21 10:00)\n- [x] {item}\n  - Done: 2026-11-21 10:00 · ok\n')
        backlog.observe()
        self.assertEqual(self.entries(), [])
        self.file.write_text(INITIAL.replace(f'- [ ] {item}\n', '').replace(f'- [x] {item}\n', ''))
        backlog.observe()

    def test_fallback_between_the_two_files_of_an_archive_records_done(self):
        self.half_done_archive('Fix warnings #B12')
        entry, = self.entries()
        self.assert_observed(entry, 'backlog.done', 'B12 done: Fix warnings', UNKNOWN)

    def test_fallback_between_the_two_files_of_an_archive_of_a_done_item_records_nothing(self):
        self.half_done_archive('Old fix #B13')
        self.assertEqual(self.entries(), [])


    # B303 round 6 review

    def test_fallback_keeps_archive_ids_the_snapshot_already_holds(self):
        self.archive.write_text('## Build (moved 2026-11-20 10:00)\n- [x] Fix warnings #B12\n')
        backlog.observe()
        self.assertEqual(self.seen()['archived'], ['B12'])
        with open(backlog.folder() / '.lock', 'rb') as handle, mock.patch.object(backlog, 'BUSY_LIMIT', 0):
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.file.write_text(INITIAL + '- [ ] Unrelated edit #B14\n')
            backlog.observe()
            self.assertEqual(self.seen()['archived'], ['B12'])
            self.file.write_text(INITIAL.replace('- [ ] Fix warnings #B12\n', '') + '- [ ] Unrelated edit #B14\n')
            backlog.observe()
        kinds = sorted((e['kind'], e['summary']) for e in self.entries())
        self.assertEqual(kinds, [('backlog.add', 'B14 added: Unrelated edit (seen in the file)'),
                                 ('backlog.remove', 'B12 removed: Fix warnings (seen in the file)')])


if __name__ == '__main__':
    unittest.main()
