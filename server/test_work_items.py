import io
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_item_cli
import paths
import work_api
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
