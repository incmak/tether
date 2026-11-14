import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_triage_cli
import paths
import work_api
import work_items
import work_triage

PROJECT = {'name': 'shop', 'prefix': 'T', 'folder': '/srv/shop', 'triage': 'Triage', 'work': 'Shop', 'jira': 'BS'}
LEDGER = """# Triage ledger

Append-only. One line per event.

- 2026-11-03 12:41 | T-001 | handed | login-fails; msgs aaa; window Triage:login-fails
- 2026-11-03 12:41 | seen | chat | 2026-11-03T07:01:47Z
- 2026-11-03 12:43 | T-002 | handed | old-crash; msgs bbb; window Triage:claude; backfilled
- 2026-11-03 12:43 | T-001 | triaging | reproduced on dev | ref: notes/t1.md
- 2026-11-03 12:43 | T-002 | duplicate | same as login-fails
- 2026-11-04 09:00 | T-003 | handed | slow-export; msgs ccc; window Triage:slow-export
- 2026-11-04 09:05 | T-003 | fixing | patch ready | ref: pl#7
- 2026-11-04 10:00 | T-004 | handed | needs-call; msgs ddd; window Triage:needs-call
- 2026-11-04 10:01 | T-004 | needs-mak | ship the hint or the fix?
- 2026-11-05 08:00 | T-005 | handed | last-one; msgs eee; window Triage:last-one
- 2026-11-05 08:01 | T-005 | closed | fixed on UAT
- 2026-11-05 09:00 | seen | chat | 2026-11-05T03:00:00Z
not a ledger line
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        patcher = mock.patch.object(paths, 'TETHER', self.home / 'tether')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        work_items._CACHE.clear()
        work_items.save_project(dict(PROJECT), 0, create=True)
        self.view = self.home / 'proj' / 'TRIAGE.md'
        self.view.parent.mkdir()
        self.view.write_text(LEDGER, encoding='utf-8')
        self.ledger = work_triage.Ledger('shop', self.view)

    def imported(self):
        return self.ledger.import_ledger()

    def lines(self, rows):
        return [work_triage.row_line(r) for r in rows]

    def old_lines(self):
        return [line for line in LEDGER.splitlines() if line.startswith('- ')]


class Import(Base):
    def test_keeps_open_items_with_ids_times_and_lines(self):
        report = self.imported()
        self.assertEqual(report['made'], ['T-001', 'T-003', 'T-004'])
        self.assertTrue((self.view.parent / work_triage.ARCHIVE).exists())
        self.assertEqual((self.view.parent / work_triage.ARCHIVE).read_text(encoding='utf-8'), LEDGER)
        items = self.ledger.items()
        self.assertEqual(sorted(items), ['T-001', 'T-003', 'T-004'])
        self.assertEqual(items['T-001']['title'], 'login-fails')
        self.assertEqual((items['T-001']['stage'], items['T-003']['stage'], items['T-004']['stage']),
                         ('triage', 'building', 'needs'))
        self.assertEqual(items['T-001']['created'], work_triage.parse_time('2026-11-03 12:41'))
        self.assertEqual(items['T-004']['ask']['q'], 'ship the hint or the fix?')
        self.assertEqual(items['T-004']['ask']['options'], work_triage.OPTIONS)
        self.assertEqual(items['T-001']['refs'], [])  # a ref stays in history: it never moves a Jira ticket

    def test_view_holds_every_old_line_in_the_old_order(self):
        self.imported()
        self.assertEqual(self.ledger.write_view(), '')
        view = [line for line in self.view.read_text(encoding='utf-8').splitlines() if line.startswith('- ')]
        self.assertEqual(view, self.old_lines())
        self.assertEqual(self.lines(self.ledger.rows()), self.old_lines())

    def test_import_makes_no_push(self):
        sent = []
        watcher = work_api.Watcher(lambda payload, urgent: sent.append(payload), every=0)
        watcher.observe()  # the first look: nothing to compare yet
        self.imported()
        self.assertEqual(watcher.observe(), 0)
        self.assertEqual(sent, [])

    def test_again_adds_only_late_lines_and_no_items(self):
        self.imported()
        archive = self.view.parent / work_triage.ARCHIVE
        with open(archive, 'a', encoding='utf-8') as out:  # an old writer that waited on the lock
            out.write('- 2026-11-06 07:00 | T-001 | ticket | BS-9 filed | ref: BS-9\n')
        report = self.imported()
        self.assertEqual((report['made'], report['added']), ([], 1))
        self.assertEqual(self.ledger.item_rows('T-001')[-1][2:5], ('ticket', 'BS-9 filed', 'BS-9'))
        self.assertEqual(self.imported()['added'], 0)

    def test_new_ids_skip_every_old_id(self):
        self.imported()
        self.assertEqual(self.ledger.new('next-bug', 'fff'), 'T-006')
        made, created = work_items.from_feed('shop', 'teams:x/1', 'From the feed', '', 'Teams')
        self.assertTrue(created)
        self.assertEqual(made['id'], 'T-007')

    def test_a_closed_old_item_comes_back_when_named(self):
        self.imported()
        item = self.ledger.add('T-002', 'triaging', 'came back')
        self.assertEqual(item['stage'], 'triage')
        self.assertEqual([r[2] for r in self.ledger.item_rows('T-002')], ['handed', 'duplicate', 'triaging'])
        self.assertEqual(len([r for r in self.ledger.rows() if r[1] == 'T-002']), 3)

    def test_unknown_item_is_refused(self):
        self.imported()
        with self.assertRaises(work_triage.TriageError):
            self.ledger.add('T-050', 'triaging', 'x')
        with self.assertRaises(work_triage.TriageError):
            self.ledger.add('X-1', 'triaging', 'x')

    def test_view_is_never_written_over_the_old_ledger(self):
        self.assertFalse(self.ledger.imported())
        self.assertEqual(self.ledger.write_view(), '')
        self.assertEqual(self.view.read_text(encoding='utf-8'), LEDGER)

    def test_board_id_that_is_not_from_the_ledger_stops_the_import(self):
        self.assertEqual(work_items.new('shop', 'Made on the board', 'agent')['id'], 'T-001')
        with self.assertRaisesRegex(work_triage.TriageError, 'T-001 on the board did not come from the ledger'):
            self.imported()
        self.assertEqual(self.view.read_text(encoding='utf-8'), LEDGER)
        self.assertFalse((self.view.parent / work_triage.ARCHIVE).exists())
        self.assertEqual(self.ledger.state(), 'old')

    def test_commands_wait_until_the_import_is_complete(self):
        real = self.ledger._import_item
        seen = []

        def import_item(item_id, rows, start=0):
            seen.append(self.ledger.state())  # the ledger is in the archive, the marker not written yet
            return real(item_id, rows, start)
        with mock.patch.object(self.ledger, '_import_item', import_item):
            self.imported()
        self.assertEqual(set(seen), {'partial'})
        self.assertEqual(self.ledger.state(), 'ready')

    def test_a_stopped_import_finishes_when_run_again(self):
        with mock.patch.object(self.ledger, '_set_mark', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.imported()
        self.assertEqual(self.ledger.state(), 'partial')
        report = self.imported()
        self.assertEqual((report['made'], report['added']), ([], 0))
        self.assertEqual(self.ledger.state(), 'ready')
        self.assertEqual(self.lines(self.ledger.rows()), self.old_lines())

    def test_tab_links_do_not_count_as_imported_lines(self):
        self.imported()
        work_items.link('T-001', '%9', 'triage', by='import')
        with open(self.view.parent / work_triage.ARCHIVE, 'a', encoding='utf-8') as out:
            out.write('- 2026-11-06 07:00 | T-001 | ticket | BS-9 filed\n')
        self.assertEqual(self.imported()['added'], 1)
        self.assertEqual(self.ledger.item_rows('T-001')[-1][2:4], ('ticket', 'BS-9 filed'))

    def test_lines_an_old_writer_put_in_the_view_are_kept_and_brought_in(self):
        self.imported()
        self.assertEqual(self.ledger.write_view(), '')
        with open(self.view, 'a', encoding='utf-8') as out:  # an old triage-log that still ran after the switch
            out.write('- 2026-11-06 07:00 | T-003 | fixed | on dev\n')
            out.write('- 2026-11-06 07:01 | seen | chat | 2026-11-06T01:00:00Z\n')
        before = self.view.read_text(encoding='utf-8')
        self.assertIn('did not write', self.ledger.write_view())
        self.assertEqual(self.view.read_text(encoding='utf-8'), before)
        report = self.imported()
        self.assertEqual((report['late'], report['added']), (2, 1))
        self.assertEqual(self.ledger.items()['T-003']['stage'], 'review')
        self.assertEqual(self.ledger.seen_mark(), '2026-11-06T01:00:00Z')
        self.assertEqual(self.ledger.write_view(), '')
        view = self.view.read_text(encoding='utf-8')
        self.assertIn('| T-003 | fixed | on dev', view)
        self.assertEqual(view.count('| seen | chat | 2026-11-06T01:00:00Z'), 1)
        self.assertNotIn('seen', (self.view.parent / work_triage.ARCHIVE).read_text(encoding='utf-8').split(
            '2026-11-05 09:00 | seen')[1])
        self.assertEqual(self.imported()['late'], 0)

    def test_a_view_write_whose_marker_failed_recovers_through_import(self):
        self.imported()
        self.assertEqual(self.ledger.write_view(), '')
        self.ledger.add('T-001', 'ticket', 'BS-9 filed')
        with mock.patch.object(self.ledger, '_set_mark', side_effect=OSError('disk full')):
            self.assertIn('could not write', self.ledger.write_view())
        self.assertIn('BS-9 filed', self.view.read_text(encoding='utf-8'))  # the view changed, the marker did not
        self.assertIn('did not write', self.ledger.write_view())
        self.assertEqual(self.imported()['late'], 0)
        self.assertEqual(self.ledger.write_view(), '')
        self.assertEqual(self.view.read_text(encoding='utf-8').count('BS-9 filed'), 1)

    def test_old_ids_are_reserved_before_the_ledger_moves(self):
        with mock.patch.object(self.ledger, '_import_item', side_effect=OSError('stopped')):
            with self.assertRaises(OSError):
                self.imported()
        self.assertEqual(self.ledger.state(), 'partial')
        self.assertEqual(work_items.new('shop', 'Feed item during the stop', 'agent')['id'], 'T-006')
        with self.assertRaisesRegex(work_triage.TriageError, 'Run the import'):
            agent_triage_cli.run(agent_triage_cli.arguments(['--project', 'shop', '--view', str(self.view), 'open']), [])
        report = self.imported()
        self.assertEqual(report['made'], ['T-001', 'T-003', 'T-004'])

    def test_an_old_writer_id_that_the_board_took_stops_the_import(self):
        self.imported()
        self.assertEqual(self.ledger.new('next-bug', 'fff'), 'T-006')
        self.assertEqual(self.ledger.write_view(), '')
        with open(self.view, 'a', encoding='utf-8') as out:
            out.write('- 2026-11-06 07:00 | T-006 | handed | other-bug; msgs ggg\n')
        before = self.view.read_text(encoding='utf-8')
        with self.assertRaisesRegex(work_triage.TriageError, 'T-006'):
            self.imported()
        self.assertEqual(self.view.read_text(encoding='utf-8'), before)
        self.assertNotIn('other-bug', (self.view.parent / work_triage.ARCHIVE).read_text(encoding='utf-8'))

    def test_a_project_without_a_ledger_works_and_keeps_working(self):
        self.view.unlink()
        self.assertTrue(self.ledger.imported())
        self.assertEqual(self.ledger.new('first-bug', 'm1'), 'T-001')
        self.assertEqual(self.ledger.write_view(), '')
        self.assertTrue(self.ledger.imported())
        self.assertEqual(self.ledger.write_view(), '')

    def test_missing_ledger_is_refused(self):
        self.view.unlink()
        with self.assertRaises(work_triage.TriageError):
            self.imported()


class Steps(Base):
    def setUp(self):
        super().setUp()
        self.imported()

    def stage(self, item_id):
        return self.ledger.items()[item_id]['stage']

    def test_statuses_move_the_item(self):
        self.ledger.add('T-001', 'fixing', 'patch')
        self.assertEqual(self.stage('T-001'), 'building')
        self.ledger.add('T-001', 'ticket', 'BS-1 for the rest', 'BS-1')
        self.assertEqual(self.stage('T-001'), 'building')  # ticket moves nothing
        self.ledger.add('T-001', 'fixed', 'on UAT')
        self.assertEqual(self.stage('T-001'), 'review')
        self.ledger.add('T-001', 'wontfix', 'by design')
        item = self.ledger.items()['T-001']
        self.assertEqual(item['stage'], 'done')
        self.assertTrue(item['doneAt'])
        self.ledger.add('T-001', 'triaging', 'reported again')
        self.assertEqual(self.stage('T-001'), 'triage')

    def test_on_hold_item_reopens_on_a_step(self):
        work_items.stage('T-001', 'hold', 'mak')
        self.ledger.add('T-001', 'ticket', 'BS-2')
        self.assertEqual(self.stage('T-001'), 'triage')

    def test_needs_mak_asks_and_routes_the_answer_to_the_tab(self):
        item = self.ledger.add('T-001', 'needs-mak', 'Hint or fix?', 'notes/q.md', pane='%7', chat='chat-7')
        self.assertEqual(item['stage'], 'needs')
        self.assertEqual((item['ask']['q'], item['ask']['pane'], item['ask']['chat']), ('Hint or fix?', '%7', 'chat-7'))
        self.assertEqual(item['ask']['options'], ['Go ahead', 'Hold'])
        self.assertEqual(self.ledger.item_rows('T-001')[-1][2:5], ('needs-mak', 'Hint or fix?', 'notes/q.md'))

    def test_needs_mak_takes_own_options(self):
        item = self.ledger.add('T-001', 'needs-mak', 'Which?', options=['Hint', 'Fix'])
        self.assertEqual(item['ask']['options'], ['Hint', 'Fix'])

    def test_second_question_replaces_the_first_without_a_push(self):
        sent = []
        watcher = work_api.Watcher(lambda payload, urgent: sent.append(payload), every=0)
        watcher.observe()
        self.ledger.add('T-001', 'needs-mak', 'First?')
        self.assertEqual(watcher.observe(), 1)
        item = self.ledger.add('T-001', 'needs-mak', 'Second?')
        self.assertEqual(item['ask']['q'], 'Second?')
        self.assertEqual(watcher.observe(), 0)

    def test_next_step_after_a_question_returns_the_item(self):
        self.ledger.add('T-003', 'needs-mak', 'Push it?')
        self.assertEqual(self.stage('T-003'), 'needs')
        self.ledger.add('T-003', 'triaging', 'Mak said yes in chat')
        item = self.ledger.items()['T-003']
        self.assertEqual((item['stage'], item['ask']), ('building', None))

    def test_question_on_a_done_item_reopens_it_first(self):
        self.ledger.add('T-001', 'closed', 'done')
        item = self.ledger.add('T-001', 'needs-mak', 'Reopen?')
        self.assertEqual(item['stage'], 'needs')
        self.assertEqual(item['askFrom'], 'triage')

    def test_board_changes_show_as_tether_lines(self):
        item = self.ledger.add('T-001', 'needs-mak', 'Hint or fix?')
        work_items.answer('T-001', item['ask']['id'], item['version'], choice='Hold')
        self.assertEqual(self.ledger.item_rows('T-001')[-1][2:4], ('tether', 'You chose: Hold'))
        self.assertEqual(self.stage('T-001'), 'hold')

    def test_bad_input_is_refused(self):
        for status, text in (('bogus', 'x'), ('triaging', '   ')):
            with self.assertRaises(work_triage.TriageError):
                self.ledger.add('T-001', status, text)
        with self.assertRaises(work_triage.TriageError):
            self.ledger.new('Bad Name', 'x')
        with self.assertRaises(work_triage.TriageError):
            self.ledger.new('good-name', '')

    def test_new_item_starts_in_triage_with_its_line(self):
        item_id = self.ledger.new('next-bug', 'm1,m2', 'Triage:next-bug', 'from QA')
        item = self.ledger.items()[item_id]
        self.assertEqual((item['title'], item['stage'], item['source']), ('next-bug', 'triage', 'Chat msgs m1,m2'))
        self.assertEqual(self.ledger.item_rows(item_id)[0][2:4],
                         ('handed', 'next-bug; msgs m1,m2; window Triage:next-bug; from QA'))

    def test_seen_marks_and_open_rows(self):
        self.assertEqual(self.ledger.seen_mark(), '2026-11-05T03:00:00Z')
        self.ledger.seen('2026-11-14T10:00:00Z')
        self.assertEqual(self.ledger.seen_mark(), '2026-11-14T10:00:00Z')
        self.assertEqual([r[1] for r in self.ledger.open_rows()], ['T-001', 'T-003', 'T-004'])
        self.ledger.add('T-003', 'closed', 'shipped')
        self.assertEqual([r[1] for r in self.ledger.open_rows()], ['T-001', 'T-004'])

    def test_seen_keeps_a_bounded_list(self):
        with mock.patch.object(work_triage, 'SEEN_KEEP', 3):
            for n in range(5):
                self.ledger.seen(f'mark-{n}')
        self.assertEqual([r[3] for r in self.ledger.seen_rows()], ['mark-2', 'mark-3', 'mark-4'])


class StageFor(unittest.TestCase):
    def test_map(self):
        item = {'stage': 'triage', 'askFrom': None}
        self.assertEqual(work_triage.stage_for(item, 'fixing'), 'building')
        self.assertEqual(work_triage.stage_for(item, 'fixed'), 'review')
        self.assertEqual(work_triage.stage_for(item, 'duplicate'), 'done')
        self.assertIsNone(work_triage.stage_for(item, 'ticket'))
        self.assertIsNone(work_triage.stage_for({'stage': 'building'}, 'fixing'))
        self.assertEqual(work_triage.stage_for({'stage': 'needs', 'askFrom': 'review'}, 'ticket'), 'review')
        self.assertEqual(work_triage.stage_for({'stage': 'done'}, 'handed'), 'triage')


class Tabs(unittest.TestCase):
    ITEM = {'id': 'T-001', 'title': 'login-fails', 'tabs': [{'pane': '%9', 'role': 'triage'}]}

    def run_with(self, answers, status, here=''):
        calls = []

        answers = {k: list(v) if isinstance(v, list) else v for k, v in answers.items()}

        def tmux(*args):
            calls.append(args)
            answer = answers.get(args[0], '')
            return answer.pop(0) if isinstance(answer, list) else answer
        with mock.patch.object(work_triage, '_tmux', tmux):
            shown = work_triage.show_status(self.ITEM, status, here)
        return shown, calls

    def test_names_the_linked_tab(self):
        shown, calls = self.run_with({'display-message': '@3\tlogin-fails · new\tTriage', 'list-panes': '%9'},
                                     'fixing')
        self.assertEqual(shown, 'window Triage:login-fails -> login-fails · fixing')
        self.assertIn(('rename-window', '-t', '@3', 'login-fails · fixing'), calls)
        self.assertNotIn('kill-window', [c[0] for c in calls])

    def test_done_closes_the_window_unless_run_inside_it(self):
        answers = {'display-message': ['@3\tlogin-fails\tTriage', '@3\tlogin-fails · done'], 'list-panes': '%9'}
        shown, calls = self.run_with(answers, 'closed')
        self.assertIn(('kill-window', '-t', '@3'), calls)
        self.assertEqual(shown, 'closed window Triage:login-fails')
        _, calls = self.run_with(answers, 'closed', here='%9')
        self.assertNotIn('kill-window', [c[0] for c in calls])

    def test_a_pane_that_left_the_window_keeps_the_window_open(self):
        shown, calls = self.run_with({'display-message': '@3\tlogin-fails\tTriage', 'list-panes': '%12'}, 'closed')
        self.assertEqual(shown, '')
        self.assertEqual([c[0] for c in calls], ['display-message', 'list-panes'])

    def test_a_window_that_changed_before_the_kill_stays_open(self):
        answers = {'display-message': ['@3\tlogin-fails\tTriage', '@8\tlogin-fails · done'], 'list-panes': '%9'}
        shown, calls = self.run_with(answers, 'closed')
        self.assertNotIn('kill-window', [c[0] for c in calls])
        self.assertIn('left open', shown)

    def test_a_window_with_another_name_is_left_alone(self):
        shown, calls = self.run_with({'display-message': '@3\tsomething-else\tWork', 'list-panes': '%9'}, 'closed')
        self.assertEqual(shown, '')
        self.assertEqual([c[0] for c in calls], ['display-message'])

    def test_no_linked_tab_or_gone_pane_does_nothing(self):
        with mock.patch.object(work_triage, '_tmux') as tmux:
            self.assertEqual(work_triage.show_status(dict(self.ITEM, tabs=[]), 'closed'), '')
            tmux.assert_not_called()
        shown, calls = self.run_with({'display-message': None}, 'closed')
        self.assertEqual((shown, len(calls)), ('', 1))

    def test_import_links_only_one_matching_window(self):
        item = {'title': 'login-fails', 'tabs': []}
        with mock.patch.object(work_triage, '_tmux', return_value='claude\t%1\nlogin-fails · ticket\t%4'):
            self.assertEqual(work_triage.link_window(item, 'Triage'), '%4')
        with mock.patch.object(work_triage, '_tmux', return_value='login-fails\t%4\nlogin-fails · new\t%5'):
            self.assertEqual(work_triage.link_window(item, 'Triage'), '')


class Cli(Base):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(agent_triage_cli.agent_item_cli, 'here', return_value=('', '')), \
                mock.patch.object(work_triage, '_tmux', return_value=None):
            code = agent_triage_cli.main(['--project', 'shop', '--view', str(self.view), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_refuses_before_the_import_and_keeps_the_ledger(self):
        code, _, err = self.cli('open')
        self.assertEqual(code, 1)
        self.assertIn('still the old ledger. Run the import', err)
        self.assertEqual(self.view.read_text(encoding='utf-8'), LEDGER)

    def test_commands_print_like_the_old_tool_and_write_the_view(self):
        self.assertEqual(self.cli('import')[0], 0)
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], ['Chat checked up to: 2026-11-05T03:00:00Z', 'Open items: 3'])
        code, out, _ = self.cli('new', 'next-bug', '--msgs', 'm9')
        self.assertEqual(out.strip(), 'T-006')
        code, out, _ = self.cli('add', 'T-006', 'fixing', 'patch', 'ready', '--ref', 'pl#9')
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r'^- \d{4}-\d\d-\d\d \d\d:\d\d \| T-006 \| fixing \| patch ready \| ref: pl#9$')
        self.assertIn('| T-006 | fixing | patch ready | ref: pl#9', self.view.read_text(encoding='utf-8'))
        self.assertEqual(self.cli('show', 'T-099')[1].strip(), 'no such item')
        self.assertEqual(len(self.cli('last', '2')[1].splitlines()), 2)
        code, _, err = self.cli('add', 'T-006', 'bogus', 'x')
        self.assertEqual(code, 1)
        self.assertIn('Status is one of', err)

    def test_help_needs_no_import(self):
        code, out, _ = self.cli('help')
        self.assertEqual(code, 0)
        self.assertIn('needs-mak asks Mak', out)
        self.assertEqual(self.view.read_text(encoding='utf-8'), LEDGER)


class ReserveIds(unittest.TestCase):
    def test_reserve_event_counts_for_the_next_id(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(paths, 'TETHER', Path(home)):
            work_items._CACHE.clear()
            work_items.save_project(dict(PROJECT), 0, create=True)
            with work_items.locked():
                work_items._append('T', {'item': 'T-040', 'type': 'reserve', 'at': time.time(), 'by': 'import'})
            self.assertEqual(work_items.new('shop', 'After the import', 'agent')['id'], 'T-041')
            self.assertNotIn('T-040', work_items.fold(work_items.project('shop')))


if __name__ == '__main__':
    unittest.main()
