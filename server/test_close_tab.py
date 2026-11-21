"""DELETE /mobile/api/session: a close names the chat the page saw, and a tab that now holds another chat stays open."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import server
import undo_store

from test_helper_tabs import HELPER, OTHER, OWNER, UNCONFIRMED_HELPER, Tmux


class CloseTabTest(unittest.TestCase):
    def setUp(self):
        self.killed = []
        self.listed = [{'id': '%1', 'thread': 'chat-a', 'pane_name': 'Audit Log'}, {'id': '%2', 'thread': None}]
        self.kill_fails = False
        self.on_kill = lambda _: None
        self.snapshot = self.session_snapshot = None
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.review_file = Path(directory) / 'review-links.json'
        self.enterContext(patch.object(server.review_links, 'FILE', self.review_file))

    def run_tmux(self, argv, **_):
        if argv[1] == 'list-sessions':
            return subprocess.CompletedProcess(argv, 0, '' if '$1' in self.killed else '$1\n', '')
        if argv[1] == 'list-panes':
            panes = ''.join(pane + '\n' for pane in ('%1', '%2') if pane not in self.killed)
            if '-s' in argv and '#{==:#{window_linked_sessions},1}' not in argv:
                panes += '%3\n'  # shared with a session that stays open
            return subprocess.CompletedProcess(argv, 0, panes, '')
        if argv[1] in ('kill-pane', 'kill-session'):
            if self.kill_fails:
                raise subprocess.CalledProcessError(1, argv)
            self.killed.append(argv[-1])
            self.on_kill(argv[-1])
        if argv[-1] == '#{pid}:#{start_time}':
            return subprocess.CompletedProcess(argv, 0, '10:20\n', '')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def close(self, payload, path='/mobile/api/session'):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = 'DELETE', path
        encoded = json.dumps(payload).encode()
        handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'X-Tether-Client': 'web', 'User-Agent': 'Mozilla/5.0 (iPhone) Safari/1',
                           'Content-Length': str(len(encoded))}
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        with patch.object(server.subprocess, 'run', self.run_tmux), \
                patch.object(server, 'sessions', lambda: self.listed), \
                patch.object(server, 'forget_sessions') as forget, \
                patch.object(server.undo, 'capture_safe', return_value=self.snapshot), \
                patch.object(server.undo, 'capture_session_safe', return_value=self.session_snapshot), \
                patch.object(server.auto_resume, 'forget_pane'), \
                patch.object(server.auto_resume, 'forget_session'), \
                patch.object(server.groups, 'closing', lambda pane, run: contextlib.nullcontext()):
            handler.do_DELETE()
        return handler.respond.call_args.args, forget

    def test_single_tab_and_session_close_keep_the_step_when_tmux_loses_the_acknowledgement(self):
        def timeout(_):
            step, = undo_store.entries()
            self.assertTrue(step['actions'][0]['pending'])
            raise subprocess.TimeoutExpired('kill', 5)

        self.on_kill = timeout
        self.snapshot = {'pane': '%1', 'name': 'Audit Log', 'server': '10:20', 'options': {}, 'session_options': {}}
        self.session_snapshot = {'session_id': '$1', 'name': 'Fixture', 'tabs': [self.snapshot]}
        for path, body, title, kind in (
            ('/mobile/api/session', {'pane': '%1', 'thread': 'chat-a'}, 'Audit Log', 'tab.close'),
            ('/mobile/api/tmux-sessions', {'session': '$1'}, 'Fixture', 'session.close'),
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as folder, \
                    patch.object(undo_store, '_ENABLED', True), \
                    patch.object(undo_store, '_path', return_value=Path(folder) / 'history.json'):
                self.killed.clear()
                (status, reply), _ = self.close(body, path)
                self.assertEqual(status, 503)
                self.assertEqual(reply['closed'], [title])
                step, = undo_store.entries()
                self.assertEqual(reply['undo'], step['id'])
                self.assertEqual(step['actions'][0]['kind'], kind)
                self.assertNotIn('pending', step['actions'][0])

    def test_audit_records_one_close_with_device_and_skips_failed_kill(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server.paths, 'CONFIG', Path(folder)):
            self.kill_fails = True
            self.assertEqual(self.close({'pane': '%1', 'thread': 'chat-a'})[0][0], 503)
            self.assertEqual(server.audit.query()['entries'], [])
            self.kill_fails = False
            self.assertEqual(self.close({'pane': '%1', 'thread': 'chat-a'})[0][0], 200)
            entry, = server.audit.query()['entries']
            self.assertEqual((entry['kind'], entry['summary']), ('tab.close', 'Closed tab Audit Log'))
            self.assertEqual(entry['actor'], {'source': 'ui', 'device': 'Safari on iPhone'})

    def test_close_with_the_same_chat_kills_the_pane(self):
        (status, body), forget = self.close({'pane': '%1', 'thread': 'chat-a'})
        self.assertEqual((status, body), (200, {'closed': '%1'}))
        self.assertEqual(self.killed, ['%1'])
        forget.assert_called()  # a fresh look, not the one-second cache

    def test_close_of_a_tab_that_now_holds_another_chat_is_refused(self):
        (status, body), _ = self.close({'pane': '%1', 'thread': 'chat-old'})
        self.assertEqual(status, 409)
        self.assertIn('another chat', body['error'])
        self.assertEqual(self.killed, [])

    def test_shell_without_a_chat_closes_and_a_new_chat_in_it_is_refused(self):
        self.assertEqual(self.close({'pane': '%2', 'thread': None})[0][0], 200)
        self.listed[1]['thread'] = 'chat-new'
        self.killed.clear()
        self.assertEqual(self.close({'pane': '%2', 'thread': ''})[0][0], 409)
        self.assertEqual(self.killed, [])

    def test_tab_missing_from_the_listing_is_refused_when_a_chat_is_named(self):
        self.listed = []
        self.assertEqual(self.close({'pane': '%1', 'thread': 'chat-a'})[0][0], 409)
        self.assertEqual(self.killed, [])

    def test_close_without_a_thread_keeps_the_old_behavior(self):
        (status, _), forget = self.close({'pane': '%1'})
        self.assertEqual(status, 200)
        forget.assert_not_called()
        self.assertEqual(self.killed, ['%1'])

    def test_only_successfully_closed_panes_lose_review_records(self):
        for path, payload, closed in (('/mobile/api/session', {'pane': '%1'}, {'%1'}),
                                      ('/mobile/api/tmux-sessions', {'session': '$1'}, {'%1', '%2'})):
            for kill_fails in (True, False):
                with self.subTest(path=path, kill_fails=kill_fails):
                    self.killed.clear()
                    self.kill_fails = kill_fails
                    saved = [dict(worker=pane, worker_thread='old-chat', saved_at=time.time(), rounds=3, paused=True)
                             for pane in ('%1', '%2', '%3')]
                    live = [dict(worker=pane, reviewer='%4') for pane in ('%1', '%2', '%3')]
                    self.review_file.write_text(json.dumps(saved + live))
                    self.assertEqual(self.close(payload, path)[0][0], 503 if kill_fails else 200)
                    expected = {'%1', '%2', '%3'} - (set() if kill_fails else closed)
                    remaining = json.loads(self.review_file.read_text())
                    self.assertEqual({link['worker'] for link in remaining if 'saved_at' in link}, expected)
                    self.assertEqual(server.review_links.reviewed_by(), {pane: ['%4'] for pane in expected})


class CloseHelperTabTest(unittest.TestCase):
    def setUp(self):
        self.owner = dict(id='%12', agent='codex', thread=OWNER, codex_binding='open')
        self.helper = dict(id='%13', agent='codex', thread=HELPER, codex_binding='open', helper='reviewer')
        self.entries = [self.owner, self.helper]
        self.record = dict(owner_pane='%12', owner_thread=OWNER, helper_thread=HELPER, sent_at=1000)
        self.run = Tmux()
        self.run.options['%13'] = json.dumps(self.record)
        self.kill = self.enterContext(patch.object(server.agent_tabs.helper_close, 'close_pane'))
        self.capture = self.enterContext(patch.object(server.undo, 'capture_safe', return_value=None))
        self.enterContext(patch.object(server.audit, 'record'))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def close(self):
        return server.agent_tabs.close_tab({'pane': '%13'}, self.owner, self.entries, self.run,
                                           lambda entry: {'state': 'Idle'})

    def test_agent_close_keeps_undo_when_the_helper_is_gone_after_a_timeout(self):
        self.capture.return_value = {'pane': '%13', 'name': 'Helper', 'server': '10:20',
                                     'options': {}, 'session_options': {}}
        self.kill.side_effect = subprocess.TimeoutExpired('kill-pane', 5)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=Path(folder) / 'history.json'), \
                patch.object(server.undo, 'kill_outcome', return_value=True):
            with self.assertRaises(server.undo.CloseInterrupted) as error:
                self.close()
            step, = undo_store.entries()
        self.assertEqual(error.exception.undo, step['id'])
        self.assertEqual(error.exception.members[0][0], '%13')
        self.assertNotIn('pending', step['actions'][0])

    def assert_pane_untouched(self):
        self.capture.assert_not_called()
        self.kill.assert_not_called()
        self.assertEqual(self.run.saves(), [])
        self.assertEqual(self.run.record(), self.record)

    def test_close_refuses_unverified_reviewer_until_thread_is_confirmed(self):
        for unknown in (dict(thread=OTHER, codex_binding='guess'),
                        dict(thread='pending-13-1', codex_binding='tie', pending_conversation=True),
                        dict(thread=HELPER, codex_binding='open', codex_unresolved=True)):
            with self.subTest(unknown=unknown):
                self.helper.update(pending_conversation=False, codex_unresolved=False)
                self.helper.update(unknown)
                with self.assertRaises(RuntimeError) as raised:
                    self.close()
                self.assertIs(type(raised.exception), RuntimeError)
                self.assertEqual(str(raised.exception), UNCONFIRMED_HELPER)
                self.assert_pane_untouched()
        self.helper.update(thread=HELPER, codex_binding='open', codex_unresolved=False)
        self.assertEqual(self.close(), {'closed': '%13'})
        self.kill.assert_called_once_with('%13', self.run)

    def test_close_refuses_a_verified_conversation_change_for_both_agents(self):
        for agent, evidence in (('claude', dict(registry='/fake/13.json')), ('codex', dict(codex_binding='title'))):
            with self.subTest(agent=agent):
                self.helper.update(agent=agent, thread=OTHER, **evidence)
                with self.assertRaisesRegex(server.helper_tabs.Stale, 'That helper changed conversations'):
                    self.close()
                self.assert_pane_untouched()

    def test_close_refuses_a_helper_without_a_confirmed_conversation(self):
        # Closing is final: an absent or shell pane (agent gone, or a discovery miss) and an unbound guess stay open.
        cases = (('absent', None, self.record), ('shell', dict(id='%13', agent='shell'), self.record),
                 ('unbound guess', dict(self.helper, thread=OTHER, codex_binding='guess'),
                  dict(self.record, helper_thread=None)),
                 ('both unbound', dict(self.helper, thread=OTHER, codex_binding='guess'),
                  dict(self.record, owner_thread=None, helper_thread=None)))
        for name, target, record in cases:
            with self.subTest(name=name):
                self.record = record
                self.run.options['%13'] = json.dumps(record)
                self.entries[:] = [self.owner] + ([target] if target else [])
                with self.assertRaisesRegex(RuntimeError, 'cannot confirm that helper'):
                    self.close()
                self.assert_pane_untouched()

    def test_close_closes_a_helper_whose_agent_exited_when_tmux_shows_a_shell(self):
        # Live 2026-11-19: after /exit, discovery reports shell and tmux shows zsh. Both must agree.
        reviewer = dict(id='%14', agent='claude', thread=OTHER, registry='/fake/14.json')
        shell = {1: (0, 'tmux'), 100: (1, 'zsh')}
        late_agent = {**shell, 101: (100, 'codex')}  # an agent that started after the cached discovery
        cases = (('zsh', shell, True), ('bash', shell, True), ('zsh', late_agent, False), ('zsh', {}, False),
                 ('claude', shell, False), ('node', shell, False), (None, shell, False))
        for command, table, closes in cases:
            for caller, links in ((self.owner, set()), (reviewer, {HELPER})):
                with self.subTest(command=command, table=table, caller=caller['id']), \
                        patch.object(server.agent_tabs.procinfo, 'table', return_value=table) as processes, \
                        patch.object(server.agent_tabs.agents, 'claude_registries', return_value=[]), \
                        patch.object(server.review_links, 'linked_worker_threads', return_value=links):
                    self.kill.reset_mock()
                    self.run.commands = {'%13': command} if command else {}
                    entries = [self.owner, dict(id='%13', agent='shell'), reviewer]
                    call = lambda: server.agent_tabs.close_tab({'pane': '%13'}, caller, entries, self.run,
                                                               lambda entry: {'state': 'Idle'})
                    if closes:
                        self.assertEqual(call(), {'closed': '%13'})
                        self.kill.assert_called_once_with('%13', self.run)
                        processes.assert_called_once_with(fresh=True)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'cannot confirm that helper'):
                            call()
                        self.kill.assert_not_called()
        self.assertEqual(self.run.saves(), [])

    def test_linked_reviewer_cannot_close_an_unconfirmed_helper(self):
        reviewer = dict(id='%14', agent='claude', thread=OTHER, registry='/fake/14.json')
        self.entries[1] = dict(self.helper, thread=OTHER, codex_binding='guess')
        with patch.object(server.review_links, 'linked_worker_threads', return_value={None}):
            with self.assertRaisesRegex(RuntimeError, 'cannot confirm that helper'):
                server.agent_tabs.close_tab({'pane': '%13'}, reviewer, self.entries + [reviewer], self.run,
                                            lambda entry: {'state': 'Idle'})
            self.assert_pane_untouched()
            self.entries[1] = self.helper
            self.assertEqual(server.agent_tabs.close_tab({'pane': '%13'}, reviewer, self.entries + [reviewer],
                                                         self.run, lambda entry: {'state': 'Idle'}),
                             {'closed': '%13'})

    def test_linked_reviewer_cannot_close_a_helper_that_changed_conversations(self):
        reviewer = dict(id='%14', agent='claude', thread=OTHER, registry='/fake/14.json')
        for bound, closes in ((HELPER, False), (None, True), (OTHER, True)):
            with self.subTest(bound=bound), \
                    patch.object(server.review_links, 'linked_worker_threads', return_value={bound}) as links:
                entries = [self.owner, dict(self.helper, thread=OTHER, codex_binding='title'), reviewer]
                if closes:
                    self.assertEqual(server.agent_tabs.close_tab({'pane': '%13'}, reviewer, entries, self.run,
                                                                 lambda entry: {'state': 'Idle'}), {'closed': '%13'})
                else:
                    with self.assertRaises(server.helper_tabs.Stale):
                        server.agent_tabs.close_tab({'pane': '%13'}, reviewer, entries, self.run,
                                                    lambda entry: {'state': 'Idle'})
                    self.assert_pane_untouched()
                links.assert_called_once_with('%13', '%14', OTHER)  # one locked read decides the link and its conversation

    def test_close_binds_and_closes_an_unbound_verified_helper(self):
        self.record = dict(self.record, helper_thread=None)
        self.run.options['%13'] = json.dumps(self.record)
        self.assertEqual(self.close(), {'closed': '%13'})
        self.kill.assert_called_once_with('%13', self.run)

if __name__ == '__main__':
    unittest.main()
