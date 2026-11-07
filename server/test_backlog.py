"""The shared backlog: no live tmux or agent. Several processes write at once, as the server and agents do."""
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_a_full_list_fits_in_a_request_in_any_language(self):
        body = json.dumps({'text': '語' * backlog.LIMIT, 'version': 'x' * 64}, ensure_ascii=False).encode()
        self.assertLessEqual(len(body), projects_api.BODY_LIMIT)

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
        self.assertEqual(self.text(), '- [ ] Inbox one\n- [ ] Inbox two\n\n## tether\n- [ ] Tether one (codex)\n- [ ] Tether two\n\n## my-app\n- [ ] App one\n')

    def test_add_cleans_text_and_refuses_empty_or_full(self):
        self.assertEqual(backlog.add(' Fix\n  this '), '- [ ] Fix this')
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
        self.assertEqual(shown, 'Inbox:\n- [ ] Mine\n  - why\n\ntether:\n- [ ] Here')
        self.assertNotIn('Secret', backlog.list_open('tether'))
        self.assertEqual(backlog.list_open('nothing'), 'Inbox:\n- [ ] Mine\n  - why')

    def test_list_with_nothing_open(self):
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
        self.assertEqual(self.text(), '- [x] Fix login (codex · chat a)\n  - first\n  - second\n- [ ] Fix upload\n\n## other\n- [ ] Fix secret\n')
        with self.assertRaises(projects.ProjectError):
            backlog.mark_done('Fix login', 'tether')

    def test_an_exact_match_beats_a_longer_one(self):
        backlog.save('- [ ] Fix\n- [ ] Fix login\n', backlog.read()['version'])
        backlog.mark_done('fix')
        self.assertEqual(self.text(), '- [x] Fix\n- [ ] Fix login\n')

    def test_versions_keep_a_direct_overwrite_and_stay_limited(self):
        state = backlog.save('- [ ] One\n', backlog.read()['version'])
        (backlog.folder() / 'BACKLOG.md').write_text('- [ ] An agent wrote over it\n')
        backlog.save('- [ ] Two\n', backlog.read()['version'])
        copies = [path.read_text() for path in sorted((backlog.folder() / 'versions').glob('*.md'))]
        self.assertEqual(copies, ['- [ ] One\n', '- [ ] An agent wrote over it\n', '- [ ] Two\n'])
        self.assertTrue(state)
        with mock.patch.object(backlog, 'VERSIONS', 3):
            for at in range(5):
                backlog.add(f'Item {at}')
        self.assertEqual(len(list((backlog.folder() / 'versions').glob('*.md'))), 3)

    def test_a_failed_copy_does_not_stop_a_save(self):
        with mock.patch.object(backlog, '_write', side_effect=[None]), mock.patch.object(Path, 'glob', side_effect=OSError('full')):
            backlog.save('- [ ] One\n', backlog.read()['version'])

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
        for name in ('a', 'b', 'c', 'd'):
            for at in range(count):
                self.assertIn(f'] {name} {at}\n', text)
        self.assertFalse([name for name in os.listdir(backlog.folder()) if name.endswith('.tmp')])

    def test_routes(self):
        got = Handler('/mobile/api/projects/backlog')
        projects_api.get(got)
        self.assertEqual((got.status, got.body['text']), (200, ''))
        saved = Handler('/mobile/api/projects/backlog', {'text': '- [ ] A\n', 'version': got.body['version']})
        projects_api.post(saved)
        self.assertEqual((saved.status, saved.body['text']), (200, '- [ ] A\n'))
        stale = Handler('/mobile/api/projects/backlog', {'text': 'B', 'version': got.body['version']})
        projects_api.post(stale)
        self.assertEqual(stale.status, 409)
        bad = Handler('/mobile/api/projects/backlog', {'text': 5, 'version': got.body['version']})
        projects_api.post(bad)
        self.assertEqual(bad.status, 400)

    def test_agents_learn_the_commands_not_the_file(self):
        text = projects.render('a1b2c3d4e5f6')
        for part in (' list`', 'add --agent claude|codex "text"', 'detail "part of item" "text"', 'done "part of item"', 'never by editing'):
            self.assertIn(part, text)
        self.assertNotIn('BACKLOG.md', text)

    def test_an_empty_backlog_gives_no_session_context(self):
        backlog.add('Global')
        self.assertFalse(projects.has_context('a1b2c3d4e5f6'))


if __name__ == '__main__':
    unittest.main()
