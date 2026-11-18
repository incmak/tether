"""DELETE /mobile/api/session: a close names the chat the page saw, and a tab that now holds another chat stays open."""
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


class CloseTabTest(unittest.TestCase):
    def setUp(self):
        self.killed = []
        self.listed = [{'id': '%1', 'thread': 'chat-a'}, {'id': '%2', 'thread': None}]
        self.kill_fails = False
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.review_file = Path(directory) / 'review-links.json'
        self.enterContext(patch.object(server.review_links, 'FILE', self.review_file))

    def run_tmux(self, argv, **_):
        if argv[1] == 'list-sessions':
            return subprocess.CompletedProcess(argv, 0, '$1\n', '')
        if argv[1] == 'list-panes':
            panes = '%1\n%2\n'
            if '-s' in argv and '#{==:#{window_linked_sessions},1}' not in argv:
                panes += '%3\n'  # shared with a session that stays open
            return subprocess.CompletedProcess(argv, 0, panes, '')
        if argv[1] in ('kill-pane', 'kill-session'):
            if self.kill_fails:
                raise subprocess.CalledProcessError(1, argv)
            self.killed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, '', '')

    def close(self, payload, path='/mobile/api/session'):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = 'DELETE', path
        encoded = json.dumps(payload).encode()
        handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(encoded))}
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        with patch.object(server.subprocess, 'run', self.run_tmux), \
                patch.object(server, 'sessions', lambda: self.listed), \
                patch.object(server, 'forget_sessions') as forget, \
                patch.object(server.undo, 'capture_safe', return_value=None), \
                patch.object(server.undo, 'record_tab_close'), \
                patch.object(server.undo, 'capture_session_safe', return_value=None), \
                patch.object(server.undo, 'record_session_close'), \
                patch.object(server.auto_resume, 'forget_pane'), \
                patch.object(server.auto_resume, 'forget_session'), \
                patch.object(server.groups, 'closing', lambda pane: contextlib.nullcontext()):
            handler._delete_plain()
        return handler.respond.call_args.args, forget

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


if __name__ == '__main__':
    unittest.main()
