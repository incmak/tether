"""Agent commands use isolated review stores and mocked launchers and HTTP."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import agent_tab_cli as cli
import agent_tabs
import audit
import auth
import delegate
import review_links
import server

THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'


class AgentTabsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(review_links, 'FILE', self.root / 'links.json'))
        self.entries = [dict(id='%12', agent='codex', thread=THREAD, codex_binding='open', writable=True,
                             transcript='/project/source.jsonl', pane_name='Source', label='Other name'),
                        dict(id='%13', agent='claude', thread=THREAD, registry='/fake/13.json',
                             writable=True, label='Reviewer'),
                        dict(id='%14', agent='claude', thread=None, pane_name='Second reviewer')]
        self.enterContext(patch.object(server, 'sessions', return_value=self.entries))
        self.fresh = self.enterContext(patch.object(server, 'fresh_sessions', return_value=self.entries))
        self.enterContext(patch.object(server, 'agent_folder', return_value=str(self.root)))
        self.enterContext(patch.object(auth, 'session_value', return_value='test-credential'))
        self.real_run = subprocess.run
        self.run = self.enterContext(patch.object(server.subprocess, 'run'))
        self.reply = self.enterContext(patch.object(server, 'last_reply', return_value='Work done.'))
        self.status = self.enterContext(patch.object(agent_tabs, 'live_status', return_value={'state': 'Idle'}))
        self.stack_calls = []
        self.start_group = agent_tabs._start_group

        @contextlib.contextmanager
        def start_group(body, caller, run, entries=(), **kwargs):  # the real rules run against tmux in test_groups.py
            self.stack_calls.append((body, caller['id'], kwargs))
            owner = dict(agent_tabs.helper_tabs.new_record(caller), root_pane=caller['id'])
            yield 'gabcdef12', 'Task', owner
        self.enterContext(patch.object(agent_tabs, '_start_group', start_group))

    def request(self, payload, headers=None, peer='127.0.0.1', denied=False, path=agent_tabs.PATH):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = path, 'POST'
        handler.client_address = (peer, 1234)
        encoded = json.dumps(dict({'session': '%12'}, **payload)).encode()
        handler.rfile = io.BytesIO(encoded)
        handler.headers = {'Host': 'localhost', 'Origin': 'http://localhost', 'X-Codex-Mobile': '1',
                           'X-Tether-Hook': 'test-credential', 'Content-Length': str(len(encoded)), **(headers or {})}
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args if handler.respond.called else None

    def link(self, worker='%12', reviewer='%13'):
        review_links.add(worker=worker, reviewer=reviewer, reviewer_thread=THREAD, name='task', agent='codex')

    def test_link_pair_rejects_invalid_or_missing_panes(self):
        for key in ('worker', 'reviewer'):
            for value in (None, '', '12', '%12\n', '%12;pwd', 12, [], {}):
                with self.subTest(key=key, value=value):
                    body = {'worker': '%12', 'reviewer': '%13'}
                    body[key] = value
                    with self.assertRaisesRegex(ValueError, 'Select worker and reviewer tabs.'):
                        agent_tabs.link_pair(body, self.entries, self.reply)
            body = {'worker': '%12', 'reviewer': '%13'}
            del body[key]
            with self.assertRaisesRegex(ValueError, 'Select worker and reviewer tabs.'):
                agent_tabs.link_pair(body, self.entries, self.reply)
            body[key] = '%99'
            with self.assertRaisesRegex(ValueError, 'That session has ended.'):
                agent_tabs.link_pair(body, self.entries, self.reply)
        self.assertEqual(review_links.reviewed_by(), {})
        self.run.assert_not_called()

    def test_link_pair_rejects_same_pane_and_unsupported_agents(self):
        with self.assertRaisesRegex(ValueError, 'Choose different worker and reviewer tabs.'):
            agent_tabs.link_pair({'worker': '%12', 'reviewer': '%12'}, self.entries, self.reply)
        for index in (0, 1):
            for agent in ('shell', 'antigravity', None):
                with self.subTest(index=index, agent=agent):
                    entries = [dict(entry) for entry in self.entries]
                    entries[index]['agent'] = agent
                    with self.assertRaisesRegex(ValueError, 'Review needs Claude or Codex tabs.'):
                        agent_tabs.link_pair({'worker': '%12', 'reviewer': '%13'}, entries, self.reply)
        self.assertEqual(review_links.reviewed_by(), {})

    def test_link_pair_rejects_existing_and_reverse_links_without_replacing_them(self):
        for worker, reviewer, message in (('%12', '%13', 'That review link already exists.'),
                                          ('%13', '%12', 'Those tabs cannot review each other.')):
            with self.subTest(worker=worker):
                self.link(worker, reviewer)
                before = (self.root / 'links.json').read_text()
                with self.assertRaisesRegex(ValueError, message):
                    agent_tabs.link_pair({'worker': '%12', 'reviewer': '%13'}, self.entries, self.reply)
                self.assertEqual((self.root / 'links.json').read_text(), before)
                review_links.remove(worker, reviewer)

    def test_link_pair_rejects_three_tab_cycle_without_changing_links(self):
        self.link('%12', '%13')
        self.link('%13', '%14')
        before = (self.root / 'links.json').read_text()
        with self.assertRaisesRegex(ValueError, 'Those tabs cannot review each other.'):
            agent_tabs.link_pair({'worker': '%14', 'reviewer': '%12'}, self.entries, self.reply)
        self.assertEqual((self.root / 'links.json').read_text(), before)

    def test_link_pair_allows_chain_without_cycle(self):
        self.link('%12', '%13')
        self.assertEqual(agent_tabs.link_pair({'worker': '%13', 'reviewer': '%14'},
                                              self.entries, self.reply), {'linked': True})
        self.assertEqual(review_links.reviewed_by(), {'%12': ['%13'], '%13': ['%14']})

    def test_link_pair_records_names_threads_and_seen_working(self):
        for thread in (THREAD, None, 'not-a-uuid', 1):
            for names, expected in (({'pane_name': 'Pane', 'agent_name': 'Agent', 'label': 'Label'}, 'Pane'),
                                     ({'agent_name': 'Agent', 'label': 'Label'}, 'Agent'),
                                     ({'label': 'Label'}, 'Label')):
                for reply, state, seen in (('Done.', 'Idle', True), ('Done.', 'Working', False),
                                            ('', 'Idle', False), ('   ', 'Idle', False)):
                    with self.subTest(thread=thread, names=names, reply=reply, state=state):
                        entries = [dict(id='%12', agent='codex', **names),
                                   dict(id='%13', agent='claude', thread=thread)]
                        self.reply.return_value = reply
                        self.status.return_value = {'state': state}
                        self.assertEqual(agent_tabs.link_pair({'worker': '%12', 'reviewer': '%13'},
                                                              entries, self.reply), {'linked': True})
                        self.assertEqual(review_links.reviewed_by(), {'%12': ['%13']})
                        link, = (row for row in json.loads(review_links.FILE.read_text()) if 'reviewer' in row)
                        self.assertEqual((link['name'], link['agent'], link['reviewer_thread'], link['seen_working']),
                                         (expected, 'codex', THREAD if thread == THREAD else None, seen))
                        self.reply.assert_called_with(entries[0])
                        review_links.remove('%12')
        self.run.assert_not_called()

    def test_link_pair_uses_store_capacity_behavior(self):
        with patch.object(review_links, 'MAX_LINKS', 2):
            self.link(worker='%20', reviewer='%21')
            self.link(worker='%22', reviewer='%23')
            self.assertEqual(agent_tabs.link_pair({'worker': '%12', 'reviewer': '%13'},
                                                  self.entries, self.reply), {'linked': True})
        self.assertEqual(review_links.reviewed_by(), {'%22': ['%23'], '%12': ['%13']})

    def test_link_action_allows_either_participant_and_rejects_other_tabs(self):
        for caller in ('%12', '%13'):
            with self.subTest(caller=caller):
                self.assertEqual(self.request({'action': 'link', 'session': caller,
                                               'worker': '%12', 'reviewer': '%13'}), (200, {'linked': True}))
                self.assertEqual(review_links.reviewed_by(), {'%12': ['%13']})
                review_links.remove('%12')
        self.assertEqual(self.request({'action': 'link', 'session': '%14', 'worker': '%12', 'reviewer': '%13'}),
                         (400, {'error': 'That review link is not on this tab.'}))
        self.assertEqual(review_links.reviewed_by(), {})
        self.run.assert_not_called()

    def test_browser_and_agent_link_use_same_function_under_send_lock(self):
        def link_pair(body, entries, last_reply, *, reset=False):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertIs(entries, self.entries)
            self.assertIs(last_reply, self.reply)
            return {'linked': True}
        with patch.object(agent_tabs, 'link_pair', side_effect=link_pair) as link:
            for path in ('/mobile/api/review-link', agent_tabs.PATH):
                self.assertEqual(self.request({'action': 'link', 'worker': '%12', 'reviewer': '%13'}, path=path),
                                 (200, {'linked': True}))
            self.assertEqual(link.call_count, 2)
            self.assertEqual([call.kwargs for call in link.call_args_list], [{'reset': True}, {}])
        self.run.assert_not_called()

    def test_browser_link_route_success_errors_auth_and_size(self):
        path = '/mobile/api/review-link'
        self.assertIn(path, server.TMUX_WRITES)
        self.assertEqual(self.request({'worker': '%12', 'reviewer': '%13'}, path=path), (200, {'linked': True}))
        self.assertEqual(review_links.reviewed_by(), {'%12': ['%13']})
        self.assertEqual(self.request({'worker': '%12', 'reviewer': '%13'}, path=path),
                         (400, {'error': 'That review link already exists.'}))
        for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
            self.assertEqual(self.request({'worker': '%12', 'reviewer': '%14'}, headers=headers, path=path)[0], 403)
        self.assertIsNone(self.request({'worker': '%12', 'reviewer': '%14'}, denied=True, path=path))
        self.assertEqual(self.request({'worker': '%12', 'reviewer': '%14', 'padding': 'x' * 65536}, path=path)[0], 413)
        self.assertEqual(review_links.reviewed_by(), {'%12': ['%13']})
        self.run.assert_not_called()

    def test_hook_auth_rejects_missing_wrong_and_nonlocal_credentials(self):
        with patch.object(delegate, 'start') as start:
            for headers, peer in (({'X-Tether-Hook': ''}, '127.0.0.1'),
                                  ({'X-Tether-Hook': 'wrong'}, '127.0.0.1'),
                                  ({'X-Tether-Hook': 'é'}, '127.0.0.1'), ({}, '192.0.2.1')):
                with self.subTest(headers=headers, peer=peer):
                    self.assertEqual(self.request({'action': 'start'}, headers=headers, peer=peer),
                                     (403, {'error': 'Local hook authentication required.'}))
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request({'action': 'start'}, headers=headers)[0], 403)
            self.assertIsNone(self.request({'action': 'start'}, denied=True))
        start.assert_not_called()
        self.run.assert_not_called()

    def test_start_fills_thread_defaults_agent_and_holds_send_lock(self):
        def start(entry, body, folder, run, recent, cwd, owner, group, name):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertEqual(group, 'gabcdef12')  # from the auto stack rules
            self.assertEqual(name, 'Task')  # the tab name from the auto stack rules
            self.assertEqual((owner['owner_pane'], owner['root_pane']), (entry['id'], entry['id']))
            self.assertEqual(self.stack_calls[-1][2], {})
            self.assertIs(entry, self.entries[0])
            self.assertEqual((body['thread'], body['agent']), (THREAD, 'codex'))
            self.assertEqual((folder, cwd), (str(self.root), str(self.root)))
            self.assertIs(run, server.subprocess.run)
            self.assertEqual(recent(), [{'role': 'user', 'text': 'Recent task'}])
            return '%15', 'New tab'
        with patch.object(delegate, 'start', side_effect=start), \
                patch.object(server, 'snapshot', return_value={'messages': [{'role': 'user', 'text': 'Recent task'}]}):
            self.assertEqual(self.request({'action': 'start', 'message': '', 'review': 'new',
                                           'thread': 'ignored', 'name': 'New tab', 'cwd': str(self.root)}),
                             (201, {'pane': '%15', 'name': 'New tab'}))

    def mock_placement(self):
        for entry in self.entries:
            entry['tmux_session'] = 'here'
        self.enterContext(patch.object(agent_tabs, '_start_group', self.start_group))
        self.enterContext(patch.object(agent_tabs.groups, 'LOCK', contextlib.nullcontext))
        stack = {'%12': {'id': 'gabcdef12', 'name': 'Source stack'},
                 '%13': {'id': 'gabcdef13', 'name': 'Owner stack'}}
        self.enterContext(patch.object(agent_tabs.groups, 'group_of_pane',
                                       side_effect=lambda pane, run: ('$1', stack[pane])))
        self.enterContext(patch.object(agent_tabs.groups, 'start_begin', return_value='token'))
        self.enterContext(patch.object(agent_tabs.groups, 'start_end'))
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout='%12\t\n')

    def test_default_and_explicit_caller_owner_use_callers_stack(self):
        self.mock_placement()
        self.entries[0]['pane_name'] = 'o' * 45
        with patch.object(delegate, 'start', return_value=('%15', 'Task')) as start:
            for options in ({}, {'owner': '%12'}):
                with self.subTest(options=options):
                    self.assertEqual(self.request(dict(action='start', message='Task', **options))[0], 201)
                    launch = start.call_args.kwargs
                    self.assertEqual(launch['group'], 'gabcdef12')
                    self.assertEqual(launch['owner']['kind'], 'helper')
                    self.assertEqual(launch['owner']['home_pane'], '%12')
                    self.assertEqual(launch['owner']['home_thread'], THREAD)
                    self.assertEqual(launch['owner']['opener_name'], 'o' * 40)

    def test_self_owner_starts_unstacked_and_saves_its_own_root(self):
        self.mock_placement()
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout='%15\t\n')
        with patch.object(delegate, 'new_window_after', return_value='%15') as window, \
                patch.object(delegate, 'write_prompt', return_value=self.root / 'prompt.md'):
            reply = self.request({'action': 'start', 'owner': 'self', 'agent': 'claude',
                                  'message': 'a task', 'group': 'Other goal'})
        self.assertEqual(reply[0], 201)
        self.assertEqual(window.call_args.kwargs['group'], '')
        options = [call.args[0] for call in self.run.call_args_list if '@tether_helper' in call.args[0]]
        record = json.loads(options[0][-1])
        self.assertEqual((record['kind'], record['root_pane'], record['home_pane'], record['home_thread']),
                         ('own', '%15', None, None))
        self.assertEqual(record['owner_pane'], '%12')

    def test_placement_owner_uses_its_stack_but_gets_no_command_authority(self):
        self.mock_placement()
        home_thread = '1f8fad5b-d9cb-469f-a165-70867728950e'
        self.entries[1]['thread'] = home_thread
        with patch.object(delegate, 'start', return_value=('%15', 'Task')) as start:
            self.assertEqual(self.request({'action': 'start', 'owner': '%13', 'message': 'Task'})[0], 201)
            launch = start.call_args.kwargs
            self.assertEqual(launch['group'], 'gabcdef13')
            record = launch['owner']
            self.assertEqual((record['root_pane'], record['home_pane'], record['home_thread']),
                             ('%13', '%13', home_thread))
            self.assertEqual((record['owner_pane'], record['owner_thread']), ('%12', THREAD))
            self.assertEqual(self.request({'action': 'start', 'owner': '%13', 'message': 'Task',
                                           'group': 'Unstacked'})[0], 201)
            self.assertEqual(start.call_args.kwargs['group'], '')
            for options in ({'review': 'new'}, {'role': 'advisor'}):
                with self.subTest(options=options):
                    self.assertEqual(self.request(dict(action='start', owner='%13', message='Task', **options))[0], 201)
                    self.assertEqual(start.call_args.kwargs['owner']['owner_pane'], '%12')
                    self.assertEqual(start.call_args.kwargs['owner']['home_pane'], '%13')
        self.entries.append(dict(id='%15', agent='claude', registry='/fake/15.json', thread=THREAD, writable=True))
        with patch.object(agent_tabs.helper_tabs, 'load', return_value=(True, record)), \
                patch.object(agent_tabs.helper_tabs, 'write_record'), \
                patch.object(server, 'shared_status', return_value={'state': 'Idle'}), \
                patch.object(server, 'timeline', return_value={}), \
                patch.object(server, 'review_send', return_value='queued') as send, \
                patch.object(agent_tabs.helper_close, 'close_pane') as close:
            for action in ('send', 'wait', 'close'):
                with self.subTest(action=action):
                    body = dict(action=action, pane='%15', message='Follow up.')
                    self.assertEqual(self.request(dict(body, session='%13'))[0], 400)
                    self.assertEqual(self.request(body)[0], 200)
            send.assert_called_once()
            close.assert_called_once_with('%15', self.run)

    def test_start_refuses_invalid_placement_before_launch_or_worktree(self):
        self.mock_placement()
        self.entries[1]['tmux_session'] = 'elsewhere'
        self.entries[2]['agent'] = 'shell'
        wrong_tab = 'Choose an owner tab in this tmux session.'
        wrong_value = 'Choose self or a pane ID like %12 for the owner.'
        needs_owner = 'A reviewer or advisor needs an owner tab.'
        with patch.object(delegate, 'start') as start, patch.object(agent_tabs.worktrees, 'from_body') as worktree:
            for options, error in (
                    ({'owner': '%13'}, wrong_tab), ({'owner': '%14'}, wrong_tab),
                    ({'owner': '%90', 'worktree': 'feature/no-owner'}, wrong_tab),
                    ({'owner': 'not-a-pane'}, wrong_value), ({'owner': None}, wrong_value),
                    ({'owner': 'self', 'review': 'new'}, needs_owner),
                    ({'owner': 'self', 'role': 'advisor'}, needs_owner)):
                with self.subTest(options=options):
                    self.assertEqual(self.request(dict(action='start', message='Task', **options)),
                                     (400, {'error': error}))
            self.run.assert_not_called()
            start.assert_not_called()
            worktree.assert_not_called()

    def test_audit_records_one_start_and_close_with_cli_actor(self):
        headers = {'X-Tether-Client': 'agent-cli', 'X-Tether-Pane': '%12'}
        ownership = {'owner_pane': '%12', 'owner_thread': THREAD, 'helper_thread': THREAD}
        with patch.object(audit.paths, 'CONFIG', self.root), patch.object(audit, '_tab_name', return_value=('Source', None)), \
                patch.object(delegate, 'start', return_value=('%15', 'Audit Log')), \
                patch.object(agent_tabs.helper_tabs, 'load', return_value=(True, ownership)), \
                patch.object(server, 'shared_status', return_value={'state': 'Idle'}), \
                patch.object(agent_tabs.auto_resume, 'forget_pane'):
            self.assertEqual(self.request({'action': 'start', 'agent': 'claude', 'message': 'Task'}, headers)[0], 201)
            self.assertEqual(self.request({'action': 'close', 'pane': '%13'}, headers)[0], 200)
            entries = {entry['kind']: entry for entry in audit.query()['entries']}
            self.assertEqual(len(audit.query()['entries']), 2)
        for kind, summary in (('tab.start', 'Started tab Audit Log (claude)'), ('tab.close', 'Closed tab Reviewer')):
            self.assertEqual(entries[kind]['summary'], summary)
            self.assertEqual(entries[kind]['actor'], {'source': 'agent-cli', 'pane': '%12', 'tab': 'Source'})

    def test_audit_records_one_link_and_stop_with_request_actor(self):
        headers = {'X-Tether-Client': 'web', 'User-Agent': 'Firefox/1 (Linux)'}
        with patch.object(audit.paths, 'CONFIG', self.root):
            pair = {'worker': '%12', 'reviewer': '%13'}
            self.assertEqual(self.request(pair, headers, path='/mobile/api/review-link')[0], 200)
            self.assertEqual(self.request({'action': 'stop', **pair}, headers)[0], 200)
            self.assertEqual(self.request({'action': 'stop', **pair}, headers)[1], {'stopped': False})
            entries = audit.query()['entries']
        self.assertCountEqual([entry['kind'] for entry in entries], ['review.link', 'review.stop'])
        for entry in entries:
            self.assertEqual(entry['actor'], {'source': 'ui', 'device': 'Firefox on Linux'})
            self.assertEqual(entry['detail'], pair)

    def test_agent_override_shell_default_and_large_body(self):
        with patch.object(delegate, 'start', return_value=('%15', 'task')) as start:
            self.assertEqual(self.request({'action': 'start', 'message': '😀' * 20000, 'agent': 'claude'})[0], 201)
            self.assertEqual(start.call_args.args[1]['agent'], 'claude')
            self.entries[0].update(agent='shell', thread=None)
            self.assertEqual(self.request({'action': 'start', 'message': 'task'})[0], 201)
            self.assertEqual(start.call_args.args[1]['agent'], 'claude')

    def test_validation_errors_and_unknown_session_do_not_launch(self):
        with patch.object(delegate, 'start') as start:
            self.assertEqual(self.request({'action': 'nope'}), (400, {'error': 'Unknown action.'}))
            self.assertEqual(self.request({'action': 'list', 'session': '%99'}),
                             (400, {'error': 'That session has ended. Select another tab.'}))
            self.assertEqual(self.request({'action': 'start', 'message': 'task', 'review': 1}),
                             (400, {'error': 'Context and review must be true or false.'}))
        start.assert_not_called()

    def test_bad_cwd_is_rejected_before_any_terminal_write(self):
        for cwd in (None, 1, 'relative', str(self.root / 'missing')):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.request({'action': 'start', 'message': 'task', 'cwd': cwd}),
                                 (400, {'error': 'Choose an absolute path to an existing directory.'}))
        self.run.assert_not_called()

    def test_worktree_reviewer_refusal_creates_no_branch_folder_or_window(self):
        repo = self.root / 'project'
        def git(*args):
            return self.real_run(['git', '-C', str(self.root), *args], check=True,
                                 capture_output=True, text=True, timeout=10).stdout
        git('init', '-b', 'main', str(repo))
        git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '--allow-empty', '-m', 'First')
        before = git('-C', str(repo), 'branch', '--list')
        with patch.object(server, 'agent_folder', return_value=str(repo)):
            self.assertEqual(self.request({'action': 'start', 'message': '', 'review': 'new',
                                           'worktree': 'feature/reviewer'}),
                             (400, {'error': "A reviewer tab stays in this tab's folder, so it can see the work. Turn off the new worktree or the new tab review."}))
        self.assertEqual(git('-C', str(repo), 'branch', '--list'), before)
        self.assertFalse((self.root / 'project-wt').exists())
        self.run.assert_not_called()

    def test_worktree_is_created_outside_lock_and_launched_inside(self):
        made = {'branch': 'feature/tab', 'path': str(self.root / 'made')}
        def make(body, folder):
            self.assertFalse(server.SEND_LOCK.locked())
            self.assertEqual(folder, str(self.root))
            self.assertEqual(body['worktree'], made['branch'])
            return made
        def start(entry, body, folder, **kwargs):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertEqual(kwargs['cwd'], made['path'])
            self.assertEqual(kwargs['worktree'], made['branch'])
            self.assertEqual(kwargs['owner']['owner_pane'], self.entries[0]['id'])
            self.assertEqual(kwargs['owner']['root_pane'], self.entries[0]['id'])
            self.assertEqual((kwargs['group'], kwargs['name']), ('gabcdef12', 'Task'))
            self.assertEqual(self.stack_calls[-1][2], {'branch': made['branch']})
            return '%15', 'task'
        with patch.object(agent_tabs.worktrees, 'from_body', side_effect=make), \
                patch.object(delegate, 'start', side_effect=start):
            self.assertEqual(self.request({'action': 'start', 'message': 'task', 'worktree': made['branch']}),
                             (201, {'pane': '%15', 'name': 'task', 'worktree': made}))
        self.run.assert_not_called()

    def test_worktree_thread_change_during_creation_does_not_launch(self):
        made = {'branch': 'feature/tab', 'path': str(self.root / 'made')}
        def make(body, folder):
            self.entries[0] = dict(self.entries[0], thread='changed')
            return made
        with patch.object(agent_tabs.worktrees, 'from_body', side_effect=make) as create, \
                patch.object(delegate, 'start') as start:
            status, reply = self.request({'action': 'start', 'message': 'task', 'worktree': made['branch']})
        start.assert_not_called()
        create.assert_called_once()
        self.assertEqual(status, 503)
        self.assertIn(made['path'], reply['error'])
        self.assertIn('This tab changed conversations', reply['error'])
        self.run.assert_not_called()

    def test_worktree_session_ending_during_creation_does_not_launch(self):
        made = {'branch': 'feature/tab', 'path': str(self.root / 'made')}
        def make(body, folder):
            self.entries.pop(0)
            return made
        with patch.object(agent_tabs.worktrees, 'from_body', side_effect=make) as create, \
                patch.object(delegate, 'start') as start:
            status, reply = self.request({'action': 'start', 'message': 'task', 'worktree': made['branch']})
        start.assert_not_called()
        create.assert_called_once()
        self.assertEqual(status, 503)
        self.assertIn(made['path'], reply['error'])
        self.assertIn('That session has ended', reply['error'])
        self.run.assert_not_called()

    def test_worktree_cwd_override_and_missing_folder_fallback(self):
        made = {'branch': 'feature/tab', 'path': str(self.root)}
        with patch.object(agent_tabs.worktrees, 'from_body', return_value=made) as make, \
                patch.object(delegate, 'start', return_value=('%15', 'task')):
            with patch.object(server, 'agent_folder', side_effect=AssertionError('cwd overrides caller folder')):
                self.assertEqual(self.request({'action': 'start', 'message': 'task', 'worktree': 'feature/tab',
                                               'cwd': str(self.root)})[0], 201)
            self.assertEqual(make.call_args.args[1], str(self.root))
            with patch.object(server, 'agent_folder', return_value='/missing/project'):
                self.assertEqual(self.request({'action': 'start', 'message': 'task', 'worktree': 'feature/tab'})[0], 201)
            self.assertEqual(make.call_args.args[1], str(server.paths.HOME))

    def test_invalid_worktree_requests_do_not_create_or_launch(self):
        with patch.object(agent_tabs.worktrees, 'from_body') as make, patch.object(delegate, 'start') as start:
            for extra in ({'session': '%99'}, {'message': ''}, {'agent': 'shell'},
                          *({'cwd': value} for value in (None, 1, 'relative', str(self.root / 'missing')))):
                with self.subTest(extra=extra):
                    request = dict(action='start', message='task', worktree='feature/tab')
                    request.update(extra)
                    self.assertEqual(self.request(request)[0], 400)
            make.assert_not_called()
            start.assert_not_called()
        self.run.assert_not_called()

    def test_worktree_refusal_is_specific_and_launch_failure_keeps_folder(self):
        request = {'action': 'start', 'agent': 'claude', 'message': 'task', 'worktree': 'feature/tab'}
        with patch.object(agent_tabs.worktrees, 'from_body', side_effect=ValueError('Branch already exists.')):
            self.assertEqual(self.request(request), (400, {'error': 'Branch already exists.'}))
        self.run.assert_not_called()
        made = {'branch': 'feature/tab', 'path': str(self.root)}
        self.run.side_effect = OSError('launch failed')
        with patch.object(agent_tabs.worktrees, 'from_body', return_value=made), \
                patch.object(delegate, 'new_window_after', return_value='%15'):
            status, reply = self.request(request)
        self.assertEqual(status, 503)
        self.assertIn(made['path'], reply['error'])
        self.assertIn('agent did not start: launch failed', reply['error'])
        self.assertTrue(self.root.is_dir())
        self.assertFalse(any('remove' in call.args[0] or 'new-window' in call.args[0]
                             for call in self.run.call_args_list))

    def test_stop_by_worker_removes_pair_or_all(self):
        self.link()
        self.link(reviewer='%14')
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12', 'reviewer': '%13'}),
                         (200, {'stopped': True}))
        self.assertEqual(review_links.reviewed_by(), {'%12': ['%14']})
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12'}), (200, {'stopped': True}))
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12'}), (200, {'stopped': False}))
        self.assertEqual(review_links.reviewed_by(), {})

    def stop_review_at(self, rounds, **state):
        self.link()
        link, = (row for row in json.loads(review_links.FILE.read_text()) if 'reviewer' in row)
        link.update(worker_thread=THREAD, rounds=rounds, **state)
        review_links.FILE.write_text(json.dumps([link]))
        self.assertEqual(self.request({'action': 'stop', 'worker': '%12', 'session': '%13'}),
                         (200, {'stopped': True}))
        self.assertEqual(review_links.reviewed_by(), {})

    def test_stopped_at_the_cap_cannot_link_either_reviewer_or_start_one(self):
        self.stop_review_at(5, paused=True, limit=5, grant_rounds=3)
        for reviewer in ('%13', '%14'):
            with self.subTest(reviewer=reviewer):
                status, reply = self.request({'action': 'link', 'worker': '%12', 'reviewer': reviewer})
                self.assertEqual(status, 400)
                self.assertIn('Mak or the parent', reply['error'])
        status, reply = self.request({'action': 'start', 'review': 'new', 'message': ''})
        self.assertEqual(status, 400)
        self.assertIn('Mak or the parent', reply['error'])
        self.run.assert_not_called()

    def test_relink_below_the_cap_carries_rounds(self):
        self.stop_review_at(1, empty_rounds=2)
        self.entries[0]['thread'] = 'pending-123-1'
        status, reply = self.request({'action': 'link', 'worker': '%12', 'reviewer': '%13'})
        self.assertEqual(status, 400)
        self.assertIn('conversation to load', reply['error'])
        self.entries[0]['thread'] = THREAD
        self.assertEqual(self.request({'action': 'link', 'worker': '%12', 'reviewer': '%13'}),
                         (200, {'linked': True}))
        link, cap = json.loads(review_links.FILE.read_text())
        self.assertEqual(cap['counts'], {review_links._reviewer_key(link): {'rounds': 1, 'empty_rounds': 2}})
        self.assertNotIn('paused', cap)
        self.assertFalse({'rounds', 'empty_rounds', 'paused'} & link.keys())

    def test_unreadable_pause_survives_stop_and_either_agent_relink(self):
        for start in (False, True):
            with self.subTest(start=start):
                review_links.FILE.write_text('[]')
                self.stop_review_at(0, paused=True, unreadable=True)
                if start:
                    self.run.return_value = subprocess.CompletedProcess([], 0, stdout='%15\t\n')
                    with patch.object(delegate, 'new_window_after', return_value='%15'), \
                            patch.object(delegate, 'write_prompt', return_value=self.root / 'prompt'):
                        status, result = self.request({'action': 'start', 'review': 'new', 'message': '',
                                                       'agent': 'claude'})
                    self.assertEqual(status, 201, result)
                    reviewer = '%15'
                else:
                    status, result = self.request({'action': 'link', 'worker': '%12', 'reviewer': '%13'})
                    self.assertEqual(status, 200, result)
                    reviewer = '%13'
                self.assertEqual(review_links.paused(), {'%12': [reviewer]})

    def test_browser_link_resets_a_saved_cap(self):
        for reviewer in ('%13', '%14'):
            with self.subTest(reviewer=reviewer):
                self.stop_review_at(5, paused=True, limit=5, grant_rounds=3)
                self.assertEqual(self.request({'worker': '%12', 'reviewer': reviewer}, path='/mobile/api/review-link'),
                                 (200, {'linked': True}))
                link, cap = json.loads(review_links.FILE.read_text())
                for row in (link, cap):
                    self.assertFalse({'counts', 'limit', 'grant_rounds', 'paused'} & row.keys())
                self.assertTrue(review_links.remove('%12', reviewer))

    def test_relink_in_a_new_worker_conversation_starts_fresh(self):
        self.stop_review_at(5, paused=True, limit=5, grant_rounds=3)
        self.entries[0]['thread'] = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'
        self.assertEqual(self.request({'action': 'link', 'worker': '%12', 'reviewer': '%13'}),
                         (200, {'linked': True}))
        link, new, old = json.loads(review_links.FILE.read_text())
        self.assertEqual(old['worker_thread'], THREAD)
        self.assertEqual(list(old['counts'].values()), [{'rounds': 5}])
        self.assertEqual(new['worker_thread'], self.entries[0]['thread'])
        self.assertEqual(new['worker_thread'], link['worker_thread'])
        self.assertFalse({'counts', 'limit', 'paused'} & new.keys())

    def test_reviewer_stop_removes_only_its_own_link_with_or_without_reviewer(self):
        for extra in ({}, {'reviewer': '%12'}):
            with self.subTest(extra=extra):
                self.link(worker='%13', reviewer='%12')
                self.link(worker='%13', reviewer='%14')
                self.assertEqual(self.request(dict(action='stop', worker='%13', **extra)), (200, {'stopped': True}))
                self.assertEqual(review_links.reviewed_by(), {'%13': ['%14']})

    def test_foreign_stop_is_rejected_and_preserves_links(self):
        self.link(worker='%13', reviewer='%14')
        for extra in ({}, {'reviewer': '%14'}):
            self.assertEqual(self.request(dict(action='stop', worker='%13', **extra)),
                             (400, {'error': 'That review link is not on this tab.'}))
        for extra in ({'worker': 'bad'}, {'worker': '%13', 'reviewer': None}):
            self.assertEqual(self.request(dict(action='stop', **extra)),
                             (400, {'error': 'Select a tab to stop reviewing.'}))
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%14']})

    def test_only_parent_can_escalate_its_workers_waiting_review(self):
        self.link(worker='%13', reviewer='%14')
        link, = (row for row in json.loads(review_links.FILE.read_text()) if 'reviewer' in row)
        link.update(worker_thread=THREAD, paused=True, rounds=3, parent_stage=dict(pane='%12', thread=THREAD, notice='Fix it.', sent=True))
        review_links.FILE.write_text(json.dumps([link]))
        payload = {'action': 'escalate', 'pane': '%13'}
        error = (400, {'error': 'That tab has no review waiting on this tab.'})
        self.assertEqual(self.request(dict(payload, session='%14')), error)
        self.entries[0]['thread'] = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'
        self.assertEqual(self.request(payload), error)
        self.assertEqual(json.loads(review_links.FILE.read_text()), [link])
        stored_link, cap = review_links._read()
        self.assertEqual(cap['parent_stage'], link['parent_stage'])
        self.entries[0]['thread'] = THREAD
        self.assertEqual(self.request(payload), (200, {'escalated': True}))
        cap.pop('parent_stage')
        self.assertEqual(json.loads(review_links.FILE.read_text()), [stored_link, dict(cap, escalate='Fix it.')])
        self.assertEqual(self.request(payload), error)
        self.run.assert_not_called()

    def test_parent_can_escalate_a_stopped_review_only_in_the_workers_current_thread(self):
        self.entries[2]['thread'] = THREAD
        other_thread = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'
        caps = [dict(worker='%14', worker_thread=thread, saved_at=1, rounds=3, paused=True,
                     parent_stage=dict(pane='%12', thread=THREAD, notice=thread, sent=True))
                for thread in (THREAD, other_thread)]
        review_links.FILE.write_text(json.dumps(caps))
        self.assertEqual(self.request({'action': 'escalate', 'pane': '%14'}), (200, {'escalated': True}))
        current, other = json.loads(review_links.FILE.read_text())
        self.assertEqual(current['escalate'], THREAD)
        self.assertEqual(other['parent_stage'], caps[1]['parent_stage'])

    def test_list_reports_both_sides_with_names_in_store_order(self):
        self.link()
        self.link(reviewer='%14')
        self.link(worker='%13', reviewer='%12')
        self.assertEqual(self.request({'action': 'list'}), (200, {
            'reviewing': [{'pane': '%13', 'name': 'Reviewer'}],
            'reviewers': [{'pane': '%13', 'name': 'Reviewer'}, {'pane': '%14', 'name': 'Second reviewer'}]}))
        self.assertEqual(self.request({'action': 'list', 'session': '%13'}), (200, {
            'reviewing': [{'pane': '%12', 'name': 'Source'}],
            'reviewers': [{'pane': '%12', 'name': 'Source'}]}))
        self.run.assert_not_called()

    def close(self, target='%13', record=None, exists=True, state='Idle', **extra):
        self.status_shared.return_value = {'state': state}
        with patch.object(agent_tabs.helper_tabs, 'load', return_value=(exists, record)), \
                patch.object(agent_tabs.auto_resume, 'forget_pane') as forget:
            result = self.request(dict(action='close', pane=target, **extra))
        return result, forget

    def kills(self):
        return [call for call in self.run.call_args_list if 'kill-pane' in call.args[0]]

    def test_close_by_owner_kills_pane_forgets_resume_and_drops_links(self):
        self.status_shared = self.enterContext(patch.object(server, 'shared_status'))
        self.link(worker='%13', reviewer='%14')
        record = {'owner_pane': '%12', 'owner_thread': THREAD, 'helper_thread': THREAD}
        result, forget = self.close(record=record)
        self.assertEqual(result, (200, {'closed': '%13'}))
        forget.assert_called_once_with('%13', run=self.run)
        self.assertEqual(self.kills()[0].args[0][-3:], ['kill-pane', '-t', '%13'])
        self.assertEqual(review_links.reviewed_by(), {})

    def test_close_reads_the_caller_and_target_from_uncached_discovery(self):
        # The one-second cache still shows the reviewer's old conversation; the fresh read shows a new one.
        self.status_shared = self.enterContext(patch.object(server, 'shared_status'))
        self.link(worker='%13', reviewer='%12')
        self.fresh.return_value = [dict(self.entries[0], thread='3c4a9a50-2c1e-4c5f-9b67-8d1f0e2a7b11'),
                                   *self.entries[1:]]
        result, _ = self.close()
        self.assertEqual(result[0], 400)
        self.assertEqual(self.kills(), [])
        self.fresh.assert_called()
        self.fresh.return_value = self.entries  # the same conversation: the reviewer may close
        self.assertEqual(self.close()[0], (200, {'closed': '%13'}))

    def test_close_by_linked_reviewer_without_owner_record(self):
        self.status_shared = self.enterContext(patch.object(server, 'shared_status'))
        self.link(worker='%13', reviewer='%12')
        result, forget = self.close()
        self.assertEqual(result, (200, {'closed': '%13'}))
        self.assertEqual(len(self.kills()), 1)

    def test_close_refusals_leave_pane_open(self):
        self.status_shared = self.enterContext(patch.object(server, 'shared_status'))
        self.link(worker='%13', reviewer='%12')
        stranger = {'owner_pane': '%14', 'owner_thread': None, 'helper_thread': None}
        cases = [
            (dict(target='%12'), 400, 'run `close --self`'),
            (dict(target='bad'), 400, 'Select a helper tab.'),
            (dict(exists=False), 400, 'That session has ended.'),
            (dict(target='%14', record=stranger), 400, 'not a helper of this tab'),
            (dict(state='Working'), 503, 'That tab is working'),
            (dict(state='Approval'), 503, 'That tab is working'),
            (dict(state='Input needed'), 503, 'That tab is working'),
        ]
        for kwargs, code, text in cases:
            with self.subTest(**kwargs):
                (status, body), forget = self.close(**kwargs)
                self.assertEqual(status, code)
                self.assertIn(text, body['error'])
                forget.assert_not_called()
        self.assertEqual(self.kills(), [])
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})

    def test_close_self_schedules_this_tab_and_kills_nothing_now(self):
        self.status_shared = self.enterContext(patch.object(server, 'shared_status'))
        with patch.object(agent_tabs.self_close, 'schedule', return_value={'closing': '%12', 'when': 'after this reply ends'}) as schedule:
            result = self.request({'action': 'close', 'self': True})
        self.assertEqual(result, (200, {'closing': '%12', 'when': 'after this reply ends'}))
        self.assertEqual(schedule.call_args.args[0]['id'], '%12')
        self.assertEqual(self.kills(), [])

    def test_close_by_owner_after_thread_change_is_stale(self):
        self.status_shared = self.enterContext(patch.object(server, 'shared_status'))
        record = {'owner_pane': '%12', 'owner_thread': 'another-thread', 'helper_thread': None}
        (status, body), forget = self.close(record=record)
        self.assertEqual(status, 503)
        self.assertIn('changed conversations', body['error'])
        self.assertEqual(self.kills(), [])

    def test_success_invalidates_session_cache(self):
        self.assertIn(agent_tabs.PATH, server.TMUX_WRITES)
        handler = object.__new__(server.Handler)
        handler.path, handler.command = agent_tabs.PATH, 'POST'
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        with patch.object(server, 'forget_sessions') as forget:
            handler.respond(201, {'pane': '%15', 'name': 'task'})
        forget.assert_called_once_with()


class CLITests(unittest.TestCase):
    def test_start_passes_placement_owner(self):
        for owner in ('self', '%13'):
            with self.subTest(owner=owner):
                self.assertEqual(cli.arguments(['start', '--owner', owner, 'Task'])['owner'], owner)
        self.assertNotIn('owner', cli.arguments(['start', 'Task']))

    def test_worktree_body_is_unchanged_and_uses_long_timeout(self):
        for branch in ('feature/foo', ' feature/foo ', ''):
            with self.subTest(branch=branch):
                self.assertEqual(cli.main(['start', '--worktree', branch, 'task']), 0)
                self.assertEqual(self.payload()['worktree'], branch)
                self.assertEqual(self.opener.open.call_args.kwargs, {'timeout': 150})

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'TMUX_PANE': '%12', 'MOBILE_TMUX_PORT': '12345'}))
        self.stdout = self.enterContext(patch.object(sys, 'stdout', new_callable=io.StringIO))
        self.stderr = self.enterContext(patch.object(sys, 'stderr', new_callable=io.StringIO))
        self.token = self.enterContext(patch.object(auth, 'TOKEN_FILE'))
        self.token.read_text.return_value = 'test-secret\n'
        self.credential = self.enterContext(patch.object(auth, 'session_value', return_value='derived-credential'))
        self.enterContext(patch.object(cli, 'install_settings', return_value={'port': 23456, 'ask_bridge': False}))
        self.opener = MagicMock()
        self.build = self.enterContext(patch.object(cli, 'build_opener', return_value=self.opener))
        self.opener.open.return_value.__enter__.return_value.read.return_value = b'{"pane": "%13", "name": "task"}'

    def payload(self):
        return json.loads(self.opener.open.call_args.args[0].data)

    def test_start_passes_helper_role(self):
        with patch('sys.stdin', io.StringIO('Which option?')):
            self.assertEqual(cli.main(['start', '--agent', 'claude', '--role', 'second-advisor', '-']), 0)
        self.assertEqual(self.payload()['role'], 'second-advisor')

    def test_start_maps_arguments_and_uses_read_only_hook_credentials_without_proxy(self):
        self.assertEqual(cli.main(['start', '--agent', 'codex', '--review-it', '--context',
                                   '--name', 'Name', '--cwd', 'relative', 'task']), 0)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:12345/mobile/api/agent-tab')
        self.assertEqual(self.payload(), dict(action='start', session='%12', message='task', review='source',
                                             context=True, agent='codex', name='Name', cwd=os.path.abspath('relative')))
        for name, value in (('Origin', 'http://127.0.0.1:12345'), ('Content-type', 'application/json'),
                            ('X-codex-mobile', '1'), ('X-tether-hook', 'derived-credential'),
                            ('X-tether-client', 'agent-cli'), ('X-tether-pane', '%12'),
                            ('Cookie', auth.COOKIE + '=derived-credential')):
            self.assertEqual(request.get_header(name), value)
        self.assertEqual(self.opener.open.call_args.kwargs, {'timeout': 30})
        self.assertEqual(self.build.call_args.args[0].proxies, {})
        self.token.read_text.assert_called_once_with()
        self.credential.assert_called_once_with('test-secret')
        self.assertEqual(self.stdout.getvalue(), '{"pane": "%13", "name": "task"}\n')
        self.assertEqual(self.stderr.getvalue(), '')

    def test_start_passes_model_and_effort_to_delegate(self):
        self.assertEqual(cli.main(['start', '--reviews-me', '--agent', 'claude', '--model', 'opus',
                                   '--effort', 'high']), 0)
        self.assertEqual(self.payload(), dict(action='start', session='%12', message='', review='new',
                                             context=False, agent='claude', model='opus', effort='high'))

    def test_reviews_me_allows_omitted_task_and_stdin_maps_to_message(self):
        self.assertEqual(cli.main(['start', '--reviews-me']), 0)
        self.assertEqual(self.payload(), dict(action='start', session='%12', message='', review='new', context=False))
        with patch.object(sys, 'stdin', io.StringIO('Task from stdin\n')):
            self.assertEqual(cli.main(['start', '-']), 0)
        self.assertEqual(self.payload()['message'], 'Task from stdin\n')

    def test_stop_and_list_and_settings_port(self):
        with patch.dict(os.environ, {'MOBILE_TMUX_PORT': ''}):
            self.assertEqual(cli.main(['stop', '--worker', '%13', '--reviewer', '%12']), 0)
        self.assertEqual(self.opener.open.call_args.args[0].full_url, 'http://127.0.0.1:23456/mobile/api/agent-tab')
        self.assertEqual(self.payload(), dict(action='stop', session='%12', worker='%13', reviewer='%12'))
        self.assertEqual(cli.main(['stop', '--worker', '%13']), 0)
        self.assertNotIn('reviewer', self.payload())
        self.assertEqual(cli.main(['list']), 0)
        self.assertEqual(self.payload(), dict(action='list', session='%12'))

    def test_close_builds_pane_body(self):
        self.assertEqual(cli.main(['close', '--pane', '%13']), 0)
        self.assertEqual(self.payload(), dict(action='close', session='%12', pane='%13'))

    def test_escalate_builds_pane_body(self):
        self.assertEqual(cli.main(['escalate', '--pane', '%13']), 0)
        self.assertEqual(self.payload(), dict(action='escalate', session='%12', pane='%13'))

    def test_link_builds_pair_body(self):
        self.assertEqual(cli.main(['link', '--worker', '%13', '--reviewer', '%12']), 0)
        self.assertEqual(self.payload(), dict(action='link', session='%12', worker='%13', reviewer='%12'))

    def test_missing_tmux_pane_never_opens_connection(self):
        with patch.dict(os.environ, {'TMUX_PANE': ''}):
            self.assertEqual(cli.main(['list']), 1)
        self.assertEqual(self.stderr.getvalue(), 'Run this inside a Tether tab.\n')
        self.opener.open.assert_not_called()
        self.token.read_text.assert_not_called()

    def test_invalid_arguments_return_one_with_tab_error(self):
        for args in (['start'], ['start', '--review-it'], ['start', '--review-it', '--reviews-me'],
                     ['start', '--agent', 'shell', 'task'], ['stop'], ['link'],
                     ['link', '--worker', '%12'], ['link', '--reviewer', '%13'], ['unknown']):
            with self.subTest(args=args):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.assertEqual(cli.main(args), 1)
                self.assertTrue(self.stderr.getvalue().startswith('Tether tab error: '))
        self.opener.open.assert_not_called()

    def test_http_errors_print_server_text_or_code_and_never_credentials(self):
        for response, expected in ((b'{"error": "That session has ended."}', 'That session has ended.'),
                                   (b'not-json', '403'), (b'{}', '403'), (b'[]', '403')):
            with self.subTest(response=response):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.opener.open.side_effect = HTTPError('http://localhost', 403, 'Forbidden', {}, io.BytesIO(response))
                self.assertEqual(cli.main(['list']), 1)
                self.assertEqual(self.stderr.getvalue(), f'Tether tab error: {expected}\n')

    def test_connection_and_local_config_errors_do_not_print_secrets(self):
        for error in (URLError('sensitive detail'), OSError('sensitive detail')):
            self.opener.open.side_effect = error
            self.assertEqual(cli.main(['list']), 1)
        self.assertNotIn('sensitive detail', self.stderr.getvalue())
        self.opener.open.reset_mock()
        self.token.read_text.side_effect = FileNotFoundError('sensitive path')
        self.assertEqual(cli.main(['list']), 1)
        self.opener.open.assert_not_called()
        self.assertNotIn('sensitive path', self.stderr.getvalue())

    def test_script_runs_by_absolute_path_from_another_folder(self):
        # No pane means this subprocess cannot contact a server or control tmux.
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(Path(cli.__file__).resolve()), 'list'], cwd=directory,
                                    env=dict(os.environ, TMUX_PANE=''), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, 'Run this inside a Tether tab.\n')


if __name__ == '__main__':
    unittest.main()
