import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import quick_tab


def tab(pane, session='$1', label='work:1.0', agent='claude', group='', **extra):
    return {'id': pane, 'tmux_session': session, 'label': label, 'agent': agent, 'group': group, **extra}


ENTRIES = [
    tab('%1', pane_name='Fix login'),
    tab('%2', group='g1', pane_name='Billing page'),
    tab('%3', session='$2', label='site:1.0', agent='codex', pane_name='Hero copy'),
    tab('%4', session='$2', label='site:2.0', role='advisor', pane_name='Advisor: secret plan'),
    tab('%5', session='$3', label='tether-login-claude:1.0'),
    tab('%6', session='$2', label='site:3.0', helper='reviewer', pane_name='Review: secret fix'),
]
GROUPS = {'$1': [{'id': 'g1', 'name': 'Billing'}]}


def folder_of(entry):
    return {'%1': quick_tab.HOME + '/apps/work', '%3': quick_tab.HOME + '/apps/site'}.get(entry['id'], '')


class Process:
    def __init__(self, pid, done=False):
        self.pid = pid
        self.done = done

    def poll(self):
        return 0 if self.done else None


class PlacesTest(unittest.TestCase):
    def test_lists_sessions_and_stacks_without_helpers_or_logins(self):
        found = quick_tab.places(ENTRIES, GROUPS, folder_of)
        self.assertEqual([(p['session'], p['group']) for p in found], [('$1', ''), ('$1', 'g1'), ('$2', '')])
        self.assertEqual(found[0]['folder'], 'apps/work')
        self.assertEqual(found[1]['titles'], ['Billing page'])
        self.assertEqual(found[2]['titles'], ['Hero copy'])
        self.assertNotIn('secret', repr(found))

    def test_tabs_in_an_unknown_group_count_as_unstacked(self):
        found = quick_tab.places([tab('%1', group='gone', pane_name='Old')], {}, folder_of)
        self.assertEqual(found, [{'session': '$1', 'group': '', 'sessionName': 'work', 'groupName': '',
                                  'folder': 'apps/work', 'titles': ['Old']}])

    def test_skips_empty_unstacked_place_when_stacks_exist(self):
        found = quick_tab.places([tab('%2', group='g1')], GROUPS, folder_of)
        self.assertEqual([p['group'] for p in found], ['g1'])

    def test_model_input_cleans_names_and_has_no_full_paths(self):
        found = quick_tab.places([tab('%1', pane_name='a|b\x1b[31m')], {}, folder_of)
        text = quick_tab.model_input('Fix it\nnow', found)
        self.assertEqual(text, 'Task: Fix it now\n\nPlaces:\n1 | work | apps/work | a b [31m')
        self.assertNotIn('/Users/', text)

    def test_paths_in_names_and_titles_keep_only_their_last_part(self):
        entries = [tab('%1', label='/Users/alice/x:1.0', pane_name='/Users/alice/private-repo'),
                   tab('%2', pane_name='~/notes/plan.md and A/B test'),
                   tab('%3', pane_name=r'C:\\Users\\bob\\taxes')]
        text = quick_tab.model_input('Fix /srv/app', quick_tab.places(entries, {}, folder_of))
        self.assertEqual(text.splitlines()[-1], '1 | x | apps/work | private-repo; plan.md and A/B test; taxes')
        self.assertIn('Task: Fix /srv/app', text, "The user's own query stays as typed")
        for secret in ('alice', 'notes', 'bob', 'Users'):
            self.assertNotIn(secret, text.splitlines()[-1])

    def test_folders_never_name_home_or_its_parents(self):
        home = '/Users/someone'
        self.assertEqual(quick_tab.short_folder('/Users/someone/apps/work/sub', home), 'work/sub')
        self.assertEqual(quick_tab.short_folder('/Users/someone', home), '')
        self.assertEqual(quick_tab.short_folder('/Users', home), '')
        self.assertEqual(quick_tab.short_folder('/srv/data/proj', home), 'proj')
        self.assertEqual(quick_tab.short_folder('', home), '')


class ParseTest(unittest.TestCase):
    def test_accepts_one_number_from_the_list(self):
        self.assertEqual(quick_tab.parse(' 2\n', 3), 1)
        self.assertEqual(quick_tab.parse('3.', 3), 2)
        self.assertIsNone(quick_tab.parse('0', 3))

    def test_refuses_anything_else(self):
        for reply in ('', 'two', '4', '1 or 2', 'Place 1', '-1'):
            with self.assertRaises(ValueError, msg=reply):
                quick_tab.parse(reply, 3)


class PickAgentTest(unittest.TestCase):
    def test_choices(self):
        which = {'claude': '/bin/claude', 'codex': '/bin/codex'}.get
        self.assertEqual(quick_tab.pick_agent('none', which), (None, None))
        self.assertEqual(quick_tab.pick_agent('auto', which), ('claude', '/bin/claude'))
        self.assertEqual(quick_tab.pick_agent('claude', which), ('claude', '/bin/claude'))
        # Codex never routes: its tools cannot all be turned off, so it could read local files.
        self.assertEqual(quick_tab.pick_agent('codex', which), (None, None))
        self.assertEqual(quick_tab.pick_agent('auto', {'codex': '/bin/codex'}.get), (None, None))
        with self.assertRaises(ValueError):
            quick_tab.pick_agent('gpt', which)


class RouterTest(unittest.TestCase):
    def router(self, reply=None, error=None):
        calls = []

        def runner(agent, executable, prompt, content, timeout, started=None):
            calls.append((agent, content, timeout))
            if error:
                raise error
            return reply
        return quick_tab.Router(runner, {'claude': '/bin/claude'}.get), calls

    def test_returns_the_chosen_place(self):
        router, calls = self.router('2')
        answer = router.route({'query': 'Fix invoices', 'agent': 'auto'}, ENTRIES, GROUPS, folder_of)
        self.assertEqual(answer, {'place': {'session': '$1', 'group': 'g1', 'sessionName': 'work',
                                            'groupName': 'Billing'}, 'source': 'ai'})
        self.assertEqual(calls[0][0], 'claude')
        self.assertEqual(calls[0][2], quick_tab.ROUTE_TIMEOUT)
        self.assertIn('Task: Fix invoices', calls[0][1])

    def test_zero_means_no_place(self):
        router, _ = self.router('0')
        answer = router.route({'query': 'x', 'agent': 'auto'}, ENTRIES, GROUPS, folder_of)
        self.assertEqual((answer['place'], answer['source']), (None, 'ai'))

    def test_failures_fall_back_to_the_default(self):
        for error in (subprocess.TimeoutExpired('claude', 15), ValueError('no'), OSError('gone')):
            router, _ = self.router(error=error)
            answer = router.route({'query': 'x', 'agent': 'auto'}, ENTRIES, GROUPS, folder_of)
            self.assertEqual((answer['place'], answer['source']), (None, 'default'))
        router, _ = self.router('banana')
        self.assertEqual(router.route({'query': 'x', 'agent': 'auto'}, ENTRIES, GROUPS, folder_of)['source'],
                         'default')

    def test_no_model_or_no_places_makes_no_call(self):
        router, calls = self.router('1')
        self.assertEqual(router.route({'query': 'x', 'agent': 'none'}, ENTRIES, GROUPS, folder_of)['source'],
                         'default')
        self.assertEqual(router.route({'query': 'x', 'agent': 'auto'}, [], {}, folder_of)['source'], 'default')
        codex = router.route({'query': 'x', 'agent': 'codex'}, ENTRIES, GROUPS, folder_of)
        self.assertEqual((codex['place'], codex['source']), (None, 'default'))
        self.assertIn('only with Claude', codex['why'])
        self.assertEqual(calls, [])

    def test_checks_the_query(self):
        router, _ = self.router('1')
        for query in ('', '   ', 'x' * 501, 'a\x1bb', None):
            with self.assertRaises(ValueError):
                router.route({'query': query, 'agent': 'auto'}, ENTRIES, GROUPS, folder_of)

    def test_a_new_call_kills_the_old_running_call(self):
        router, _ = self.router('1')
        with mock.patch.object(quick_tab.os, 'killpg') as killpg:
            router._started(Process(11))
            router._started(Process(12, done=True))
            router._started(Process(13))
        killpg.assert_called_once_with(11, quick_tab.signal.SIGKILL)


class StarterTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.now = 1000.0
        self.commands = []
        self.moved = {}
        self.kill_fails = False
        self.patches = [
            mock.patch.object(quick_tab.delegate, 'start', side_effect=self.fake_start),
            mock.patch.object(quick_tab.prefs_sync, 'session_folder', return_value=self.folder),
            mock.patch.object(quick_tab.groups, 'find_or_create',
                              return_value={'id': 'gt', 'name': 'Triage'}),
            mock.patch.object(quick_tab.groups, 'move'),
            mock.patch.object(quick_tab.tmux_merge, 'move_pane'),
            mock.patch.object(quick_tab.terminal_input, 'create_session', return_value='%90'),
        ]
        self.mocks = [patch.start() for patch in self.patches]
        self.starter = quick_tab.Starter(self.tmux, lambda: self.now)

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def fake_start(self, anchor, body, folder, run, *, cwd, group, by_user):
        self.assertIs(by_user, True)
        self.started = (anchor['id'], body, folder, cwd, group)
        return '%50', 'Fix invoices'

    def tmux(self, command, **_):
        self.commands.append(command)
        if 'display-message' in command:
            live = {'%90': '$7', **{entry['id']: entry['tmux_session'] for entry in ENTRIES}, **self.moved}
            return SimpleNamespace(returncode=0, stdout=live.get(command[4], '$1') + '\n', stderr='')
        if 'kill-pane' in command and self.kill_fails:
            raise subprocess.TimeoutExpired(command, 5)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def test_starts_in_the_chosen_stack_from_a_tab_of_that_stack(self):
        answer = self.starter.start({'request': 'req-00001', 'message': 'Fix invoices', 'session': '$1',
                                     'group': 'g1'}, ENTRIES, GROUPS, lambda entry: self.folder)
        self.assertEqual(self.started, ('%2', {'agent': 'claude', 'message': 'Fix invoices'}, self.folder,
                                        self.folder, 'g1'))
        self.assertEqual(answer, {'pane': '%50', 'name': 'Fix invoices', 'agent': 'claude',
                                  'place': {'session': '$1', 'group': 'g1', 'sessionName': 'work',
                                            'groupName': 'Billing'}})

    def test_uses_the_agent_of_the_session(self):
        answer = self.starter.start({'request': 'req-00002', 'message': 'Copy', 'session': '$2', 'group': ''},
                                    ENTRIES, GROUPS, lambda entry: self.folder)
        self.assertEqual((answer['agent'], self.started[0]), ('codex', '%3'))

    def test_a_missing_folder_falls_back_to_the_default_folder(self):
        self.starter.start({'request': 'req-00003', 'message': 'x', 'session': '$1', 'group': ''},
                           ENTRIES, GROUPS, lambda entry: '/no/such/folder')
        self.assertEqual(self.started[2], self.folder)

    def test_repeated_request_starts_nothing_new(self):
        body = {'request': 'req-00004', 'message': 'x', 'session': '$1', 'group': ''}
        first = self.starter.start(body, ENTRIES, GROUPS, folder_of)
        self.mocks[0].reset_mock()
        self.assertEqual(self.starter.start(body, ENTRIES, GROUPS, folder_of), first)
        self.mocks[0].assert_not_called()

    def test_refuses_when_the_anchor_left_the_session(self):
        self.moved = {'%2': '$9'}
        with self.assertRaisesRegex(ValueError, 'session changed'):
            self.starter.start({'request': 'req-00010', 'message': 'x', 'session': '$1', 'group': 'g1'},
                               ENTRIES, GROUPS, folder_of)
        self.mocks[0].assert_not_called()

    def test_a_failed_cleanup_still_records_the_started_tab(self):
        self.kill_fails = True
        body = {'request': 'req-00011', 'message': 'x', 'default': True}
        first = self.starter.start(body, ENTRIES, GROUPS, folder_of)
        self.mocks[0].reset_mock()
        self.assertEqual(self.starter.start(body, ENTRIES, GROUPS, folder_of), first)
        self.mocks[0].assert_not_called()

    def test_a_failed_undo_record_keeps_the_answer_for_a_retry(self):
        broken = SimpleNamespace(record_tab_start=mock.Mock(side_effect=subprocess.TimeoutExpired(['tmux'], 5)))
        body = {'request': 'req-00013', 'message': 'x', 'session': '$1', 'group': ''}
        with mock.patch.object(quick_tab, '_undo', return_value=broken):
            try:
                first = self.starter.start(body, ENTRIES, GROUPS, folder_of)
            except subprocess.TimeoutExpired:
                first = None
            self.mocks[0].reset_mock()
            again = self.starter.start(body, ENTRIES, GROUPS, folder_of)
        self.mocks[0].assert_not_called()
        self.assertEqual(again['pane'], '%50')
        self.assertIn(first, (None, again))

    def test_an_unclear_launch_failure_blocks_a_second_launch(self):
        for error in (subprocess.TimeoutExpired(['tmux'], 5), RuntimeError('Could not split the pane.')):
            with self.subTest(error=type(error).__name__):
                self.mocks[0].side_effect = error
                body = {'request': f'req-unclear-{type(error).__name__}', 'message': 'x', 'session': '$1', 'group': ''}
                with self.assertRaisesRegex(ValueError, 'may have started'):
                    self.starter.start(body, ENTRIES, GROUPS, folder_of)
                self.mocks[0].side_effect = self.fake_start
                self.mocks[0].reset_mock()
                with self.assertRaisesRegex(ValueError, 'may have started'):
                    self.starter.start(body, ENTRIES, GROUPS, folder_of)
                self.mocks[0].assert_not_called()

    def test_a_checked_input_error_allows_a_retry(self):
        self.mocks[0].side_effect = ValueError('Choose an absolute path to an existing directory.')
        body = {'request': 'req-00012', 'message': 'x', 'session': '$1', 'group': ''}
        with self.assertRaisesRegex(ValueError, 'absolute path'):
            self.starter.start(body, ENTRIES, GROUPS, folder_of)
        self.mocks[0].side_effect = self.fake_start
        self.assertEqual(self.starter.start(body, ENTRIES, GROUPS, folder_of)['pane'], '%50')

    def test_refuses_bad_places(self):
        bad = [
            {'request': 'short', 'message': 'x', 'session': '$1', 'group': ''},
            {'request': 'req-00005', 'message': '', 'session': '$1', 'group': ''},
            {'request': 'req-00005', 'message': 'x', 'session': '1', 'group': ''},
            {'request': 'req-00005', 'message': 'x', 'session': '$9', 'group': ''},
            {'request': 'req-00005', 'message': 'x', 'session': '$1', 'group': 'nope'},
            {'request': 'req-00005', 'message': 'x', 'session': '$1'},
            {'request': 'req-00005', 'message': 'x', 'session': '$3', 'group': ''},  # login session only
        ]
        for body in bad:
            with self.assertRaises(ValueError, msg=body):
                self.starter.start(body, ENTRIES, GROUPS, folder_of)
        self.mocks[0].assert_not_called()

    def test_default_uses_existing_general_session(self):
        entries = [*ENTRIES, tab('%8', session='$4', label='General:1.0', agent='')]
        self.moved = {'%8': '$4'}
        answer = self.starter.start({'request': 'req-00006', 'message': 'x', 'default': True}, entries, GROUPS,
                                    lambda entry: self.folder)
        self.mocks[2].assert_called_once_with('$4', 'Triage', self.tmux)
        self.mocks[5].assert_not_called()
        self.assertEqual((self.started[0], self.started[4], answer['agent']), ('%8', 'gt', 'claude'))
        self.assertEqual(answer['place'], {'session': '$4', 'group': 'gt', 'sessionName': 'General',
                                           'groupName': 'Triage'})

    def test_default_makes_general_and_closes_its_empty_shell(self):
        answer = self.starter.start({'request': 'req-00007', 'message': 'x', 'default': True}, ENTRIES, GROUPS,
                                    folder_of)
        self.mocks[5].assert_called_once_with('General', self.folder, run=self.tmux)
        self.assertEqual((self.started[0], self.started[2]), ('%90', self.folder))
        self.assertIn(['tmux', 'kill-pane', '-t', '%90'][1:], [command[1:] for command in self.commands])
        self.assertEqual(answer['place']['session'], '$7')

    def test_move_only_works_for_recent_tabs_it_started(self):
        with self.assertRaises(ValueError):
            self.starter.move({'pane': '%1', 'session': '$2', 'group': ''}, ENTRIES, GROUPS)
        self.starter.start({'request': 'req-00008', 'message': 'x', 'session': '$1', 'group': ''},
                           ENTRIES, GROUPS, folder_of)
        answer = self.starter.move({'pane': '%50', 'session': '$2', 'group': ''}, ENTRIES, GROUPS)
        self.mocks[4].assert_called_once_with('%50', '$2', self.tmux)
        self.mocks[3].assert_called_once_with('$2', '%50', '', self.tmux, by_user=True)
        self.assertEqual(answer['place']['sessionName'], 'site')
        self.now += quick_tab.MOVE_WINDOW
        with self.assertRaises(ValueError):
            self.starter.move({'pane': '%50', 'session': '$1', 'group': 'g1'}, ENTRIES, GROUPS)

    def test_move_in_the_same_session_only_changes_the_stack(self):
        self.starter.start({'request': 'req-00009', 'message': 'x', 'session': '$1', 'group': ''},
                           ENTRIES, GROUPS, folder_of)
        self.starter.move({'pane': '%50', 'session': '$1', 'group': 'g1'}, ENTRIES, GROUPS)
        self.mocks[4].assert_not_called()
        self.mocks[3].assert_called_once_with('$1', '%50', 'g1', self.tmux, by_user=True)


if __name__ == '__main__':
    unittest.main()
