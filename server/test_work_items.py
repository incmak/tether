import io
import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_item_cli
import paths
import work_api
import work_fix
import work_items
from projects import ProjectError

PROJECT = {'name': 'shop', 'prefix': 'T', 'folder': '/srv/shop', 'triage': 'Triage', 'work': 'Shop', 'jira': 'BS'}


def _add_many(home, count):
    with mock.patch.object(paths, 'TETHER', Path(home)):
        for number in range(count):
            work_items.new('shop', f'item {number}', 'agent')


class Store(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        patcher = mock.patch.object(paths, 'TETHER', self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        work_items._CACHE.clear()
        work_items.save_project(dict(PROJECT), 0, create=True)

    def events_file(self, prefix='T'):
        return self.home / 'items' / prefix / 'items.jsonl'

    def test_paths_live_outside_session_bundles(self):
        self.assertEqual(work_items._registry_path(), self.home / 'work' / 'projects.json')
        self.assertEqual(self.events_file(), work_items._items_path('T'))

    def test_new_item_gets_next_id_and_version_one(self):
        first = work_items.new('shop', 'Login fails', 'agent', refs=['BS-12'])
        second = work_items.new('shop', 'Cart is empty', 'mak')
        self.assertEqual((first['id'], first['stage'], first['version']), ('T-001', 'triage', 1))
        self.assertEqual(second['id'], 'T-002')
        self.assertEqual(first['refs'], ['BS-12'])
        self.assertEqual(second['history'][0]['text'], 'Added: Cart is empty')

    def test_ask_answer_build_moves_to_ready(self):
        item = work_items.new('shop', 'Login fails', 'agent')
        asked = work_items.ask('T-001', 'Fix the token check?', ['Build', 'Hold'], pane='%5', chat='abc')
        self.assertEqual(asked['stage'], 'needs')
        self.assertEqual(asked['askFrom'], 'triage')
        self.assertEqual(len(work_items.needs([asked])), 1)
        done = work_items.answer('T-001', asked['ask']['id'], asked['version'], choice='Build')
        self.assertEqual(done['stage'], 'ready')
        self.assertIsNone(done['ask'])
        self.assertEqual(done['history'][-1]['text'], 'You chose: Build')
        self.assertGreater(done['version'], item['version'])

    def test_answer_in_words_returns_to_previous_stage(self):
        work_items.new('shop', 'Login fails', 'agent', stage='building')
        asked = work_items.ask('T-001', 'Which page?', ['Login', 'Signup'])
        done = work_items.answer('T-001', asked['ask']['id'], asked['version'], text='Both of them')
        self.assertEqual(done['stage'], 'building')
        self.assertEqual(done['answer']['text'], 'Both of them')

    def test_hold_answer_parks_item(self):
        work_items.new('shop', 'Login fails', 'agent')
        asked = work_items.ask('T-001', 'Fix now?', ['Build', 'Hold'])
        self.assertEqual(work_items.answer('T-001', asked['ask']['id'], asked['version'], choice='Hold')['stage'], 'hold')

    def test_stale_version_is_refused(self):
        item = work_items.new('shop', 'Login fails', 'agent')
        work_items.note('T-001', 'agent looked', 'agent')
        with self.assertRaises(ProjectError) as caught:
            work_items.stage('T-001', 'hold', 'mak', version=item['version'])
        self.assertEqual(caught.exception.status, 409)

    def test_stale_ask_is_refused(self):
        work_items.new('shop', 'Login fails', 'agent')
        first = work_items.ask('T-001', 'One?', ['Build'])
        second = work_items.ask('T-001', 'Two?', ['Build', 'Hold'])
        with self.assertRaises(ProjectError) as caught:
            work_items.answer('T-001', first['ask']['id'], second['version'], choice='Build')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(second['askFrom'], 'triage')  # a second ask keeps where it came from

    def test_answer_must_be_an_option(self):
        work_items.new('shop', 'Login fails', 'agent')
        asked = work_items.ask('T-001', 'One?', ['Build'])
        with self.assertRaises(ProjectError) as caught:
            work_items.answer('T-001', asked['ask']['id'], asked['version'], choice='Delete everything')
        self.assertEqual(caught.exception.status, 400)

    def test_answer_twice_is_refused(self):
        work_items.new('shop', 'Login fails', 'agent')
        asked = work_items.ask('T-001', 'One?', ['Build'])
        done = work_items.answer('T-001', asked['ask']['id'], asked['version'], choice='Build')
        with self.assertRaises(ProjectError):
            work_items.answer('T-001', asked['ask']['id'], done['version'], choice='Build')

    def test_cannot_ask_on_done_or_stage_to_needs(self):
        work_items.new('shop', 'Login fails', 'agent', stage='done')
        with self.assertRaises(ProjectError):
            work_items.ask('T-001', 'Again?', ['Build'])
        with self.assertRaises(ProjectError):
            work_items.stage('T-001', 'needs', 'mak')

    def test_stage_away_from_needs_clears_ask(self):
        work_items.new('shop', 'Login fails', 'agent')
        work_items.ask('T-001', 'One?', ['Build'])
        moved = work_items.stage('T-001', 'hold', 'mak', text='later')
        self.assertIsNone(moved['ask'])
        self.assertEqual(moved['history'][-1]['text'], 'Moved to On hold: later')

    def test_done_sets_and_reopen_clears_done_at(self):
        work_items.new('shop', 'Login fails', 'agent')
        self.assertIsNotNone(work_items.stage('T-001', 'done', 'agent')['doneAt'])
        self.assertIsNone(work_items.stage('T-001', 'review', 'agent')['doneAt'])

    def test_links_and_tab_marks(self):
        work_items.new('shop', 'Login fails', 'agent')
        work_items.link('T-001', '%7', 'fix', chat='chat-1', name='Fix login', stack='Fixes')
        item = work_items.link('T-001', '%7', 'review')  # relink replaces, never duplicates
        self.assertEqual([(t['pane'], t['role']) for t in item['tabs']], [('%7', 'review')])
        self.assertEqual(work_items.tab_marks([item])['%7']['item'], 'T-001')
        self.assertEqual(work_items.unlink('T-001', '%7')['tabs'], [])

    def test_bad_input_is_refused(self):
        work_items.new('shop', 'Login fails', 'agent')
        for call in (lambda: work_items.new('shop', '', 'agent'),
                     lambda: work_items.new('shop', 'x' * 201, 'agent'),
                     lambda: work_items.new('nope', 'x', 'agent'),
                     lambda: work_items.new('shop', 'x', 'agent', item_id='Q-001'),
                     lambda: work_items.new('shop', 'x', 'agent', item_id='T-001'),
                     lambda: work_items.ask('T-001', 'q', []),
                     lambda: work_items.ask('T-001', 'q', ['A', 'a']),
                     lambda: work_items.ask('T-001', 'q', ['A'], pane='7'),
                     lambda: work_items.link('T-001', '%7', 'boss'),
                     lambda: work_items.find('../etc')):
            with self.assertRaises(ProjectError):
                call()

    def test_import_keeps_id_and_counts_on(self):
        work_items.new('shop', 'Old one', 'agent', item_id='t-041')
        self.assertEqual(work_items.new('shop', 'Next', 'agent')['id'], 'T-042')

    def test_projects_are_isolated(self):
        work_items.save_project({'name': 'web', 'prefix': 'W', 'folder': '/srv/web'}, 1, create=True)
        work_items.new('shop', 'Shop item', 'agent')
        work_items.new('web', 'Web item', 'agent')
        # A line with another prefix in a project's file is ignored.
        with open(self.events_file('W'), 'a') as out:
            out.write(json.dumps({'item': 'T-001', 'type': 'stage', 'stage': 'done', 'n': 99}) + '\n')
        board = work_items.board()
        self.assertEqual(sorted(i['id'] for i in board['items']), ['T-001', 'W-001'])
        self.assertEqual(work_items.find('T-001')[1]['stage'], 'triage')

    def test_project_rules(self):
        with self.assertRaises(ProjectError) as caught:
            work_items.save_project(dict(PROJECT, name='other'), 0, create=True)
        self.assertEqual(caught.exception.status, 409)  # stale revision
        with self.assertRaises(ProjectError) as caught:
            work_items.save_project(dict(PROJECT, name='other'), 1, create=True)
        self.assertEqual(caught.exception.status, 409)  # prefix taken
        with self.assertRaises(ProjectError) as caught:
            work_items.save_project({'name': 'back', 'prefix': 'B', 'folder': '/x'}, 1, create=True)
        self.assertEqual(caught.exception.status, 400)  # reserved for the backlog
        changed = work_items.save_project({'name': 'shop', 'prefix': 'Z', 'folder': '/srv/new'}, 1, create=False)
        shop = changed['projects'][0]
        self.assertEqual((shop['prefix'], shop['folder'], changed['revision']), ('T', '/srv/new', 2))

    def test_torn_tail_is_skipped_and_next_write_starts_a_new_line(self):
        work_items.new('shop', 'Login fails', 'agent')
        with open(self.events_file(), 'a') as out:
            out.write('{"item": "T-001", "type": "no')
        self.assertEqual(work_items.find('T-001')[1]['version'], 1)
        work_items.note('T-001', 'after the crash', 'agent')
        item = work_items.find('T-001')[1]
        self.assertEqual((item['version'], item['history'][-1]['text']), (2, 'after the crash'))
        numbers = [e['n'] for e in work_items._events('T')]
        self.assertEqual(numbers, [1, 2])

    def test_out_of_order_event_number_is_skipped(self):
        work_items.new('shop', 'Login fails', 'agent')
        with open(self.events_file(), 'a') as out:
            out.write(json.dumps({'item': 'T-001', 'type': 'stage', 'stage': 'done', 'n': 1}) + '\n')
        self.assertEqual(work_items.find('T-001')[1]['stage'], 'triage')

    def test_symlinked_events_file_is_refused(self):
        target = self.home / 'elsewhere.jsonl'
        target.write_text('')
        self.events_file().parent.mkdir(parents=True)
        self.events_file().symlink_to(target)
        with self.assertRaises((ProjectError, OSError)):
            work_items.new('shop', 'Login fails', 'agent')
        self.assertEqual(target.read_text(), '')

    def test_symlinked_project_folder_is_refused(self):
        target = self.home / 'elsewhere'
        target.mkdir()
        (self.home / 'items').mkdir()
        (self.home / 'items' / 'T').symlink_to(target)
        with self.assertRaises(ProjectError):
            work_items.new('shop', 'Login fails', 'agent')
        self.assertEqual(list(target.iterdir()), [])

    def test_folder_swapped_for_a_link_gets_no_write(self):
        work_items.new('shop', 'Login fails', 'agent')
        target = self.home / 'elsewhere'
        target.mkdir()
        os.rename(self.home / 'items' / 'T', self.home / 'moved')
        (self.home / 'items' / 'T').symlink_to(target)
        real_open = work_items._open_project
        with self.assertRaises(ProjectError):
            work_items._append('T', {'item': 'T-001', 'type': 'note', 'text': 'x'})
        with mock.patch.object(work_items, '_events', return_value=[]), self.assertRaises(ProjectError):
            real_open('T', create=True)
        self.assertEqual(list(target.iterdir()), [])

    def test_fifo_is_refused_without_hanging(self):
        self.events_file().parent.mkdir(parents=True)
        os.mkfifo(self.events_file())
        with self.assertRaises(ProjectError):
            work_items._events('T')
        self.assertEqual(work_items.board()['items'], [])

    def test_ill_typed_events_are_skipped(self):
        work_items.new('shop', 'Login fails', 'agent')
        bad = [{'item': 'T-002', 'type': 'new', 'refs': 1, 'n': 2},
               {'item': 'T-001', 'type': 'stage', 'stage': 'gone', 'n': 3},
               {'item': 'T-001', 'type': 'link', 'pane': 7, 'role': 'fix', 'n': 4},
               {'item': 'T-001', 'type': 'ask', 'q': 'x', 'options': 'Build', 'n': 5},
               {'item': 'T-001', 'type': 'note', 'text': 'x', 'at': 'soon', 'n': 6},
               {'item': 'T-001', 'type': 'qa', 'qa': 'yes', 'n': 7},
               {'item': 'T-001', 'type': 'note', 'text': 'x', 'n': True}]
        with open(self.events_file(), 'a') as out:
            for event in bad:
                out.write(json.dumps(event) + '\n')
        board = work_items.board()
        self.assertEqual([(i['id'], i['version'], i['stage']) for i in board['items']], [('T-001', 1, 'triage')])
        self.assertEqual(work_items.note('T-001', 'fine', 'agent')['version'], 2)

    def test_concurrent_writers_never_lose_or_repeat_an_event(self):
        context = multiprocessing.get_context('fork')
        workers = [context.Process(target=_add_many, args=(str(self.home), 15)) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        work_items._CACHE.clear()
        items = work_items.board()['items']
        self.assertEqual(len(items), 60)
        self.assertEqual(len({i['id'] for i in items}), 60)
        self.assertEqual([e['n'] for e in work_items._events('T')], list(range(1, 61)))

    def test_items_survive_session_bundle_changes(self):
        work_items.new('shop', 'Login fails', 'agent')
        bundles = self.home / 'projects'
        bundles.mkdir()
        (bundles / 'a1b2c3d4e5f6').mkdir()
        os.rename(bundles / 'a1b2c3d4e5f6', self.home / 'archived')  # a merge or archive moves bundles only
        self.assertEqual(work_items.find('T-001')[1]['title'], 'Login fails')


class Handler:
    def __init__(self, path, body=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.sent = None

    def respond(self, status, payload):
        self.sent = (status, payload)


class Api(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(paths, 'TETHER', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        work_items._CACHE.clear()
        work_items.save_project(dict(PROJECT), 0, create=True)
        work_items.new('shop', 'Login fails', 'agent')
        self.asked = work_items.ask('T-001', 'Fix it?', ['Build', 'Hold'], pane='%5', chat='chat-1')
        self.entry = {'id': '%5', 'thread': 'chat-1', 'writable': True}
        self.state = 'Working'
        self.sends = []

    def post(self, name, body):
        handler = Handler(work_api.PREFIX + name, body)
        work_api.post(handler, sessions=lambda: [self.entry], status=lambda entry: {'state': self.state},
                      send=lambda entry, text, state: self.sends.append((entry['id'], text)))
        return handler.sent

    def answer(self, **extra):
        body = {'item': 'T-001', 'ask': self.asked['ask']['id'], 'version': self.asked['version'], 'choice': 'Build'}
        return self.post('answer', dict(body, **extra))

    def test_board(self):
        handler = Handler(work_api.PREFIX + 'board')
        work_api.get(handler)
        status, payload = handler.sent
        self.assertEqual(status, 200)
        self.assertEqual(payload['items'][0]['stage'], 'needs')

    def test_answer_goes_to_the_asking_tab(self):
        status, payload = self.answer()
        self.assertEqual(status, 200)
        self.assertTrue(payload['sent'])
        self.assertEqual(self.sends[0][0], '%5')
        self.assertIn('Mak answered T-001 on the work board: Build', self.sends[0][1])
        self.assertEqual(payload['item']['stage'], 'ready')
        self.assertEqual(payload['item']['history'][-1]['text'], 'Sent to tab %5')

    def test_answer_is_kept_when_the_pane_runs_another_chat(self):
        self.entry['thread'] = 'chat-2'
        status, payload = self.answer()
        self.assertEqual(status, 200)
        self.assertFalse(payload['sent'])
        self.assertEqual(self.sends, [])
        self.assertIn('another conversation', payload['item']['history'][-1]['text'])
        self.assertEqual(payload['item']['stage'], 'ready')

    def test_answer_is_kept_when_the_tab_is_closed_or_waits_on_a_prompt(self):
        self.state = 'Approval'
        self.assertFalse(self.answer()[1]['sent'])
        work_items.stage('T-001', 'triage', 'mak')
        self.asked = work_items.ask('T-001', 'Again?', ['Build'], pane='%9', chat='chat-1')
        self.state = 'Working'
        self.assertIn('closed', self.answer()[1]['why'])
        self.assertEqual(self.sends, [])

    def test_answer_is_kept_when_the_asking_chat_is_unknown(self):
        work_items.stage('T-001', 'triage', 'mak')
        self.asked = work_items.ask('T-001', 'Again?', ['Build'], pane='%5', chat='')
        payload = self.answer()[1]
        self.assertFalse(payload['sent'])
        self.assertIn('which conversation', payload['why'])
        self.assertEqual(self.sends, [])

    def test_stale_answer_is_409_and_sends_nothing(self):
        self.answer()
        status, _ = self.answer()
        self.assertEqual(status, 409)
        self.assertEqual(len(self.sends), 1)

    def test_writes_need_a_version(self):
        status, _ = self.post('stage', {'item': 'T-001', 'stage': 'hold'})
        self.assertEqual(status, 400)
        status, payload = self.post('stage', {'item': 'T-001', 'stage': 'hold', 'version': self.asked['version']})
        self.assertEqual((status, payload['item']['stage']), (200, 'hold'))

    def test_new_item_and_project(self):
        status, payload = self.post('item', {'project': 'shop', 'title': 'From Mak'})
        self.assertEqual((status, payload['item']['id']), (200, 'T-002'))
        status, payload = self.post('project', {'create': True, 'revision': 1,
                                                'project': {'name': 'web', 'prefix': 'W', 'folder': '/srv/web'}})
        self.assertEqual((status, payload['revision']), (200, 2))

    def test_bad_body(self):
        handler = Handler(work_api.PREFIX + 'stage')
        handler.headers = {'Content-Length': '0'}
        work_api.post(handler)
        self.assertEqual(handler.sent[0], 413)
        self.assertEqual(self.post('nope', {'version': 1})[0], 404)


class Watcher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(paths, 'TETHER', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        work_items._CACHE.clear()
        work_items.save_project(dict(PROJECT), 0, create=True)
        self.sent = []
        self.watcher = work_api.Watcher(lambda payload, urgent: self.sent.append((payload, urgent)), every=0)

    def test_first_look_sends_nothing_then_each_new_ask_once(self):
        work_items.new('shop', 'Login fails', 'agent')
        work_items.ask('T-001', 'Old question?', ['Build'])
        self.assertEqual(self.watcher.observe(), 0)
        asked = work_items.ask('T-001', 'Fix the token check?', ['Build', 'Hold'])
        self.assertEqual(self.watcher.observe(), 1)
        payload, urgent = self.sent[0]
        self.assertTrue(urgent)
        self.assertEqual((payload['go'], payload['item'], payload['tag']), ('work', 'T-001', 'work-T-001'))
        self.assertEqual(payload['body'], 'Fix the token check?')
        self.assertEqual(self.watcher.observe(), 0)
        # A restart reads the saved number: no repeat.
        self.assertEqual(work_api.Watcher(lambda *a: self.sent.append(a), every=0).observe(), 0)
        work_items.answer('T-001', asked['ask']['id'], asked['version'], choice='Build')
        self.assertEqual(self.watcher.observe(), 0)

    def test_answered_before_the_look_sends_nothing(self):
        work_items.new('shop', 'Login fails', 'agent')
        self.watcher.observe()
        asked = work_items.ask('T-001', 'Quick?', ['Build'])
        work_items.answer('T-001', asked['ask']['id'], asked['version'], choice='Build')
        self.assertEqual(self.watcher.observe(), 0)

    def test_long_question_is_cut(self):
        work_items.new('shop', 'Login fails', 'agent')
        self.watcher.observe()
        work_items.ask('T-001', 'word ' * 100, ['Build'])
        self.watcher.observe()
        self.assertLessEqual(len(self.sent[0][0]['body']), work_api.NOTICE_LIMIT)

    def test_urgent_setting_off_or_silent(self):
        mode = {'urgent': 'off'}
        watcher = work_api.Watcher(lambda payload, urgent: self.sent.append(payload), every=0, delivery=lambda: mode)
        work_items.new('shop', 'Login fails', 'agent')
        watcher.observe()
        work_items.ask('T-001', 'One?', ['Build'])
        self.assertEqual(watcher.observe(), 0)
        self.assertEqual(self.sent, [])
        mode['urgent'] = 'silent'
        work_items.ask('T-001', 'Two?', ['Build'])
        self.assertEqual(watcher.observe(), 1)
        self.assertTrue(self.sent[0]['silent'])
        self.assertEqual(self.sent[0]['body'], 'Two?')


class Cli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(paths, 'TETHER', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        work_items._CACHE.clear()
        tab = mock.patch.object(agent_item_cli, 'tab', return_value={'name': 'Triage tab', 'session': 'Triage', 'stack': ''})
        tab.start()
        self.addCleanup(tab.stop)

    def run_cli(self, *argv, pane='%4'):
        out, err = io.StringIO(), io.StringIO()
        env = {'TMUX_PANE': pane, 'CLAUDE_CODE_SESSION_ID': 'chat-9'} if pane else {}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            if not pane:
                os.environ.pop('TMUX_PANE', None)
            code = agent_item_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_flow(self):
        code, out, _ = self.run_cli('project', 'add', '--name', 'shop', '--prefix', 'T', '--folder', '/srv/shop')
        self.assertEqual((code, out.strip()), (0, 'Added project shop.'))
        code, out, _ = self.run_cli('new', 'Login fails', '--project', 'shop', '--ref', 'BS-1', '--link')
        self.assertEqual(code, 0)
        self.assertIn('T-001 [Triage] Login fails', out)
        code, out, _ = self.run_cli('ask', 'T-001', 'Fix it?', '--option', 'Build', '--option', 'Hold')
        self.assertEqual(code, 0)
        self.assertIn('Mak sees it in Needs you', out)
        item = work_items.find('T-001')[1]
        self.assertEqual(item['ask']['pane'], '%4')
        self.assertEqual([t['pane'] for t in item['tabs']], ['%4'])
        code, out, _ = self.run_cli('show', 'T-001')
        self.assertIn('Asks: Fix it? [Build / Hold]', out)
        code, out, _ = self.run_cli('list')
        self.assertIn('T-001 [Needs you]', out)

    def test_ask_outside_a_tab_fails(self):
        self.run_cli('project', 'add', '--name', 'shop', '--prefix', 'T', '--folder', '/srv/shop')
        self.run_cli('new', 'Login fails', '--project', 'shop')
        code, _, err = self.run_cli('ask', 'T-001', 'Fix?', '--option', 'Build', pane='')
        self.assertEqual(code, 1)
        self.assertIn('Run ask from a Tether tab', err)

    def test_write_failure_says_restart(self):
        with mock.patch.object(work_items, 'new', side_effect=PermissionError()):
            code, _, err = self.run_cli('new', 'x', '--project', 'shop')
        self.assertEqual(code, 1)
        self.assertIn('restart this tab', err)


if __name__ == '__main__':
    unittest.main()


class FixTab(unittest.TestCase):
    """A Build answer opens a fix tab in the project's work session (stack Fixes) and moves the item to Building."""
    answer = Api.answer

    def setUp(self):
        Api.setUp(self)
        self.work = {'id': '%7', 'tmux_session': 'Shop', 'label': 'Shop', 'agent': 'claude', 'thread': 'chat-7',
                     'writable': True}
        self.entry['agent'] = 'claude'
        self.entries = [self.entry, self.work]
        self.started = []
        for target, value in (('group_of_pane', lambda pane, run: ('$1', 'Shop')),
                              ('find_or_create', lambda session, name, run: {'id': 'g-fixes', 'name': name})):
            patcher = mock.patch.object(work_fix.groups, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def start(entry, body, folder, run=None, group=None, **_):
            self.started.append((entry['id'], body, group))
            return '%42', body['name']
        patcher = mock.patch.object(work_fix.delegate, 'start', side_effect=start)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.temp.name
        patcher = mock.patch.object(work_fix.work_items, 'project', side_effect=lambda name: dict(PROJECT, folder=self.folder))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = {'%7': 'Shop'}

    def tmux(self, command, **_):
        """Fake tmux: display-message tells the pane's current session."""
        pane = command[command.index('-t') + 1]
        if pane not in self.sessions:
            return subprocess.CompletedProcess(command, 1, '', "can't find pane")
        return subprocess.CompletedProcess(command, 0, f'{pane}\t{self.sessions[pane]}\n', '')

    def post(self, name, body):
        handler = Handler(work_api.PREFIX + name, body)
        work_api.post(handler, sessions=lambda: self.entries, status=lambda entry: {'state': self.state},
                      send=lambda entry, text, state: self.sends.append((entry['id'], text)), run=self.tmux)
        return handler.sent

    def test_build_starts_a_linked_fix_tab(self):
        status, payload = self.answer()
        self.assertEqual(status, 200)
        self.assertEqual(payload['fix'], {'started': True, 'why': ''})
        item = payload['item']
        self.assertEqual(item['stage'], 'building')
        tab = next(tab for tab in item['tabs'] if tab['pane'] == '%42')
        self.assertEqual((tab['role'], tab['stack'], tab['name']), ('fix', 'Fixes', 'T-001 fix'))
        source, body, group = self.started[0]
        self.assertEqual((source, group, body['agent']), ('%7', 'g-fixes', 'claude'))
        self.assertIn('Fix work item T-001: Login fails', body['message'])
        self.assertIn('Mak chose Build on: Fix it?', body['message'])

    def test_hold_starts_no_fix_tab(self):
        status, payload = self.answer(choice='Hold')
        self.assertEqual(payload['item']['stage'], 'hold')
        self.assertNotIn('fix', payload)
        self.assertEqual(self.started, [])

    def test_closed_work_session_keeps_the_item_ready(self):
        self.entries = [self.entry]
        status, payload = self.answer()
        self.assertEqual(status, 200)
        self.assertFalse(payload['fix']['started'])
        self.assertIn('work session Shop is not open', payload['fix']['why'])
        self.assertEqual(payload['item']['stage'], 'ready')
        self.assertTrue(payload['item']['history'][-1]['text'].startswith('Fix tab not started. The work session'))
        self.assertEqual(self.started, [])

    def test_a_failed_launch_keeps_the_item_ready(self):
        work_fix.delegate.start.side_effect = RuntimeError('tmux refused')
        payload = self.answer()[1]
        self.assertEqual(payload['fix'], {'started': False, 'why': 'tmux refused'})
        self.assertEqual(payload['item']['stage'], 'ready')

    def test_retry_starts_the_tab_and_rejects_stale_or_doubled_starts(self):
        self.entries = [self.entry]
        item = self.answer()[1]['item']
        self.entries = [self.entry, self.work]
        status, payload = self.post('fix', {'item': 'T-001', 'version': item['version'] - 1})
        self.assertEqual(status, 409)
        status, payload = self.post('fix', {'item': 'T-001', 'version': item['version']})
        self.assertEqual(status, 200)
        self.assertTrue(payload['fix']['started'])
        self.assertEqual(payload['item']['stage'], 'building')
        self.entries.append({'id': '%42', 'thread': 'chat-42', 'agent': 'claude'})
        status, payload = self.post('fix', {'item': 'T-001', 'version': payload['item']['version']})
        self.assertEqual(status, 409)
        self.assertEqual(len(self.started), 1)

    def test_live_fix_tab_blocks_a_second_one(self):
        item = self.answer()[1]['item']
        work_items.stage('T-001', 'ready', 'mak', version=item['version'])
        self.entries.append({'id': '%42', 'thread': 'chat-42', 'agent': 'claude'})
        status, payload = self.post('fix', {'item': 'T-001', 'version': work_items.find('T-001')[1]['version']})
        self.assertEqual(status, 409)
        self.assertIn('already has a fix tab', payload['error'])

    def test_a_fix_pane_left_at_a_shell_does_not_block_a_retry(self):
        item = self.answer()[1]['item']
        work_items.stage('T-001', 'ready', 'mak', version=item['version'])
        self.entries.append({'id': '%42', 'thread': None, 'agent': 'shell'})
        status, payload = self.post('fix', {'item': 'T-001', 'version': work_items.find('T-001')[1]['version']})
        self.assertEqual(status, 200)
        self.assertTrue(payload['fix']['started'])

    def test_an_anchor_moved_to_another_session_starts_nothing(self):
        self.sessions['%7'] = 'Other'
        payload = self.answer()[1]
        self.assertFalse(payload['fix']['started'])
        self.assertIn('no longer in the work session Shop', payload['fix']['why'])
        self.assertEqual(self.started, [])
        self.assertEqual(payload['item']['stage'], 'ready')

    def test_a_missing_project_folder_starts_nothing(self):
        self.folder = os.path.join(self.temp.name, 'gone')
        payload = self.answer()[1]
        self.assertFalse(payload['fix']['started'])
        self.assertIn('project folder', payload['fix']['why'])
        self.assertEqual(self.started, [])

    def test_a_move_while_waiting_for_the_lock_starts_nothing(self):
        self.entries = [self.entry]
        item = self.answer()[1]['item']
        self.entries = [self.entry, self.work]

        class Lock:
            def __enter__(lock):
                work_items.stage('T-001', 'hold', 'mak')
            def __exit__(lock, *exc):
                return False
        with self.assertRaises(work_fix.ProjectError) as caught:
            work_fix.try_start(item, self.entries, self.tmux, Lock(), strict=True)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.started, [])
        self.assertEqual(work_items.find('T-001')[1]['stage'], 'hold')

    def test_a_failed_launch_returns_the_claim(self):
        work_fix.delegate.start.side_effect = RuntimeError('tmux refused')
        item = self.answer()[1]['item']
        self.assertEqual(item['stage'], 'ready')
        self.assertIn('Starting fix tab', ' '.join(entry.get('text', '') for entry in item['history']))

    def test_a_note_during_a_failed_launch_still_returns_the_claim(self):
        def start(entry, body, folder, run=None, group=None, **_):
            work_items.note('T-001', 'meanwhile', 'mak')
            raise RuntimeError('tmux refused')
        work_fix.delegate.start.side_effect = start
        payload = self.answer()[1]
        self.assertFalse(payload['fix']['started'])
        self.assertEqual(payload['item']['stage'], 'ready')

    def test_a_stage_move_during_a_failed_launch_is_kept(self):
        def start(entry, body, folder, run=None, group=None, **_):
            work_items.stage('T-001', 'hold', 'mak')
            raise RuntimeError('tmux refused')
        work_fix.delegate.start.side_effect = start
        self.assertEqual(self.answer()[1]['item']['stage'], 'hold')

    def test_a_change_during_the_launch_still_links_the_tab(self):
        def start(entry, body, folder, run=None, group=None, **_):
            work_items.note('T-001', 'meanwhile', 'mak')
            self.started.append(entry['id'])
            return '%42', body['name']
        work_fix.delegate.start.side_effect = start
        payload = self.answer()[1]
        self.assertTrue(payload['fix']['started'])
        self.assertEqual(payload['item']['stage'], 'building')
        self.assertTrue(any(tab['pane'] == '%42' and tab['role'] == 'fix' for tab in payload['item']['tabs']))


class TriageTab(unittest.TestCase):
    """New item with "Start a triage tab" opens an unstacked Claude tab in the triage session; the stage stays Triage."""
    tmux = FixTab.tmux
    post = FixTab.post

    def setUp(self):
        FixTab.setUp(self)
        self.triage = {'id': '%8', 'tmux_session': 'Triage', 'label': 'Triage', 'agent': 'codex', 'thread': 'chat-8',
                       'writable': True}
        self.entries.append(self.triage)
        self.sessions['%8'] = 'Triage'

    def new(self, **extra):
        return self.post('item', dict({'project': 'shop', 'title': 'Export is slow', 'refs': ['BS-9']}, **extra))

    def test_tab_starts_a_linked_triage_tab(self):
        status, payload = self.new(tab=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload['tab'], {'started': True, 'why': ''})
        item = payload['item']
        self.assertEqual((item['id'], item['stage'], item['source']), ('T-002', 'triage', 'Added by hand'))
        self.assertEqual([(t['pane'], t['role'], t['name'], t['session'], t['stack']) for t in item['tabs']],
                         [('%42', 'triage', 'T-002 triage', 'Triage', '')])
        source, body, group = self.started[0]
        self.assertEqual((source, group, body['agent']), ('%8', '', 'claude'))
        self.assertIn('Triage work item T-002: Export is slow', body['message'])
        self.assertIn('Links: BS-9', body['message'])
        self.assertIn('tether item ask T-002', body['message'])
        work_fix.groups.find_or_create.assert_not_called()

    def test_no_tab_by_default(self):
        payload = self.new()[1]
        self.assertEqual(payload['item']['tabs'], [])
        self.assertNotIn('tab', payload)
        self.assertEqual(self.started, [])

    def test_tab_must_be_a_flag(self):
        self.assertEqual(self.new(tab='yes')[0], 400)
        self.assertEqual(work_items.find('T-001')[1]['id'], 'T-001')
        with self.assertRaises(work_items.ProjectError):
            work_items.find('T-002')

    def test_closed_triage_session_keeps_the_item(self):
        self.entries = [self.entry, self.work]
        status, payload = self.new(tab=True)
        self.assertEqual(status, 200)
        self.assertFalse(payload['tab']['started'])
        self.assertIn('triage session Triage is not open', payload['tab']['why'])
        self.assertEqual(payload['item']['stage'], 'triage')
        self.assertTrue(payload['item']['history'][-1]['text'].startswith('Triage tab not started. The triage session'))
        self.assertEqual(self.started, [])

    def test_a_moved_anchor_or_failed_launch_is_reported(self):
        self.sessions['%8'] = 'Elsewhere'
        payload = self.new(tab=True)[1]
        self.assertIn('no longer in the triage session Triage', payload['tab']['why'])
        self.assertEqual(self.started, [])
        self.sessions['%8'] = 'Triage'
        work_fix.delegate.start.side_effect = RuntimeError('tmux refused')
        payload = self.new(tab=True)[1]
        self.assertEqual(payload['tab'], {'started': False, 'why': 'tmux refused'})
        self.assertEqual(payload['item']['history'][-1]['text'], 'Triage tab not started. tmux refused')

    def test_tabs_off_without_a_runner(self):
        handler = Handler(work_api.PREFIX + 'item', {'project': 'shop', 'title': 'X', 'tab': True})
        work_api.post(handler)
        status, payload = handler.sent
        self.assertEqual(status, 200)
        self.assertEqual(payload['tab'], {'started': False, 'why': 'Triage tabs are off.'})
