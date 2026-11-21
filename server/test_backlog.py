"""The shared backlog: no live tmux or agent. Several processes write at once, as the server and agents do."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import multiprocessing
import os
import re
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit
import backlog
import paths
import projects
import projects_api
from test_projects import Handler


def _writer(home, prefix, count):
    paths.TETHER = Path(home)
    for at in range(count):
        backlog.add(f'{prefix} {at}', project=prefix if at % 2 else '')


def _ticker(home, count):
    """Saves the whole file over and over, as the person's checklist does."""
    paths.TETHER = Path(home)
    done = 0
    while done < count:
        state = backlog.read()
        try:
            backlog.save(state['text'].replace('[ ]', '[x]', 1), state['version'])
            done += 1
        except projects.ProjectError as exc:
            if exc.status != 409:
                raise


class BacklogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = temp.name
        patch = mock.patch.object(paths, 'TETHER', Path(temp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def text(self):
        return (backlog.folder() / 'BACKLOG.md').read_text()

    def test_read_makes_no_folder_and_save_round_trips(self):
        empty = backlog.read()
        self.assertEqual(empty['text'], '')
        self.assertFalse(backlog.folder().exists())
        saved = backlog.save('- [ ] First\n', empty['version'])
        self.assertEqual(backlog.read(), saved)

    def test_a_stale_save_changes_nothing(self):
        seen = backlog.save('- [ ] First\n', backlog.read()['version'])
        backlog.add('From an agent')
        with self.assertRaises(projects.ProjectError) as caught:
            backlog.save('Mine\n', seen['version'])
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('From an agent', self.text())

    def test_too_long_is_refused_not_cut(self):
        version = backlog.read()['version']
        with self.assertRaises(projects.ProjectError):
            backlog.save('x' * (backlog.LIMIT + 1), version)
        backlog.folder().mkdir(parents=True, exist_ok=True)
        (backlog.folder() / 'BACKLOG.md').write_text('x' * (backlog.LIMIT + 1))
        with self.assertRaises(projects.ProjectError) as caught:
            backlog.read()
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(len(self.text()), backlog.LIMIT + 1)

    def test_a_list_near_its_limit_moves_the_oldest_finished_items_to_done(self):
        filler = 'word ' * 200
        blocks = [f'- [x] Old {n} #B{n}\n  - Done: {filler}\n' for n in range(1, 51)]
        text = '- [ ] Inbox open #B60\n\n## web\n- [x] Newest done #B62\n  - Done: ok\n' + ''.join(blocks) + '- [ ] Open web item #B61\n'
        with mock.patch.object(backlog, 'ARCHIVE_AT', backlog.LIMIT):  # save as is, then let one change cross the line
            backlog.save(text, backlog.read()['version'])
        self.assertGreater(len(self.text()), backlog.ARCHIVE_AT)
        backlog.mark_done('B61', result='shipped')
        kept, done = self.text(), (backlog.folder() / 'DONE.md').read_text()
        self.assertLessEqual(len(kept), backlog.ARCHIVE_TO)
        self.assertIn('- [ ] Inbox open #B60', kept)
        self.assertIn('- [x] Newest done #B62', done)
        self.assertRegex(kept, r'- \[x\] Open web item #B61\n  - Done: [0-9-]+ [0-9:]+ · shipped')
        self.assertNotIn('Old 1 #B1', kept)
        self.assertTrue(done.startswith('# Finished backlog items'))
        self.assertRegex(done, r'\n## web \(moved [0-9-]+ [0-9:]+\)\n- \[x\] Newest done #B62\n  - Done: ok\n- \[x\] Old 1 #B1\n  - Done: word')
        for number in range(1, 51):  # every item is in exactly one of the two files
            self.assertEqual((f'Old {number} #B{number}' in kept) + (f'Old {number} #B{number}' in done), 1, number)
        self.assertEqual(backlog.read()['next_id'], 63)
        backlog.add('Next item', project='web')
        self.assertIn('- [ ] Next item #B63', self.text())  # no archived ID is given again

    def test_an_id_is_never_given_twice_after_its_item_leaves_the_list(self):
        backlog.save('- [ ] One\n- [ ] Two\n', backlog.read()['version'])
        state = backlog.read()
        self.assertEqual(state['next_id'], 3)
        backlog.save('- [ ] One #B1\n', state['version'])  # the person deletes the newest item
        self.assertEqual(backlog.read()['next_id'], 3)
        backlog.add('Three')
        self.assertIn('- [ ] Three #B3', self.text())
        self.assertEqual(backlog.with_ids('- [ ] Typed in the file'), '- [ ] Typed in the file #B4')

    def test_a_first_save_without_last_id_does_not_give_a_deleted_id_again(self):
        backlog._write(backlog._path(), '- [ ] One #B1\n- [ ] Two #B2\n')  # a list from before last-id
        self.assertFalse((backlog.folder() / 'last-id').exists())
        backlog.save('- [ ] Replacement\n', backlog.read()['version'])
        self.assertEqual(self.text(), '- [ ] Replacement #B3\n')

    def test_archive_done_moves_every_finished_item_and_a_dry_run_moves_none(self):
        text = '- [x] Old #B1\n  - why\n- [ ] Open #B2\n\n## tether\n- [x] Fixed #B3\n- [ ] Next #B4\n'
        backlog.save(text, backlog.read()['version'])
        self.assertEqual(backlog.archive_done(dry_run=True), (2, len(text), len(text) - len('- [x] Old #B1\n  - why\n- [x] Fixed #B3\n')))
        self.assertEqual(self.text(), text)
        self.assertFalse((backlog.folder() / 'DONE.md').exists())
        count, before, after = backlog.archive_done()
        self.assertEqual((count, before, after), (2, len(text), len(self.text())))
        self.assertEqual(self.text(), '- [ ] Open #B2\n\n## tether\n- [ ] Next #B4\n')
        done = (backlog.folder() / 'DONE.md').read_text()
        self.assertIn('## Inbox (moved ', done)
        self.assertIn('- [x] Old #B1\n  - why\n', done)
        self.assertIn('## tether (moved ', done)
        self.assertEqual(backlog.archive_done(), (0, len(self.text()), len(self.text())))
        backlog.add('New')
        self.assertIn('- [ ] New #B5', self.text())

    def test_a_save_over_the_limit_is_kept_when_its_finished_items_can_move(self):
        done = ''.join(f'- [x] Done {n} ' + 'x' * 900 + f' #B{n}\n' for n in range(1, 41))
        open_part = '- [ ] Open ' + 'y' * (backlog.LIMIT - 1000) + ' #B41\n'
        text = done + open_part
        self.assertGreater(len(text), backlog.LIMIT)
        backlog.save(text, backlog.read()['version'])
        self.assertEqual(self.text(), open_part)
        with self.assertRaises(backlog.ProjectError):
            backlog.save(open_part + '- [ ] Over ' + 'z' * 2000 + '\n', backlog.read()['version'])

    def test_details_after_a_blank_line_move_with_their_item(self):
        text = ('- [x] Old #B1\n  - first\n\n  - second\n\n- [ ] Open #B2\n  - ' + 'x' * backlog.ARCHIVE_AT + '\n')
        with mock.patch.object(backlog, 'ARCHIVE_TO', backlog.LIMIT):
            backlog.save(text, backlog.read()['version'])
        kept, done = self.text(), (backlog.folder() / 'DONE.md').read_text()
        self.assertTrue(kept.startswith('- [ ] Open #B2\n'), kept[:40])
        self.assertNotIn('second', kept)
        self.assertIn('- [x] Old #B1\n  - first\n\n  - second\n', done)

    def test_a_list_with_no_finished_items_stays_and_a_short_list_is_not_moved(self):
        version = backlog.read()['version']
        backlog.save('- [x] Done #B1\n- [ ] Open #B2\n', version)
        self.assertFalse((backlog.folder() / 'DONE.md').exists())
        big = '- [ ] Open ' + 'x' * (backlog.ARCHIVE_AT + 10) + ' #B1\n'
        backlog.save(big, backlog.read()['version'])
        self.assertEqual(self.text(), big)
        self.assertFalse((backlog.folder() / 'DONE.md').exists())

    def test_a_full_list_fits_in_a_request_in_any_language(self):
        for char in ('語', '\U0001F600'):  # 3 and 4 bytes in UTF-8
            body = json.dumps({'text': char * backlog.LIMIT, 'version': 'x' * 64}, ensure_ascii=False).encode()
            self.assertLessEqual(len(body), projects_api.BACKLOG_BODY_LIMIT)
        self.assertGreater(backlog.LIMIT, 60000)  # the list held 60,000 characters on 15 Nov 2026

    def test_links_are_refused(self):
        backlog.folder().mkdir(parents=True)
        target = Path(self.home) / 'outside.md'
        target.write_text('private')
        (backlog.folder() / 'BACKLOG.md').symlink_to(target)
        for call in (backlog.read, lambda: backlog.save('x', 'any'), lambda: backlog.add('x')):
            with self.assertRaises(projects.ProjectError):
                call()
        self.assertEqual(target.read_text(), 'private')

    def test_items_go_to_the_inbox_or_their_project(self):
        backlog.add('Tether one', 'codex', 'tether')
        backlog.add('Inbox one')
        backlog.add('App one', project='my-app')
        backlog.add('Tether two', project='TETHER')
        backlog.add('Inbox two')
        self.assertEqual(self.text(), '- [ ] Inbox one #B2\n- [ ] Inbox two #B5\n\n## tether\n- [ ] Tether one (codex) #B1\n- [ ] Tether two #B4\n\n## my-app\n- [ ] App one #B3\n')

    def test_add_cleans_text_and_refuses_empty_or_full(self):
        self.assertEqual(backlog.add(' Fix\n  this '), '- [ ] Fix this #B1')
        for bad in ('', '  \n', None):
            with self.assertRaises(projects.ProjectError):
                backlog.add(bad)
        (backlog.folder() / 'BACKLOG.md').write_text('x' * (backlog.LIMIT - 2))
        with self.assertRaises(projects.ProjectError):
            backlog.add('Too much')
        self.assertEqual(len(self.text()), backlog.LIMIT - 2)

    def test_list_shows_the_inbox_and_this_project_only(self):
        backlog.save('- [ ] Mine\n  - why\n- [x] Old\n  - old detail\n\n## tether\n- [ ] Here\n- [x] Gone\n\n## other\n- [ ] Secret\n', backlog.read()['version'])
        shown = backlog.list_open('tether')
        self.assertEqual(shown, 'Inbox:\n- [ ] Mine #B1\n  - why\n\ntether:\n- [ ] Here #B3')
        self.assertNotIn('Secret', backlog.list_open('tether'))
        self.assertEqual(backlog.list_open('nothing'), 'Inbox:\n- [ ] Mine #B1\n  - why')

    def test_brief_list_keeps_the_same_items_without_their_details(self):
        backlog.save('- [ ] Mine (claude · tmux $1 · pane %2 · chat abc)\n  - Doing: codex · tmux $3 · pane %4 · chat def\n  - why\n\n  - after a blank line\n'
                     '- [x] Old\n  - old detail\n\n## tether\n- [ ] Here\n  - Doing: someone\n- [x] Gone\n\n## other\n- [ ] Secret\n  - hidden\n',
                     backlog.read()['version'])
        self.assertEqual(backlog.list_brief('tether'), 'Inbox:\n- B1 Mine [doing: codex · pane %4] [2 detail lines]\n\n'
                                                       'tether:\n- B3 Here [doing: someone]\n\nThe detail lines of an item: show B12.')
        self.assertEqual(backlog.list_brief('other'), 'Inbox:\n- B1 Mine [doing: codex · pane %4] [2 detail lines]\n\n'
                                                      'other:\n- B5 Secret [1 detail line]\n\nThe detail lines of an item: show B12.')
        self.assertEqual(backlog.show('B1'), '- [ ] Mine (claude · tmux $1 · pane %2 · chat abc) #B1\n  - Doing: codex · tmux $3 · pane %4 · chat def\n'
                                             '  - why\n  - after a blank line')
        self.assertEqual(backlog.show('b2'), '- [x] Old #B2\n  - old detail')
        with self.assertRaises(projects.ProjectError):
            backlog.show('Secret', 'tether')  # words find an item only in the inbox and this project

    def test_list_with_nothing_open(self):
        self.assertEqual(backlog.list_brief('tether'), 'No open items.')
        self.assertEqual(backlog.list_open('tether'), 'No open items.')

    def test_detail_and_done_find_one_item_in_scope(self):
        backlog.save('- [ ] Fix login (codex · chat a)\n  - first\n- [ ] Fix upload\n\n## other\n- [ ] Fix secret\n', backlog.read()['version'])
        with self.assertRaises(projects.ProjectError) as caught:
            backlog.mark_done('fix', 'tether')
        self.assertIn('2 open items', str(caught.exception))
        with self.assertRaises(projects.ProjectError):
            backlog.mark_done('secret', 'tether')
        backlog.add_detail('LOGIN', 'second', 'tether')
        backlog.mark_done('Fix login', 'tether')
        self.assertRegex(self.text(), r'^- \[x\] Fix login \(codex · chat a\) #B1\n  - first\n  - second\n  - Done: \d{4}-\d\d-\d\d \d\d:\d\d · finished\n- \[ \] Fix upload #B2\n\n## other\n- \[ \] Fix secret #B3\n$')
        with self.assertRaises(projects.ProjectError):
            backlog.mark_done('Fix login', 'tether')

    def test_an_exact_match_beats_a_longer_one(self):
        backlog.save('- [ ] Fix\n- [ ] Fix login\n', backlog.read()['version'])
        backlog.mark_done('fix')
        self.assertRegex(self.text(), r'^- \[x\] Fix #B1\n  - Done: \d{4}-\d\d-\d\d \d\d:\d\d · finished\n- \[ \] Fix login #B2\n$')

    def test_versions_keep_a_direct_overwrite_and_stay_limited(self):
        state = backlog.save('- [ ] One\n', backlog.read()['version'])
        (backlog.folder() / 'BACKLOG.md').write_text('- [ ] An agent wrote over it\n')
        backlog.save('- [ ] Two\n', backlog.read()['version'])
        copies = [path.read_text() for path in sorted((backlog.folder() / 'versions').glob('*.md'))]
        self.assertEqual(copies, ['- [ ] One #B1\n', '- [ ] An agent wrote over it\n', '- [ ] Two #B2\n'])  # B1 was given once
        self.assertTrue(state)
        with mock.patch.object(backlog, 'VERSIONS', 3):
            for at in range(5):
                backlog.add(f'Item {at}')
        self.assertEqual(len(list((backlog.folder() / 'versions').glob('*.md'))), 3)

    def test_a_failed_copy_does_not_stop_a_save(self):
        with mock.patch.object(Path, 'glob', side_effect=OSError('full')):
            backlog.save('- [ ] One\n', backlog.read()['version'])
        self.assertEqual(self.text(), '- [ ] One #B1\n')

    def test_nothing_is_lost_with_many_processes_writing(self):
        context = multiprocessing.get_context('fork')
        count = 30
        workers = [context.Process(target=_writer, args=(self.home, name, count)) for name in ('a', 'b', 'c', 'd')]
        ticker = context.Process(target=_ticker, args=(self.home, 40))
        backlog.add('Seed')
        for worker in [*workers, ticker]:
            worker.start()
        for worker in [*workers, ticker]:
            worker.join(60)
            self.assertEqual(worker.exitcode, 0)
        text = self.text()
        ids = re.findall(r'#B([0-9]+)$', text, re.M)
        self.assertEqual(len(ids), len(set(ids)))
        for name in ('a', 'b', 'c', 'd'):
            for at in range(count):
                self.assertRegex(text, rf'\] {name} {at} #B[0-9]+\n')
        self.assertFalse([name for name in os.listdir(backlog.folder()) if name.endswith('.tmp')])

    def test_routes(self):
        got = Handler('/mobile/api/projects/backlog')
        projects_api.get(got)
        self.assertEqual((got.status, got.body['text']), (200, ''))
        saved = Handler('/mobile/api/projects/backlog', {'text': '- [ ] A\n', 'version': got.body['version']})
        projects_api.post(saved)
        self.assertEqual((saved.status, saved.body['text']), (200, '- [ ] A #B1\n'))
        stale = Handler('/mobile/api/projects/backlog', {'text': 'B', 'version': got.body['version']})
        projects_api.post(stale)
        self.assertEqual(stale.status, 409)
        bad = Handler('/mobile/api/projects/backlog', {'text': 5, 'version': got.body['version']})
        projects_api.post(bad)
        self.assertEqual(bad.status, 400)

    def test_long_runs_of_spaces_save_fast(self):
        """A full list of spaces once held the backlog lock for minutes: the patterns ran in quadratic time."""
        spaces = ' ' * (backlog.LIMIT // 3 - 20)
        text = f'# x{spaces}y\n- [ ] {spaces}x (claude{spaces}\n'
        started = time.monotonic()
        saved = backlog.save(text, backlog.read()['version'])
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(saved['text'].endswith(' #B1\n'))

    def test_too_long_is_refused_before_it_is_parsed(self):
        with mock.patch.object(backlog, 'with_ids') as parse, self.assertRaises(projects.ProjectError):
            backlog.save('x' * (backlog.LIMIT + 1), backlog.read()['version'])
        parse.assert_not_called()

    def test_only_the_backlog_save_takes_a_large_body(self):
        text = '- [ ] ' + '語' * 100000 + '\n'  # over the 262,144-byte body limit of the other routes
        saved = Handler('/mobile/api/projects/backlog', {'text': text, 'version': backlog.read()['version']})
        projects_api.post(saved)
        self.assertEqual(saved.status, 200)
        added = Handler('/mobile/api/projects/backlog-add', {'text': '語' * 100000})
        projects_api.post(added)
        self.assertEqual(added.status, 413)

    def test_friday_adds_and_starts_without_a_version(self):
        added = Handler('/mobile/api/projects/backlog-add', {'text': 'Try the dark theme'})
        projects_api.post(added)
        self.assertEqual((added.status, added.body), (200, {'line': '- [ ] Try the dark theme #B1', 'ref': 'B1'}))
        started = Handler('/mobile/api/projects/backlog-start', {'item': 'B1', 'who': 'claude · pane %4'})
        projects_api.post(started)
        self.assertEqual(started.status, 200)
        self.assertEqual(self.text(), '- [ ] Try the dark theme #B1\n  - Doing: claude · pane %4\n')
        empty = Handler('/mobile/api/projects/backlog-add', {'text': ' '})
        projects_api.post(empty)
        self.assertEqual(empty.status, 400)

    def test_a_selection_adds_an_item_with_details_and_its_chat(self):
        added = Handler('/mobile/api/projects/backlog-add', {
            'text': 'Make the\nsheet resizable', 'details': ['Drag the top edge on a phone', ' ', 'Keep  the size'],
            'source': 'claude · tmux $3 · pane %12 · chat abc-1'})
        projects_api.post(added)
        self.assertEqual(added.status, 200)
        self.assertEqual(self.text(), '- [ ] Make the sheet resizable (claude · tmux $3 · pane %12 · chat abc-1) #B1\n'
                         '  - Drag the top edge on a phone\n  - Keep the size\n')
        for body in ({'text': 'A', 'source': 'claude) #B9 (x'}, {'text': 'A', 'details': 'one'}, {'text': 'A', 'details': ['x'] * 21}):
            bad = Handler('/mobile/api/projects/backlog-add', body)
            projects_api.post(bad)
            self.assertEqual(bad.status, 400, body)

    def test_agents_learn_the_commands_not_the_file(self):
        text = projects.render('a1b2c3d4e5f6')
        for part in ('agent_backlog_cli.py`', '`list`', '`add "title" --detail', '`move B12 PROJECT`', '`--project NAME`', '`start B12`', '`done B12 "result"`', 'never edit its file'):
            self.assertIn(part, text)
        self.assertNotIn('BACKLOG.md', text)

    def test_a_tab_sees_its_open_items_and_the_preview_does_not(self):
        backlog.save('- [ ] Mine\n\n## tether\n' + ''.join(f'- [ ] Item {at}\n' for at in range(6)) + '\n## other\n- [ ] Secret\n', backlog.read()['version'])
        text = projects.render('a1b2c3d4e5f6', backlog_project='tether')
        self.assertIn('### Open backlog items (7)\n- B1 Mine\n- B2 Item 0\n', text)
        self.assertIn('2 more: run `list`.', text)
        self.assertNotIn('Secret', text)
        self.assertNotIn('Open backlog items', projects.render('a1b2c3d4e5f6'))

    def test_an_empty_backlog_gives_no_session_context(self):
        backlog.add('Global')
        self.assertFalse(projects.has_context('a1b2c3d4e5f6'))

    def test_ids_are_given_once_in_file_order_and_kept(self):
        saved = backlog.save('- [ ] One #B7\n- [ ] Two\n  - [ ] sub step\n\n## p\n* [x] Three\n', backlog.read()['version'])
        self.assertEqual(saved['text'], '- [ ] One #B7\n- [ ] Two #B8\n  - [ ] sub step\n\n## p\n* [x] Three #B9\n')
        self.assertEqual(backlog.with_ids(saved['text']), saved['text'])
        self.assertEqual(backlog.add('Four'), '- [ ] Four #B10')

    def test_a_list_from_before_ids_gets_them_when_read(self):
        backlog.folder().mkdir(parents=True)
        (backlog.folder() / 'BACKLOG.md').write_text('- [ ] Old one\n- [x] Older\n')
        self.assertEqual(backlog.read()['text'], '- [ ] Old one\n- [x] Older\n')
        self.assertEqual(backlog.list_open(), 'Inbox:\n- [ ] Old one #B1')
        self.assertEqual(backlog.numbered()['text'], '- [ ] Old one #B1\n- [x] Older #B2\n')
        got = Handler('/mobile/api/projects/backlog')
        projects_api.get(got)
        self.assertTrue(got.body['command'].endswith('agent_backlog_cli.py'))

    def test_an_id_finds_any_item_and_words_still_work(self):
        backlog.save('- [ ] Fix login\n\n## other\n- [ ] Fix secret\n- [x] Old\n', backlog.read()['version'])
        backlog.add_detail('#b2', 'why', 'tether')
        self.assertIn('Fix secret #B2\n  - why\n', self.text())
        backlog.add_detail('B3', 'note on a done item')
        for bad, message in (('B3', 'already done'), ('B9', 'No item B9')):
            with self.assertRaises(projects.ProjectError) as caught:
                backlog.mark_done(bad)
            self.assertIn(message, str(caught.exception))
        self.assertEqual(backlog.mark_done('login'), '- [x] Fix login #B1')

    def test_start_then_done_records_who_and_the_result(self):
        backlog.save('- [ ] Fix login (codex · chat a)\n  - why\n', backlog.read()['version'])
        backlog.start('B1', 'claude · pane %4 · chat x')
        backlog.start('B1', 'claude · pane %5 · chat y')
        self.assertEqual(self.text(), '- [ ] Fix login (codex · chat a) #B1\n  - Doing: claude · pane %5 · chat y\n  - why\n')
        self.assertEqual(backlog.list_open(), 'Inbox:\n- [ ] Fix login (codex · chat a) #B1\n  - Doing: claude · pane %5 · chat y\n  - why')
        backlog.mark_done('B1', result='fixed in 1234abc', who='claude · chat y')
        self.assertRegex(self.text(), r'^- \[x\] Fix login \(codex · chat a\) #B1\n  - why\n  - Done: \d{4}-\d\d-\d\d \d\d:\d\d · fixed in 1234abc \(claude · chat y\)\n$')
        with self.assertRaises(projects.ProjectError):
            backlog.start('B1', 'claude')

    def test_summary_names_the_first_open_items_and_counts_all(self):
        self.assertEqual(backlog.summary('tether'), ([], 0))
        backlog.save('- [ ] Mine\n- [x] Old\n\n## tether\n- [ ] A (codex · chat a)\n- [ ] B\n\n## other\n- [ ] C\n', backlog.read()['version'])
        self.assertEqual(backlog.summary('tether', 2), (['B1 Mine', 'B3 A'], 3))

    def test_the_cli_starts_and_finishes_an_item(self):
        import agent_backlog_cli
        backlog.add('Fix it', project='')
        env = {'TMUX_PANE': '', 'CLAUDE_CODE_SESSION_ID': 'abc', 'CODEX_THREAD_ID': ''}
        with mock.patch.dict(os.environ, env), mock.patch('sys.stdout'):
            self.assertEqual(agent_backlog_cli.main(['start', 'B1']), 0)
            self.assertIn('  - Doing: claude · chat abc', self.text())
            self.assertEqual(agent_backlog_cli.main(['done', 'B1', 'shipped in PR 9']), 0)
        self.assertRegex(self.text(), r'^- \[x\] Fix it #B1\n  - Done: \d{4}-\d\d-\d\d \d\d:\d\d · shipped in PR 9 \(claude · chat abc\)\n$')

    def test_reword_and_move_keep_the_id_source_and_details(self):
        backlog.save('## other-app\n- [ ] A long title (claude · chat a) #B1\n  - Doing: claude\n\n## tether\n- [ ] Other #B2\n', backlog.read()['version'])
        self.assertEqual(backlog.reword('B1', 'Short'), '- [ ] Short (claude · chat a) #B1')
        backlog.move('B1', 'tether')
        self.assertEqual(self.text(), '## tether\n- [ ] Other #B2\n- [ ] Short (claude · chat a) #B1\n  - Doing: claude\n')
        backlog.move('B2', '')
        self.assertEqual(self.text(), '- [ ] Other #B2\n\n## tether\n- [ ] Short (claude · chat a) #B1\n  - Doing: claude\n')

    def test_the_cli_adds_a_short_title_with_details_to_another_project(self):
        import agent_backlog_cli
        env = {'TMUX_PANE': '', 'CLAUDE_CODE_SESSION_ID': 'abc', 'CODEX_THREAD_ID': ''}
        with mock.patch.dict(os.environ, env), mock.patch('sys.stdout'), mock.patch('sys.stderr') as err:
            self.assertEqual(agent_backlog_cli.main(['add', 'x' * 101]), 1)
            self.assertIn('--detail', ''.join(call.args[0] for call in err.write.call_args_list))
            self.assertEqual(agent_backlog_cli.main(['add', 'Chat links', '--detail', 'why one', '--detail', 'why two', '--project', 'tether']), 0)
            self.assertEqual(agent_backlog_cli.main(['reword', 'B1', 'Chat links for every file type']), 0)
        self.assertEqual(self.text(), '## tether\n- [ ] Chat links for every file type (claude · chat abc) #B1\n  - why one\n  - why two\n')



class BacklogAuditTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(paths, 'TETHER', root / 'tether'))
        self.enterContext(mock.patch.object(paths, 'CONFIG', root / 'config'))
        initial = '## Docs\n- [ ] Write the release notes #B56\n\n## Build\n- [ ] Fix warnings #B57\n'
        backlog.folder().mkdir(parents=True)
        (backlog.folder() / 'BACKLOG.md').write_text(initial)
        self.actor = {'source': 'ui', 'device': 'Chrome on Mac'}
        self.enterContext(audit.acting(**self.actor))

    def assert_entry(self, kind, summary, project):
        entries = audit.query()['entries']
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry['kind'], entry['summary'], entry['project'], entry['actor']),
                         (kind, summary, project, self.actor))
        self.assertNotIn('old', entry)
        self.assertNotIn('new', entry)
        return entry

    def test_add_caps_the_title(self):
        title = 'Add the checklist ' * 6
        backlog.add(title, project='Docs')
        self.assert_entry('backlog.add', f'B58 added: {title[:80].rstrip()}', 'Docs')

    def test_start_uses_the_item_project_and_ignores_a_repeat(self):
        backlog.start('B56', 'claude', project='Build')
        backlog.start('B56', 'claude', project='Build')
        self.assert_entry('backlog.start', 'B56 started: Write the release notes', 'Docs')

    def test_done_records_the_actor_and_ignores_archiving(self):
        backlog.mark_done('B57')
        backlog.archive_done()
        self.assert_entry('backlog.done', 'B57 done: Fix warnings', 'Build')

    def test_move_uses_the_actual_source_and_destination(self):
        backlog.move('B56', 'Build')
        entry = self.assert_entry('backlog.move', 'B56 moved: Write the release notes', 'Build')
        self.assertEqual(entry['detail'], {'from_project': 'Docs', 'to_project': 'Build'})

    def test_save_records_a_removal_and_no_event_for_unchanged_items(self):
        state = backlog.read()
        backlog.save(state['text'].replace('- [ ] Fix warnings #B57\n', ''), state['version'])
        self.assert_entry('backlog.remove', 'B57 removed: Fix warnings', 'Build')

    def test_work_board_status_writes_use_the_same_kind(self):
        card = backlog.board_items()[1]
        backlog.set_stage(card['id'], 'building', card['version'])
        self.assert_entry('backlog.start', 'B57 started: Fix warnings', 'Build')

    def test_sheet_mark_done_uses_the_same_kind(self):
        state = backlog.read()
        backlog.save(state['text'].replace('- [ ] Write', '- [x] Write'), state['version'])
        self.assert_entry('backlog.done', 'B56 done: Write the release notes', 'Docs')

    def test_assigning_id_to_existing_legacy_item_is_silent(self):
        file = backlog.folder() / 'BACKLOG.md'
        file.write_text(file.read_text() + '- [ ] Typed by hand\n')
        legacy = backlog.read()
        saved = backlog.save(legacy['text'], legacy['version'])
        self.assertIn('Typed by hand #B58', saved['text'])
        self.assertEqual(audit.query()['entries'], [])

    def test_an_add_preserves_legacy_id_assignment(self):
        (backlog.folder() / 'BACKLOG.md').write_text('## Docs\n- [ ] Typed by hand\n')
        self.assertEqual(backlog.add('New item', project='Docs'), '- [ ] New item #B2')
        self.assertIn('Typed by hand #B3', backlog.read()['text'])
        self.assert_entry('backlog.add', 'B2 added: New item', 'Docs')

    def test_same_legacy_title_in_two_projects_pairs_by_project(self):
        # B56 review: both done in their own projects must not read as two moves.
        (backlog.folder() / 'BACKLOG.md').write_text('## Docs\n- [ ] Same title\n\n## Build\n- [ ] Same title\n')
        legacy = backlog.read()
        backlog.save(legacy['text'].replace('- [ ]', '- [x]'), legacy['version'])
        entries = audit.query()['entries']
        self.assertEqual(sorted((entry['kind'], entry['project']) for entry in entries),
                         [('backlog.done', 'Build'), ('backlog.done', 'Docs')])

    def test_mixed_save_with_one_title_records_one_move_and_one_done(self):
        # B56 review: in one save, move the Build item to Ops and mark the Docs item done. Each list order must give
        # one move (Build → Ops) and one done (Docs), not two moves.
        build, docs = '## Build\n- [ ] Same title\n', '## Docs\n- [ ] Same title\n'
        for build_first in (True, False):
            for ops_first in (True, False):
                with self.subTest(build_first=build_first, ops_first=ops_first):
                    for file in audit.folder().glob('*') if audit.folder().exists() else ():
                        file.unlink()
                    (backlog.folder() / 'BACKLOG.md').write_text(build + '\n' + docs if build_first else docs + '\n' + build)
                    legacy = backlog.read()
                    ops, done = '## Ops\n- [ ] Same title\n', '## Docs\n- [x] Same title\n'
                    backlog.save((ops + '\n' + done if ops_first else done + '\n' + ops) + '\n## Build\n', legacy['version'])
                    found = sorted((entry['kind'], entry['project'], entry.get('detail', {}).get('from_project'))
                                   for entry in audit.query()['entries'])
                    self.assertEqual(found, [('backlog.done', 'Docs', None), ('backlog.move', 'Ops', 'Build')])

    def test_start_a_legacy_item_tracks_its_assigned_id(self):
        (backlog.folder() / 'BACKLOG.md').write_text('## Docs\n- [ ] Typed by hand\n- [ ] Another old item\n')
        self.assertEqual(backlog.start('typed', 'claude', project='Docs'), '- [ ] Typed by hand')
        self.assert_entry('backlog.start', 'B1 started: Typed by hand', 'Docs')

    def test_save_records_removal_of_an_item_without_an_id(self):
        (backlog.folder() / 'BACKLOG.md').write_text('## Docs\n- [ ] Typed by hand\n- [ ] Another old item\n')
        legacy = backlog.read()
        backlog.save(legacy['text'].replace('- [ ] Typed by hand\n', ''), legacy['version'])
        self.assert_entry('backlog.remove', 'Removed backlog item: Typed by hand', 'Docs')


class BoardTests(unittest.TestCase):
    """The backlog's items as work board cards, and moves from the board (work_backlog.py)."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(paths, 'TETHER', Path(temp.name))
        patch.start()
        self.addCleanup(patch.stop)
        today = time.strftime('%Y-%m-%d %H:%M')
        old = time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() - 8 * 86400))
        self.text = (f'- [ ] Inbox item (codex · chat a) #B1\n  - why it matters\n\n'
                     f'## tether\n- [ ] Started #B2\n  - Doing: claude · pane %4\n  - [ ] a sub step\n'
                     f'- [x] Shipped #B3\n  - Done: {today} · in 1234abc (claude)\n'
                     f'- [x] Ticked by hand #B4\n- [x] Long ago #B5\n  - Done: {old} · old\n'
                     f'- [ ] No id yet\n')
        (backlog.folder()).mkdir(parents=True)
        (backlog.folder() / 'BACKLOG.md').write_text(self.text)

    def file(self):
        return (backlog.folder() / 'BACKLOG.md').read_text()

    def card(self, ref):
        return next(c for c in backlog.board_items() if c['id'] == ref)

    def test_cards_come_from_items_with_ids_and_reading_writes_nothing(self):
        cards = backlog.board_items()
        self.assertEqual(self.file(), self.text)
        self.assertEqual([(c['id'], c['area'], c['stage']) for c in cards],
                         [('B1', 'Inbox', 'ready'), ('B2', 'tether', 'building'), ('B3', 'tether', 'done')])
        first, started, shipped = cards
        self.assertEqual((first['title'], first['source'], first['details']), ('Inbox item', 'codex · chat a', ['why it matters']))
        self.assertEqual((started['doing'], started['details']), ('claude · pane %4', ['a sub step']))
        self.assertEqual(shipped['done'], 'in 1234abc (claude)')
        self.assertLess(abs(shipped['doneAt'] - time.time()), 120)
        self.assertEqual([c['rank'] for c in cards], [0, 1, 2])
        self.assertTrue(all(0 < c['version'] < 2 ** 53 for c in cards))

    def test_a_change_to_the_item_changes_only_its_version(self):
        before = {c['id']: c['version'] for c in backlog.board_items()}
        (backlog.folder() / 'BACKLOG.md').write_text(self.text.replace('why it matters', 'why it matters a lot'))
        after = {c['id']: c['version'] for c in backlog.board_items()}
        self.assertNotEqual(before['B1'], after['B1'])
        self.assertEqual(before['B2'], after['B2'])

    def test_moves_between_ready_building_and_done(self):
        card = backlog.set_stage('B1', 'building', self.card('B1')['version'])
        self.assertEqual((card['stage'], card['doing']), ('building', 'Mak'))
        self.assertIn('#B1\n  - Doing: Mak\n  - why it matters\n', self.file())
        card = backlog.set_stage('b-2', 'ready', self.card('B2')['version'])
        self.assertEqual((card['stage'], card['doing'], card['details']), ('ready', None, ['a sub step']))
        card = backlog.set_stage('B2', 'done', card['version'])
        self.assertEqual(card['stage'], 'done')
        self.assertRegex(self.file(), r'- \[x\] Started #B2\n  - \[ \] a sub step\n  - Done: [0-9-]+ [0-9:]+ · finished \(Mak\)\n')
        card = backlog.set_stage('B3', 'ready', self.card('B3')['version'])
        self.assertEqual((card['stage'], card['done']), ('ready', None))
        self.assertIn('- [ ] Shipped #B3\n- [x] Ticked', self.file())
        self.assertIn('- [ ] No id yet #B6\n', self.file())  # a write gives IDs, as every write does

    def test_a_move_to_the_same_stage_changes_nothing(self):
        backlog.set_stage('B2', 'building', self.card('B2')['version'])
        backlog.set_stage('B3', 'done', self.card('B3')['version'])
        self.assertEqual(self.file(), self.text)

    def test_refuses_a_stale_version_other_stages_and_bad_ids(self):
        version = self.card('B1')['version']
        backlog.add_detail('B1', 'an agent wrote this')
        with self.assertRaises(projects.ProjectError) as caught:
            backlog.set_stage('B1', 'done', version)
        self.assertEqual(caught.exception.status, 409)
        for ref, stage, why in (('B1', 'review', 'Ready, Building or Done'), ('B99', 'done', 'No item B99'),
                                ('T-1', 'done', 'like B12'), ('B1', 'done', 'Reload')):
            with self.assertRaises(projects.ProjectError) as caught:
                backlog.set_stage(ref, stage, 'x' if why == 'Reload' else 1)
            self.assertIn(why, str(caught.exception))
        self.assertNotIn('[x] Inbox item', self.file())


    def test_status_lines_after_a_blank_line_belong_to_the_item(self):
        text = '- [ ] Spaced #B1\n  - why\n\n  - Doing: claude\n- [ ] Again #B2\n  - why\n\n  - Done: long ago\n'
        (backlog.folder() / 'BACKLOG.md').write_text(text)
        self.assertEqual(self.card('B1')['stage'], 'building')
        card = backlog.set_stage('B1', 'ready', self.card('B1')['version'])
        self.assertEqual((card['stage'], card['doing']), ('ready', None))
        card = backlog.set_stage('B2', 'done', self.card('B2')['version'])  # the old Done line goes; the new one counts
        self.assertEqual((card['stage'], card['done']), ('done', 'finished (Mak)'))
        self.assertNotIn('long ago', self.file())

    def test_the_archive_keeps_this_weeks_done_items_on_the_board(self):
        filler = 'word ' * 200
        old = ''.join(f'- [x] Old {n} #B{n + 10}\n  - Done: {filler}\n' for n in range(60))
        (backlog.folder() / 'BACKLOG.md').write_text(self.text + old)
        self.assertGreater(len(self.file()), backlog.ARCHIVE_AT)
        card = backlog.set_stage('B1', 'done', self.card('B1')['version'])
        self.assertEqual(card['stage'], 'done')
        self.assertNotIn('gone', card)
        kept, done = self.file(), (backlog.folder() / 'DONE.md').read_text()
        self.assertLessEqual(len(kept), backlog.ARCHIVE_TO)
        self.assertIn('- [x] Shipped #B3', kept)  # done today: it stays
        self.assertIn('Old 0 #B10', done)
        self.assertNotIn('Inbox item', done)

    def test_the_last_done_line_counts_for_the_card_and_the_archive(self):
        today = time.strftime('%Y-%m-%d %H:%M')
        old = time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() - 8 * 86400))
        twice = f'- [x] Done twice #B9\n  - Done: {old} · first\n  - Done: {today} · again\n'
        filler = ''.join(f'- [x] Old {n} #B{n + 10}\n  - Done: ' + 'word ' * 200 + '\n' for n in range(60))
        (backlog.folder() / 'BACKLOG.md').write_text(twice + filler)
        self.assertEqual(self.card('B9')['done'], 'again')
        backlog.add('Push it past the archive size')
        self.assertIn('Done twice #B9', self.file())
        self.assertEqual(self.card('B9')['stage'], 'done')

    def test_this_weeks_done_items_move_too_when_the_list_would_not_fit(self):
        today = time.strftime('%Y-%m-%d %H:%M')
        done = ''.join(f'- [x] Done {n} ' + 'x' * 900 + f' #B{n}\n  - Done: {today} · ok\n' for n in range(1, 41))
        open_part = '- [ ] Open ' + 'y' * (backlog.LIMIT - 1000) + ' #B41\n'
        backlog.save(done + open_part, backlog.read()['version'])
        self.assertEqual(self.file(), open_part)


if __name__ == '__main__':
    unittest.main()
