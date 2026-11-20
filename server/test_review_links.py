"""Watcher transitions use isolated stores and mocked sends, never live panes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from contextlib import nullcontext
import hashlib
import importlib.util
import io
from itertools import product
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import re
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

import audit
import helper_close
import review_links
import review_scope
import server
import terminal_input
import undo
from own_time import own_time
from test_notification_priority import ASSISTANT, BACKGROUND, claude, codex
from terminal_input import DraftError


THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'
WORKER_THREAD = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'
PARENT_THREAD = 'b9f98ea7-fffc-42fe-b94e-8d231cf3b019'
FINDINGS = 'Still broken: the lock is not released.'
# The UI card (ui/src/Messages.tsx REVIEW_REQUEST) reads the request with this regex.
REVIEW_REQUEST = (r'^\[Tether review\] The (\S+) tab “([^”]*)” \((%\d+)\) finished a turn\.'
                  r'[\s\S]*?Its last reply:\s*([\s\S]*?)\s*Transcript: (\S*)'
                  r'( This is the last round: your reply stays here\.)?\s*$')
CLAUDE_DONE = {'type': 'system', 'subtype': 'turn_duration'}
LONG_REVIEW_OUTPUT = 300 * 1024
CLIPPED_REVIEW_OUTPUT = 9 * 1024 * 1024
REVIEW_TRANSCRIPT_AGENTS = ('claude', 'codex', 'antigravity')


def claude_final(text):
    record = claude('assistant', text)
    record['message']['stop_reason'] = 'end_turn'
    return record


def write_records(path, records, mode='w'):
    with path.open(mode) as transcript:
        transcript.writelines(json.dumps(record) + '\n' for record in records)


def guard_subprocess(test):
    for name in ('run', 'Popen'):
        process = test.enterContext(patch.object(
            subprocess, name, side_effect=AssertionError(f'unexpected subprocess.{name}')))
        # Monitor observers can catch the exception. The attempted call must still fail the test.
        test.addCleanup(process.assert_not_called)


def isolate_notice_monitor(test):
    guard_subprocess(test)
    test.enterContext(patch.object(server.helper_tabs, 'listing_fields', return_value={}))
    # Keep the monitor loop real. Other observers can read or write panes,
    # start background jobs, or change shared state between tests.
    for observer in (
        server.NOTIFIER, server.auto_merge, server.attention.STORE, server.coordinator.FEED,
        server.tab_brief.BRIEFER, server.WORK_WATCHER, server.activity_time.STORE, server.work_jira.MOVER,
        server.work_feeds.POLLER, server.HELPER_CLOSER, server.UPKEEP,
    ):
        test.enterContext(patch.object(observer, 'observe'))
    test.enterContext(patch.object(server.attention.STORE, 'digest_due', return_value=None))
    test.enterContext(patch.object(server.activity_time.STORE, 'maybe_publish'))


class ReviewLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.file = Path(temporary.name) / 'cache' / 'review-links.json'
        self.enterContext(patch.object(review_links, 'FILE', self.file))
        self.enterContext(patch.object(review_links, '_last_sent', {}))
        self.enterContext(patch.object(review_links, '_feedback_in_flight', {}))
        self.clock = self.enterContext(own_time(review_links, monotonic=Mock(return_value=1000)))
        self.worker = dict(id='%13', agent='codex', thread=WORKER_THREAD, transcript='/project/worker.jsonl')
        self.reviewer = dict(id='%12', agent='claude', thread=THREAD, writable=True)
        self.states = {'%13': 'Idle', '%12': 'Idle'}
        self.status = Mock(side_effect=lambda entry: {'state': self.states[entry['id']]})
        self.send = Mock()
        self.alert = Mock()
        self.work_mark = None
        self.review_reply = None
        self.turn_source = None
        self.scope = None
        self.commit = None
        self.fresh = None
        self.level = None
        self.parent = None
        self.entries = [self.worker, self.reviewer]
        self.reply = Mock(return_value='I fixed it.')
        self.add()

    def add(self, **extra):
        review_links.add(**dict({'worker': '%13', 'reviewer': '%12', 'reviewer_thread': THREAD, 'name': 'Fix the bug',
                                'agent': 'codex', 'reset': True}, **extra))

    def rows(self):
        return json.loads(self.file.read_text())

    def links(self):
        # Legacy fixtures and existing behavioral assertions use a joined view of the watcher state.
        rows = self.rows()
        caps = {(row['worker'], row.get('worker_thread')): row for row in rows
                if isinstance(row, dict) and row.get('cap') is True}
        joined, saved = [], []
        for row in rows:
            if isinstance(row, dict) and row.get('cap') is True:
                if 'saved_at' in row:
                    counts = list(row.get('counts', {}).values())
                    fields = {key: value for key, value in row.items() if key not in ('cap', 'name', 'counts')}
                    saved.append({**fields, **(counts[0] if len(counts) == 1 else {})})
            elif isinstance(row, dict) and isinstance(row.get('worker'), str):
                cap = caps.get((row['worker'], row.get('worker_thread')), {})
                fields = {key: value for key, value in cap.items()
                          if key not in ('cap', 'worker', 'worker_thread', 'name', 'saved_at', 'pending_reset', 'counts')}
                counts = cap.get('counts', {}).get(review_links._reviewer_key(row), {})
                joined.append({**row, **fields, **counts, **({'paused': True} if cap.get('unreadable') else {})})
            else:
                joined.append(row)
        return saved + joined

    def observe(self, *, entries=None, elapsed=6, **states):
        self.clock.return_value += elapsed
        self.states.update(states)
        review_links.observe(entries if entries is not None else self.entries,
                             self.status, self.send, self.reply, alert=self.alert, work_mark=self.work_mark,
                             review_reply=self.review_reply, turn_source=self.turn_source,
                             level=self.level, parent=self.parent,
                             scope=self.scope, commit=self.commit, fresh=self.fresh)

    def test_idle_review_forwards_anchored_reply_before_unrelated_turn(self):
        path, request = self.review_transcript()
        self.reply.return_value = 'Unrelated reply'
        write_records(path, [claude('user', request), claude_final('APPROVED fine'), CLAUDE_DONE,
                             claude('user', 'Other request'), claude_final('Unrelated reply'), CLAUDE_DONE])
        self.observe()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('APPROVED fine', self.send.call_args.args[1])
        self.assertNotIn('Unrelated reply', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('request_id', self.links()[0])

    def test_busy_review_forwards_queued_request_reply_on_first_idle(self):
        self.observe(**{'%12': 'Working'})
        path, request = self.review_transcript()
        self.assertTrue(self.links()[0]['reviewer_busy_send'])
        self.reply.return_value = 'Unrelated reply'
        write_records(path, [claude('user', 'Existing task'),
                             {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': request}},
                             claude_final('APPROVED mid-turn'), CLAUDE_DONE,
                             claude('user', 'Other request'), claude_final('Unrelated reply'), CLAUDE_DONE])
        self.observe(**{'%12': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('APPROVED mid-turn', self.send.call_args.args[1])
        self.assertNotIn('Unrelated reply', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('reviewer_busy_send', self.links()[0])
        self.assertNotIn('request_id', self.links()[0])

    def test_busy_review_waits_until_request_reaches_conversation(self):
        self.review_reply = Mock(return_value=None)
        self.observe(**{'%12': 'Working'})
        self.request_review()
        request_id = self.links()[0]['request_id']
        self.send.reset_mock()
        self.reply.reset_mock()
        self.reply.return_value = 'Unrelated reply'
        self.observe(**{'%12': 'Idle'})
        self.review_reply.assert_called_once_with(self.reviewer, request_id)
        self.send.assert_not_called()
        self.reply.assert_not_called()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0]['idle_ticks'], 1)
        self.review_reply.return_value = 'Fix the regression.'
        self.observe()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('Fix the regression.', self.send.call_args.args[1])
        self.assertNotIn('Unrelated reply', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('request_id', self.links()[0])

    def test_missing_or_empty_anchored_reply_expires_after_30_idle_ticks(self):
        for reply in (None, ''):
            with self.subTest(reply=reply):
                self.add()
                self.alert.reset_mock()
                self.review_reply = Mock(return_value=reply)
                self.request_review()
                request_id = self.links()[0]['request_id']
                self.send.reset_mock()
                self.reply.reset_mock()
                self.reply.return_value = 'Unrelated reply'
                self.observe(**{'%12': 'Working'})
                for tick in range(1, 30):
                    self.observe(**{'%12': 'Idle'})
                    self.assertTrue(self.links()[0]['awaiting'])
                    self.assertEqual(self.links()[0]['idle_ticks'], tick)
                with patch('review_links.sys.stderr', new_callable=io.StringIO) as stderr:
                    self.observe()
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertEqual(self.links()[0]['stalled'], request_id)
                self.alert.assert_called_once_with('Review stalled',
                    '“Fix the bug”: Tether could not find the reviewer reply. '
                    'Ask the reviewer again, or send its latest reply. Latest reply: “Unrelated reply”', '%13',
                    review={'worker': '%13', 'reviewer': '%12', 'id': request_id})
                self.assertEqual(stderr.getvalue(),
                    'review stalled: worker=%13 reviewer=%12 name=Fix the bug\n')
                for key in ('request_id', 'idle_ticks', 'reviewer_seen_working', 'reviewer_before'):
                    self.assertNotIn(key, self.links()[0])
                self.send.assert_not_called()
                self.reply.assert_called_with(self.reviewer)
                self.observe()
                self.assertEqual(self.alert.call_count, 1)
                self.send.assert_not_called()

    def test_pending_review_stalls_only_after_idle_limit_since_working(self):
        path, request = self.review_transcript()
        request_id = self.links()[0]['request_id']
        write_records(path, [claude('user', request)])
        self.reply.return_value = 'Last reviewer reply'

        for _ in range(149):
            self.observe(elapsed=2)
        for _ in range(150):
            self.observe(elapsed=2, **{'%12': 'Working'})
        for _ in range(149):
            self.observe(elapsed=2, **{'%12': 'Idle'})
            self.assertTrue(self.links()[0]['awaiting'])
        self.alert.assert_not_called()

        with patch('review_links.sys.stderr', new_callable=io.StringIO):
            self.observe(elapsed=2)
        link = self.links()[0]
        self.assertFalse(link['awaiting'])
        self.assertEqual(link['stalled'], request_id)
        self.assertEqual(link['stalled_reply'], review_links._digest('Last reviewer reply'))
        self.assertNotIn('pending_idle_ticks', link)
        self.send.assert_not_called()
        self.alert.assert_called_once_with('Review stalled', ANY, '%13',
                                           review={'worker': '%13', 'reviewer': '%12', 'id': request_id})
        self.assertIn('Latest reply: “Last reviewer reply”', self.alert.call_args.args[1])

    def test_stalled_alert_preview_strips_flattens_and_bounds_the_reply(self):
        for reply, preview in [('  First line\nSecond line  ', 'First line Second line'),
                               ('x' * 120, 'x' * 120),
                               ('x' * 121, 'x' * 120 + '…'), ('', '')]:
            with self.subTest(reply=reply):
                self.add()
                self.alert.reset_mock()
                self.reply.return_value = reply
                self.stall_review()
                self.assertTrue(self.alert.call_args.args[1].endswith(f'Latest reply: “{preview}”'))
                self.assertIn('stalled_reply', self.links()[0])

    def test_stalled_preview_and_digest_use_the_same_reply_snapshot(self):
        self.review_reply = Mock(return_value=None)
        self.request_review()
        request_id = self.links()[0]['request_id']
        for _ in range(29):
            self.observe()
        self.reply.side_effect = ['The reply Mak saw.', 'A newer reply.']
        with patch('review_links.sys.stderr', new_callable=io.StringIO):
            self.observe()
        self.assertTrue(self.alert.call_args.args[1].endswith('Latest reply: “The reply Mak saw.”'))
        self.reply.side_effect = None
        self.reply.return_value = 'The reply Mak saw.'
        updated = review_links.resume_stalled('%13', '%12', request_id, 'latest',
                                              last_reply=self.reply, reviewer_entry=self.reviewer)
        self.assertTrue(updated['awaiting'])

    def test_anchored_reply_on_tick_29_forwards_without_stalling(self):
        self.review_reply = Mock(return_value=None)
        self.request_review()
        self.send.reset_mock()
        for _ in range(28):
            self.observe()
        self.review_reply.return_value = 'Fix the regression.'
        self.observe()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('Fix the regression.', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('stalled', self.links()[0])
        self.alert.assert_not_called()

    def stall_review(self):
        self.review_reply = Mock(return_value=None)
        self.request_review()
        request_id = self.links()[0]['request_id']
        with patch('review_links.sys.stderr', new_callable=io.StringIO):
            for _ in range(30):
                self.observe()
        self.send.reset_mock()
        return request_id

    def test_resume_stalled_resend_queues_a_fresh_request_for_idle_worker(self):
        with patch.object(review_links.secrets, 'token_hex', return_value='a1b2c3d4'):
            request_id = self.stall_review()
        updated = review_links.resume_stalled('%13', '%12', request_id, 'resend')
        self.assertEqual(updated, next(row for row in self.rows() if row.get('reviewer') == '%12'))
        self.assertNotIn('stalled', updated)
        self.assertNotIn('stalled_reply', updated)
        self.assertTrue(updated['seen_working'])
        self.send.assert_not_called()
        updated['name'] = 'Local copy'
        self.assertEqual(self.links()[0]['name'], 'Fix the bug')
        self.observe(**{'%13': 'Input needed'})
        self.send.assert_not_called()
        with patch.object(review_links.secrets, 'token_hex', return_value='b2c3d4e5'):
            self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn('Review id: b2c3d4e5.', self.send.call_args.args[1])
        self.assertNotEqual(self.links()[0]['request_id'], request_id)
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertNotIn('stalled', self.links()[0])
        before = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, '^This review is no longer stalled\\.$'):
            review_links.resume_stalled('%13', '%12', request_id, 'resend')
        self.assertEqual(self.file.read_bytes(), before)

    def test_new_review_delivery_clears_stalled_only_after_successful_send(self):
        self.stall_review()
        link, = self.links()
        link['send_reply'] = link['stalled_reply']
        self.file.write_text(json.dumps([link]))
        self.send.side_effect = DraftError('Keep the draft.')
        self.request_review()
        self.assertIn('stalled', self.links()[0])
        self.assertIn('stalled_reply', self.links()[0])
        self.assertIn('send_reply', self.links()[0])
        self.assertFalse(self.links()[0]['awaiting'])
        self.send.side_effect = None
        self.observe()
        self.assertNotIn('stalled', self.links()[0])
        self.assertNotIn('stalled_reply', self.links()[0])
        self.assertNotIn('send_reply', self.links()[0])
        self.assertTrue(self.links()[0]['awaiting'])

    def test_resume_stalled_latest_re_stalls_if_reply_changes_before_forwarding(self):
        for working in (False, True):
            with self.subTest(working=working):
                self.add()
                self.reply.return_value = 'The reply Mak saw.'
                with patch.object(review_links.secrets, 'token_hex', return_value='a1b2c3d4'):
                    request_id = self.stall_review()
                review_links.resume_stalled('%13', '%12', request_id, 'latest',
                                            last_reply=self.reply, reviewer_entry=self.reviewer)
                self.alert.reset_mock()
                if working:
                    self.observe(**{'%12': 'Working'})
                    self.send.assert_not_called()
                    self.assertTrue(self.links()[0]['awaiting'])
                self.reply.return_value = '  A newer reply.\nCheck this instead.  '
                with patch.object(review_links.secrets, 'token_hex', return_value='b2c3d4e5'), \
                        patch('review_links.sys.stderr', new_callable=io.StringIO):
                    self.observe(**{'%12': 'Idle'})
                self.send.assert_not_called()
                link, = self.links()
                self.assertFalse(link['awaiting'])
                self.assertEqual(link['stalled'], 'b2c3d4e5')
                self.assertNotIn('send_reply', link)
                self.alert.assert_called_once_with('Review stalled',
                    '“Fix the bug”: Tether could not find the reviewer reply. '
                    'Ask the reviewer again, or send its latest reply. '
                    'Latest reply: “A newer reply. Check this instead.”', '%13',
                    review={'worker': '%13', 'reviewer': '%12', 'id': 'b2c3d4e5'})
                before = self.file.read_bytes()
                for action in ('resend', 'latest'):
                    with self.assertRaisesRegex(ValueError, '^This review is no longer stalled\\.$'):
                        review_links.resume_stalled('%13', '%12', request_id, action,
                                                    last_reply=self.reply, reviewer_entry=self.reviewer)
                    self.assertEqual(self.file.read_bytes(), before)
                updated = review_links.resume_stalled('%13', '%12', 'b2c3d4e5', 'latest',
                                                      last_reply=self.reply, reviewer_entry=self.reviewer)
                self.assertTrue(updated['awaiting'])
                self.send.assert_not_called()

    def test_resume_stalled_latest_forwards_approval_or_feedback_with_existing_round_rules(self):
        cases = [('APPROVED fine', 1, False), ('Fix the regression.', 1, False),
                 ('APPROVED at limit', review_links.MAX_ROUNDS, False),
                 ('Still needs fixes.', review_links.MAX_ROUNDS, True)]
        for reply, rounds, pauses in cases:
            with self.subTest(reply=reply):
                self.add()
                self.reply.return_value = reply
                request_id = self.stall_review()
                link, = self.links()
                link.update(rounds=rounds, empty_rounds=1)
                self.file.write_text(json.dumps([link]))
                updated = review_links.resume_stalled('%13', '%12', request_id, 'latest',
                                                      last_reply=self.reply, reviewer_entry=self.reviewer)
                self.assertEqual(updated, next(row for row in self.rows() if row.get('reviewer') == '%12'))
                self.assertNotIn('stalled', updated)
                self.assertNotIn('stalled_reply', updated)
                self.assertNotIn('request_id', updated)
                self.assertTrue(updated['awaiting'])
                self.assertEqual(updated['send_reply'], link['stalled_reply'])
                self.assertTrue(updated['reviewer_seen_working'])
                self.assertEqual(updated['idle_ticks'], 0)
                self.send.assert_not_called()
                self.reply.return_value = reply
                self.review_reply.reset_mock()
                self.observe(**{'%12': 'Input needed'})
                self.send.assert_not_called()
                self.observe(**{'%12': 'Idle'})
                self.review_reply.assert_not_called()
                self.reply.assert_any_call(self.reviewer)
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertNotIn('send_reply', self.links()[0])
                if pauses:
                    self.send.assert_not_called()
                    self.assertTrue(self.links()[0]['paused'])
                    self.assertEqual(self.links()[0]['rounds'], rounds)
                else:
                    self.send.assert_called_once()
                    self.assertEqual(self.send.call_args.args[0], self.worker)
                    self.assertIn(reply, self.send.call_args.args[1])
                    if reply.startswith('APPROVED'):
                        self.assertIn('Approved by the reviewer tab', self.send.call_args.args[1])
                        self.assertNotIn('rounds', self.links()[0])
                        self.assertNotIn('empty_rounds', self.links()[0])
                    else:
                        self.assertIn('Feedback from the reviewer tab', self.send.call_args.args[1])
                        self.assertEqual(self.links()[0]['rounds'], rounds + 1)
                        self.assertEqual(self.links()[0]['empty_rounds'], 1)

    def test_resume_stalled_latest_refuses_changed_reply_without_changing_store(self):
        request_id = self.stall_review()
        before = self.file.read_bytes()
        for reply in ('A newer reply.', ' I fixed it. ', ''):
            with self.subTest(reply=reply):
                self.reply.return_value = reply
                with self.assertRaisesRegex(ValueError, '^The reviewer replied again\\. Open the tab\\.$'):
                    review_links.resume_stalled('%13', '%12', request_id, 'latest',
                                                last_reply=self.reply, reviewer_entry=self.reviewer)
                self.assertEqual(self.file.read_bytes(), before)
                self.send.assert_not_called()

    def test_resume_stalled_rejects_stale_identity_without_changing_store(self):
        request_id = self.stall_review()
        before = self.file.read_bytes()
        for worker, reviewer, identifier in [('%13', '%12', '00000000'),
                                             ('%14', '%12', request_id),
                                             ('%13', '%14', request_id)]:
            with self.subTest(worker=worker, reviewer=reviewer, identifier=identifier):
                with self.assertRaisesRegex(ValueError, '^This review is no longer stalled\\.$'):
                    review_links.resume_stalled(worker, reviewer, identifier, 'resend')
                self.assertEqual(self.file.read_bytes(), before)
        self.add()
        before = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, '^This review is no longer stalled\\.$'):
            review_links.resume_stalled('%13', '%12', request_id, 'resend')
        self.assertEqual(self.file.read_bytes(), before)

    def test_resume_stalled_rejects_bad_arguments_without_changing_store(self):
        request_id = self.stall_review()
        before = self.file.read_bytes()
        for key, values in [('worker', [None, '', '13', '%13;pwd', '%13\n', 13, [], {}]),
                            ('reviewer', [None, '', '12', '%12;pwd', '%12\n', 12, [], {}]),
                            ('request_id', [None, '', 'A1B2C3D4', 'a1b2c3d', 'a1b2c3d45',
                                            'a1b2c3d4\n', 'zzzzzzzz', 12345678, [], {}]),
                            ('action', [None, '', 'stop', 'RESEND', [], {}])]:
            for value in values:
                with self.subTest(key=key, value=value):
                    arguments = dict(worker='%13', reviewer='%12', request_id=request_id, action='resend')
                    arguments[key] = value
                    with self.assertRaises(ValueError) as error:
                        review_links.resume_stalled(**arguments)
                    if key == 'action':
                        self.assertEqual(str(error.exception), 'Choose resend or latest.')
                    self.assertEqual(self.file.read_bytes(), before)

    def stalled_request(self, payload, *, headers=None, denied=False):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/review-stalled'
        handler.command = 'POST'
        encoded = json.dumps(payload).encode()
        handler.rfile = io.BytesIO(encoded)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(encoded)), **(headers or {})}
        handler.headers = {key: value for key, value in handler.headers.items() if value is not None}
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond

    def test_review_stalled_endpoint_queues_actions_without_tmux_writes(self):
        self.assertIn('/mobile/api/review-stalled', server.TMUX_WRITES)
        for action in ('resend', 'latest'):
            with self.subTest(action=action):
                self.add()
                request_id = self.stall_review()
                with patch.object(server, 'choose') as choose, \
                        patch.object(server, 'review_send') as send, \
                        patch.object(server.subprocess, 'run') as run, \
                        patch.object(server, 'sessions', return_value=[self.worker, self.reviewer]), \
                        patch.object(server, 'last_reply', self.reply):
                    response = self.stalled_request(
                        {'worker': '%13', 'reviewer': '%12', 'id': request_id, 'action': action})
                response.assert_called_once_with(200, {'ok': True})
                choose.assert_not_called()
                send.assert_not_called()
                run.assert_not_called()
                self.assertNotIn('stalled', self.links()[0])
                self.assertEqual(bool(self.links()[0]['awaiting']), action == 'latest')
                self.assertEqual(self.links()[0]['seen_working'], action == 'resend')
                self.send.assert_not_called()

    def test_review_stalled_endpoint_refuses_changed_reply_without_changing_store(self):
        request_id = self.stall_review()
        before = self.file.read_bytes()
        with patch.object(server, 'sessions', return_value=[self.worker, self.reviewer]), \
                patch.object(server, 'last_reply', return_value='A newer reply.') as reply:
            response = self.stalled_request(
                {'worker': '%13', 'reviewer': '%12', 'id': request_id, 'action': 'latest'})
        response.assert_called_once_with(400, {'error': 'The reviewer replied again. Open the tab.'})
        reply.assert_called_once_with(self.reviewer)
        self.assertEqual(self.file.read_bytes(), before)
        self.send.assert_not_called()

    def test_review_stalled_endpoint_preserves_auth_origin_and_header_guards(self):
        request_id = self.stall_review()
        payload = {'worker': '%13', 'reviewer': '%12', 'id': request_id, 'action': 'resend'}
        before = self.file.read_bytes()
        for headers in ({'Origin': 'https://evil.test'}, {'Origin': ''}, {'X-Codex-Mobile': None}):
            with self.subTest(headers=headers):
                self.stalled_request(payload, headers=headers).assert_called_once_with(
                    403, {'error': 'Reload the page and try again.'})
                self.assertEqual(self.file.read_bytes(), before)
        self.stalled_request(payload, denied=True).assert_not_called()
        self.assertEqual(self.file.read_bytes(), before)

    def test_review_stalled_endpoint_returns_specific_errors_without_changing_store(self):
        request_id = self.stall_review()
        payload = {'worker': '%13', 'reviewer': '%12', 'id': request_id, 'action': 'resend'}
        before = self.file.read_bytes()
        for changes, error in [({'id': '00000000'}, 'This review is no longer stalled.'),
                               ({'worker': '%14'}, 'This review is no longer stalled.'),
                               ({'worker': '%13;pwd'}, 'This review is no longer stalled.'),
                               ({'id': None}, 'This review is no longer stalled.'),
                               ({'action': 'stop'}, 'Choose resend or latest.')]:
            with self.subTest(changes=changes):
                self.stalled_request({**payload, **changes}).assert_called_once_with(400, {'error': error})
                self.assertEqual(self.file.read_bytes(), before)

    def test_old_link_without_request_id_uses_last_reply(self):
        self.request_review()
        link, = self.links()
        link.pop('request_id')
        self.file.write_text(json.dumps([link]))
        self.review_reply = Mock(return_value='APPROVED anchored')
        self.reply.return_value = 'APPROVED legacy'
        self.send.reset_mock()
        self.observe()
        self.review_reply.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('APPROVED legacy', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['awaiting'])

    def test_review_reply_length_keeps_12000_characters_and_reports_cuts(self):
        for approved in (False, True):
            for size in (3500, 12000, 15000):
                for transcript in ('/project/reviewer.jsonl', ''):
                    with self.subTest(approved=approved, size=size, transcript=transcript):
                        self.add()
                        self.reviewer.pop('transcript', None)
                        if transcript:
                            self.reviewer['transcript'] = transcript
                        self.review_reply = Mock(return_value=None)
                        self.request_review()
                        self.send.reset_mock()
                        prefix = 'APPROVED ' if approved else 'Fix '
                        self.review_reply.return_value = prefix + 'x' * (size - len(prefix))
                        self.observe()
                        self.send.assert_called_once()
                        self.assertEqual(self.send.call_args.args[0], self.worker)
                        if size == 15000:
                            body = prefix + 'x' * (12000 - len(prefix))
                            body += f'\n\n[Cut at 12000 characters. Full reply: {transcript}]'
                        else:
                            body = prefix + 'x' * (size - len(prefix))
                        if approved:
                            expected = ('[Tether review] Approved by the reviewer tab (%12):\n\n'
                                        + body + '\n\nThe review is complete. Do the next step your '
                                        'instructions give for an approved review, then stop.')
                        else:
                            expected = ('[Tether review] Feedback from the reviewer tab (%12):\n\n'
                                        + body + '\n\nFix what it raises, then stop and wait.')
                        self.assertEqual(self.send.call_args.args[1], expected)

    def test_feedback_waits_for_workers_review_reply_before_reaching_worker(self):
        for review_reply in ('APPROVED', 'Needs a worker test.'):
            with self.subTest(review_reply=review_reply):
                self.add()
                self.send.reset_mock()
                worker = dict(self.worker, id='%20')
                self.worker['writable'] = True
                entries = [self.worker, self.reviewer, worker]
                self.states.update({'%13': 'Idle', '%12': 'Idle', '%20': 'Idle'})
                replies = {'%13': 'Work done.', '%12': 'Old review.', '%20': 'Other work done.'}
                self.reply.side_effect = lambda entry: replies[entry['id']]
                self.request_review()
                review_links.add(worker='%20', reviewer='%13', reviewer_thread=WORKER_THREAD,
                                 name='Other task', agent='codex', seen_working=True)
                self.observe(entries=entries)
                self.assertEqual([call.args[0]['id'] for call in self.send.call_args_list], ['%12', '%13'])
                replies['%12'] = 'Fix the original task.'
                self.observe(entries=entries, **{'%13': 'Working'})
                self.assertEqual(self.send.call_count, 2)
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertNotIn('wait_ticks', self.links()[0])

                replies['%13'] = review_reply
                self.observe(entries=entries, **{'%13': 'Idle'})
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertFalse(self.links()[1]['awaiting'])
                self.assertEqual(self.send.call_count, 3)
                self.assertEqual(self.send.call_args.args[0], worker)
                self.assertIn(review_reply, self.send.call_args.args[1])

                def send(entry, text, state):
                    if entry['id'] == '%13':
                        replies['%13'] = 'READY'
                self.send.side_effect = send
                self.observe(entries=entries)
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertIn('Fix the original task.', self.send.call_args.args[1])
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertFalse(self.links()[1]['awaiting'])
                self.send.side_effect = None
                review_links.remove('%20')

    def test_shared_reviewer_gets_one_request_until_first_reply_is_read(self):
        self.add(seen_working=True)
        worker = dict(self.worker, id='%20')
        self.states['%20'] = 'Idle'
        entries = [self.worker, self.reviewer, worker]
        self.add(worker='%20', seen_working=True)
        self.observe(entries=entries)
        self.send.assert_called_once()
        first, second = self.links()
        self.assertTrue(first['awaiting'])
        self.assertTrue(second['seen_working'])
        self.assertNotIn('wait_ticks', second)
        self.assertNotIn('retries', second)
        self.observe(entries=entries)
        self.assertEqual(self.send.call_count, 1)
        self.reply.return_value = 'APPROVED'
        self.observe(entries=entries)
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertTrue(self.links()[1]['seen_working'])
        self.assertEqual(self.send.call_count, 2)
        self.observe(entries=entries, elapsed=0)
        self.assertEqual(self.send.call_count, 3)
        self.assertIn('(%20) finished a turn.', self.send.call_args.args[1])

    def test_review_turn_does_not_request_review_of_reviewers_work(self):
        link, = self.links()
        link.update(rounds=2, paused=True)
        self.file.write_text(json.dumps([link]))
        worker = dict(self.worker, id='%20')
        self.worker['writable'] = True
        self.states['%20'] = 'Idle'
        entries = [self.worker, self.reviewer, worker]
        review_links.add(worker='%20', reviewer='%13', reviewer_thread=WORKER_THREAD,
                         name='Other task', agent='codex', seen_working=True)
        self.observe(entries=entries)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.observe(entries=entries, **{'%13': 'Working'})
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertEqual(self.links()[0]['rounds'], 2)
        self.assertTrue(self.links()[0]['paused'])
        self.assertTrue(self.links()[1]['reviewer_seen_working'])
        self.reply.return_value = 'APPROVED'
        self.observe(entries=entries, **{'%13': 'Idle'})
        self.observe(entries=entries)
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse(self.links()[0]['seen_working'])

    def test_send_gap_blocks_request_and_feedback_until_six_seconds(self):
        self.assertEqual(review_links.SEND_GAP, 6)
        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                self.add(seen_working=True)
                self.send.reset_mock()
                link, = self.links()
                if feedback:
                    link.update(awaiting=True, reviewer_seen_working=True)
                    self.file.write_text(json.dumps([link]))
                pane = '%13' if feedback else '%12'
                review_links._last_sent[pane] = self.clock.return_value
                self.observe(elapsed=5)
                self.send.assert_not_called()
                self.assertNotIn('wait_ticks', self.links()[0])
                self.assertNotIn('retries', self.links()[0])
                self.observe(elapsed=1)
                self.send.assert_called_once()
                self.assertEqual(self.send.call_args.args[0]['id'], pane)
                self.assertEqual(review_links._last_sent[pane], self.clock.return_value)
                self.assertNotIn('wait_ticks', self.links()[0])

    def test_guard_wait_holds_for_1000_ticks_then_delivers(self):
        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                self.add(seen_working=True)
                self.send.reset_mock()
                link, = self.links()
                if feedback:
                    link.update(awaiting=True, reviewer_seen_working=True)
                    self.file.write_text(json.dumps([link]))
                pane = '%13' if feedback else '%12'
                review_links._last_sent[pane] = self.clock.return_value
                for _ in range(1000):
                    self.observe(elapsed=0)
                self.assertTrue(self.links()[0]['awaiting' if feedback else 'seen_working'])
                self.send.assert_not_called()
                self.observe()
                self.send.assert_called_once()
                self.assertEqual(self.send.call_args.args[0]['id'], pane)

    def test_reviewed_by_reads_once_under_lock_and_skips_malformed_rows(self):
        links = [*self.links(), {'worker': '%20', 'reviewer': '%21'}, None, [], {},
                 {'worker': [], 'reviewer': '%12'}, {'worker': '%30'},
                 {'worker': '%31', 'reviewer': 12}, {'worker': 'bad', 'reviewer': '%12'},
                 {'worker': '%32', 'reviewer': '%12\n'}]
        def read():
            self.assertTrue(review_links._lock.locked())
            return links
        with patch.object(review_links, '_read', side_effect=read) as read_store:
            self.assertEqual(review_links.reviewed_by(), {'%13': ['%12'], '%20': ['%21']})
        read_store.assert_called_once_with()

    def test_linked_worker_threads_reads_links_of_one_reviewer_conversation_under_lock(self):
        link, cap = self.rows()
        links = [dict(link, worker='%20', reviewer='%21', reviewer_thread=THREAD, worker_thread=WORKER_THREAD),
                 {'worker': '%20', 'reviewer': '%21'},  # not bound to a reviewer conversation yet: counts
                 {'worker': '%20', 'reviewer': '%21', 'reviewer_thread': PARENT_THREAD, 'worker_thread': 'z'},
                 {'worker': '%20', 'reviewer': '%22', 'worker_thread': 'x'},
                 dict(cap, worker='%20', worker_thread='y'), None, {'worker': '%20', 'reviewer': 21}]
        def read():
            self.assertTrue(review_links._lock.locked())
            return links
        with patch.object(review_links, '_read', side_effect=read) as reads:
            self.assertEqual(review_links.linked_worker_threads('%20', '%21', THREAD), {WORKER_THREAD, None})
            # A link of an earlier reviewer conversation in the same pane does not count.
            self.assertEqual(review_links.linked_worker_threads('%20', '%21', PARENT_THREAD), {'z', None})
            self.assertEqual(review_links.linked_worker_threads('%13', '%21', THREAD), set())
            self.assertEqual(reads.call_count, 3)
            self.assertEqual(review_links.linked_worker_threads('%20', '%21', None), set())  # unverified reviewer
            self.assertEqual(reads.call_count, 3)

    def test_paused_reads_once_under_lock_and_skips_unpaused_or_malformed_rows(self):
        link, cap = self.rows()
        links = [link, dict(link), dict(cap, paused=True), {'worker': '%20', 'reviewer': '%21'},
                 dict(cap, worker='%20', unreadable=True),
                 {'worker': '%20', 'worker_thread': WORKER_THREAD, 'reviewer': '%22'}, None, [], {},
                 {'worker': [], 'reviewer': '%12', 'unreadable': True},
                 {'worker': '%30', 'reviewer': '%12\n', 'unreadable': True}]
        def read():
            self.assertTrue(review_links._lock.locked())
            return links
        with patch.object(review_links, '_read', side_effect=read) as read_store:
            self.assertEqual(review_links.paused(), {'%13': ['%12'], '%20': ['%21']})
        read_store.assert_called_once_with()

    def test_remove_drops_every_matching_link_and_keeps_other_rows_under_lock(self):
        link, cap = self.rows()
        other = dict(link, worker='%20')
        other_cap = dict(cap, worker='%20')
        self.file.write_text(json.dumps([link, other, dict(link, reviewer='%21'), None, {}, cap, other_cap]))
        write_store = review_links._write

        def write(rows, **kwargs):
            self.assertTrue(review_links._lock.locked())
            write_store(rows, **kwargs)

        with patch.object(review_links, '_write', side_effect=write) as write_mock:
            self.assertTrue(review_links.remove('%13'))
        write_mock.assert_called_once_with([other, None, {}, cap, other_cap], adopt={('%13', None)})
        self.assertEqual(self.rows(), [other, None, {}, other_cap])

    def test_remove_missing_worker_returns_false_without_writing(self):
        with patch.object(review_links, '_write') as write:
            self.assertFalse(review_links.remove('%99'))
        write.assert_not_called()
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})

    def test_pairs_are_unique_and_reviewers_keep_store_order_without_duplicates(self):
        self.add(reviewer='%21')
        self.add(name='Replacement')
        self.assertEqual([(link['reviewer'], link['name']) for link in self.links()],
                         [('%21', 'Fix the bug'), ('%12', 'Replacement')])
        self.file.write_text(json.dumps(self.links() + self.links()))
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%21', '%12']})
        self.assertTrue(review_links.remove('%13', '%21'))
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})
        self.assertFalse(review_links.remove('%13', '%21'))
        self.assertTrue(review_links.remove('%13'))
        self.assertEqual(self.links(), [])

    def test_unpinned_reviewer_waits_for_uuid_then_sends_to_busy_claude(self):
        self.add(reviewer_thread=None, seen_working=True)
        for thread in (None, 'pending-123-1', 'pending-123-2', 'not-a-uuid', THREAD.upper()):
            self.reviewer['thread'] = thread
            self.observe()
            self.assertIsNone(self.links()[0]['reviewer_thread'])
            self.send.assert_not_called()
        self.reviewer['thread'] = THREAD
        self.observe(**{'%12': 'Working'})
        self.assertEqual(self.links()[0]['reviewer_thread'], THREAD)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[2], 'Working')
        self.observe(**{'%12': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.observe()
        self.assertEqual(self.send.call_count, 1)

    def test_unpinned_reviewer_expires_only_after_start_wait(self):
        self.assertEqual(review_links.START_WAIT, 300)
        with own_time(review_links, time=Mock(return_value=1000)):
            self.add(reviewer_thread=None, seen_working=True)
        self.reviewer['thread'] = 'pending-1-1'
        for now in (1031, 1299, 1300):
            with own_time(review_links, time=Mock(return_value=now)):
                self.observe()
            self.assertEqual(len(self.links()), 1)
        with own_time(review_links, time=Mock(return_value=1301)):
            self.observe()
        self.assertEqual(self.links(), [])
        self.send.assert_not_called()

    def test_several_reviewers_receive_each_workers_turn(self):
        self.add(reviewer='%21')
        reviewer = dict(self.reviewer, id='%21')
        self.states['%21'] = 'Idle'
        for state in ('Working', 'Idle'):
            self.states['%13'] = state
            review_links.observe([self.worker, self.reviewer, reviewer], self.status, self.send, self.reply)
        self.assertEqual([call.args[0]['id'] for call in self.send.call_args_list], ['%12', '%21'])

    def test_store_fields_atomic_replace_and_bound(self):
        link, cap = self.rows()
        self.assertEqual(set(link), {'worker', 'reviewer', 'reviewer_thread', 'name', 'agent',
                                     'created', 'seen_working', 'worker_thread'})
        self.assertIn('cap_reset', cap)
        self.assertFalse(link['seen_working'])
        self.assertIsNone(link['worker_thread'])
        self.assertIsInstance(link['created'], float)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        with patch.object(review_links.os, 'replace', wraps=review_links.os.replace) as replace:
            for index in range(51):
                review_links.add(worker=f'%{index + 20}', reviewer='%12', reviewer_thread=THREAD, name='task', agent='claude')
        self.assertEqual(replace.call_count, 51)
        self.assertEqual(len(self.links()), 50)
        self.assertEqual(self.links()[0]['worker'], '%21')
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])

    def test_three_finished_workers_share_one_reviewer_without_losing_delayed_feedback(self):
        workers = [self.worker, dict(self.worker, id='%20'), dict(self.worker, id='%21')]
        entries = [*workers, self.reviewer]
        for worker in workers[1:]:
            self.add(worker=worker['id'])
        replies = {worker['id']: f"Work from {worker['id']}" for worker in workers}
        replies['%12'] = 'Previous reviewer reply.'
        self.reply.side_effect = lambda entry: replies[entry['id']]
        requests, feedback = [], {}
        outstanding = None

        def send(entry, text, state):
            nonlocal outstanding
            if entry['id'] == '%12':
                self.assertIsNone(outstanding, 'A second review request opened before the first finished.')
                matches = [worker['id'] for worker in workers if f"({worker['id']}) finished" in text]
                self.assertEqual(len(matches), 1)
                outstanding = matches[0]
                requests.append(outstanding)
            else:
                self.assertEqual(entry['id'], outstanding)
                self.assertIn(f'Feedback only for {outstanding}.', text)
                for worker in workers:
                    if worker['id'] != outstanding:
                        self.assertNotIn(f"Feedback only for {worker['id']}.", text)
                self.assertNotIn(outstanding, feedback)
                feedback[outstanding] = text
                outstanding = None

        self.send.side_effect = send

        def tick(**states):
            self.observe(entries=entries, **states)
            self.assertLessEqual(sum(bool(link.get('awaiting')) for link in self.links()), 1)

        tick(**{worker['id']: 'Working' for worker in workers}, **{'%12': 'Approval'})
        tick(**{worker['id']: 'Idle' for worker in workers})
        # All three requests survive more than the former fifteen-minute limit.
        for _ in range(160):
            tick()
        self.send.assert_not_called()
        self.assertTrue(all(link['seen_working'] for link in self.links()))
        tick(**{'%12': 'Idle'})
        for worker in workers:
            pane = worker['id']
            self.assertEqual(outstanding, pane)
            tick(**{'%12': 'Working', pane: 'Approval'})
            replies['%12'] = f'Feedback only for {pane}.'
            tick(**{'%12': 'Idle'})
            # Feedback also survives more than fifteen minutes while its worker is blocked.
            for _ in range(160):
                tick()
            self.assertEqual(outstanding, pane)
            self.assertNotIn(pane, feedback)
            tick(**{pane: 'Idle'})
            self.assertIn(pane, feedback)
            tick()
        self.assertEqual(requests, [worker['id'] for worker in workers])
        self.assertEqual(set(feedback), {worker['id'] for worker in workers})
        self.assertIsNone(outstanding)
        self.assertEqual(self.send.call_count, 6)

    def test_working_to_idle_sends_once_per_turn_with_exact_text(self):
        self.observe(**{'%13': 'Working'})
        self.send.assert_not_called()
        self.assertTrue(self.links()[0]['seen_working'])
        self.assertNotIn('request_id', self.links()[0])
        self.assertEqual(self.links()[0]['worker_thread'], WORKER_THREAD)
        self.observe(**{'%13': 'Idle'})
        request_id = self.links()[0].get('request_id')
        self.assertRegex(request_id or '', r'^[0-9a-f]{8}$')
        self.send.assert_called_once_with(self.reviewer,
            '[Tether review] The Codex tab “Fix the bug” (%13) finished a turn. '
            'Review its work: check the changes and the claims, then list what is wrong or missing. '
            'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. '
            f'Review id: {request_id}. '
            'Its last reply:\n\nI fixed it.\n\nTranscript: /project/worker.jsonl', 'Idle')
        pattern = (r'^\[Tether review\] The (\S+) tab “([^”]*)” \((%\d+)\) finished a turn\.'
                   r'[\s\S]*?Its last reply:\s*([\s\S]*?)\s*Transcript: (\S*)'
                   r'( This is the last round: your reply stays here\.)?\s*$')
        match = re.match(pattern, self.send.call_args.args[1])
        self.assertIsNotNone(match)
        self.assertEqual(match.groups()[:5],
                         ('Codex', 'Fix the bug', '%13', 'I fixed it.', '/project/worker.jsonl'))
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertFalse(self.links()[0]['reviewer_seen_working'])
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 1)  # no new request while awaiting
        self.reply.return_value = 'APPROVED'
        self.observe(**{'%12': 'Working'})
        self.observe(**{'%12': 'Idle'})
        self.assertNotIn('request_id', self.links()[0])
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 2)

    def test_claude_review_requests_use_structured_terminal_errors(self):
        cases = (
            ('session limit', {'isApiErrorMessage': True, 'error': 'rate_limit'},
             "You've hit your session limit · resets 12:50pm", False),
            ('login expired', {'isApiErrorMessage': True, 'error': 'authentication_failed'},
             'Login expired · Please run /login', False),
            ('server error', {'isApiErrorMessage': True, 'error': 'server_error'}, 'API Error', True),
            ('normal reply', {}, 'I fixed it.', True),
            ('limit in prose', {}, 'The test prints "usage limit reached".', True),
            ('no API error flag', {'error': 'rate_limit'}, 'I fixed it.', True),
            ('unreadable transcript', None, 'I fixed it.', True),
            ('malformed transcript', {'isApiErrorMessage': True, 'error': 'rate_limit'},
             "You've hit your weekly limit", True),
        )
        self.worker['agent'] = 'claude'
        for index, (case, fields, reply, sent) in enumerate(cases):
            with self.subTest(case=case):
                transcript = self.file.parent / f'claude-{index}.jsonl'
                self.worker['transcript'] = str(transcript)
                if fields is not None:
                    record = {'type': 'assistant', 'message': {'role': 'assistant',
                              'content': [{'type': 'text', 'text': reply}]}, **fields}
                    transcript.write_text(json.dumps(record) + '\n'
                                          + ('{\n' if case == 'malformed transcript' else '{"type":"system"}\n'))
                self.reply.return_value = reply
                self.add(agent='claude')
                self.send.reset_mock()
                self.observe(**{'%13': 'Working'})
                self.assertTrue(self.links()[0]['seen_working'])
                self.observe(**{'%13': 'Idle'})
                self.assertFalse(self.links()[0]['seen_working'])
                self.assertEqual(self.send.call_count, int(sent))
                if sent:
                    self.assertIn('finished a turn.', self.send.call_args.args[1])

    def test_claude_next_turn_is_reviewed_after_usage_limit(self):
        self.add(agent='claude')
        transcript = self.file.parent / 'claude.jsonl'
        self.worker.update(agent='claude', transcript=str(transcript))
        transcript.write_text('{"type":"assistant","isApiErrorMessage":true,"error":"rate_limit"}\n')
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.observe()
        self.send.assert_not_called()
        self.assertFalse(self.links()[0]['seen_working'])

        self.observe(**{'%13': 'Working'})
        with transcript.open('a') as stream:
            stream.write('{"type":"user","message":{"role":"user","content":"continue"}}\n'
                         '{"type":"assistant","message":{"role":"assistant","content":"I fixed it."}}\n')
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()
        self.assertIn('finished a turn.', self.send.call_args.args[1])

    def test_claude_limit_detection_uses_only_current_turn_in_bounded_tail(self):
        transcript = self.file.parent / 'claude.jsonl'
        worker = dict(self.worker, agent='claude', transcript=str(transcript))
        transcript.write_bytes(b'x' * review_links.TRANSCRIPT_TAIL_BYTES + b'\n'
                               b'{"type":"assistant","isApiErrorMessage":true,"error":"rate_limit"}\n'
                               b'{"type":"assistant","isSidechain":true}\n')
        self.assertTrue(review_links._turn_ended_on_limit(worker))
        with transcript.open('a') as stream:
            stream.write('{"type":"user","message":{"role":"user","content":"continue"}}\n')
        self.assertFalse(review_links._turn_ended_on_limit(worker))

    def test_idle_to_idle_does_not_send_or_rewrite(self):
        self.observe()  # record the first worker thread
        with patch.object(review_links, '_write') as write:
            self.observe()
            self.observe()
        write.assert_not_called()
        self.send.assert_not_called()

    def test_worker_approval_and_input_do_not_finish_turn(self):
        self.observe(**{'%13': 'Working'})
        for state in ('Approval', 'Input needed', 'Unavailable'):
            self.observe(**{'%13': state})
            self.send.assert_not_called()
            self.assertTrue(self.links()[0]['seen_working'])
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()

    def test_reviewer_approval_input_unavailable_or_unwritable_retries(self):
        self.observe(**{'%13': 'Working'})
        for state in ('Approval', 'Input needed', 'Unavailable'):
            self.observe(**{'%13': 'Idle', '%12': state})
            self.send.assert_not_called()
            self.assertTrue(self.links()[0]['seen_working'])
        self.reviewer['writable'] = False
        self.observe(**{'%12': 'Idle'})
        self.send.assert_not_called()
        self.reviewer['writable'] = True
        self.observe()
        self.send.assert_called_once()

    def test_working_codex_reviewer_waits_until_idle(self):
        self.reviewer['agent'] = 'codex'
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle', '%12': 'Working'})
        self.send.assert_not_called()
        self.assertTrue(self.links()[0]['seen_working'])
        self.observe(**{'%12': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[2], 'Idle')

    def test_request_waits_1000_ticks_for_approval_and_uses_current_reply(self):
        self.observe(**{'%13': 'Working', '%12': 'Approval'})
        for _ in range(1000):
            self.observe(**{'%13': 'Idle'})
        self.assertTrue(self.links()[0]['seen_working'])
        self.send.assert_not_called()
        self.reply.return_value = 'Latest worker reply.'
        self.observe(**{'%12': 'Idle'})
        self.send.assert_called_once()
        self.assertIn('Latest worker reply.', self.send.call_args.args[1])

    def test_draft_and_runtime_errors_retry_and_success_resets_retries(self):
        self.observe(**{'%13': 'Working'})
        self.send.side_effect = [DraftError('draft'), RuntimeError('unavailable'), None]
        for count in (1, 2):
            self.observe(**{'%13': 'Idle'})
            self.assertTrue(self.links()[0]['seen_working'])
            self.assertNotIn('retries', self.links()[0])
            self.assertNotIn('%12', review_links._last_sent)
        self.observe()
        self.assertEqual(review_links._last_sent['%12'], self.clock.return_value)
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertNotIn('retries', self.links()[0])
        self.observe()
        self.assertEqual(self.send.call_count, 3)

    def test_request_retries_1000_failures_then_delivers(self):
        for failure in (DraftError('draft'), RuntimeError('unavailable'), 'unwritable'):
            with self.subTest(failure=failure):
                self.add(seen_working=True)
                self.send.reset_mock(side_effect=True)
                self.reviewer['writable'] = failure != 'unwritable'
                self.send.side_effect = None if failure == 'unwritable' else failure
                for _ in range(1000):
                    self.observe()
                self.assertTrue(self.links()[0]['seen_working'])
                self.assertEqual(self.send.call_count, 0 if failure == 'unwritable' else 1000)
                self.send.side_effect = None
                self.reviewer['writable'] = True
                self.reply.return_value = 'Newest worker reply.'
                self.observe()
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertIn('Newest worker reply.', self.send.call_args.args[1])

    def test_live_link_from_before_the_saved_cap_finishes_its_review_after_an_upgrade(self):
        # The store shape of a link that was awaiting a review when the backend restarted with the saved cap.
        for counts, reply, rounds in (({}, 'APPROVED', None), ({}, FINDINGS, 1),
                                      ({'rounds': 4, 'limit': 5, 'grant_rounds': 3}, FINDINGS, 5)):
            with self.subTest(counts=counts, reply=reply):
                self.add_parent()
                self.add()
                self.send.reset_mock()
                self.alert.reset_mock()
                self.request_review()
                link = self.links()[0]
                self.assertTrue(link['awaiting'])
                link.update(counts)
                self.file.write_text(json.dumps([link]))
                self.finish_review(reply)
                link = self.links()[0]
                self.assertEqual(len(self.links()), 1)
                self.assertFalse(link['awaiting'])
                self.assertEqual(link.get('rounds'), rounds)
                self.assertEqual(self.send.call_args.args[0], self.worker)
                if rounds == 5:  # the granted rounds are spent: Mak decides, not the parent
                    self.assertTrue(link['paused'])
                    self.assertNotIn('parent_stage', link)
                    self.assert_mak_alert(rounds=5)
                else:
                    self.assertNotIn('paused', link)
                    self.alert.assert_not_called()

    def request_review(self):
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})

    def test_audit_verdict_records_forwarded_reply_once_across_poll_retries(self):
        self.reviewer['pane_name'] = 'Reviewer'
        commit = 'a1b2c3d4' * 5
        self.commit = Mock(return_value=commit)
        self.review_reply = Mock()
        for approved, reply in ((True, f'APPROVED {commit}'), (False, f'Fix the lock in commit {commit}.')):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory() as folder, \
                    patch.object(audit.paths, 'CONFIG', Path(folder)):
                self.add()
                self.request_review()
                self.review_reply.return_value = reply
                self.send.side_effect = DraftError('draft')
                self.observe()
                self.assertEqual(audit.query(kinds=['review.verdict'])['entries'], [])
                self.send.side_effect = None
                for _ in range(3):
                    self.observe()
                entry, = audit.query(kinds=['review.verdict'])['entries']
                self.assertEqual(entry['actor'], {'source': 'server', 'pane': '%12', 'tab': 'Reviewer'})
                self.assertEqual(entry['detail'], {'worker': '%13', 'approved': approved, 'commit': commit[:12]})

    def review_transcript(self, agent='claude'):
        self.review_reply = server.review_reply
        path = self.file.with_name('reviewer.jsonl')
        self.reviewer.update(agent=agent, transcript=str(path))
        self.request_review()
        request = self.send.call_args.args[1]
        self.send.reset_mock()
        return path, request

    def test_long_claude_review_waits_for_turn_end_despite_idle_polls(self):
        path, request = self.review_transcript()
        tool = {'type': 'tool_use', 'name': 'Bash', 'id': 'tool-1', 'input': {'command': 'run-checks'}}
        records = [
            claude('user', request, timestamp='2026-11-15T12:00:00Z'),
            claude('assistant', 'I will check the change.'),
            claude('assistant', [tool], timestamp='2026-11-15T12:11:00Z'),
        ]
        records[-1]['message']['stop_reason'] = 'tool_use'
        write_records(path, records)
        self.observe(elapsed=660, **{'%12': 'Working'})
        for _ in range(31):
            self.observe(**{'%12': 'Idle'})
        self.send.assert_not_called()
        self.alert.assert_not_called()
        self.assertTrue(self.links()[0]['awaiting'])

        write_records(path, [claude_final(FINDINGS)], 'a')
        self.observe()
        self.send.assert_not_called()
        write_records(path, [dict(CLAUDE_DONE, durationMs=850000)], 'a')
        self.observe()
        self.observe()
        self.send.assert_called_once()
        self.assertIn(FINDINGS, self.send.call_args.args[1])
        self.assertNotIn('Ran ', self.send.call_args.args[1])

    def test_claude_review_crosses_compaction_and_uses_verdict(self):
        path, request = self.review_transcript()
        records = [
            claude('user', request),
            claude('assistant', [{'type': 'tool_use', 'name': 'Bash', 'id': 'tool-1',
                                  'input': {'command': 'run-checks'}}]),
            {'type': 'system', 'subtype': 'compact_boundary'},
            claude('user', 'Summary of the review so far.', isCompactSummary=True, isVisibleInTranscriptOnly=True),
            claude_final(FINDINGS),
            dict(CLAUDE_DONE, durationMs=420000),
        ]
        write_records(path, records)
        self.observe()
        self.send.assert_called_once()
        self.assertIn(FINDINGS, self.send.call_args.args[1])
        self.assertNotIn('Ran ', self.send.call_args.args[1])

    def test_claude_review_uses_verdict_after_a_blocked_stop(self):
        path, request = self.review_transcript()
        blocked = {'type': 'attachment', 'attachment': {'type': 'hook_blocking_error', 'blockingError': 'Continue'}}
        write_records(path, [claude('user', request), claude_final('APPROVED too early'), blocked])
        self.observe()
        self.send.assert_not_called()
        write_records(path, [claude_final(FINDINGS), CLAUDE_DONE], 'a')
        self.observe()
        self.send.assert_called_once()
        self.assertIn(FINDINGS, self.send.call_args.args[1])

    def test_codex_review_waits_for_task_complete(self):
        path, request = self.review_transcript('codex')
        event = lambda kind: {'type': 'event_msg', 'payload': {'type': kind, 'turn_id': 'turn-1'}}
        records = [
            event('task_started'),
            codex('user', request),
            codex('assistant', 'I will check the change.', phase='commentary'),
            codex('assistant', FINDINGS, phase='final_answer'),
        ]
        write_records(path, records)
        self.observe()
        self.send.assert_not_called()
        write_records(path, [event('task_complete')], 'a')
        self.observe()
        self.send.assert_called_once()
        self.assertIn(FINDINGS, self.send.call_args.args[1])

    def test_busy_claude_reviewer_skips_first_idle_reply(self):
        self.observe(**{'%13': 'Working', '%12': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[2], 'Working')
        self.assertTrue(self.links()[0]['reviewer_busy_send'])
        self.observe()
        self.assertTrue(self.links()[0]['reviewer_busy_send'])
        self.reply.reset_mock()
        self.reply.return_value = 'Old turn reply.'
        self.observe(**{'%12': 'Idle'})
        self.reply.assert_not_called()
        self.assertNotIn('reviewer_busy_send', self.links()[0])
        self.assertTrue(self.links()[0]['awaiting'])
        self.reply.return_value = 'Actual review reply.'
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('Actual review reply.', self.send.call_args.args[1])

    def test_busy_claude_worker_feedback_settles_before_review_without_resetting_rounds(self):
        for shows_working in (False, True):
            with self.subTest(shows_working=shows_working):
                self.add()
                self.worker['agent'] = 'claude'
                self.states.update({'%13': 'Idle', '%12': 'Idle'})
                self.send.reset_mock()
                self.request_review()
                link, = self.links()
                link['rounds'] = 1
                self.file.write_text(json.dumps([link]))
                self.observe(**{'%12': 'Working'})
                # A new worker turn starts before the feedback; retain its round marker.
                link, = self.links()
                link['seen_working'] = True
                self.file.write_text(json.dumps([link]))
                self.observe(**{'%13': 'Working'})
                self.finish_review()
                self.assertEqual(self.send.call_count, 2)
                self.assertEqual(self.send.call_args.args[2], 'Working')
                self.assertEqual(self.links()[0]['rounds'], 2)
                self.assertTrue(self.links()[0]['worker_busy_send'])
                if shows_working:
                    self.observe()
                    self.assertTrue(self.links()[0]['worker_busy_send'])
                self.reply.reset_mock()
                self.observe(**{'%13': 'Idle'})
                self.reply.assert_not_called()
                self.assertEqual(self.send.call_count, 2)
                self.assertNotIn('worker_busy_send', self.links()[0])
                self.observe(**{'%13': 'Working'})  # queued fix starts after the transient Idle
                self.assertEqual(self.links()[0]['rounds'], 2)
                self.observe(**{'%13': 'Idle'})
                self.assertEqual(self.send.call_count, 3)
                self.assertEqual(self.send.call_args.args[0], self.reviewer)
                self.assertEqual(self.links()[0]['rounds'], 2)

    def test_busy_claude_worker_can_finish_between_ticks(self):
        self.worker['agent'] = 'claude'
        self.request_review()
        self.observe(**{'%13': 'Working'})
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 2)
        self.observe()
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.links()[0]['rounds'], 1)

    def test_old_wait_and_retry_fields_drop_on_next_state_change(self):
        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                self.add(seen_working=True)
                self.send.reset_mock()
                link, = self.links()
                link.update(wait_ticks=10000, retries=10000)
                if feedback:
                    link.update(awaiting=True, reviewer_seen_working=True)
                self.file.write_text(json.dumps([link]))
                pane = '%13' if feedback else '%12'
                self.observe(**{pane: 'Approval'})
                self.send.assert_not_called()
                self.observe(**{pane: 'Idle'})
                self.send.assert_called_once()
                self.assertNotIn('wait_ticks', self.links()[0])
                self.assertNotIn('retries', self.links()[0])

    def test_review_send_subprocess_failure_preserves_pending_state_and_other_links(self):
        self.add(seen_working=True)
        worker = dict(self.worker, id='%20')
        reviewer = dict(self.reviewer, id='%21')
        self.states.update({'%20': 'Idle', '%21': 'Idle'})
        self.add(worker='%20', reviewer='%21', seen_working=True)
        self.send.side_effect = server.review_send
        for failure in (OSError('gone'), subprocess.TimeoutExpired('tmux', 5)):
            with self.subTest(failure=failure), \
                    patch.object(server, 'stash_quiet_draft', return_value=None), \
                    patch.object(server, 'send_text', side_effect=[failure, None]), \
                    patch.object(server, 'clear_unsent', side_effect=failure):
                self.observe(entries=[self.worker, self.reviewer, worker, reviewer])
                first, second = self.links()
                self.assertTrue(first['seen_working'])
                self.assertEqual(first['worker_thread'], WORKER_THREAD)
                self.assertTrue(second['awaiting'])
                # Replace only the delivered second link for the next failure case.
                self.add(worker='%20', reviewer='%21', seen_working=True)
        self.send.side_effect = None
        self.observe(entries=[self.worker, self.reviewer, worker, reviewer])
        self.assertTrue(self.links()[0]['awaiting'])

    def finish_review(self, reply='Needs a regression test.'):
        self.reply.return_value = reply
        self.anchored_reply = reply
        self.observe(**{'%12': 'Working'})
        self.observe(**{'%12': 'Idle'})

    def test_reviewer_reply_returns_exact_feedback_once_and_next_turn_gets_reviewed(self):
        self.request_review()
        self.finish_review('  Needs a regression test.\n ')
        self.assertEqual(self.send.call_count, 2)
        self.send.assert_called_with(self.worker,
            '[Tether review] Feedback from the reviewer tab (%12):\n\n'
            'Needs a regression test.\n\nFix what it raises, then stop and wait.', 'Idle')
        self.reply.assert_called_with(self.reviewer)
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.assertFalse(self.links()[0]['awaiting'])
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.request_review()
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_review_turn_shorter_than_a_tick_is_seen_by_a_changed_reply(self):
        self.request_review()
        self.reply.return_value = 'Missing a test.'  # the reviewer never shows Working between ticks
        self.observe()
        self.send.assert_called_with(self.worker, '[Tether review] Feedback from the reviewer tab (%12):\n\n'
                                     'Missing a test.\n\nFix what it raises, then stop and wait.', 'Idle')
        self.assertFalse(self.links()[0]['awaiting'])

    def test_unchanged_idle_reviewer_gives_up_after_30_ticks(self):
        self.request_review()
        calls = self.send.call_count
        for _ in range(29):
            self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.observe()
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('reviewer_before', self.links()[0])
        self.assertEqual(self.send.call_count, calls)
        self.assertNotIn('stalled', self.links()[0])
        self.alert.assert_not_called()

    def test_feedback_is_stripped_without_cut_below_limit(self):
        self.request_review()
        self.finish_review(' \n' + 'x' * 4000 + '\n ')
        self.send.assert_called_with(self.worker,
            '[Tether review] Feedback from the reviewer tab (%12):\n\n'
            + 'x' * 4000 + '\n\nFix what it raises, then stop and wait.', 'Idle')

    def test_approved_reply_reaches_worker_with_exact_text(self):
        for reply in ('APPROVED', '  approved: all checks pass.\n', 'Approved with notes'):
            with self.subTest(reply=reply):
                self.add()
                self.send.reset_mock()
                self.request_review()
                self.finish_review(reply)
                self.assertEqual(self.send.call_count, 2)
                self.send.assert_called_with(self.worker,
                    '[Tether review] Approved by the reviewer tab (%12):\n\n'
                    + reply.strip() + '\n\nThe review is complete. Do the next step your instructions give '
                    'for an approved review, then stop.', 'Idle')
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertTrue(self.links()[0]['approved_turn'])
                self.assertEqual(self.links()[0]['approved_before'], review_links._digest(self.reply(self.worker)))
                self.assertEqual(self.links()[0].get('rounds', 0), 0)

    def fail_to_limit(self, findings=FINDINGS):
        """Run reviews until the limit. Every review still finds a blocking problem."""
        for _ in range(review_links.MAX_ROUNDS):
            self.request_review()
            self.finish_review(findings)

    def add_parent(self, level=5, entry=None):
        if entry is None:
            entry = dict(id='%11', agent='codex', thread=PARENT_THREAD, writable=True)
            self.entries.append(entry)
            self.states['%11'] = 'Idle'
        self.level = Mock(return_value=level)
        self.parent = Mock(return_value=(entry['id'], entry['thread']))
        return entry

    def assert_mak_alert(self, rounds=3):
        self.alert.assert_called_once_with(
            'Review needs you',
            f'“Fix the bug”: the reviewer still found blocking problems after {rounds} rounds. '
            f'Answer in that tab to restart reviews. Open findings: “{FINDINGS}”',
            '%13',
        )

    def assert_mak_stop(self, rounds=3, reviewer='%12'):
        self.send.assert_called_with(
            self.worker,
            f'[Tether review] Stopped after {rounds} review rounds: the reviewer tab ({reviewer}) still finds '
            f'blocking problems. Do not fix them in this turn. Mak decides the next step.\n\n'
            f'Open findings:\n\n{FINDINGS}\n\nReply with a short list of the open findings and what you tried, '
            'then end with "Needs you: yes".',
            'Idle',
        )
        self.assert_mak_alert(rounds)
        link, = self.links()
        self.assertTrue(link['paused'])
        self.assertNotIn('parent_stage', link)

    def assert_parent_notice(self, entry, preview=FINDINGS, state='Idle'):
        cli = Path(review_links.__file__).resolve().with_name('agent_tab_cli.py')
        self.send.assert_called_with(
            entry,
            '[Tether review] “Fix the bug” (%13) still has blocking review findings after 3 rounds. '
            f'Open findings: “{preview}”\n\n'
            f'To allow 2 more rounds, send it the next step: python3 {cli} send --pane %13 "<what to fix>". '
            f'To hand it to Mak: python3 {cli} escalate --pane %13.',
            state,
        )

    def test_level_three_with_live_parent_uses_unchanged_mak_copy(self):
        self.add_parent(level=3)
        self.fail_to_limit()
        self.assert_mak_stop()
        self.level.assert_called_once_with(self.worker)
        self.parent.assert_not_called()

    def test_levels_four_and_five_send_only_to_the_worker_and_parent_at_round_three(self):
        for level in (4, 5):
            with self.subTest(level=level):
                self.file.write_text('[]')
                self.add()
                self.send.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent(level=level)
                self.fail_to_limit()
                self.send.assert_any_call(
                    self.worker,
                    '[Tether review] 3 review rounds still found blocking problems. Your parent tab decides '
                    f'the next step. Do not fix them in this turn.\n\nOpen findings:\n\n{FINDINGS}\n\n'
                    'Reply with the open findings and what you tried. End with "Needs you: no" unless '
                    'a finding needs Mak (taste, new authority, or you disagree with the reviewer).',
                    'Idle',
                )
                self.assert_parent_notice(parent)
                link, = self.links()
                self.assertEqual(link['rounds'], 3)
                self.assertTrue(link['paused'])
                self.assertFalse(link['awaiting'])
                self.assertEqual(link['parent_stage'], dict(pane='%11', thread=PARENT_THREAD,
                                                           notice=FINDINGS, sent=True))
                self.request_review()  # The worker answers the findings.
                self.assertEqual(self.send.call_count, 7)
                self.alert.assert_not_called()
                self.level.assert_called_once_with(self.worker)
                self.parent.assert_called_once_with(self.worker)

    def test_parent_notice_retries_busy_draft_and_send_gap_then_sends_once(self):
        parent = self.add_parent()
        self.states['%11'] = 'Working'
        findings = 'Some\n' + 'x' * 400
        self.fail_to_limit(findings)
        self.assertFalse(self.links()[0]['parent_stage']['sent'])
        self.assertEqual(self.send.call_count, 6)
        review_links._last_sent['%11'] = self.clock.return_value
        self.observe(elapsed=5, **{'%11': 'Idle'})
        self.assertEqual(self.send.call_count, 6)
        self.send.side_effect = DraftError('Keep the draft.')
        self.observe(elapsed=1)
        self.assertFalse(self.links()[0]['parent_stage']['sent'])
        self.send.side_effect = None
        self.observe()
        self.assert_parent_notice(parent, preview='Some ' + 'x' * 295 + '…')
        self.assertTrue(self.links()[0]['parent_stage']['sent'])
        self.observe()
        self.assertEqual(self.send.call_count, 8)
        self.alert.assert_not_called()

    def test_parent_notice_refused_by_the_terminal_goes_to_mak(self):
        for error in (RuntimeError('Pane too small.'), DraftError('Keep the draft.')):
            with self.subTest(error=type(error).__name__):
                self.file.write_text('[]')
                self.add()
                self.alert.reset_mock()
                self.send.side_effect = None
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.send.side_effect = lambda entry, *args: (_ for _ in ()).throw(error) if entry is parent else None
                self.fail_to_limit()  # the first refusal comes on the limit tick
                for _ in range(review_links.LOSS_TICKS - 2):
                    self.observe(elapsed=review_links.SEND_GAP)
                self.alert.assert_not_called()
                self.assertEqual(self.links()[0]['parent_stage']['failed'], review_links.LOSS_TICKS - 1)
                self.observe(elapsed=review_links.SEND_GAP)  # the last refusal
                self.observe(elapsed=review_links.SEND_GAP)
                self.assert_mak_alert()
                link, = self.links()
                self.assertTrue(link['paused'])
                self.assertNotIn('parent_stage', link)

    def test_working_claude_parent_can_receive_notice(self):
        parent = self.add_parent()
        parent['agent'] = 'claude'
        self.states['%11'] = 'Working'
        self.fail_to_limit()
        self.assert_parent_notice(parent, state='Working')
        self.assertTrue(self.links()[0]['parent_stage']['sent'])

    def test_parent_can_grant_two_rounds_before_or_after_the_findings_reply(self):
        for summary_finished in (False, True):
            with self.subTest(summary_finished=summary_finished):
                self.add()
                self.alert.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.turn_source = Mock(return_value='user')
                self.fail_to_limit()
                if summary_finished:
                    self.request_review()
                self.assertTrue(review_links.agent_prompt('%13', parent['id'], parent['thread']))
                link, = self.links()
                self.assertEqual((link['rounds'], link['limit']), (3, 5))
                self.assertNotIn('parent_stage', link)
                self.assertNotIn('paused', link)
                if not summary_finished:
                    summary = next(call.args[1] for call in reversed(self.send.call_args_list)
                                   if call.args[0] == self.worker)
                    records = [codex('user', summary), codex('assistant', 'Open findings.')]
                    self.turn_source = server.review_turn_source
                    with patch.object(server, 'notification_records', return_value=records):
                        self.request_review()
                    self.assertFalse(self.links()[0]['awaiting'])
                self.request_review()
                self.assertTrue(self.links()[0]['awaiting'])
                self.finish_review()
                self.assertEqual(self.links()[0]['rounds'], 4)
                self.assertIn('Fix what it raises', self.send.call_args.args[1])
                self.request_review()
                self.finish_review(FINDINGS)
                self.assert_mak_stop(rounds=5)
                self.assertEqual(self.links()[0]['rounds'], 5)
                review_links.agent_prompt('%13', parent['id'], parent['thread'])
                self.request_review()
                self.assertTrue(self.links()[0]['paused'])
                self.assertEqual(self.links()[0]['limit'], 5)
                self.assertEqual(self.links()[0]['rounds'], 5)
                self.assert_mak_alert(rounds=5)

    def test_parent_can_grant_as_soon_as_it_receives_notice(self):
        parent = self.add_parent()
        self.send.side_effect = lambda entry, *_: (
            review_links.agent_prompt('%13', parent['id'], parent['thread']) if entry == parent else None)
        self.fail_to_limit()
        link, = self.links()
        self.assertEqual(link['limit'], 5)
        self.assertNotIn('paused', link)
        self.assertNotIn('parent_stage', link)

    def test_parent_grant_during_watcher_tick_keeps_the_observed_working_turn(self):
        parent = self.add_parent()
        self.fail_to_limit()
        self.request_review()  # the findings summary ends before the next step starts
        status = self.status.side_effect

        def grant_during_status(entry):
            if entry == self.worker:
                review_links.agent_prompt('%13', parent['id'], parent['thread'])
            return status(entry)

        self.status.side_effect = grant_during_status
        self.observe(**{'%13': 'Working'})
        self.status.side_effect = status
        self.observe(**{'%13': 'Idle'})
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0]['rounds'], 3)
        self.alert.assert_not_called()

    def test_grant_during_first_summary_idle_keeps_the_next_turn_reviewable(self):
        parent = self.add_parent()
        self.fail_to_limit()
        summary = next(call.args[1] for call in reversed(self.send.call_args_list) if call.args[0] == self.worker)
        records = [codex('user', summary), codex('assistant', 'Open findings.')]
        self.turn_source = server.review_turn_source
        self.enterContext(patch.object(server, 'notification_records', return_value=records))
        self.observe(**{'%13': 'Working'})
        status = self.status.side_effect

        def grant_on_idle(entry):
            if entry == self.worker:
                review_links.agent_prompt('%13', parent['id'], parent['thread'])
            return status(entry)

        self.status.side_effect = grant_on_idle
        self.observe(**{'%13': 'Idle'})
        self.status.side_effect = status
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertEqual((self.links()[0]['rounds'], self.links()[0]['limit']), (3, 5))
        self.assertNotIn('paused', self.links()[0])
        self.observe()
        self.assertFalse(self.links()[0]['awaiting'])
        records.extend([codex('user', 'Fix the missing lock.'), codex('assistant', 'Fixed the lock.')])
        self.request_review()
        self.assertTrue(self.links()[0]['awaiting'])
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 4)

    def test_grant_while_the_findings_summary_is_working_reviews_only_the_next_turn(self):
        for agent, message in (('claude', claude), ('codex', codex)):
            with self.subTest(agent=agent):
                self.add()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.worker['agent'] = agent
                if agent == 'codex':
                    self.work_mark = Mock(return_value='same tree')
                    self.request_review()
                    self.finish_review()
                self.fail_to_limit()
                failed = 4 if agent == 'codex' else 3
                summary = next(call.args[1] for call in reversed(self.send.call_args_list)
                               if call.args[0] == self.worker)
                if agent == 'claude':
                    records = [{'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': summary}}]
                else:
                    records = [{'type': 'event_msg', 'payload': {'type': 'user_message', 'message': summary}}]
                    self.work_mark.return_value = 'changed tree'
                self.turn_source = server.review_turn_source
                with patch.object(server, 'notification_records', return_value=records):
                    self.observe(**{'%13': 'Working'})
                    review_links.agent_prompt('%13', parent['id'], parent['thread'])
                    records.append(message('assistant', 'Open findings.'))
                    self.send.reset_mock()
                    self.observe(**{'%13': 'Idle'})
                    self.send.assert_not_called()
                    self.assertFalse(self.links()[0]['awaiting'])
                    self.assertEqual(sum(self.links()[0].get(key, 0) for key in ('rounds', 'empty_rounds')), failed)
                    records.extend([message('user', 'Fix the missing lock.'), message('assistant', 'Fixed the lock.')])
                    self.request_review()
                    self.assertTrue(self.links()[0]['awaiting'])
                    self.finish_review(FINDINGS)
                    self.assertEqual(sum(self.links()[0].get(key, 0) for key in ('rounds', 'empty_rounds')), failed + 1)
                    if failed == 4:
                        self.assert_mak_stop(rounds=5)

    def test_findings_summary_keeps_the_grant_after_watcher_memory_is_lost(self):
        parent = self.add_parent()
        self.fail_to_limit()
        summary = next(call.args[1] for call in reversed(self.send.call_args_list) if call.args[0] == self.worker)
        records = [codex('user', summary)]
        self.turn_source = server.review_turn_source
        self.states['%13'] = 'Working'
        review_links.agent_prompt('%13', parent['id'], parent['thread'])
        review_links._last_sent.clear()  # the persisted cap survives a watcher restart
        with patch.object(server, 'notification_records', return_value=records):
            self.observe()
            later = time.time() + review_links.AGENT_PROMPT_TTL + 1
            with patch.object(review_links.time, 'time', return_value=later):
                records.append(codex('assistant', 'Open findings.'))
                self.observe(**{'%13': 'Idle'})
                self.assertFalse(self.links()[0]['awaiting'])
                records.extend([codex('user', 'Fix the missing lock.'), codex('assistant', 'Fixed the lock.')])
                self.request_review()
                self.finish_review()
                self.assertEqual(self.links()[0]['rounds'], 4)

    def test_mak_prompt_queued_during_stopped_summary_reviews_only_the_next_turn(self):
        for agent, message in (('claude', claude), ('codex', codex)):
            with self.subTest(agent=agent):
                self.add()
                self.alert.reset_mock()
                self.worker['agent'] = agent
                self.fail_to_limit()
                summary = self.send.call_args.args[1]
                records = [message('user', summary)]
                self.turn_source = server.review_turn_source
                with patch.object(server, 'notification_records', return_value=records):
                    self.observe(**{'%13': 'Working'})
                    records.append(message('assistant', 'Open findings.'))
                    self.send.reset_mock()
                    self.observe(**{'%13': 'Idle'})
                    self.send.assert_not_called()
                    self.assertTrue(self.links()[0]['paused'])
                    records.extend([message('user', 'Fix the missing lock.'), message('assistant', 'Fixed the lock.')])
                    self.request_review()
                    self.assertTrue(self.links()[0]['awaiting'])
                    self.finish_review()
                    self.assertEqual(self.links()[0]['rounds'], 1)

    def test_clipped_findings_summary_cannot_spend_a_granted_round(self):
        hook = {'type': 'attachment', 'attachment': {'type': 'hook_blocking_error', 'hookName': 'Stop'}}
        for agent, message, prefix in (('claude', claude, []), ('claude', claude, [hook]), ('codex', codex, [])):
            with self.subTest(agent=agent, hook=bool(prefix)):
                self.add()
                self.entries = [self.worker, self.reviewer]
                self.worker['agent'] = agent
                parent = self.add_parent()
                self.fail_to_limit()
                self.turn_source = server.review_turn_source
                self.observe(**{'%13': 'Working'})
                review_links.agent_prompt('%13', parent['id'], parent['thread'])
                self.send.reset_mock()
                records = [*prefix, message('assistant', 'Open findings.')]
                with patch.object(server, 'notification_records', return_value=records):
                    self.observe(**{'%13': 'Idle'})
                    self.send.assert_not_called()
                    self.assertEqual(self.links()[0]['rounds'], 3)
                    records[:] = [message('user', 'Fix the lock.'), message('assistant', 'Fixed.')]
                    self.request_review()
                    self.finish_review()
                    self.assertEqual(self.links()[0]['rounds'], 4)
                    records[:] = [message('user', 'Review feedback.'), message('assistant', 'Fixed.')]
                    self.request_review()  # the turn that follows the review feedback
                    self.finish_review()
                    self.assertEqual(self.links()[0]['rounds'], 5)
                    self.request_review()  # the turn that reports the remaining findings to Mak
                    records[:] = [message('user', 'Mak starts another task.'), message('assistant', 'Done.')]
                    self.request_review()
                    self.finish_review()
                    self.assertEqual(self.links()[0]['rounds'], 1)

    def test_parent_grant_cannot_come_from_another_pane_or_a_different_thread(self):
        self.add_parent()
        self.turn_source = Mock(return_value='user')
        self.fail_to_limit()
        self.request_review()
        for sender, thread in (('%14', PARENT_THREAD), ('%11', THREAD)):
            with self.subTest(sender=sender, thread=thread):
                review_links.agent_prompt('%13', sender, thread)
                self.request_review()
                link, = self.links()
                self.assertTrue(link['paused'])
                self.assertIn('parent_stage', link)
                self.assertNotIn('limit', link)
        self.alert.assert_not_called()

    def test_parent_grant_while_unlinked_preserves_the_five_round_cap_on_relink(self):
        for prior, remaining, reviewer in ((3, 2, '%12'), (4, 1, '%12'), (4, 1, '%21')):
            with self.subTest(prior=prior, reviewer=reviewer):
                self.file.write_text('[]')
                self.add()
                self.entries = [self.worker, self.reviewer]
                self.alert.reset_mock()
                parent = self.add_parent()
                self.fail_to_limit()
                self.request_review()  # finish the summary
                if prior == 4:
                    link, = self.links()
                    link['empty_rounds'] = 1
                    self.file.write_text(json.dumps([link]))
                self.assertTrue(review_links.remove('%13', '%12'))
                self.assertEqual(review_links.reviewed_by(), {})
                self.assertEqual(review_links.paused(), {})
                for sender, thread, worker_thread in (('%15', parent['thread'], WORKER_THREAD),
                                                       (parent['id'], THREAD, WORKER_THREAD),
                                                       (parent['id'], parent['thread'], THREAD)):
                    review_links.agent_prompt('%13', sender, thread, worker_thread=worker_thread)
                    with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
                        self.add(worker_thread=WORKER_THREAD, reset=False)
                review_links.agent_prompt('%13', parent['id'], parent['thread'], worker_thread=WORKER_THREAD)
                if reviewer != '%12':
                    self.entries.append(dict(self.reviewer, id=reviewer))
                    self.states[reviewer] = 'Idle'
                self.add(reviewer=reviewer, worker_thread=WORKER_THREAD, reset=False)
                self.assertEqual(self.links()[0]['grant_rounds'], prior)
                self.assertEqual(sum(self.links()[0].get(field, 0) for field in review_links.COUNTS), 5 - remaining)
                for _ in range(remaining):
                    self.request_review()
                    self.reply.return_value = FINDINGS
                    self.observe(**{reviewer: 'Working'})
                    self.observe(**{reviewer: 'Idle'})
                self.assertIn('Stopped after 5 review rounds', self.send.call_args.args[1])
                self.assert_mak_alert(5)
                self.assertTrue(self.links()[0]['paused'])
                self.assertEqual(self.links()[0].get('rounds', 0) + self.links()[0].get('empty_rounds', 0), 5)
                self.assertTrue(review_links.remove('%13'))
                with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
                    self.add(worker_thread=WORKER_THREAD, reset=False)

    def test_paused_cap_survives_reviewer_close_and_expiry(self):
        for reviewer in ('%12', '%21'):
            for close in (False, True):
                with self.subTest(reviewer=reviewer, close=close):
                    self.file.write_text('[]')
                    self.add()
                    self.fail_to_limit()
                    if close:
                        review_links.close('%12')
                    else:
                        self.assertTrue(review_links.remove('%13'))
                        review_links.close('%12')
                    record, = self.links()
                    later = record['saved_at'] + review_links.SAVED_CAP_TTL + 1
                    with own_time(review_links, time=lambda: later):
                        with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
                            self.add(reviewer=reviewer, worker_thread=WORKER_THREAD, reset=False)
                        review_links.close('%13')
                        self.add(reviewer=reviewer, worker_thread=WORKER_THREAD, reset=False)
                    link, = self.links()
                    self.assertNotIn('rounds', link)

    def test_saved_counts_follow_the_reviewer_pane_and_thread_on_relink(self):
        for counts in ((3, 1), (1, 3)):
            with self.subTest(counts=counts):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                cap.update(limit=5, grant_rounds=3)
                self.set_counts(cap, '%12', rounds=counts[0], empty_rounds=counts[1])
                self.file.write_text(json.dumps(rows))
                review_links.remove('%13')
                record, = self.links()
                self.assertEqual((record['rounds'], record['empty_rounds']), counts)
                self.assertEqual((record['limit'], record['grant_rounds']), (5, 3))
                for reviewer, thread in (('%22', THREAD), ('%12', PARENT_THREAD), ('%12', THREAD)):
                    self.add(reviewer=reviewer, reviewer_thread=thread, worker_thread=WORKER_THREAD, reset=False)
                    link, = self.links()
                    self.assertEqual((link.get('rounds', 0), link.get('empty_rounds', 0)), counts)
                    self.assertEqual((link['limit'], link['grant_rounds']), (5, 3))
                    review_links.remove('%13', reviewer)

    def test_saved_cap_budget_evicts_unpaused_records_first(self):
        with patch.object(review_links, 'MAX_SAVED_CAPS', 2, create=True), \
                own_time(review_links, time=iter(range(1000, 1100)).__next__):
            for pane, rounds in (('%13', 3), ('%20', 1), ('%21', 2)):
                self.add(worker=pane, worker_thread=WORKER_THREAD)
                links = self.links()
                links[-1].update(rounds=rounds, paused=rounds == 3)
                self.file.write_text(json.dumps(links))
                self.assertTrue(review_links.remove(pane))
            self.assertEqual({link['worker'] for link in self.links()}, {'%13', '%21'})
            with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
                self.add(worker_thread=WORKER_THREAD, reset=False)
            later = max(link['saved_at'] for link in self.links()) + review_links.SAVED_CAP_TTL
            with own_time(review_links, time=lambda: later):
                self.add(worker='%21', worker_thread=WORKER_THREAD, reset=False)
            self.assertNotIn('rounds', self.links()[-1])

    def test_full_saved_cap_store_accepts_live_links_and_evicts_oldest_paused_cap(self):
        self.file.write_text('[]')
        with patch.object(review_links, 'MAX_SAVED_CAPS', 2), \
                own_time(review_links, time=iter(range(1000, 1100)).__next__):
            for pane in ('%20', '%21', '%22'):
                self.add(worker=pane, worker_thread=WORKER_THREAD, reset=False)
                links = self.links()
                links[-1].update(rounds=3, paused=True)
                self.file.write_text(json.dumps(links))
                if pane == '%22':
                    self.assertEqual(review_links.reviewed_by(), {'%22': ['%12']})
                    self.assertEqual(len(self.links()), 3)
                self.assertTrue(review_links.remove(pane))
            self.assertEqual({link['worker'] for link in self.links()}, {'%21', '%22'})
            for pane in ('%21', '%22'):
                with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
                    self.add(worker=pane, worker_thread=WORKER_THREAD, reset=False)
            self.add(worker='%20', worker_thread=WORKER_THREAD, reset=False)
            self.assertEqual(review_links.reviewed_by(), {'%20': ['%12']})

    def test_expired_saved_caps_cannot_raise_the_count_of_duplicate_rows(self):
        for second_age in (0, review_links.SAVED_CAP_TTL + 1):
            with self.subTest(second_age=second_age), own_time(review_links, time=lambda: 100_000):
                rows = [dict(worker='%13', worker_thread=WORKER_THREAD,
                             saved_at=99_999 - review_links.SAVED_CAP_TTL, rounds=4, limit=5, grant_rounds=3),
                        dict(worker='%13', worker_thread=WORKER_THREAD,
                             saved_at=100_000 - second_age, empty_rounds=1)]
                self.file.write_text(json.dumps(rows))
                self.add(worker_thread=WORKER_THREAD, reset=False)
                link, = self.links()
                self.assertNotIn('rounds', link)
                self.assertEqual(link.get('empty_rounds', 0), 0 if second_age else 1)

    def test_below_cap_relink_continues_from_the_saved_round_count(self):
        for _ in range(2):
            self.request_review()
            self.finish_review()
        self.assertTrue(review_links.remove('%13', '%12'))
        self.add(worker_thread=WORKER_THREAD, reset=False)
        self.assertEqual(self.links()[0]['rounds'], 2)
        self.request_review()
        self.finish_review(FINDINGS)
        self.assert_mak_stop()

    def test_relinking_the_reviewer_during_a_feedback_turn_keeps_the_round_count(self):
        # B281: the turn that acts on feedback is not Mak's restart, also when the reviewer is relinked during it.
        for agent, relink_while_working in product(('codex', 'claude'), (True, False)):
            with self.subTest(agent=agent, relink_while_working=relink_while_working):
                self.file.write_text('[]')
                self.worker['agent'] = agent
                self.states.update({'%13': 'Idle', '%12': 'Idle'})
                self.send.reset_mock()
                self.alert.reset_mock()
                self.add(worker_thread=WORKER_THREAD, reset=False)
                for _ in range(2):
                    self.request_review()
                    self.finish_review()
                if relink_while_working:
                    self.observe(**{'%13': 'Working'})  # the worker acts on the second findings
                    self.observe()
                self.assertTrue(review_links.remove('%13', '%12'))
                self.add(worker_thread=WORKER_THREAD, reset=False)
                self.assertEqual(self.links()[0]['rounds'], 2)
                self.observe(**{'%13': 'Working'})  # the new link sees the feedback turn run
                self.observe(**{'%13': 'Idle'})  # the feedback turn ends; the new link asks for review 3
                self.assertEqual(self.links()[0]['rounds'], 2)
                self.finish_review(FINDINGS)
                self.assert_mak_stop()

    def test_replacement_reviewer_continues_at_round_three_in_either_order(self):
        for stop, limit in (('before add', 3), ('before add', 5), ('after add', 3), ('close after add', 3)):
            with self.subTest(stop=stop, limit=limit):
                self.file.write_text('[]')
                self.entries = [self.worker, self.reviewer, dict(self.reviewer, id='%21')]
                self.states.update({'%13': 'Idle', '%12': 'Idle', '%21': 'Idle'})
                self.turn_source = None
                self.level = lambda _: limit
                self.add(worker_thread=WORKER_THREAD, reset=False)
                for _ in range(2):
                    self.request_review()
                    self.finish_review()
                    self.turn_source = lambda _: 'review-feedback'
                self.assert_one_cap(2)
                if limit == 5:
                    rows = self.rows()
                    next(row for row in rows if row.get('cap')).update(limit=5, grant_rounds=3)
                    self.file.write_text(json.dumps(rows))

                if stop == 'before add':
                    self.assertTrue(review_links.remove('%13', '%12'))
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                if stop == 'after add':
                    self.assertTrue(review_links.remove('%13', '%12'))
                if stop == 'close after add':
                    review_links.close('%12')

                self.alert.reset_mock()
                for rounds in range(3, limit + 1):
                    self.request_review()
                    self.send.reset_mock()
                    self.reply.return_value = FINDINGS
                    self.observe(**{'%21': 'Working'})
                    self.observe(**{'%21': 'Idle'})
                    if rounds == limit:
                        self.assert_mak_stop(rounds, reviewer='%21')
                    else:
                        self.send.assert_called_once()
                        self.assertIn('Feedback from the reviewer tab (%21)', self.send.call_args.args[1])
                        self.alert.assert_not_called()
                    self.assert_one_cap(rounds, reviewer='%21')

    def test_only_a_sole_reviewer_without_counts_adopts_a_stopped_reviewers_history(self):
        for own_count in (0, 1):
            with self.subTest(own_count=own_count):
                self.file.write_text('[]')
                for pane in ('%12', '%21', '%22'):
                    self.add(reviewer=pane, worker_thread=WORKER_THREAD, reset=False)
                self.entries = [self.worker, self.reviewer, *(dict(self.reviewer, id=pane) for pane in ('%21', '%22'))]
                self.states.update({'%21': 'Idle', '%22': 'Idle'})
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                self.set_counts(cap, '%12', rounds=2)
                if own_count:
                    self.set_counts(cap, '%21', rounds=own_count)
                self.file.write_text(json.dumps(rows))

                review_links.remove('%13', '%12')
                self.observe()
                by_reviewer = {link['reviewer']: link.get('rounds', 0) for link in self.links()}
                self.assertEqual(by_reviewer, {'%21': own_count, '%22': 0})
                self.assert_one_cap(2)

                review_links.remove('%13', '%22')
                self.observe()
                expected = {f'%21:{THREAD}': {'rounds': own_count or 2}}
                if own_count:
                    expected[f'%12:{THREAD}'] = {'rounds': 2}
                cap, = (row for row in self.rows() if row.get('cap'))
                self.assertEqual(cap['counts'], expected)

    def test_granted_replacement_moves_one_whole_entry_and_its_restart_baseline(self):
        self.add(worker_thread=WORKER_THREAD)
        self.add(reviewer='%22', worker_thread=WORKER_THREAD, reset=False)
        rows = self.rows()
        cap = next(row for row in rows if row.get('cap'))
        cap.update(limit=5, grant_rounds=3)
        self.set_counts(cap, '%12', rounds=3, stale_rounds=1)
        self.set_counts(cap, '%22', empty_rounds=2)
        self.set_counts(cap.setdefault('restart', {}), '%12', rounds=2, stale_rounds=1)
        self.set_counts(cap['restart'], '%22', empty_rounds=2)
        self.file.write_text(json.dumps(rows))
        review_links.remove('%13')

        self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
        cap, = (row for row in self.rows() if row.get('cap'))
        self.assertEqual(cap['counts'], {f'%21:{THREAD}': {'rounds': 3, 'stale_rounds': 1},
                                         f'%22:{THREAD}': {'empty_rounds': 2}})
        self.assertEqual(cap['restart']['counts'], {f'%21:{THREAD}': {'rounds': 2, 'stale_rounds': 1},
                                                    f'%22:{THREAD}': {'empty_rounds': 2}})

        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        self.turn_source = lambda _: 'review-feedback'
        self.request_review()
        self.send.reset_mock()
        self.reply.return_value = FINDINGS
        self.observe(**{'%21': 'Working'})
        self.observe(**{'%21': 'Idle'})
        self.send.assert_called_once()
        self.assertIn('Feedback from the reviewer tab (%21)', self.send.call_args.args[1])
        self.assert_one_cap(4, reviewer='%21')
        self.assertEqual(review_links.paused(), {})
        self.alert.assert_not_called()

    def test_orphan_selection_uses_stale_rounds_and_breaks_ties_by_rounds(self):
        for first, second in (({'rounds': 1}, {'stale_rounds': 2}),
                              ({'empty_rounds': 2}, {'rounds': 1, 'empty_rounds': 1})):
            with self.subTest(first=first, second=second):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD, reset=False)
                self.add(reviewer='%22', worker_thread=WORKER_THREAD, reset=False)
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                self.set_counts(cap, '%12', **first)
                self.set_counts(cap, '%22', **second)
                self.file.write_text(json.dumps(rows))
                review_links.remove('%13')

                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                cap, = (row for row in self.rows() if row.get('cap'))
                self.assertEqual(cap['counts'], {f'%12:{THREAD}': first, f'%21:{THREAD}': second})

    def test_a_reviewer_with_a_live_sibling_takes_its_own_counts_from_before_its_thread_was_known(self):
        self.file.write_text('[]')
        self.add(worker_thread=WORKER_THREAD, reset=False)
        rows = self.rows()
        cap = next(row for row in rows if row.get('cap'))
        cap['counts'] = {'%21:': {'rounds': 2}, review_links.UNOWNED: {'rounds': 1}}
        self.file.write_text(json.dumps(rows))
        self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
        cap = next(row for row in self.rows() if row.get('cap'))
        self.assertEqual(cap['counts'], {f'%21:{THREAD}': {'rounds': 2}, review_links.UNOWNED: {'rounds': 1}})

    def ready_siblings(self, rounds, *, level=3, reverse=False):
        self.file.write_text('[]')
        sibling = dict(self.reviewer, id='%21')
        self.entries = [self.worker, self.reviewer, sibling]
        self.states.update({'%13': 'Idle', '%12': 'Idle', '%21': 'Idle'})
        self.add_parent(level)
        reviewers = ('%21', '%12') if reverse else ('%12', '%21')
        for reviewer in reviewers:
            self.add(reviewer=reviewer, worker_thread=WORKER_THREAD, seen_working=True, reset=False)
        rows = self.rows()
        cap = next(row for row in rows if row.get('cap'))
        if rounds:
            for reviewer in reviewers:
                self.set_counts(cap, reviewer, rounds=rounds)
        if level == 5:
            cap.update(limit=5, grant_rounds=3)
        self.file.write_text(json.dumps(rows))

        def findings(entry, _request_id):
            if entry['id'] == reviewers[1]:
                # A slow transcript read lets the send gap expire before the second reviewer sends.
                self.clock.return_value += review_links.SEND_GAP
            return FINDINGS

        self.review_reply = findings
        self.observe()
        self.assertTrue(all(link['awaiting'] for link in self.links()))
        self.send.reset_mock()
        self.alert.reset_mock()
        return reviewers

    def test_same_tick_findings_share_one_round_per_worker_conversation(self):
        for (level, starting_rounds), reverse in product(((5, 4), (3, 2)), (False, True)):
            with self.subTest(level=level, reverse=reverse):
                first, _ = self.ready_siblings(starting_rounds, level=level, reverse=reverse)
                for _ in range(3):
                    self.observe()
                self.send.assert_called_once()
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertIn(f'Stopped after {level} review rounds: the reviewer tab ({first})',
                              self.send.call_args.args[1])
                self.assert_mak_alert(level)
                self.assertEqual([link['rounds'] for link in self.links()], [level, starting_rounds])
                self.assertTrue(all(link['paused'] and not link['awaiting'] for link in self.links()))
                self.assert_one_cap(level, first)

    def test_sibling_approval_preserves_the_other_reply_at_the_cap(self):
        replies = {'rounds': FINDINGS, 'stale_rounds': f'APPROVED {self.NEXT}', 'empty_rounds': ''}
        for count, blocking_reply in replies.items():
            for limit in (3, 5):
                for approval_first in (False, True):
                    for same_tick in (False, True):
                        with self.subTest(count=count, limit=limit, approval_first=approval_first, same_tick=same_tick):
                            self.bind()
                            first, second = self.ready_siblings(0)
                            approver, blocker = (first, second) if approval_first else (second, first)
                            rows = self.rows()
                            cap = next(row for row in rows if row.get('cap'))
                            blocking_link = next(row for row in rows if row.get('reviewer') == blocker)
                            if count == 'empty_rounds':
                                # A completed review without an anchor can return an empty reply for an empty turn.
                                blocking_link.pop('request_id')
                                blocking_link.update(empty_turn=True, reviewer_seen_working=True)
                            if limit == 5:
                                parent = self.entries[-1]
                                grant_count = 'rounds' if count == 'stale_rounds' else count
                                self.set_counts(cap, blocker, **{grant_count: 3})
                                cap.update(paused=True, parent_stage=dict(
                                    pane=parent['id'], thread=parent['thread'], notice=FINDINGS, sent=True))
                                self.file.write_text(json.dumps(rows))
                                self.assertTrue(review_links.agent_prompt('%13', parent['id'], parent['thread']))
                                rows = self.rows()
                                cap = next(row for row in rows if row.get('cap'))
                                self.assertEqual((cap['limit'], cap['grant_rounds']), (5, 3))
                            self.set_counts(cap, blocker, **{count: (3 if count == 'stale_rounds' else limit) - 1})
                            self.file.write_text(json.dumps(rows))

                            approval = f'APPROVED {self.HASH}'
                            expected_alert = (
                                '“Fix the bug”: 3 approvals in a row could not be verified for the current commit. '
                                'Answer in that tab to restart reviews.' if count == 'stale_rounds' else
                                f'“Fix the bug”: the reviewer still found blocking problems after {limit} rounds. '
                                f'Answer in that tab to restart reviews. Open findings: “{blocking_reply}”'
                            )
                            before_second = []

                            def on_read(entry, request_id=None):
                                if entry['id'] == second:
                                    before_second.append((review_links.paused(), self.alert.call_args_list.copy()))
                                    self.clock.return_value += review_links.SEND_GAP
                                return approval if entry['id'] == approver else blocking_reply

                            self.reply.side_effect = lambda entry: on_read(entry) if entry['id'] == blocker else 'Worker'
                            self.review_reply = Mock(side_effect=on_read)
                            if not same_tick:
                                self.observe(**{second: 'Working'})
                            self.observe(**{second: 'Idle'})
                            self.review_reply = Mock()
                            self.observe()
                            self.reply.side_effect = None

                            worker_sends = [call.args[1] for call in self.send.call_args_list if call.args[0] == self.worker]
                            approved = f'Approved by the reviewer tab ({approver})'
                            stopped = f'Stopped after {limit} review rounds: the reviewer tab ({blocker})'
                            expected = [approved] if count == 'stale_rounds' else (
                                [approved, stopped] if approval_first else [stopped, approved])
                            self.assertEqual(len(worker_sends), len(expected))
                            for text, prefix in zip(worker_sends, expected):
                                self.assertIn(prefix, text.splitlines()[0])
                            self.assertTrue(any(approval in text for text in worker_sends))
                            self.assertEqual(review_links.approved_commits()['%13'][approver]['commit'], self.HASH)
                            self.assertEqual(review_links.paused(), {'%13': [first, second]})
                            cap = next(row for row in self.rows() if row.get('cap'))
                            self.assertEqual(cap['counts'][review_links._reviewer_key(blocking_link)][count],
                                             3 if count == 'stale_rounds' else limit)
                            self.alert.assert_called_once_with('Review needs you', expected_alert, '%13')
                            if not approval_first:
                                self.assertEqual(before_second, [({'%13': [first, second]}, self.alert.call_args_list)])

    def test_sibling_approval_preserves_an_already_counted_reply_below_the_cap(self):
        for stop_blocker in (False, True):
            for change in (None, 'commit', 'work_mark'):
                with self.subTest(stop_blocker=stop_blocker, change=change):
                    self.bind()
                    self.work_mark = Mock(return_value='original tree')
                    blocker, approver = self.ready_siblings(0)
                    replies = {blocker: FINDINGS}
                    self.review_reply = lambda entry, _: replies.get(entry['id'])
                    self.observe()
                    self.assert_one_cap(1)
                    if stop_blocker:
                        self.assertTrue(review_links.remove('%13', blocker))
                    if change:
                        if change == 'commit':
                            self.head = self.NEXT
                        else:
                            self.work_mark.return_value = 'updated tree'
                        rows = self.rows()
                        link = next(row for row in rows if row.get('reviewer') == approver)
                        link.update(awaiting=False, seen_working=True)
                        self.file.write_text(json.dumps(rows))
                        self.observe()  # request a review of the updated work
                    replies[approver] = f'APPROVED {self.head}'
                    self.observe()
                    self.assertIn(replies[approver], self.send.call_args.args[1])
                    if stop_blocker:
                        cap, = (row for row in self.rows() if row.get('cap'))
                        self.assertFalse(cap.get('counts'))  # the sole reviewer cleared the entry it adopted
                    else:
                        self.assert_one_cap(1, blocker)
                    self.alert.assert_not_called()

    def test_reviewer_reply_matrix_keeps_counts_with_the_reviewer(self):
        replies = {'rounds': FINDINGS, 'empty_rounds': '', 'stale_rounds': f'APPROVED {self.NEXT}',
                   'approval': f'APPROVED {self.HASH}'}
        for limit, reverse, same_tick, final in product((3, 5), (False, True), (False, True), (False, True)):
            for reply_a, reply_b in product(replies, repeat=2):
                kinds = {'%12': reply_a, '%21': reply_b}
                with self.subTest(limit=limit, reverse=reverse, same_tick=same_tick, final=final, kinds=kinds):
                    self.bind()
                    first, second = self.ready_siblings(0, level=limit, reverse=reverse)
                    rows = self.rows()
                    cap = next(row for row in rows if row.get('cap'))
                    expected = {}
                    for link in (row for row in rows if row.get('reviewer')):
                        reviewer = link['reviewer']
                        kind = kinds[reviewer]
                        start = {'rounds': 1, 'stale_rounds': 1}
                        if kind == 'empty_rounds':
                            link.pop('request_id')
                            link.update(empty_turn=True, reviewer_seen_working=True)
                            start = {'empty_rounds': 1, 'stale_rounds': 1}
                        if final and kind != 'approval':
                            start[kind] = (3 if kind == 'stale_rounds' else limit) - 1
                        self.set_counts(cap, reviewer, **start)
                        expected[reviewer] = start
                    self.file.write_text(json.dumps(rows))

                    def reply(entry, _request=None):
                        if entry['id'] == second:
                            self.clock.return_value += review_links.SEND_GAP
                        return replies[kinds[entry['id']]] if entry['id'] in kinds else 'Worker'

                    self.review_reply = reply
                    self.reply.side_effect = reply
                    if not same_tick:
                        self.observe(**{second: 'Working'})
                    self.observe(**{second: 'Idle'})
                    self.reply.side_effect = None

                    paused = False
                    sent = []
                    for reviewer in (first, second):
                        kind = kinds[reviewer]
                        own = expected[reviewer]
                        if kind == 'approval':
                            own.clear()
                            sent.append(reviewer)
                        elif kind == 'stale_rounds':
                            own[kind] += 1
                            paused |= final
                        elif not paused:
                            own[kind] += 1
                            own.pop('stale_rounds')
                            sent.append(reviewer)
                            paused |= final
                    for link in self.links():
                        self.assertEqual({k: link[k] for k in review_links.COUNTS if k in link},
                                         expected[link['reviewer']])
                        self.assertEqual(link.get('paused', False), paused)
                    worker_sends = [call.args[1].splitlines()[0] for call in self.send.call_args_list
                                    if call.args[0] == self.worker]
                    self.assertEqual(len(worker_sends), len(sent))
                    for text, reviewer in zip(worker_sends, sent):
                        self.assertIn(f'({reviewer})', text)
                    self.assertEqual(self.alert.call_count, int(paused))

    def test_sibling_findings_leave_a_run_of_mismatched_approvals_intact(self):
        for limit, empty_turn in product((3, 5), (False, True)):
            with self.subTest(limit=limit, empty_turn=empty_turn):
                self.bind()
                approver, sibling = self.ready_siblings(0, level=limit)
                if empty_turn:
                    rows = self.rows()
                    next(row for row in rows if row.get('reviewer') == sibling)['empty_turn'] = True
                    self.file.write_text(json.dumps(rows))
                self.review_reply = lambda entry, _: f'APPROVED {self.NEXT}' if entry['id'] == approver else FINDINGS
                self.observe()
                self.alert.assert_not_called()
                for _ in range(4):  # two more review requests and mismatched approvals from A
                    self.observe(**{sibling: 'Working'})
                self.assertEqual(review_links.paused(), {'%13': [approver, sibling]})
                self.assertEqual(self.links()[0].get('stale_rounds'), 3)
                self.assertEqual(self.links()[1]['empty_rounds' if empty_turn else 'rounds'], 1)
                self.alert.assert_called_once()

    def test_approval_clears_its_counts_while_a_sibling_stays_pending(self):
        for limit, count, stalled in product((3, 5), ('rounds', 'empty_rounds'), (False, True)):
            with self.subTest(limit=limit, count=count, stalled=stalled):
                self.bind()
                approver, sibling = self.ready_siblings(0, level=limit)
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                self.set_counts(cap, approver, **{count: limit - 1})
                if stalled:
                    next(row for row in rows if row.get('reviewer') == sibling)['idle_ticks'] = 29
                self.file.write_text(json.dumps(rows))
                self.review_reply = lambda entry, _: f'APPROVED {self.HASH}' if entry['id'] == approver else None
                self.observe(**{sibling: 'Working'})
                if stalled:
                    self.observe(**{sibling: 'Idle'})
                    self.assertTrue(self.links()[1].get('stalled'))
                else:
                    self.assertTrue(review_links.remove('%13', sibling))
                self.assertNotIn(count, self.links()[0])
                self.alert.reset_mock()
                if stalled:
                    self.states[sibling] = 'Working'
                self.request_review()  # worker's response to approval
                self.request_review()  # worker starts its next task
                link = self.links()[0]
                self.assertTrue(link['awaiting'])
                self.review_reply = Mock(return_value=FINDINGS)
                self.send.reset_mock()
                self.finish_review(FINDINGS)
                self.assertIn('Feedback from', self.send.call_args.args[1])
                self.assertEqual(self.links()[0]['rounds'], 1)
                self.alert.assert_not_called()

    def test_first_approval_preserves_other_reviewers_counts(self):
        for limit, reverse, count in product((3, 5), (False, True), review_links.COUNTS):
            with self.subTest(limit=limit, reverse=reverse, count=count):
                self.bind()
                self.ready_siblings(0, level=limit, reverse=reverse)
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                self.set_counts(cap, '%12', **{count: 1})
                next(row for row in rows if row.get('reviewer') == '%12')['awaiting'] = False
                self.file.write_text(json.dumps(rows))
                self.review_reply = Mock(return_value=f'APPROVED {self.HASH}')
                self.observe()
                self.assertIn('Approved by the reviewer tab (%21)', self.send.call_args.args[1])
                by_reviewer = {link['reviewer']: link for link in self.links()}
                self.assertEqual(by_reviewer['%12'].get(count), 1)
                self.assertNotIn(count, by_reviewer['%21'])
                self.alert.assert_not_called()

    def set_counts(self, cap, reviewer, **counts):
        key = review_links._reviewer_key(dict(reviewer=reviewer, reviewer_thread=THREAD))
        cap.setdefault('counts', {})[key] = counts

    def assert_one_cap(self, rounds, reviewer='%12'):
        caps = [row for row in self.rows() if row.get('cap') is True]
        self.assertEqual(len(caps), 1)
        key = review_links._reviewer_key(dict(reviewer=reviewer, reviewer_thread=THREAD))
        self.assertEqual(caps[0]['counts'][key]['rounds'], rounds)
        for row in self.rows():
            if row.get('reviewer'):
                self.assertFalse(set((*review_links.CAP_FIELDS, *review_links.COUNTS)) & row.keys())

    def test_both_siblings_count_below_the_cap(self):
        self.ready_siblings(0)
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(all(call.args[0] == self.worker and 'Feedback from' in call.args[1]
                            for call in self.send.call_args_list))
        self.assertEqual([link['rounds'] for link in self.links()], [1, 1])
        self.assert_one_cap(1)
        self.alert.assert_not_called()

    def test_refused_feedback_releases_the_round_for_a_sibling_in_the_same_tick(self):
        for failure in (DraftError('Terminal rejected the text.'), RuntimeError('Send failed.')):
            with self.subTest(failure=type(failure).__name__):
                first, second = self.ready_siblings(2)
                delivered = []

                def send(entry, text, _state):
                    if entry == self.worker and f'({first})' in text:
                        raise failure
                    delivered.append(text)

                self.send.side_effect = send
                self.observe()
                text, = delivered
                self.assertIn(f'Stopped after 3 review rounds: the reviewer tab ({second})', text)
                self.assert_one_cap(3, second)
                self.assert_mak_alert()
                self.send.side_effect = None

    def test_browser_reset_while_reading_third_reply_prevents_stale_stop(self):
        for level, pending in ((3, False), (5, False), (3, True), (5, True)):
            with self.subTest(level=level, pending_thread=pending):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD)
                self.review_reply = None
                self.entries = [self.worker, self.reviewer]
                self.add_parent(level)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                self.request_review()
                prior = self.rows()
                cap = next(row for row in prior if row.get('cap'))
                for reviewer in ('%12', '%21'):
                    self.set_counts(cap, reviewer, rounds=level - 1)
                if level == 5:
                    cap.update(limit=5, grant_rounds=3)
                self.file.write_text(json.dumps(prior))
                self.entries.append(dict(self.reviewer, id='%22'))
                self.states['%22'] = 'Idle'

                def reset_on_read(*_):
                    entries = [dict(entry, thread=None) if entry == self.worker and pending else entry
                               for entry in self.entries]
                    with server.SEND_LOCK:
                        server.agent_tabs.link_pair(
                            {'worker': '%13', 'reviewer': '%22'}, entries, self.reply, reset=True)
                    return FINDINGS

                self.review_reply = Mock(side_effect=reset_on_read)
                self.send.reset_mock()
                self.alert.reset_mock()
                with patch.object(server.agent_tabs, 'live_status', self.status):
                    self.observe()

                self.review_reply.assert_called_once()
                self.assertEqual(review_links.reviewed_by(), {'%13': ['%12', '%21', '%22']})
                self.send.assert_not_called()
                self.alert.assert_not_called()
                if not pending:
                    for link in self.links():
                        self.assertFalse({'rounds', 'paused', 'parent_stage'} & link.keys())

                self.review_reply.side_effect = None
                self.review_reply.return_value = FINDINGS
                self.observe()
                worker_sends = [call.args[1] for call in self.send.call_args_list if call.args[0] == self.worker]
                self.assertEqual(len(worker_sends), 1)
                self.assertIn('Feedback from the reviewer tab', worker_sends[0])
                self.assertEqual([link.get('rounds', 0) for link in self.links()], [1, 0, 0])
                self.alert.assert_not_called()

    def test_upgrade_pauses_with_live_progress_and_an_older_saved_cap(self):
        for level in (3, 5):
            with self.subTest(level=level):
                sibling = dict(self.reviewer, id='%21')
                self.entries = [self.worker, self.reviewer, sibling]
                self.states['%21'] = 'Idle'
                self.add_parent(level)
                now = time.time()
                # Captured on origin/main with B linked before A stopped. B then restarted, received its first
                # finding, and started this review. Use relative timestamps for the saved-cap TTL.
                live = dict(worker='%13', worker_thread=WORKER_THREAD, reviewer='%21', reviewer_thread=THREAD,
                            name='Fix the bug', agent='codex', created=now - 30, seen_working=False, work_mark=None,
                            awaiting=True, rounds=1, request_id='55c16497', reviewer_seen_working=False,
                            reviewer_before='9743695e94520be194c308560d5ae462dece7df744e296c805d2d2cc7162b824')
                old = dict(worker='%13', worker_thread=WORKER_THREAD, saved_at=now - 10, rounds=level, paused=True)
                if level == 5:
                    live.update(review_prompts=[['review-summary', now - 25]], agent_prompt=now - 20)
                    old.update(limit=5, grant_rounds=3, review_prompts=live['review_prompts'], agent_prompt=now - 20)
                self.file.write_text(json.dumps([live, old]))
                self.review_reply = Mock(return_value=FINDINGS)
                self.send.reset_mock()
                self.alert.reset_mock()
                self.observe()
                self.send.assert_not_called()
                self.alert.assert_called_once()
                self.assertEqual(self.upgrade_cap()['counts'],
                                 {f'%21:{THREAD}': {'rounds': 1}, review_links.UNOWNED: {'rounds': level}})

    def test_upgrade_keeps_a_saved_only_cap_on_relink(self):
        saved = dict(worker='%13', worker_thread=WORKER_THREAD, saved_at=time.time(), rounds=3, paused=True)
        self.file.write_text(json.dumps([saved]))
        for reviewer in ('%12', '%21'):
            with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
                self.add(reviewer=reviewer, worker_thread=WORKER_THREAD, reset=False)
        self.assertEqual(self.links(), [saved])
        with patch.object(review_links, '_write', wraps=review_links._write) as write:
            self.assertEqual(review_links.reviewed_by(), {})
            write.assert_not_called()
        self.add(worker_thread=WORKER_THREAD, reset=True)
        self.assertNotIn('rounds', self.links()[0])

    def legacy_pair(self, first, second):
        common = dict(worker='%13', worker_thread=WORKER_THREAD, reviewer_thread=THREAD,
                      name='Old tab name', agent='codex', created=time.time())
        return [dict(common, reviewer=pane, **fields)
                for pane, fields in (('%12', first), ('%21', second))]

    def upgrade_cap(self):
        cap, = (row for row in self.rows() if row.get('cap'))
        self.assertEqual(cap['paused'], 'upgrade')
        return cap

    def test_upgrade_pauses_conflicting_unreadable_notices_and_grants_once(self):
        for first, second in (
                (dict(rounds=1, paused=True, unreadable=True), dict(rounds=2)),
                (dict(rounds=3, paused=True, parent_stage=dict(pane='%11', thread=PARENT_THREAD, notice=FINDINGS)),
                 dict(rounds=2, empty_rounds=2)),
                (dict(rounds=3, limit=5, grant_rounds=3), dict(rounds=2, empty_rounds=2))):
            with self.subTest(first=first, second=second):
                rows = self.legacy_pair(first, second)
                rows[0].update(awaiting=True, request_id='12345678')
                rows[1]['seen_working'] = True
                worker = dict(self.worker, pane_name='Visible worker name', label='Secondary label')
                parent = dict(id='%11', thread=PARENT_THREAD, agent='codex')
                self.entries = [worker, self.reviewer, dict(self.reviewer, id='%21'), parent]
                self.states.update({'%21': 'Idle', '%11': 'Idle'})
                self.review_reply = Mock(return_value='APPROVED')
                self.turn_source = lambda _: 'background'
                self.file.write_text(json.dumps(rows))
                self.alert.reset_mock()
                self.send.reset_mock()

                for _ in range(10):
                    self.observe()
                self.send.assert_not_called()
                self.review_reply.assert_not_called()
                self.alert.assert_called_once()
                title, body, pane = self.alert.call_args.args
                self.assertEqual((title, pane), ('Review needs you', '%13'))
                self.assertIn('Visible worker name', body)
                self.assertIn('upgrade', body)
                self.assertIn('browser', body)
                cap = self.upgrade_cap()
                self.assertFalse((set(review_links.CAP_FIELDS) - {'counts', 'paused'}) & cap.keys())
                self.assertFalse(review_links.in_parent_stage('%13', WORKER_THREAD))
                with self.assertRaisesRegex(ValueError, 'no review waiting'):
                    review_links.escalate('%13', '%11', PARENT_THREAD, worker_thread=WORKER_THREAD)
                review_links.agent_prompt('%13', '%11', PARENT_THREAD, worker_thread=WORKER_THREAD)
                self.assertNotIn('limit', self.upgrade_cap())

                spec = importlib.util.spec_from_file_location('restarted_review_links', review_links.__file__)
                restarted = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(restarted)
                restarted.FILE = self.file
                restarted.observe(self.entries, self.status, self.send, self.reply, alert=self.alert,
                                  turn_source=self.turn_source, review_reply=self.review_reply)
                self.alert.assert_called_once()
                self.send.assert_not_called()

                self.assertTrue(review_links.remove('%13', '%12'))
                with server.SEND_LOCK, patch.object(server.agent_tabs, 'live_status', self.status):
                    server.agent_tabs.link_pair({'worker': '%13', 'reviewer': '%12'}, self.entries,
                                               self.reply, reset=True)
                cap, = (row for row in self.rows() if row.get('cap'))
                self.assertFalse({'paused', 'counts', 'upgrade_alerted'} & cap.keys())

    def test_upgrade_measures_each_reviewer_against_its_own_restart_baseline(self):
        for field, limit, reverse in product(('rounds', 'empty_rounds'), (3, 5), (False, True)):
            with self.subTest(field=field, limit=limit, reverse=reverse):
                grant = {'limit': 5, 'grant_rounds': 3} if limit == 5 else {}
                rows = self.legacy_pair(
                    {field: limit - 2, 'restart': {field: limit - 2}, **grant},
                    {field: limit, 'restart': {field: limit - 1}, **grant},
                )
                if reverse:
                    rows.reverse()
                self.file.write_text(json.dumps(rows))
                self.turn_source = lambda _: 'background'
                self.alert.reset_mock()
                self.observe()
                self.assertEqual(self.upgrade_cap()['counts'], {f'%21:{THREAD}': {field: 1}})
                self.alert.assert_called_once()

    def test_upgrade_pause_clears_after_a_new_mak_turn(self):
        self.file.write_text(json.dumps(self.legacy_pair(dict(rounds=1), dict(empty_rounds=2))))
        self.turn_source = lambda _: 'background'
        self.observe()
        self.upgrade_cap()
        self.request_review()
        self.upgrade_cap()
        self.send.assert_not_called()

        upgrade_at = self.upgrade_cap()['upgrade_at']
        for source in (('user', upgrade_at - 1, upgrade_at + 1), ('unknown', None, None)):
            self.turn_source = lambda _: source
            self.request_review()
            self.upgrade_cap()
            self.send.assert_not_called()

        self.turn_source = lambda _: 'user'
        self.request_review()
        self.send.assert_called_once()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(review_links.paused(), {})
        cap, = (row for row in self.rows() if row.get('cap'))
        self.assertFalse({'upgrade_alerted', 'upgrade_at'} & cap.keys())
        self.assertFalse(cap.get('counts'))
        self.alert.assert_called_once()

    def test_binding_merges_compatible_counts_or_pauses_on_conflict(self):
        for conflict, reverse, same_reviewer in product((False, True), repeat=3):
            with self.subTest(conflict=conflict, reverse=reverse, same_reviewer=same_reviewer):
                self.entries = [self.worker, self.reviewer, dict(self.reviewer, id='%21')]
                self.states['%21'] = 'Idle'
                self.turn_source = lambda _: 'background'
                rows = self.legacy_pair(dict(rounds=1), dict(rounds=1))
                rows[1].pop('worker_thread')
                if same_reviewer:
                    rows[1].update(reviewer='%12', rounds=2, empty_rounds=1)
                if conflict:
                    rows[1].update(paused=True, limit=5, grant_rounds=3)
                if reverse:
                    rows.reverse()
                self.file.write_text(json.dumps(rows))
                self.alert.reset_mock()
                self.send.reset_mock()

                self.observe()
                self.observe()
                cap, = (row for row in self.rows() if row.get('cap'))
                self.assertEqual(cap['worker_thread'], WORKER_THREAD)
                expected = ({f'%12:{THREAD}': {'rounds': 2, 'empty_rounds': 1}} if same_reviewer else
                            {f'%12:{THREAD}': {'rounds': 1}, f'%21:{THREAD}': {'rounds': 1}})
                self.assertEqual(cap['counts'], expected)
                self.assertEqual(cap.get('paused'), 'upgrade' if conflict else None)
                self.assertNotIn('limit', cap)
                self.assertEqual(self.alert.call_count, int(conflict))
                self.send.assert_not_called()

    def test_binding_new_unbound_reviewer_keeps_bound_decision_and_epoch(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        for grant in ({}, dict(limit=5, grant_rounds=3)):
            with self.subTest(grant=grant):
                self.file.write_text('[]')
                review_links._last_sent.clear()
                self.send.reset_mock()
                self.add(worker_thread=WORKER_THREAD)
                self.request_review()
                self.send.assert_called_once()
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                cap.update(review_turn_at=self.clock.return_value, **grant)
                self.assertFalse(cap.get('counts'))
                self.file.write_text(json.dumps(rows))
                self.add(reviewer='%21', worker_thread=None, reset=False)
                self.observe(entries=[dict(self.worker, thread=None), *self.entries[1:]])
                self.assertEqual(review_links.linked_worker_threads('%13', '%21', THREAD), {None})
                self.send.reset_mock()
                self.alert.reset_mock()

                self.observe()
                bound, = (row for row in self.rows() if row.get('cap'))
                self.assertEqual(bound, cap)
                self.assertEqual(review_links.linked_worker_threads('%13', '%21', THREAD), {WORKER_THREAD})
                self.assertEqual(review_links.paused(), {})
                self.send.assert_not_called()
                self.alert.assert_not_called()

    def test_binding_merges_turn_tracking_without_a_decision_conflict(self):
        self.enterContext(own_time(review_links, time=self.clock))
        feedback = dict(sent_at=996, phase='queued')
        first_baseline = dict(counts={f'%12:{THREAD}': {'rounds': 2}}, paused=True)
        second_baseline = dict(counts={f'%21:{THREAD}': {'empty_rounds': 1}}, agent=True)
        for baselines in ((first_baseline, second_baseline), (first_baseline, None), (None, second_baseline)):
            with self.subTest(baselines=baselines):
                self.clock.return_value = 1000
                first = dict(cap=True, worker='%13', worker_thread=WORKER_THREAD, review_turn_at=997,
                             review_prompts=[['review-summary', 995], 'review-notice', ['review-feedback', 994]],
                             agent_prompt=995, feedback_turn=feedback, cap_reset='bound')
                second = dict(cap=True, worker='%13', worker_thread=None, review_turn_at=999,
                              review_prompts=['review-notice', ['review-summary', 995], ['review-notice', 993]],
                              agent_prompt=998, feedback_turn=dict(sent_at=999, phase='pending'), cap_reset='unbound')
                for cap, baseline in zip((first, second), baselines):
                    if baseline is not None:
                        cap['restart'] = baseline
                links = self.legacy_pair({}, {})
                links[1]['worker_thread'] = None
                self.file.write_text(json.dumps([*links, first, second]))
                self.states['%21'] = 'Idle'
                self.observe(entries=[*self.entries, dict(self.reviewer, id='%21')], **{'%13': 'Starting'})

                bound, = (row for row in self.rows() if row.get('cap'))
                expected = baselines[0] or baselines[1]
                if all(baselines):
                    expected = dict(counts={**first_baseline['counts'], **second_baseline['counts']},
                                    paused=True, agent=True)
                self.assertEqual(bound['restart'], expected)
                self.assertEqual(bound['review_prompts'], [
                    'review-notice', ['review-notice', 993], ['review-feedback', 994], ['review-summary', 995]])
                self.assertEqual((bound['review_turn_at'], bound['agent_prompt']), (999, 998))
                self.assertEqual((bound['feedback_turn'], bound['cap_reset']), (feedback, 'bound'))
                self.assertEqual(review_links.paused(), {})
                self.alert.assert_not_called()

    def test_binding_conflict_keeps_only_counts_since_each_restart(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        prompts = [['review-feedback', 1001]]
        feedback = dict(sent_at=1001, phase='working')
        for field, reverse in (('rounds', False), ('empty_rounds', True)):
            with self.subTest(field=field, reverse=reverse):
                self.clock.return_value = 1000
                # The explicit unbound pause is a decision; the restart baseline is turn tracking.
                rows = self.legacy_pair(
                    {field: 3, 'restart': {field: 2}, 'limit': 5, 'grant_rounds': 3, 'agent_prompt': 998,
                     'review_prompts': prompts, 'feedback_turn': feedback, 'review_turn_at': 997},
                    {field: 2, 'restart': {field: 2}, 'paused': False, 'agent_prompt': 999},
                )
                rows[1]['worker_thread'] = None
                self.file.write_text(json.dumps(rows[::-1] if reverse else rows))
                self.alert.reset_mock()
                self.observe(elapsed=0, **{'%13': 'Working'})
                self.turn_source = Mock(return_value=('user', 995, 1005))
                self.observe(**{'%13': 'Idle'})

                cap = self.upgrade_cap()
                self.assertEqual(cap['counts'], {f'%12:{THREAD}': {field: 1}})
                self.assertEqual(cap['agent_prompt'], 999)
                self.assertEqual(cap['review_prompts'], prompts)
                self.assertEqual(cap['feedback_turn'], feedback)
                self.assertGreaterEqual(cap['review_turn_at'], 997)
                self.assertFalse({'limit', 'grant_rounds', 'restart'} & cap.keys())
                self.alert.assert_called_once()
                self.send.assert_not_called()

    def test_legacy_saved_counts_follow_a_sole_reviewer_across_relinks(self):
        for named in (False, True):
            with self.subTest(named=named):
                saved = dict(worker='%13', worker_thread=WORKER_THREAD, saved_at=time.time(),
                             rounds=1, empty_rounds=1)
                if named:
                    saved.update(reviewer='%12', reviewer_thread=THREAD)
                self.file.write_text(json.dumps([saved]))
                owner = '%12' if named else '%21'
                for pane, thread in (('%21', THREAD), ('%12', THREAD), (owner, PARENT_THREAD), (owner, THREAD)):
                    self.add(reviewer=pane, reviewer_thread=thread, worker_thread=WORKER_THREAD, reset=False)
                    link, = self.links()
                    self.assertEqual((link.get('rounds', 0), link.get('empty_rounds', 0)), (1, 1))
                    self.assertTrue(review_links.remove('%13', pane))

    def test_upgrade_keeps_both_counts_without_the_highest_totals_grant(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                live = dict(worker='%13', worker_thread=WORKER_THREAD, reviewer='%12', reviewer_thread=THREAD,
                            name='Fix the bug',
                            rounds=2, empty_rounds=1, stale_rounds=1, limit=5, grant_rounds=3)
                sibling = dict(live, reviewer='%21', rounds=1, empty_rounds=2, stale_rounds=2)
                sibling.pop('limit')
                sibling.pop('grant_rounds')
                self.file.write_text(json.dumps([sibling, live] if reverse else [live, sibling]))
                review_links._write(review_links._read())
                self.assertEqual({link['reviewer']: tuple(link[field] for field in review_links.COUNTS)
                                  for link in self.links()}, {'%12': (2, 1, 1), '%21': (1, 2, 2)})
                self.assertNotIn('grant_rounds', self.upgrade_cap())

    def test_upgrade_copies_shared_fields_only_from_a_single_legacy_row(self):
        now = time.time()
        values = {
            'limit': (3, 5), 'grant_rounds': (2, 3), 'paused': (True, False), 'unreadable': (True, False),
            'parent_stage': (
                dict(pane='%11', notice='First notice', thread=PARENT_THREAD, sent=True),
                dict(pane='%22', notice='Second notice', thread=THREAD, sent=False),
            ),
            'parent_missing': (1, 2), 'escalate': ('first escalation', 'second escalation'),
            'cap_reset': ('first reset', 'second reset'),
            'restart': (dict(paused=True, agent=True), dict(paused=False)),
            'review_prompts': ([['review-summary', now - 10]], [['review-notice', now - 5]]),
            'review_turn_at': (now - 8, now - 3), 'agent_prompt': (now - 11, now - 6),
            'parent_send_at': (now - 12, now - 7),
            'feedback_turn': (dict(sent_at=now - 2, phase='pending'), dict(sent_at=now - 1, phase='queued')),
        }
        fields = tuple(field for field in review_links.CAP_FIELDS if field != 'counts')
        link = dict(worker='%13', worker_thread=WORKER_THREAD, name='Fix the bug')

        # Exercise all fields together, then omit each field in turn from each source.
        for first_missing, second_missing in [(None, None)] + [(field, None) for field in fields] + [
                (None, field) for field in fields]:
            first = {field: values[field][0] for field in fields if field != first_missing}
            second = {field: values[field][1] for field in fields if field != second_missing}
            for first_kind, second_kind in (
                    (dict(reviewer='%12'), dict(reviewer='%21')),
                    (dict(reviewer='%12'), dict(saved_at=now - 1)),
                    (dict(saved_at=now - 2), dict(saved_at=now - 1))):
                with self.subTest(first_missing=first_missing, second_missing=second_missing,
                                  first_kind=first_kind, second_kind=second_kind):
                    rows = [dict(link, rounds=3, **first_kind, **first), dict(link, rounds=2, **second_kind, **second)]
                    for row, expected in zip(rows, (first, second)):
                        self.file.write_text(json.dumps([row]))
                        cap, = (row for row in review_links._read() if row.get('cap'))
                        shared = {field: cap[field] for field in fields if field in cap}
                        if 'restart' in shared:
                            shared['restart'] = {key: value for key, value in shared['restart'].items()
                                                 if key != 'counts'}
                        self.assertEqual(shared, expected)
                    for order in (rows, rows[::-1]):
                        self.file.write_text(json.dumps(order))
                        cap, = (row for row in review_links._read() if row.get('cap'))
                        shared = {field: cap[field] for field in fields if field in cap}
                        expected = {'paused': 'upgrade', **{field: max(row[field] for row in order if field in row)
                                                            for field in ('agent_prompt', 'parent_send_at')}}
                        if any('saved_at' not in row and row.get('restart', {}).get('agent') for row in order):
                            # A live row's agent flag is the only evidence of a parent turn whose marker was consumed.
                            expected['restart'] = {'paused': True, 'agent': True}
                            shared['restart'] = {key: value for key, value in shared['restart'].items()
                                                 if key != 'counts'}
                        self.assertEqual(shared, expected)

    def test_legacy_store_queries_leave_the_file_for_the_next_write(self):
        link = dict(self.rows()[0], worker_thread=WORKER_THREAD, paused=True,
                    parent_stage=dict(pane='%21', thread=THREAD, notice=FINDINGS, sent=True),
                    approved_commit='a' * 40)
        original = json.dumps([link], indent=2).encode()
        self.file.write_bytes(original)
        before = self.file.stat().st_mtime_ns
        backup = self.file.with_name('review-links.json.pre-b278')
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})
        self.assertEqual(review_links.paused(), {'%13': ['%12']})
        self.assertTrue(review_links.in_parent_stage('%13', WORKER_THREAD))
        self.assertEqual(review_links.approved_commits()['%13']['%12']['commit'], 'a' * 40)
        with (patch.object(server, 'sessions', return_value=self.entries),
              patch.object(server, 'session_rows', side_effect=lambda entries: [dict(entry) for entry in entries]),
              patch.object(server, 'settings', return_value={'thread': None}),
              patch.object(server.groups, 'all_groups', return_value={}),
              patch.object(server.auto_resume, 'notice', return_value={'failed': False})):
            worker, _ = server.build_listing()['sessions']
        self.assertEqual(worker['reviewers'], ['%12'])
        self.assertEqual(worker['reviewers_paused'], ['%12'])
        self.assertEqual(self.file.stat().st_mtime_ns, before)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertFalse(backup.exists())
        self.add(worker_thread=WORKER_THREAD, reviewer='%22')
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(any(row.get('cap') for row in self.rows()))

        # Even if the old server writes another legacy file, keep the first backup.
        self.file.write_text(json.dumps([dict(link, rounds=1)]))
        self.assertTrue(review_links.remove('%13', '%12'))
        self.assertEqual(backup.read_bytes(), original)

    def test_single_row_upgrade_keeps_restart_for_either_legacy_reviewer(self):
        for reviewer in ('%12', '%21'):
            with self.subTest(reviewer=reviewer):
                self.states.update({'%13': 'Idle', '%12': 'Idle', '%21': 'Idle'})
                row = dict(worker='%13', worker_thread=WORKER_THREAD, reviewer=reviewer, reviewer_thread=THREAD,
                           name='Fix the bug', agent='codex', seen_working=True, created=time.time(), rounds=3,
                           paused=True, restart={'rounds': 3, 'paused': True})
                self.file.write_text(json.dumps([row]))
                entries = [self.worker, dict(self.reviewer, id=reviewer)]
                self.send.reset_mock()
                self.observe(entries=entries)
                self.send.assert_called_once()
                self.assertEqual(review_links.paused(), {})
                self.assertTrue(self.links()[0]['awaiting'])
                self.review_reply = Mock(return_value=FINDINGS)
                self.observe(entries=entries)
                self.assert_one_cap(1, reviewer)
                self.review_reply = None

    def test_upgrade_keeps_counts_without_accepting_a_legacy_restart_as_shared(self):
        for limit in (3, 5):
            for reverse in (False, True):
                for source, progress in (('user', 0), ('user', 1), ('background', 0), (None, 0)):
                    with self.subTest(limit=limit, reverse=reverse, source=source, progress=progress):
                        # The first reviewer reached the old cap, then disappeared from discovery. The second
                        # reviewer still had the old count and was the only one to see Mak start a new task.
                        first = dict(worker='%13', worker_thread=WORKER_THREAD, reviewer='%12', reviewer_thread=THREAD,
                                     name='Fix the bug', agent='codex', seen_working=False, created=time.time(),
                                     rounds=limit, paused=True)
                        if limit == 5:
                            first.update(limit=5, grant_rounds=3)
                        second = dict(first, reviewer='%21', seen_working=source is not None, rounds=limit - 1,
                                      paused=False)
                        if source is not None:
                            second['restart'] = dict(rounds=limit - 1 - progress, empty_rounds=0, stale_rounds=0,
                                                     paused=False)
                        self.file.write_text(json.dumps([second, first] if reverse else [first, second]))
                        entries = [self.worker, dict(self.reviewer, id='%21')]
                        self.states['%21'] = 'Idle'
                        self.turn_source = lambda _: source
                        self.review_reply = Mock(return_value=FINDINGS)
                        self.send.reset_mock()
                        self.alert.reset_mock()

                        self.observe(entries=entries)
                        self.observe(entries=entries)
                        expected = {f'%12:{THREAD}': {'rounds': limit}}
                        remaining = limit - 1 if source is None else progress
                        if remaining:
                            expected[f'%21:{THREAD}'] = {'rounds': remaining}
                        self.assertEqual(self.upgrade_cap()['counts'], expected)
                        self.send.assert_not_called()
                        self.alert.assert_called_once()

    def test_upgrade_keeps_a_queued_parent_prompt_from_legacy_reviewers(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        ttl = review_links.AGENT_PROMPT_TTL
        for case in ('browser relink', 'TTL elapsed', 'earlier turn first', 'parent finishes after TTL'):
            with self.subTest(case=case):
                self.clock.return_value = 1000
                first = dict(rounds=3, paused=True, agent_prompt=999)
                second = dict(empty_rounds=3, paused=True, agent_prompt=999)
                if case == 'TTL elapsed':
                    second.pop('agent_prompt')  # One reviewer already consumed its copy.
                    second.update(restart=dict(empty_rounds=2, paused=True), seen_working=True)
                self.file.write_text(json.dumps(self.legacy_pair(first, second)))
                self.send.reset_mock()
                self.alert.reset_mock()
                self.turn_source = Mock(return_value=('background', None, None))
                self.observe(elapsed=0, **{'%13': 'Working'})
                if case == 'earlier turn first':
                    self.turn_source.return_value = ('user', 995, 1001)
                    self.observe(elapsed=2, **{'%13': 'Idle'})
                    self.assertEqual(self.upgrade_cap().get('agent_prompt'), 999)
                    self.send.assert_not_called()
                    self.observe(elapsed=1, **{'%13': 'Working'})
                self.turn_source.return_value = ('user', 1004, 1005)
                elapsed = 6
                if case == 'parent finishes after TTL':
                    self.turn_source.return_value = ('user', 1004, 999 + ttl + 1)
                    elapsed = ttl + 2
                self.observe(elapsed=elapsed, **{'%13': 'Idle'})
                self.observe()
                self.send.assert_not_called()
                self.assertEqual(self.upgrade_cap()['agent_prompt'], 999)
                self.alert.assert_called_once()

                if case == 'browser relink':
                    self.add(worker_thread=WORKER_THREAD)
                    self.assertEqual(review_links.paused(), {})
                elif case == 'TTL elapsed':
                    self.turn_source.return_value = ('user', 999 + ttl + 1, 999 + ttl + 2)
                    self.observe(elapsed=ttl)
                    self.assertEqual(review_links.paused(), {})

    def test_upgrade_judges_an_untimed_turn_against_the_kept_parent_marker(self):
        # Antigravity turns have no prompt time. A watched turn inside the marker window stays an agent turn;
        # a watched Mak turn after the window clears the pause.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        ttl = review_links.AGENT_PROMPT_TTL
        for label, start in (('inside the window', 1002), ('after the window', 999 + ttl + 1)):
            with self.subTest(label=label):
                self.clock.return_value = 1000
                self.file.write_text(json.dumps(self.legacy_pair(dict(rounds=3, paused=True, agent_prompt=999),
                                                                 dict(empty_rounds=3, paused=True, agent_prompt=999))))
                self.send.reset_mock()
                self.turn_source = Mock(return_value=('background', None, None))
                self.observe(elapsed=0)
                self.upgrade_cap()
                self.turn_source.return_value = 'user'
                self.observe(elapsed=start - 1000, **{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.observe()
                if start > 999 + ttl:
                    self.assertEqual(review_links.paused(), {})
                    self.assertFalse(self.rows()[-1].get('counts'))
                else:
                    self.assertEqual(self.upgrade_cap()['agent_prompt'], 999)
                    self.send.assert_not_called()

    def test_upgrade_reads_a_parent_marker_recorded_during_the_tick(self):
        # The parent records its marker before it sends the prompt. A turn readable in this tick must see it.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        for observed in (False, True):
            with self.subTest(observed=observed):
                self.clock.return_value = 1000
                self.file.write_text(json.dumps(self.legacy_pair(dict(rounds=1), dict(empty_rounds=2))))
                self.send.reset_mock()
                self.turn_source = Mock(return_value=('background', None, None))
                self.observe(elapsed=0)
                self.upgrade_cap()
                if observed:
                    self.observe(**{'%13': 'Working'})

                def parent_turn(worker):
                    review_links.agent_prompt('%13', worker_thread=WORKER_THREAD)
                    now = review_links.time.time()
                    return ('user', now + 0.1, now + 0.9)
                self.turn_source = Mock(side_effect=parent_turn)
                self.observe(**{'%13': 'Idle'})
                self.observe()
                self.assertIn('agent_prompt', self.upgrade_cap())
                self.send.assert_not_called()

    def test_upgrade_judges_a_timed_turn_by_its_own_start_not_an_earlier_agent_flag(self):
        # A parent turn seen Working inside the window can end before a Mak turn that starts after it.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        ttl = review_links.AGENT_PROMPT_TTL
        for label, start in (('Mak after the window', 999 + ttl + 0.1), ('parent inside the window', 999 + ttl - 0.4)):
            with self.subTest(label=label):
                self.clock.return_value = 1000
                self.file.write_text(json.dumps(self.legacy_pair(dict(rounds=3, paused=True, agent_prompt=999),
                                                                 dict(empty_rounds=3, paused=True, agent_prompt=999))))
                self.send.reset_mock()
                self.turn_source = Mock(return_value=('background', None, None))
                self.observe(elapsed=0)
                self.observe(elapsed=ttl - 1.5, **{'%13': 'Working'})
                self.turn_source.return_value = ('user', start, 999 + ttl + 0.9)
                self.observe(elapsed=2, **{'%13': 'Idle'})
                self.observe()
                if label == 'Mak after the window':
                    self.assertEqual(review_links.paused(), {})
                else:
                    self.assertEqual(self.upgrade_cap()['agent_prompt'], 999)
                    self.send.assert_not_called()

    def test_upgrade_keeps_a_live_agent_flag_when_the_marker_was_consumed(self):
        # Old watchers popped the parent's marker and kept only restart.agent while its turn ran.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        for path in ('migration', 'conflicting bind'):
            with self.subTest(path=path):
                self.clock.return_value = 1000
                rows = self.legacy_pair(dict(rounds=3, restart=dict(rounds=1, agent=True)),
                                        dict(empty_rounds=2, restart=dict(empty_rounds=1, agent=True)))
                if path == 'conflicting bind':
                    rows[0].update(limit=5, grant_rounds=3)
                    rows[1].update(worker_thread=None, paused=False)
                self.file.write_text(json.dumps(rows))
                self.send.reset_mock()
                self.turn_source = Mock(return_value=('user', None, 1001))
                self.observe(elapsed=0, **{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.observe()
                self.upgrade_cap()
                self.send.assert_not_called()

    def test_upgrade_alerts_once_for_bound_and_unbound_caps_of_one_conversation(self):
        self.enterContext(own_time(review_links, time=self.clock))
        late = [dict(self.reviewer, id=pane) for pane in ('%22', '%23')]
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states.update({'%21': 'Idle', '%22': 'Idle', '%23': 'Idle'})
        self.turn_source = Mock(return_value=('background', None, None))
        hidden = [dict(self.worker, thread=None), *self.entries[1:]]
        found = [*self.entries, *late]
        # Each case lists the discovery result of each poll. A one-poll gap must not send a second alert.
        for reverse, gap, polls in ((False, 'none', (None, found, found)), (True, 'none', (None, found, found)),
                                    (False, 'first poll', (hidden, found, found)),
                                    (False, 'after the bound alert', (None, hidden, found, found))):
            with self.subTest(reverse=reverse, discovery_gap=gap):
                self.clock.return_value = 1000
                rows = self.legacy_pair(dict(rounds=1), dict(empty_rounds=2))
                rows += [dict(row, reviewer=pane, worker_thread=None)
                         for row, pane in zip(self.legacy_pair(dict(rounds=1), dict(empty_rounds=1)), ('%22', '%23'))]
                self.file.write_text(json.dumps(rows[::-1] if reverse else rows))
                self.alert.reset_mock()
                self.observe(elapsed=0, entries=polls[0])
                for entries in polls[1:]:
                    self.observe(entries=entries)
                self.alert.assert_called_once()
                self.assertTrue(self.upgrade_cap()['upgrade_alerted'])

    def test_parent_send_stamp_marks_live_caps_and_survives_a_bind(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.clock.return_value = 1000
        rows = [dict(row, worker_thread=WORKER_THREAD) for row in self.rows() if not row.get('cap')]
        rows += [dict(cap=True, worker='%13', worker_thread=WORKER_THREAD, name='Live', parent_send_at=900),
                 dict(cap=True, worker='%13', worker_thread=None, name='Saved', saved_at=990)]
        self.file.write_text(json.dumps(rows))
        review_links.mark_parent_send('%13', worker_thread=WORKER_THREAD)
        caps = {row['name']: row for row in self.rows() if row.get('cap')}
        self.assertEqual(caps['Live']['parent_send_at'], 1000)
        self.assertNotIn('parent_send_at', caps['Saved'])
        # The newest stamp and the newest parent marker survive a bind, whichever side holds them.
        for bound, unbound in ((dict(parent_send_at=1001), dict(agent_prompt=999)),
                               (dict(agent_prompt=999), dict(parent_send_at=1001))):
            for conflict in (False, True):
                with self.subTest(bound=bound, unbound=unbound, conflict=conflict):
                    rows = [dict(cap=True, worker='%13', worker_thread=WORKER_THREAD, name='Bound', **bound),
                            dict(cap=True, worker='%13', worker_thread=None, name='Unbound', **unbound,
                                 **(dict(limit=5) if conflict else {}))]
                    review_links._bind_worker(rows, '%13', WORKER_THREAD)
                    cap, = rows
                    self.assertEqual((cap.get('agent_prompt'), cap.get('parent_send_at')), (999, 1001))

    def test_upgrade_holds_for_a_parent_turn_after_a_failed_send(self):
        # The send failed after the stamp, or the parent's turn started before agent_prompt() ran.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        self.clock.return_value = 1000
        self.file.write_text(json.dumps(self.legacy_pair(dict(rounds=3), dict(empty_rounds=2))))
        self.observe(elapsed=0)
        self.clock.return_value = 1001
        review_links.mark_parent_send('%13', worker_thread=WORKER_THREAD)
        for source in (('user', 1002, 1004), ('user', 1001 + review_links.AGENT_PROMPT_TTL - 1, 1700)):
            with self.subTest(source=source):
                self.turn_source = Mock(return_value=source)
                self.observe(**{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.assertEqual(self.upgrade_cap()['paused'], 'upgrade')
                self.send.assert_not_called()
        # A Mak turn that starts after the window clears the pause.
        self.turn_source = Mock(return_value=('user', 1001 + review_links.AGENT_PROMPT_TTL, 1800))
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.assertFalse([row for row in self.rows() if row.get('cap') and row.get('paused') == 'upgrade'])

    def test_upgrade_does_not_clear_on_a_replayed_turn_it_already_judged(self):
        # A transcript reader can return the last completion again after a background turn without a reply.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        for replay in (('user', 999, 1003), ('user', None, 1003)):
            with self.subTest(replay=replay):
                self.clock.return_value = 1000
                self.file.write_text(json.dumps(self.legacy_pair(
                    dict(rounds=3, restart=dict(rounds=1, agent=True)),
                    dict(empty_rounds=2, restart=dict(empty_rounds=1, agent=True)))))
                self.send.reset_mock()
                self.turn_source = Mock(return_value=('user', 999, 1003))
                self.observe(elapsed=0, **{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.assertEqual(self.upgrade_cap()['review_turn_at'], 1003)
                self.turn_source.return_value = replay
                self.observe(**{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.observe()
                self.upgrade_cap()
                self.send.assert_not_called()

    def test_upgrade_agent_flag_without_a_marker_holds_the_pause_for_one_turn(self):
        # The flag without a marker comes from an older watcher. Its turn's owner is unknown, so the pause holds
        # until the next Mak turn. A flag from a saved cap never crosses a bind.
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        for path in ('migration', 'saved bound cap', 'old watcher tracked a parent turn'):
            with self.subTest(path=path):
                self.clock.return_value = 1000
                self.send.reset_mock()
                rows = self.legacy_pair(dict(rounds=3, restart=dict(rounds=1, agent=True)),
                                        dict(empty_rounds=2, restart=dict(empty_rounds=1, agent=True)))
                if path == 'saved bound cap':
                    rows[0].update(limit=5, grant_rounds=3, saved_at=999)
                    del rows[0]['reviewer']
                    rows[1].update(worker_thread=None, paused=False, restart=dict(empty_rounds=1))
                self.file.write_text(json.dumps(rows))
                paused = lambda: [row for row in self.rows() if row.get('cap') and row.get('paused') == 'upgrade']
                if path == 'old watcher tracked a parent turn':
                    # The previous watcher paused at 1000, saw the parent turn Working and consumed the marker.
                    self.observe(elapsed=0)
                    rows = self.rows()
                    cap, = (row for row in rows if row.get('cap'))
                    cap['restart'] = dict(review_links._restart_state(cap), agent=True)
                    cap.pop('agent_prompt', None)
                    self.file.write_text(json.dumps(rows))
                    self.turn_source = Mock(return_value=('user', 1002, 1005))
                    self.observe(**{'%13': 'Idle'})
                else:
                    self.turn_source = Mock(return_value=('user', 1001, 1002))
                    self.observe(elapsed=0, **{'%13': 'Working'})
                    self.observe(**{'%13': 'Idle'})
                if path == 'saved bound cap':
                    self.assertFalse(paused())
                    continue
                self.assertTrue(paused())
                self.assertFalse(self.send.called)
                self.turn_source = Mock(return_value=('user', 1006, 1008))
                self.observe(**{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.assertFalse(paused())

    def test_upgrade_alert_for_an_unbound_cap_waits_only_for_the_current_conversation(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.turn_source = Mock(return_value=('background', None, None))
        for alerted, calls, gap, old_fails in ((True, 1, False, False), (False, 2, False, False),
                                               (True, 1, True, False), (False, 2, True, False), (False, 4, True, True)):
            with self.subTest(old_cap_alerted=alerted, discovery_gap=gap, old_alert_fails=old_fails):
                self.clock.return_value = 1000
                old = dict(cap=True, worker='%13', worker_thread='old-thread', name='Old', paused='upgrade',
                           upgrade_at=990, saved_at=990, upgrade_alerted=alerted)
                rows = [dict(row, reviewer=pane, worker_thread=None)
                        for row, pane in zip(self.legacy_pair(dict(rounds=1), dict(empty_rounds=1)), ('%22', '%23'))]
                self.file.write_text(json.dumps([old, *rows]))
                self.alert.reset_mock()
                entries = [dict(self.worker, thread=None), *self.entries[1:]] if gap else None
                # The old conversation's alert can keep failing. The unbound alert must not wait on it.
                def push(title, text, *args, **kwargs):
                    if old_fails and '“Old”' in text:
                        raise RuntimeError('push failed')
                self.alert.side_effect = push
                self.observe(elapsed=0, entries=entries)
                self.observe(entries=entries)
                if old_fails:
                    self.observe(entries=entries)
                self.assertEqual(self.alert.call_count, calls)
                unbound, = (row for row in self.rows() if row.get('cap') and row['worker_thread'] is None)
                self.assertTrue(unbound['upgrade_alerted'])

    def test_upgrade_pause_clears_for_a_timed_mak_turn_between_idle_ticks(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        for label, source, marker in (
                ('new Mak turn', ('user', 1002, 1004), {}),
                ('starts at upgrade', ('user', 1000, 1004), {}),
                ('old Mak turn', ('user', 999, 1004), {}),
                ('queued parent', ('user', 1002, 1004), dict(agent_prompt=999)),
                ('queued parent finishes after TTL', ('user', 1002, 1604), dict(agent_prompt=999)),
                ('already seen', ('user', 1002, 1004), dict(review_turn_at=1004)),
                ('background', ('background', 1002, 1004), {}),
                ('clipped', ('unknown', 1002, 1004), {}),
                ('untimed', 'user', {}),
                ('missing finish', ('user', 1002, None), {}),
        ):
            with self.subTest(label=label):
                self.clock.return_value = 1000
                rows = self.legacy_pair(dict(rounds=1), dict(empty_rounds=2))
                self.file.write_text(json.dumps(rows))
                self.turn_source = Mock(return_value=('background', None, None))
                self.alert.reset_mock()
                self.observe(elapsed=0)
                self.upgrade_cap()
                rows = self.rows()
                next(row for row in rows if row.get('cap')).update(marker)
                self.file.write_text(json.dumps(rows))
                self.turn_source.reset_mock()
                self.turn_source.return_value = source
                elapsed = 606 if label == 'queued parent finishes after TTL' else 6
                self.observe(elapsed=elapsed)
                self.turn_source.assert_called_once_with(self.worker)
                if label in ('new Mak turn', 'starts at upgrade'):
                    cap, = (row for row in self.rows() if row.get('cap'))
                    self.assertEqual(review_links.paused(), {})
                    self.assertFalse({'upgrade_alerted', 'upgrade_at'} & cap.keys())
                    self.assertFalse(cap.get('counts'))
                    self.assertIn('cap_reset', cap)
                else:
                    cap = self.upgrade_cap()
                    self.assertEqual({field: cap.get(field) for field in marker}, marker)
                self.alert.assert_called_once()
                self.send.assert_not_called()

    def test_upgrade_alerts_once_before_discovery_and_keeps_the_alert_on_binding(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        self.turn_source = Mock(return_value=('background', None, None))
        for bind in (False, True):
            with self.subTest(bind=bind):
                self.clock.return_value = 1000
                rows = self.legacy_pair(dict(rounds=1), dict(empty_rounds=2))
                for row in rows:
                    row['worker_thread'] = None
                self.file.write_text(json.dumps(rows))
                self.alert.reset_mock()
                undiscovered = [dict(self.worker, thread=None, pane_name='Worker tab'), *self.entries[1:]]
                self.observe(entries=undiscovered, elapsed=0)
                self.observe(entries=undiscovered)
                self.alert.assert_called_once()
                self.assertIn('Worker tab', self.alert.call_args.args[1])
                cap = self.upgrade_cap()
                self.assertIsNone(cap['worker_thread'])
                self.assertTrue(cap['upgrade_alerted'])
                if bind:
                    # Binding to a newer paused cap must keep the alert and time of the unresolved cap.
                    rows = self.rows()
                    rows.append(dict(cap=True, worker='%13', worker_thread=WORKER_THREAD,
                                     paused='upgrade', upgrade_at=1003))
                    self.file.write_text(json.dumps(rows))
                    self.observe()
                    cap = self.upgrade_cap()
                    self.assertEqual((cap['worker_thread'], cap['upgrade_at']), (WORKER_THREAD, 1000))
                    self.assertTrue(cap['upgrade_alerted'])
                    self.alert.assert_called_once()
                self.send.assert_not_called()

    def test_upgrade_retries_failed_alert_until_one_success_across_watcher_restart(self):
        self.enterContext(own_time(review_links, time=self.clock))
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        self.turn_source = Mock(return_value=('background', None, None))
        for failures in (1, 2):
            with self.subTest(failures=failures):
                self.clock.return_value = 1000
                self.file.write_text(json.dumps(self.legacy_pair(dict(rounds=1), dict(empty_rounds=2))))
                successful = Mock()
                self.alert.reset_mock()

                def alert(*args):
                    if self.alert.call_count <= failures:
                        raise OSError('cannot queue alert')
                    successful(*args)

                self.alert.side_effect = alert
                for _ in range(failures):
                    self.observe()
                    successful.assert_not_called()
                    self.assertFalse(self.upgrade_cap().get('upgrade_alerted'))
                self.observe()
                successful.assert_called_once()
                self.assertEqual(self.alert.call_count, failures + 1)
                self.assertTrue(self.upgrade_cap()['upgrade_alerted'])

                spec = importlib.util.spec_from_file_location('restarted_review_links', review_links.__file__)
                restarted = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(restarted)
                restarted.FILE = self.file
                with own_time(restarted, time=self.clock, monotonic=self.clock):
                    restarted.observe(self.entries, self.status, self.send, self.reply, alert=self.alert,
                                      turn_source=self.turn_source)
                successful.assert_called_once()
                self.assertEqual(self.alert.call_count, failures + 1)
                self.send.assert_not_called()

    def test_binding_keeps_live_unbound_progress_and_pauses_on_conflicting_saved_cap(self):
        for limit in (3, 5):
            with self.subTest(limit=limit):
                self.states.update({'%13': 'Idle', '%12': 'Idle'})
                live = dict(worker='%13', reviewer='%12', reviewer_thread=None, name='Fix the bug', agent='codex',
                            seen_working=True, created=time.time(), rounds=1, paused=False)
                saved = dict(worker='%13', worker_thread=WORKER_THREAD, saved_at=time.time(), rounds=limit, paused=True)
                if limit == 5:
                    saved.update(limit=5, grant_rounds=3)
                self.file.write_text(json.dumps([live, saved]))
                self.turn_source = lambda _: 'background'
                self.send.reset_mock()
                self.alert.reset_mock()
                self.observe()
                self.send.assert_not_called()
                self.alert.assert_called_once()
                cap = self.upgrade_cap()
                self.assertEqual(cap['counts'], {f'%12:{THREAD}': {'rounds': 1},
                                                 review_links.UNOWNED: {'rounds': limit}})
                self.assertNotIn('limit', cap)

    def test_malformed_legacy_fields_do_not_stop_healthy_reviews_or_listing(self):
        for bad in (dict(review_prompts=None), dict(parent_stage='broken'),
                    dict(rounds='3', grant_rounds=[], restart={'rounds': {}},
                         review_prompts=[None, ['review-summary', 'never']])):
            with self.subTest(bad=bad):
                self.states.update({'%13': 'Idle', '%12': 'Idle'})
                live = dict(worker='%13', worker_thread=WORKER_THREAD, reviewer='%12', reviewer_thread=THREAD,
                            name='Fix the bug', agent='codex', seen_working=True, created=time.time())
                self.file.write_text(json.dumps([dict(live, worker='%20', reviewer='%21', **bad), [], live]))
                self.assertEqual(review_links.reviewed_by(), {'%20': ['%21'], '%13': ['%12']})
                self.assertFalse(review_links.in_parent_stage('%20', WORKER_THREAD))
                self.send.reset_mock()
                self.observe()
                self.send.assert_called_once()
                self.assertIn('finished a turn', self.send.call_args.args[1])

    def test_agent_link_during_feedback_keeps_the_conversation_rounds(self):
        for limit in (3, 5):
            with self.subTest(limit=limit):
                self.file.write_text('[]')
                self.entries = [self.worker, self.reviewer]
                self.states.update({'%13': 'Idle', '%12': 'Idle', '%21': 'Idle'})
                self.review_reply = None
                self.add(worker_thread=WORKER_THREAD)
                parent = self.add_parent(limit)
                for _ in range(2):
                    self.request_review()
                    self.finish_review()
                if limit == 5:
                    self.request_review()
                    self.finish_review()  # summary at round three
                    self.request_review()
                    self.assertTrue(review_links.agent_prompt('%13', parent['id'], parent['thread']))
                    self.request_review()
                    self.finish_review()  # fourth finding
                self.observe(**{'%13': 'Working'})
                self.entries.append(dict(self.reviewer, id='%21'))
                with patch.object(server.agent_tabs, 'live_status', self.status):
                    server.agent_tabs.link_pair({'worker': '%13', 'reviewer': '%21'}, self.entries, self.reply)
                self.observe()  # B sees the still-running fix for the first time
                self.observe(**{'%13': 'Idle'})
                self.send.reset_mock()
                self.alert.reset_mock()
                self.review_reply = lambda entry, _: FINDINGS if entry is self.reviewer else None
                self.finish_review(FINDINGS)
                self.assert_one_cap(limit)
                self.send.assert_called_once()
                self.assertIn(f'Stopped after {limit} review rounds: the reviewer tab (%12)', self.send.call_args.args[1])
                self.assert_mak_alert(rounds=limit)
                self.assertEqual(review_links.paused(), {'%13': ['%12', '%21']})

    def stopped_review_prompt(self, agent, limit, *, legacy=False):
        self.file.write_text('[]')
        self.entries = [self.worker, self.reviewer]
        self.states.update({'%13': 'Idle', '%12': 'Idle'})
        self.worker.update(agent=agent, transcript=str(self.file.parent / 'worker.jsonl'))
        self.turn_source = None
        self.alert.reset_mock()
        self.add(agent=agent, worker_thread=WORKER_THREAD)
        parent = self.add_parent(limit)
        self.fail_to_limit()
        if limit == 5:
            summary = next(call.args[1] for call in reversed(self.send.call_args_list) if call.args[0] == self.worker)
            self.write_worker_turn(summary)
            self.turn_source = server.review_turn_source
            self.request_review()
            self.turn_source = None
            self.assertTrue(review_links.agent_prompt('%13', parent['id'], parent['thread']))
            self.alert.reset_mock()
            for _ in range(2):
                self.request_review()
                self.finish_review(FINDINGS)
        self.assert_mak_stop(rounds=limit)
        stop = self.send.call_args.args[1]
        if legacy:
            row, = self.links()
            row.pop('feedback_turn')
            self.file.write_text(json.dumps([row]))
        self.send.reset_mock()
        self.turn_source = server.review_turn_source
        return stop

    def write_worker_turn(self, prompt, output_size=0, *, hook_blocked=False):
        agent = self.worker['agent']
        now = int(time.time())
        started_at, finished_at = now + 1, now + 2
        if agent == 'antigravity':
            records = [dict(type='USER_INPUT', step_index=1, content=f'<USER_REQUEST>{prompt}</USER_REQUEST>')]
            if output_size:
                records.append(dict(type='PLANNER_RESPONSE', step_index=2, content='x' * output_size))
            records.append(dict(type='AGENT_RESPONSE', step_index=3, content='Done.'))
            started_at = finished_at = None
        else:
            message = claude if agent == 'claude' else codex
            records = [message('user', prompt)]
            if output_size:
                records.append(message('assistant', 'x' * output_size))
            if hook_blocked:
                records.append(dict(type='attachment', attachment={'type': 'hook_blocking_error'}))
            records.append(message('assistant', 'Done.'))
            records[0]['timestamp'] = datetime.fromtimestamp(started_at, timezone.utc).isoformat()
            records[-1]['timestamp'] = datetime.fromtimestamp(finished_at, timezone.utc).isoformat()
        write_records(Path(self.worker['transcript']), records)
        if output_size:
            # The normal notification reader starts inside the large output record.
            self.assertEqual(server.notification_records(self.worker), records[2:])
        return started_at, finished_at

    def assert_stopped_review(self, limit):
        self.observe()  # An extra idle tick must not reinterpret the same turn.
        self.send.assert_not_called()
        self.assert_one_cap(limit)
        self.assertEqual(review_links.paused(), {'%13': ['%12']})
        self.assertEqual(review_links.escalated(self.entries), {'%13'})
        self.assertNotIn('restart', self.links()[0])

    def assert_reviews_restarted(self):
        self.request_review()
        self.send.assert_called_once()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(review_links.paused(), {})
        self.assertEqual(review_links.escalated(self.entries), set())
        self.finish_review()
        self.assert_one_cap(1)

    def test_watcher_restart_during_stopped_feedback_preserves_cap_until_mak_answers(self):
        for agent, limit in product(REVIEW_TRANSCRIPT_AGENTS, (3, 5)):
            with self.subTest(agent=agent, limit=limit):
                stop = self.stopped_review_prompt(agent, limit)
                self.write_worker_turn(stop, CLIPPED_REVIEW_OUTPUT)
                self.observe(**{'%13': 'Working'})  # persist the running turn before the restart
                self.assertEqual(self.links()[0]['feedback_turn']['phase'], 'working')
                with patch.object(review_links, '_last_sent', {}), patch.object(review_links, '_feedback_in_flight', {}):
                    self.observe(**{'%13': 'Idle'})  # the stop turn finishes after the watcher restarts
                    self.assert_stopped_review(limit)
                    self.write_worker_turn('Mak starts another task.')
                    self.assert_reviews_restarted()

    def test_unread_stop_prompt_preserves_an_upgraded_cap_without_a_delivery_marker(self):
        for agent, limit in product(REVIEW_TRANSCRIPT_AGENTS, (3, 5)):
            with self.subTest(agent=agent, limit=limit):
                stop = self.stopped_review_prompt(agent, limit, legacy=True)
                before, = self.links()
                _, finished_at = self.write_worker_turn(stop, CLIPPED_REVIEW_OUTPUT)
                self.assertEqual(self.turn_source(self.worker), ('unknown', None, finished_at))
                self.request_review()
                self.assert_stopped_review(limit)
                after, = self.links()
                for field in ('cap_reset', 'limit', 'grant_rounds'):
                    self.assertEqual(after.get(field), before.get(field), field)
                self.write_worker_turn('Mak starts another task.')
                self.assert_reviews_restarted()

    def test_long_stop_turn_preserves_an_upgraded_cap_without_a_delivery_marker(self):
        for agent, limit in product(REVIEW_TRANSCRIPT_AGENTS, (3, 5)):
            with self.subTest(agent=agent, limit=limit):
                stop = self.stopped_review_prompt(agent, limit, legacy=True)
                times = self.write_worker_turn(stop, LONG_REVIEW_OUTPUT)
                self.assertEqual(self.turn_source(self.worker), ('review-feedback', *times))
                self.request_review()
                self.assert_stopped_review(limit)

    def test_long_mak_turn_restarts_a_paused_cap(self):
        for agent in REVIEW_TRANSCRIPT_AGENTS:
            with self.subTest(agent=agent):
                self.stopped_review_prompt(agent, 3, legacy=True)
                times = self.write_worker_turn('Mak asks to fix the findings.', LONG_REVIEW_OUTPUT)
                self.assertEqual(self.turn_source(self.worker), ('user', *times))
                self.assert_reviews_restarted()

    def test_wider_tail_does_not_skip_a_background_prompt_without_a_reply(self):
        # B289 review r2/r3: a scheduled prompt or wakeup starts a turn that aborts after long tool output.
        for start in (claude('user', '<scheduled-task>Check CI.</scheduled-task>', isSystemInjected=True),
                      dict(type='system', subtype='scheduled_wakeup', content='Wake up.')):
            with self.subTest(start=start.get('subtype') or 'user'):
                self.stopped_review_prompt('claude', 3, legacy=True)
                write_records(Path(self.worker['transcript']), [
                    claude('user', 'An earlier task from Mak.'), claude('assistant', 'Done.'), start,
                    claude('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x' * LONG_REVIEW_OUTPUT}]),
                ])
                self.assertEqual(self.turn_source(self.worker)[0], 'unknown')
                self.request_review()
                self.assert_stopped_review(3)

    def test_antigravity_stop_turn_without_a_reply_keeps_the_cap(self):
        # B289 review r3: the whole export is read, but the stop turn ends in an error with no reply.
        stop = self.stopped_review_prompt('antigravity', 3, legacy=True)
        write_records(Path(self.worker['transcript']), [
            dict(type='USER_INPUT', step_index=1, content='<USER_REQUEST>An earlier task from Mak.</USER_REQUEST>'),
            dict(type='AGENT_RESPONSE', step_index=2, content='Done.'),
            dict(type='USER_INPUT', step_index=3, content=f'<USER_REQUEST>{stop}</USER_REQUEST>'),
            dict(type='ERROR_MESSAGE', step_index=4, content='x' * LONG_REVIEW_OUTPUT),
        ])
        self.assertEqual(self.turn_source(self.worker)[0], 'unknown')
        self.request_review()
        self.assert_stopped_review(3)

    def test_antigravity_prompt_from_a_partial_export_does_not_restart(self):
        # B289 review r2: the older Mak step is rewritten inside the normal tail; the stop prompt is beyond 8 MiB.
        stop = self.stopped_review_prompt('antigravity', 3, legacy=True)
        write_records(Path(self.worker['transcript']), [
            dict(type='USER_INPUT', step_index=1, content='<USER_REQUEST>An earlier task from Mak.</USER_REQUEST>'),
            dict(type='AGENT_RESPONSE', step_index=2, content='Done.'),
            dict(type='USER_INPUT', step_index=3, content=f'<USER_REQUEST>{stop}</USER_REQUEST>'),
            dict(type='PLANNER_RESPONSE', step_index=4, content='x' * CLIPPED_REVIEW_OUTPUT),
            dict(type='USER_INPUT', step_index=1, content='<USER_REQUEST>An earlier task from Mak.</USER_REQUEST>'),
            dict(type='AGENT_RESPONSE', step_index=5, content='Done.'),
        ])
        self.assertEqual(server.notification_priority.turn_source(
            'antigravity', server.notification_records(self.worker), distinguish_missing=True), 'user')
        self.assertEqual(self.turn_source(self.worker)[0], 'unknown')
        self.request_review()
        self.assert_stopped_review(3)
        # A partial export cannot show which prompt is newest, even when a new prompt and reply are in the tail.
        # The handoff rule: a clipped prompt keeps the cap. Mak can still restart reviews from Tether.
        with open(self.worker['transcript'], 'a') as stream:
            for record in (dict(type='USER_INPUT', step_index=6, content='<USER_REQUEST>Mak asks again.</USER_REQUEST>'),
                           dict(type='AGENT_RESPONSE', step_index=7, content='Done.')):
                stream.write(json.dumps(record) + '\n')
        self.assertEqual(self.turn_source(self.worker)[0], 'unknown')
        self.assert_stopped_review(3)
        self.write_worker_turn('Mak asks again.')  # a whole export: the newest prompt is known
        self.assert_reviews_restarted()

    def test_wider_tail_does_not_credit_an_earlier_mak_prompt_to_a_turn_without_a_reply(self):
        # B289 review: the stop turn aborts after long tool output, before any assistant record.
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                stop = self.stopped_review_prompt(agent, 3, legacy=True)
                message = claude if agent == 'claude' else codex
                if agent == 'claude':
                    output = claude('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x' * LONG_REVIEW_OUTPUT}])
                else:
                    output = {'type': 'response_item',
                              'payload': {'type': 'function_call_output', 'call_id': 't1', 'output': 'x' * LONG_REVIEW_OUTPUT}}
                write_records(Path(self.worker['transcript']), [
                    message('user', 'An earlier task from Mak.'), message('assistant', 'Done.'),
                    message('user', stop), output,
                ])
                self.assertEqual(self.turn_source(self.worker)[0], 'unknown')
                self.request_review()
                self.assert_stopped_review(3)

    def test_out_of_order_antigravity_export_does_not_restart_from_an_older_prompt(self):
        # B289 review: an older Mak step is rewritten after the clipped stop prompt.
        stop = self.stopped_review_prompt('antigravity', 3, legacy=True)
        write_records(Path(self.worker['transcript']), [
            dict(type='USER_INPUT', step_index=1, content='<USER_REQUEST>An earlier task from Mak.</USER_REQUEST>'),
            dict(type='AGENT_RESPONSE', step_index=2, content='Done.'),
            dict(type='USER_INPUT', step_index=3, content=f'<USER_REQUEST>{stop}</USER_REQUEST>'),
            dict(type='PLANNER_RESPONSE', step_index=4, content='x' * CLIPPED_REVIEW_OUTPUT),
            dict(type='USER_INPUT', step_index=1, content='<USER_REQUEST>An earlier task from Mak.</USER_REQUEST>'),
            dict(type='PLANNER_RESPONSE', step_index=5, content='x' * LONG_REVIEW_OUTPUT),
            dict(type='AGENT_RESPONSE', step_index=6, content='Done.'),
        ])
        self.assertEqual(self.turn_source(self.worker)[0], 'unknown')
        self.request_review()
        self.assert_stopped_review(3)

    def test_second_unread_turn_preserves_the_cap(self):
        for agent in REVIEW_TRANSCRIPT_AGENTS:
            with self.subTest(agent=agent):
                stop = self.stopped_review_prompt(agent, 3, legacy=True)
                for prompt in (stop, 'Mak asks to fix the findings.'):
                    self.write_worker_turn(prompt, CLIPPED_REVIEW_OUTPUT)
                    self.request_review()
                    self.assert_stopped_review(3)

    def test_unread_work_turn_requests_review_without_resetting_rounds(self):
        for agent in REVIEW_TRANSCRIPT_AGENTS:
            with self.subTest(agent=agent):
                self.add(agent=agent, worker_thread=WORKER_THREAD)
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                self.set_counts(cap, '%12', rounds=1)
                self.file.write_text(json.dumps(rows))
                self.worker.update(agent=agent, transcript=str(self.file.parent / 'worker.jsonl'))
                self.turn_source = server.review_turn_source
                self.send.reset_mock()
                self.write_worker_turn('Fix the remaining finding.', CLIPPED_REVIEW_OUTPUT)
                self.request_review()
                self.send.assert_called_once()
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertEqual(self.links()[0].get('rounds'), 1)
                self.assertEqual(self.links()[0].get('cap_reset'), cap.get('cap_reset'))

    def test_long_hook_blocked_stop_turn_recovers_review_feedback(self):
        stop = self.stopped_review_prompt('claude', 3, legacy=True)
        times = self.write_worker_turn(stop, LONG_REVIEW_OUTPUT, hook_blocked=True)
        self.assertEqual(self.turn_source(self.worker), ('review-feedback', *times))
        self.request_review()
        self.assert_stopped_review(3)

    def test_visible_stop_prompt_preserves_an_upgraded_cap_without_a_delivery_marker(self):
        self.fail_to_limit()
        stop = self.send.call_args.args[1]
        legacy, = self.links()
        legacy.pop('feedback_turn', None)
        self.file.write_text(json.dumps([legacy]))
        self.turn_source = server.review_turn_source
        records = [codex('user', stop), codex('assistant', 'Open findings.')]
        self.send.reset_mock()
        with patch.object(server, 'notification_records', return_value=records):
            self.request_review()
            self.send.assert_not_called()
            self.assertEqual(review_links.paused(), {'%13': ['%12']})
            records.extend([codex('user', 'Mak starts another task.'), codex('assistant', 'Done.')])
            self.request_review()
        self.send.assert_called_once()
        self.assertEqual(review_links.paused(), {})

    def test_unresolved_browser_link_does_not_block_ready_reviewer_or_its_own_timeout(self):
        self.add(worker_thread=WORKER_THREAD)
        self.request_review()
        sibling = dict(self.reviewer, id='%21', thread=None)
        self.states['%21'] = 'Idle'
        pending_entries = [dict(self.worker, thread=None), self.reviewer, sibling]
        with server.SEND_LOCK, patch.object(server.agent_tabs, 'live_status', self.status):
            server.agent_tabs.link_pair({'worker': '%13', 'reviewer': '%21'}, pending_entries, self.reply, reset=True)
        self.send.reset_mock()
        self.finish_review()
        self.send.assert_called_once()
        self.assertIn('Feedback from the reviewer tab (%12)', self.send.call_args.args[1])
        with patch.object(review_links.time, 'time', return_value=time.time() + review_links.START_WAIT + 1):
            self.observe()
        self.assertEqual(review_links.reviewed_by(), {'%13': ['%12']})
        self.assert_one_cap(1)

    def test_caps_on_the_same_pane_are_isolated_between_worker_threads(self):
        self.add(worker_thread=WORKER_THREAD, seen_working=True)
        self.observe()
        live, = self.links()
        live.update(rounds=1)
        other = dict(worker='%13', worker_thread=THREAD, saved_at=time.time(), rounds=5, limit=5,
                     grant_rounds=3, paused=True)
        self.file.write_text(json.dumps([other, live]))
        self.finish_review(FINDINGS)
        self.assertEqual(self.links()[0], other)
        self.assertEqual(self.links()[1]['rounds'], 2)
        self.add(reviewer='%22', worker_thread=WORKER_THREAD, reset=True)
        self.assertEqual(self.links()[0], other)
        with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
            review_links.check_cap('%13', THREAD)

    def test_mak_turn_restarts_the_cap_with_a_stopped_sibling(self):
        self.add(worker_thread=WORKER_THREAD)
        self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
        rows = self.rows()
        cap = next(row for row in rows if row.get('cap'))
        for reviewer in ('%12', '%21'):
            self.set_counts(cap, reviewer, rounds=2)
        self.file.write_text(json.dumps(rows))
        self.assertTrue(review_links.remove('%13', '%21'))
        self.request_review()  # Mak starts a new task with one reviewer still linked.
        cap, = (row for row in self.rows() if row.get('cap'))
        self.assertFalse(cap.get('counts'))

        self.send.reset_mock()
        self.finish_review(FINDINGS)
        self.send.assert_called_once()
        self.assertIn('Feedback from the reviewer tab', self.send.call_args.args[1])
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.alert.assert_not_called()

    def test_mak_turn_restarts_the_cap_with_a_live_sibling(self):
        for discovery in ('present', 'missing at Idle', 'missing whole turn'):
            with self.subTest(discovery=discovery):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                sibling = dict(self.reviewer, id='%21')
                self.entries = [self.worker, self.reviewer, sibling]
                self.states['%21'] = 'Idle'

                def keep_sibling_draft(entry, *_):
                    if entry == sibling:
                        raise DraftError('Keep the draft.')

                self.send.side_effect = keep_sibling_draft
                self.fail_to_limit(FINDINGS)
                self.request_review()  # the reply to the round limit
                without_sibling = [self.worker, self.reviewer]
                self.observe(entries=without_sibling if discovery == 'missing whole turn' else self.entries,
                             **{'%13': 'Working'})  # Mak starts a new task
                entries = self.entries if discovery == 'present' else without_sibling
                self.observe(entries=entries, **{'%13': 'Idle'})
                self.assertTrue(self.links()[0]['awaiting'])
                for link in self.links():
                    self.assertFalse({'rounds', 'paused', 'restart'} & link.keys())

                self.send.reset_mock()
                self.alert.reset_mock()
                self.finish_review(FINDINGS)
                worker_sends = [call.args[1] for call in self.send.call_args_list if call.args[0] == self.worker]
                feedback, = worker_sends
                self.assertIn('Feedback from the reviewer tab', feedback)
                self.assertEqual([link.get('rounds', 0) for link in self.links()], [1, 0])
                self.assert_one_cap(1)
                self.alert.assert_not_called()

    def test_parent_notice_reaches_parent_with_an_idle_surviving_reviewer(self):
        for decision in ('grant', 'escalate'):
            with self.subTest(decision=decision):
                self.file.write_text('[]')
                self.turn_source = None
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.states['%11'] = 'Working'
                self.add(worker_thread=WORKER_THREAD)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                sibling = dict(self.reviewer, id='%21')
                self.entries.append(sibling)
                self.states['%21'] = 'Idle'

                def hold_siblings_draft(entry, *_):
                    if entry == sibling:
                        raise DraftError('Keep the draft.')

                self.send.side_effect = hold_siblings_draft
                self.review_reply = lambda entry, _: FINDINGS if entry == self.reviewer else None
                self.fail_to_limit(FINDINGS)
                self.assertFalse(self.links()[0]['parent_stage']['sent'])
                self.assertFalse(self.links()[1].get('awaiting'))
                self.assertTrue(review_links.remove('%13', '%12'))
                self.turn_source = lambda _: 'review-summary'
                self.request_review()  # The worker finishes the findings summary while the parent is still busy.
                self.send.side_effect = None

                self.send.reset_mock()
                self.alert.reset_mock()
                self.observe(**{'%21': 'Idle', '%11': 'Idle'})
                self.assert_parent_notice(parent)
                self.assertTrue(review_links.in_parent_stage('%13', WORKER_THREAD))
                self.observe()
                self.send.assert_called_once()
                self.alert.assert_not_called()
                if decision == 'grant':
                    review_links.agent_prompt('%13', parent['id'], parent['thread'], worker_thread=WORKER_THREAD)
                    review_links.check_cap('%13', WORKER_THREAD)
                    self.assertEqual(review_links.paused(), {})
                else:
                    review_links.escalate('%13', parent['id'], parent['thread'])
                    self.observe()
                    self.assert_mak_alert()
                    self.assertFalse(review_links.in_parent_stage('%13', WORKER_THREAD))

    def test_parent_stage_outlives_the_capped_reviewer_at_levels_four_and_five(self):
        # B283 (B278 review r2, finding 3): reviewer A reaches the cap while the parent is busy, then stops. Reviewer B
        # never paused: its draft blocked every request. The parent stage belongs to the worker conversation's cap row,
        # so the parent gets the notice, or Mak gets the alert, while B stays linked.
        for level, outcome in ((5, 'notice'), (4, 'notice'), (5, 'parent gone')):
            with self.subTest(level=level, outcome=outcome):
                self.file.write_text('[]')
                self.turn_source = None
                self.send.reset_mock(side_effect=True)
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent(level=level)
                self.states['%11'] = 'Working'
                self.add(worker_thread=WORKER_THREAD)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                sibling = dict(self.reviewer, id='%21')
                self.entries.append(sibling)
                self.states['%21'] = 'Idle'
                draft = [True]

                def hold_siblings_draft(entry, *_):
                    if entry == sibling and draft[0]:
                        raise DraftError('Keep the draft.')

                self.send.side_effect = hold_siblings_draft
                self.review_reply = lambda entry, _: FINDINGS if entry == self.reviewer else None
                self.fail_to_limit(FINDINGS)
                self.assertFalse(self.links()[0]['parent_stage']['sent'])
                self.assertTrue(review_links.remove('%13', '%12'))
                self.turn_source = lambda _: 'review-summary'
                self.request_review()  # The worker finishes the findings summary while the parent is still busy.
                self.send.reset_mock()
                self.alert.reset_mock()
                if outcome == 'parent gone':
                    self.entries.remove(parent)
                draft[0] = False  # B's draft clears as the parent becomes idle.
                self.observe(**{'%11': 'Idle'})
                for _ in range(review_links.LOSS_TICKS * 4):
                    self.observe()
                to_parent = [call for call in self.send.call_args_list if call.args[0] == parent]
                if outcome == 'notice':
                    self.assertEqual(len(to_parent), 1, self.send.call_args_list)
                    self.assert_parent_notice(parent)
                    self.alert.assert_not_called()
                    self.assertTrue(review_links.in_parent_stage('%13', WORKER_THREAD))
                else:
                    self.assertEqual(to_parent, [])
                    self.assert_mak_alert()
                    self.assertFalse(review_links.in_parent_stage('%13', WORKER_THREAD))

    def test_parent_grant_reopens_the_shared_cap_for_both_reviewers(self):
        parent = self.add_parent()
        self.add(worker_thread=WORKER_THREAD)
        self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Idle'
        rows = self.rows()
        cap = next(row for row in rows if row.get('cap'))
        for reviewer in ('%12', '%21'):
            self.set_counts(cap, reviewer, rounds=3)
        cap.update(paused=True, parent_stage=dict(pane=parent['id'], thread=parent['thread'], notice=FINDINGS, sent=True))
        self.file.write_text(json.dumps(rows))

        review_links.agent_prompt('%13', parent['id'], parent['thread'], worker_thread=WORKER_THREAD)
        review_links.check_cap('%13', WORKER_THREAD)
        self.request_review()
        self.assertCountEqual([call.args[0]['id'] for call in self.send.call_args_list], ['%12', '%21'])
        self.states['%21'] = 'Working'
        self.finish_review(FINDINGS)
        self.assertEqual([link['rounds'] for link in self.links()], [4, 3])
        self.alert.assert_not_called()

    def test_parent_grant_during_siblings_reply_keeps_the_shared_cap_open(self):
        parent = self.add_parent()
        self.add(worker_thread=WORKER_THREAD)
        self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
        self.entries.append(dict(self.reviewer, id='%21'))
        self.states['%21'] = 'Working'
        self.review_reply = lambda entry, _: FINDINGS if entry == self.reviewer else None
        self.fail_to_limit(FINDINGS)
        self.assert_parent_notice(parent)

        def grant_on_reply(*_):
            review_links.agent_prompt('%13', parent['id'], parent['thread'], worker_thread=WORKER_THREAD)
            return FINDINGS

        self.review_reply = grant_on_reply
        self.observe(**{'%21': 'Idle'})
        self.assertEqual(review_links.paused(), {})
        self.alert.assert_not_called()

    def test_stop_during_feedback_cannot_relink_before_the_count_is_saved(self):
        for _ in range(2):
            self.request_review()
            self.finish_review()
        self.request_review()
        stopped = []

        def stop_on_send(entry, *_):
            if entry == self.worker:
                stopped.append(review_links.remove('%13', '%12'))
                with self.assertRaisesRegex(ValueError, 'Review feedback is being delivered'):
                    self.add(worker_thread=WORKER_THREAD, reset=False)
        self.send.side_effect = stop_on_send
        self.finish_review()
        self.assertEqual(stopped, [True])
        record, = self.links()
        self.assertEqual(record['rounds'], 3)
        self.assertTrue(record['paused'])
        self.assertEqual(review_links.reviewed_by(), {})
        with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
            self.add(worker_thread=WORKER_THREAD, reset=False)

    def test_stop_during_final_feedback_passes_the_saved_round_to_the_surviving_reviewer(self):
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                self.file.write_text('[]')
                self.states.update({'%13': 'Idle', '%12': 'Idle', '%21': 'Idle'})
                self.entries = [self.worker, self.reviewer, dict(self.reviewer, id='%21')]
                self.send.reset_mock(side_effect=True)
                self.alert.reset_mock()
                self.turn_source = None
                self.add(worker_thread=WORKER_THREAD, reset=False)
                for _ in range(2):
                    self.request_review()
                    self.finish_review()
                    self.turn_source = lambda _: 'review-feedback'
                self.request_review()
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                during_send = []

                def stop_on_send(entry, *_):
                    if entry == self.worker:
                        stopped = review_links.remove('%13', '%12')
                        cap, = (row for row in self.rows() if row.get('cap'))
                        during_send.append((stopped, cap['counts']))
                        if not delivered:
                            raise DraftError('Keep the draft.')

                self.send.side_effect = stop_on_send
                self.finish_review(FINDINGS)
                self.assertEqual(during_send, [(True, {f'%12:{THREAD}': {'rounds': 2}})])
                if delivered:
                    self.assert_mak_stop()
                self.send.reset_mock(side_effect=True)
                self.request_review()
                if delivered:
                    self.send.assert_not_called()
                    self.assert_mak_alert()
                else:
                    self.reply.return_value = FINDINGS
                    self.observe(**{'%21': 'Working'})
                    self.observe(**{'%21': 'Idle'})
                    self.assert_mak_stop(reviewer='%21')
                self.assertEqual(review_links.paused(), {'%13': ['%21']})
                cap, = (row for row in self.rows() if row.get('cap'))
                self.assertEqual(cap['counts'], {f'%21:{THREAD}': {'rounds': 3}})

    def test_stop_during_final_feedback_alert_gives_browser_restart(self):
        for starting_rounds, limit in ((2, 3), (4, 5)):
            with self.subTest(limit=limit):
                self.add(worker_thread=WORKER_THREAD)
                self.alert.reset_mock()
                self.request_review()
                link, = self.links()
                link.update(rounds=starting_rounds, limit=limit)
                self.file.write_text(json.dumps([link]))
                self.send.side_effect = lambda entry, *_: review_links.remove('%13') if entry == self.worker else None
                self.finish_review()
                self.alert.assert_called_once()
                self.assertIn('Link the tabs again from Tether', self.alert.call_args.args[1])
                self.assertNotIn('Answer in that tab', self.alert.call_args.args[1])

    def test_browser_reset_cannot_pass_a_claim_waiting_for_send_lock(self):
        for level in (3, 5):
            with self.subTest(level=level):
                self.file.write_text('[]')
                self.entries = [self.worker, self.reviewer]
                self.add_parent(level)
                self.entries.append(dict(self.reviewer, id='%22'))
                self.states['%22'] = 'Idle'
                self.review_reply = None
                self.add(worker_thread=WORKER_THREAD)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                self.request_review()
                prior = self.rows()
                cap = next(row for row in prior if row.get('cap'))
                for reviewer in ('%12', '%21'):
                    self.set_counts(cap, reviewer, rounds=level - 1)
                if level == 5:
                    cap.update(limit=5, grant_rounds=3)
                self.file.write_text(json.dumps(prior))
                self.send.reset_mock()
                self.alert.reset_mock()
                holding_lock, claimed = threading.Event(), threading.Event()
                order = []

                def reset():
                    return server.agent_tabs.link_pair(
                        {'worker': '%13', 'reviewer': '%22'}, self.entries, self.reply, reset=True)

                def browser():
                    with server.SEND_LOCK:
                        holding_lock.set()
                        if claimed.wait(5):
                            try:
                                reset()
                            except ValueError as error:
                                order.append(str(error))
                            else:
                                order.append('reset')

                def folder_note(*_):
                    order.append('claimed')
                    claimed.set()
                    return ''

                terminal = Mock(side_effect=lambda *_: order.append('sent'))
                reader = threading.Thread(target=browser)
                with (patch.object(server.agent_tabs, 'live_status', self.status),
                      patch.object(server, 'agent_folder', return_value='/project'),
                      patch.object(server.shell_log, 'folder_note', side_effect=folder_note),
                      patch.object(server.continuations, 'prepare', side_effect=lambda _, msg: (msg, None)),
                      patch.object(server, 'terminal_submit', terminal)):
                    reader.start()
                    try:
                        self.assertTrue(holding_lock.wait(5))
                        self.send.side_effect = lambda entry, text, state: (
                            server.review_send(entry, text, state) if entry == self.worker else None)
                        self.finish_review(FINDINGS)
                    finally:
                        claimed.set()
                        reader.join(5)
                        self.send.side_effect = None

                    self.assertFalse(reader.is_alive())
                    self.assertEqual(order, ['claimed', 'Review feedback is being delivered. Try linking again.', 'sent'])
                    terminal.assert_called_once()
                    self.assertEqual([link['rounds'] for link in self.links()], [level, level - 1])
                    self.assert_mak_alert(level)
                    with server.SEND_LOCK:
                        self.assertEqual(reset(), {'linked': True})
                self.assertTrue(all('rounds' not in link for link in self.links()))
                self.send.reset_mock()
                self.alert.reset_mock()
                self.review_reply = Mock(return_value=FINDINGS)
                self.observe()  # The newly linked reviewer receives a fresh request.
                self.send.reset_mock()
                self.observe()
                self.send.assert_called_once()
                self.assertIn('Feedback from the reviewer tab (%22)', self.send.call_args.args[1])
                self.assert_one_cap(1, '%22')
                self.alert.assert_not_called()

    def test_browser_reset_retries_after_a_failed_or_refused_delivery(self):
        for failure in (DraftError('Keep the draft.'), RuntimeError('Could not send.'),
                        ValueError('Delivery raised an unexpected error.')):
            with self.subTest(failure=type(failure).__name__):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD)
                self.send.side_effect = None
                self.request_review()
                self.send.reset_mock()
                refusals = []

                def fail_send(*_):
                    try:
                        self.add(reviewer='%22', worker_thread=WORKER_THREAD, reset=True)
                    except ValueError as error:
                        refusals.append(str(error))
                    raise failure

                self.send.side_effect = fail_send
                self.finish_review(FINDINGS)
                self.send.assert_called_once()
                self.assertEqual(refusals, ['Review feedback is being delivered. Try linking again.'])
                link, = self.links()
                self.assertTrue(link['awaiting'])
                self.assertNotIn('rounds', link)
                self.add(reviewer='%22', worker_thread=WORKER_THREAD, reset=True)
                self.assertEqual(review_links.reviewed_by(), {'%13': ['%12', '%22']})

    def test_a_paused_review_stays_paused_below_the_granted_cap(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD)
                link, = self.links()
                link.update(rounds=3, limit=5, grant_rounds=3, paused=True)
                if stale:
                    link['stale_rounds'] = 3
                self.file.write_text(json.dumps([link]))
                self.turn_source = lambda _: 'background'
                self.send.reset_mock()
                self.request_review()
                self.send.assert_not_called()
                self.assertTrue(self.links()[0]['paused'])
                self.turn_source = lambda _: 'user'
                self.request_review()
                self.send.assert_called_once()
                self.assertNotIn('paused', self.links()[0])

    def test_mak_prompt_restarts_at_either_stage_with_the_original_limit(self):
        for grant in (False, True):
            with self.subTest(grant=grant):
                self.add()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.turn_source = Mock(return_value='user')
                self.fail_to_limit()
                self.request_review()
                if grant:
                    review_links.agent_prompt('%13', parent['id'], parent['thread'])
                    for _ in range(2):
                        self.request_review()
                        self.finish_review()
                    self.request_review()
                self.request_review()  # Mak sends a new task.
                link, = self.links()
                self.assertTrue(link['awaiting'])
                for key in ('paused', 'parent_stage', 'rounds', 'empty_rounds', 'limit', 'grant_rounds'):
                    self.assertNotIn(key, link)

    def test_approval_after_grant_clears_own_counts_and_keeps_the_grant(self):
        parent = self.add_parent()
        self.fail_to_limit()
        review_links.agent_prompt('%13', parent['id'], parent['thread'])
        self.request_review()
        self.finish_review('APPROVED')
        link, = self.links()
        self.assertTrue(link['approved_turn'])
        self.assertEqual((link['limit'], link['grant_rounds']), (5, 3))
        for key in ('parent_stage', *review_links.COUNTS):
            self.assertNotIn(key, link)

    def test_missing_or_stale_parent_and_callback_errors_use_mak_path(self):
        for failure in ('no record', 'new thread', 'unsupported agent', 'level error', 'parent error'):
            with self.subTest(failure=failure):
                self.add()
                self.alert.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                if failure == 'no record':
                    self.parent.return_value = None
                elif failure == 'new thread':
                    parent['thread'] = THREAD
                elif failure == 'unsupported agent':
                    parent['agent'] = 'antigravity'
                elif failure == 'level error':
                    self.level.side_effect = OSError('Could not read level')
                else:
                    self.parent.side_effect = OSError('Could not read parent')
                self.fail_to_limit()
                self.assert_mak_stop()

    def test_parent_missing_on_limit_tick_can_return_or_time_out(self):
        for level, returns in ((4, True), (5, False)):
            with self.subTest(level=level, returns=returns):
                self.file.write_text('[]')
                self.add()
                self.alert.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent(level=level)
                self.entries.remove(parent)
                self.parent = lambda entry: server.review_parent(entry, self.entries)
                with patch.object(server.helper_tabs, 'load', return_value=(True, dict(
                        owner_pane=parent['id'], owner_thread=parent['thread']))):
                    self.fail_to_limit()
                self.alert.assert_not_called()
                self.assertEqual(self.links()[0]['parent_missing'], 1)
                self.assertFalse(self.links()[0]['parent_stage']['sent'])
                for _ in range(review_links.LOSS_TICKS - 2):
                    self.observe()
                self.alert.assert_not_called()
                if returns:
                    self.entries.append(parent)
                    self.observe()
                    self.assert_parent_notice(parent)
                    self.assertNotIn('parent_missing', self.links()[0])
                    self.alert.assert_not_called()
                else:
                    self.observe()
                    self.assert_mak_alert()
                    self.assertNotIn('parent_stage', self.links()[0])

    def test_unmatched_parent_binds_its_conversation_before_receiving_the_notice(self):
        parent = self.add_parent()
        parent.update(thread='pending-process', pending_conversation=True)
        self.parent = lambda entry: server.review_parent(entry, self.entries)
        with patch.object(server.helper_tabs, 'load', return_value=(True, dict(
                owner_pane=parent['id'], owner_thread=None))):
            self.fail_to_limit()
        self.assertEqual(self.links()[0]['parent_missing'], 1)
        self.assertFalse(self.links()[0]['parent_stage']['sent'])
        parent.update(thread=PARENT_THREAD, pending_conversation=False)
        self.observe()
        self.assert_parent_notice(parent)
        self.assertEqual(self.links()[0]['parent_stage']['thread'], PARENT_THREAD)
        review_links.agent_prompt('%13', parent['id'], THREAD)
        self.assertTrue(self.links()[0]['paused'])
        self.alert.assert_not_called()

    def test_grant_on_parent_discovery_recovery_does_not_restore_the_parent_stage(self):
        parent = self.add_parent()
        self.entries.remove(parent)
        self.fail_to_limit()
        self.entries.append(parent)
        status = self.status.side_effect

        def grant_on_recovery(entry):
            if entry == self.worker:
                review_links.agent_prompt('%13', parent['id'], parent['thread'])
            return status(entry)

        self.status.side_effect = grant_on_recovery
        self.send.reset_mock()
        self.observe()
        self.send.assert_not_called()
        self.assertNotIn('parent_stage', self.links()[0])
        self.assertNotIn('parent_missing', self.links()[0])
        self.assertEqual(self.links()[0]['limit'], 5)

    def test_unbound_parent_stage_takes_no_grant_or_escalation(self):
        parent = self.add_parent()
        self.entries.remove(parent)
        self.parent.return_value = parent['id'], None  # the helper record does not name the owner's conversation
        self.fail_to_limit()
        self.assertIsNone(self.links()[0]['parent_stage']['thread'])
        for thread in (parent['thread'], 'a-replacement-conversation'):
            review_links.agent_prompt('%13', parent['id'], thread)
            link, = self.links()
            self.assertNotIn('limit', link)
            self.assertTrue(link['paused'])
            with self.assertRaisesRegex(ValueError, 'That tab has no review waiting on this tab.'):
                review_links.escalate('%13', parent['id'], thread)
        self.entries.append(parent)
        self.observe()  # the watcher binds the stage to the live parent and sends the notice
        self.assertEqual(self.links()[0]['parent_stage']['thread'], parent['thread'])
        self.assert_parent_notice(parent)
        review_links.agent_prompt('%13', parent['id'], parent['thread'])
        self.assertEqual(self.links()[0]['limit'], 5)

    def test_parent_leaving_or_changing_thread_escalates_once(self):
        for failure in ('pane gone', 'changed thread', 'parent also reviewer'):
            with self.subTest(failure=failure):
                self.add()
                self.alert.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent(entry=self.reviewer if failure == 'parent also reviewer' else None)
                self.fail_to_limit()
                if failure == 'pane gone':
                    self.entries.remove(parent)
                elif failure == 'changed thread':
                    parent['thread'] = THREAD
                else:
                    self.entries.remove(parent)
                self.observe()  # one missed discovery tick is not a lost parent
                self.alert.assert_not_called()
                self.assertIn('parent_stage', self.links()[0])
                for _ in range(review_links.LOSS_TICKS):
                    self.observe()
                self.assert_mak_alert()
                self.assertTrue(self.links()[0]['paused'])
                self.assertNotIn('parent_stage', self.links()[0])

    def test_parent_ok_or_escalate_after_all_reviewers_stop(self):
        for decision in ('grant', 'immediate grant', 'escalate', 'missing'):
            with self.subTest(decision=decision):
                self.file.write_text('[]')
                self.add()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.states[parent['id']] = 'Working'
                self.fail_to_limit()
                self.assertFalse(self.links()[0]['parent_stage']['sent'])
                review_links.remove('%13')
                self.send.reset_mock()
                self.alert.reset_mock()
                def grant():
                    review_links.agent_prompt('%13', parent['id'], parent['thread'], worker_thread=WORKER_THREAD)

                if decision == 'missing':
                    self.entries.remove(parent)
                if decision == 'immediate grant':
                    self.send.side_effect = lambda entry, *_: grant() if entry == parent else None
                for _ in range(review_links.LOSS_TICKS + 1):
                    self.observe(**{parent['id']: 'Idle'})
                self.send.side_effect = None
                if decision in ('grant', 'immediate grant'):
                    self.assert_parent_notice(parent)
                    self.send.assert_called_once()
                    self.alert.assert_not_called()
                    if decision == 'grant':
                        grant()
                    self.assertEqual(self.links()[0]['limit'], 5)
                    review_links.check_cap('%13', WORKER_THREAD)
                else:
                    if decision == 'escalate':
                        self.assert_parent_notice(parent)
                        self.send.assert_called_once()
                        review_links.escalate('%13', parent['id'], parent['thread'])
                        self.observe()
                    else:
                        self.send.assert_not_called()
                    self.alert.assert_called_once()
                    self.assertIn('Link the tabs again from Tether', self.alert.call_args.args[1])
                self.assertFalse(review_links.in_parent_stage('%13', WORKER_THREAD))
                self.assertEqual(review_links.reviewed_by(), {})

    def test_old_stopped_escalation_cannot_alert_for_a_new_conversation(self):
        parent = self.add_parent()
        self.fail_to_limit()
        review_links.escalate('%13', parent['id'], parent['thread'])
        review_links.remove('%13')
        self.worker['thread'] = PARENT_THREAD
        self.add(worker_thread=PARENT_THREAD, reset=False)
        self.parent = None
        self.fail_to_limit()
        self.assert_mak_alert()

    def test_parent_missing_for_a_few_ticks_keeps_the_parent_stage(self):
        parent = self.add_parent()
        self.fail_to_limit()
        self.entries.remove(parent)
        for _ in range(review_links.LOSS_TICKS - 1):
            self.observe()
        self.entries.append(parent)
        self.observe()
        self.assertNotIn('parent_missing', self.links()[0])
        self.entries.remove(parent)
        for _ in range(review_links.LOSS_TICKS - 1):
            self.observe()
        self.alert.assert_not_called()
        self.assertIn('parent_stage', self.links()[0])

    def test_parent_notice_does_not_restart_the_parents_own_review_loop(self):
        parent = self.add_parent()
        parent['agent'] = 'claude'
        reviewer = dict(id='%14', agent='codex', thread=THREAD, writable=True)
        self.entries.append(reviewer)
        self.states['%14'] = 'Idle'
        review_links.add(worker=parent['id'], reviewer='%14', reviewer_thread=THREAD,
                         name='Parent work', agent='claude')
        child, own = self.links()
        own.update(rounds=5, paused=True, limit=5)
        self.file.write_text(json.dumps([child, own]))
        self.fail_to_limit()
        self.assert_parent_notice(parent)
        notice = self.send.call_args.args[1]
        records = [BACKGROUND, ASSISTANT]
        self.turn_source = server.review_turn_source
        self.send.reset_mock()
        with patch.object(server, 'notification_records', return_value=records):
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
            self.assertTrue(self.links()[1]['paused'])
            prefixed = '[Working directory changed to /work. Run commands and resolve relative paths there.] ' + notice
            records.extend([{'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': prefixed}},
                            ASSISTANT])
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
            self.send.assert_not_called()
            self.assertEqual((self.links()[1]['paused'], self.links()[1]['limit']), (True, 5))
            records.extend([claude('user', notice.splitlines()[0] + '\nMak sends a fresh task.'), ASSISTANT])
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
        self.assertNotIn('paused', self.links()[1])
        self.assertNotIn('limit', self.links()[1])
        self.assertTrue(self.links()[1]['awaiting'])
        self.assertEqual(self.send.call_args.args[0], reviewer)

    def test_parent_notice_turn_with_a_clipped_transcript_keeps_the_parents_loop_paused(self):
        parent = self.add_parent()
        parent['agent'] = 'claude'
        reviewer = dict(id='%14', agent='codex', thread=THREAD, writable=True)
        self.entries.append(reviewer)
        self.states['%14'] = 'Idle'
        review_links.add(worker=parent['id'], reviewer='%14', reviewer_thread=THREAD,
                         name='Parent work', agent='claude')
        child, own = self.links()
        own.update(rounds=5, paused=True, limit=5)
        self.file.write_text(json.dumps([child, own]))
        self.states['%11'] = 'Working'
        self.fail_to_limit()
        self.assert_parent_notice(parent, state='Working')
        self.turn_source = server.review_turn_source
        self.send.reset_mock()
        records = [BACKGROUND, ASSISTANT]
        with patch.object(server, 'notification_records', return_value=records):
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
            self.assertTrue(self.links()[1]['paused'])
            records[:] = [ASSISTANT]  # a large tool result pushed the notice out of the tail
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
            self.send.assert_not_called()
            self.assertEqual((self.links()[1]['paused'], self.links()[1]['limit']), (True, 5))
            records[:] = [claude('user', 'Mak starts another task.'), ASSISTANT]
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
        self.assertTrue(self.links()[1]['awaiting'])
        self.assertNotIn('paused', self.links()[1])

    def test_visible_mak_prompt_restarts_with_a_parent_notice_still_pending(self):
        parent = self.add_parent()
        reviewer = dict(id='%14', agent='claude', thread=THREAD, writable=True)
        self.entries.append(reviewer)
        self.states['%14'] = 'Idle'
        review_links.add(worker=parent['id'], reviewer='%14', reviewer_thread=THREAD,
                         name='Parent work', agent='codex')
        child, own = self.links()
        own.update(rounds=5, paused=True, limit=5, grant_rounds=3)
        self.file.write_text(json.dumps([child, own]))
        self.fail_to_limit()
        self.assert_parent_notice(parent)
        self.turn_source = server.review_turn_source
        records = [codex('user', 'Mak starts another task.'), codex('assistant', 'Done.')]
        with patch.object(server, 'notification_records', return_value=records):
            self.observe(**{'%11': 'Working'})
            self.observe(**{'%11': 'Idle'})
        own = self.links()[1]
        self.assertTrue(own['awaiting'])
        self.assertNotIn('limit', own)

    def test_notice_finishes_between_ticks_and_does_not_mask_a_later_long_prompt(self):
        for agent, message, short_mak in (('claude', claude, False), ('claude', claude, True),
                                          ('codex', codex, False), ('codex', codex, True)):
            with self.subTest(agent=agent, short_mak=short_mak):
                self.file.write_text('[]')
                self.add()
                self.entries = [self.worker, self.reviewer]
                self.states.update({'%13': 'Idle', '%12': 'Idle'})
                parent = self.add_parent()
                transcript = self.file.parent / 'parent.jsonl'
                parent.update(agent=agent, transcript=str(transcript))
                self.entries.append(dict(id='%14', agent='codex', thread=THREAD, writable=True))
                self.states['%14'] = 'Idle'
                review_links.add(worker=parent['id'], reviewer='%14', reviewer_thread=THREAD,
                                 name='Parent work', agent=agent)
                child, own = self.links()
                own.update(rounds=5, paused=True, limit=5)
                self.file.write_text(json.dumps([child, own]))
                with own_time(review_links, time=lambda: 1000):
                    self.fail_to_limit()
                notice = self.send.call_args.args[1]
                self.turn_source = server.review_turn_source
                write_records(transcript, [
                    dict(message('user', 'Earlier Mak prompt.'), timestamp='1970-01-01T00:16:39Z'),
                    dict(message('assistant', 'Done.'), timestamp='1970-01-01T00:16:40.500Z'),
                ])
                self.observe()  # Mak's earlier prompt finished after Tether sent the notice
                self.assertIn('review_prompts', self.links()[1])
                write_records(transcript, [
                    message('user', notice),
                    dict(message('assistant', 'Read.'), timestamp='1970-01-01T00:16:41Z'),
                ], 'a')
                if short_mak:
                    write_records(transcript, [
                        dict(message('user', 'Mak asks a short question.'), timestamp='1970-01-01T00:16:42Z'),
                        dict(message('assistant', 'Answered.'), timestamp='1970-01-01T00:16:43Z'),
                    ], 'a')
                self.observe()  # the notice and any short Mak turn ended without a Working tick
                self.assertTrue(self.links()[1]['paused'])
                self.assertNotIn('review_prompts', self.links()[1])
                self.observe(**{'%11': 'Working'})
                reply = dict(message('assistant', 'Done.'), timestamp='1970-01-01T00:16:45Z')
                write_records(transcript, [
                    dict(message('user', 'Mak asks for more work.'), timestamp='1970-01-01T00:16:44Z'),
                    message('assistant', 'x' * LONG_REVIEW_OUTPUT), reply,
                ], 'a')
                self.assertEqual(server.notification_records(parent), [reply])
                self.observe(**{'%11': 'Idle'})
                own = self.links()[1]
                self.assertNotIn('limit', own)
                self.assertTrue(own['awaiting'])
                self.assertNotIn('review_prompts', own)

    def test_clipped_reply_from_before_notice_was_sent_cannot_consume_its_marker(self):
        for agent, message in (('claude', claude), ('codex', codex)):
            with self.subTest(agent=agent):
                self.file.write_text('[]')
                self.add()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                transcript = self.file.parent / 'parent.jsonl'
                parent.update(agent=agent, transcript=str(transcript))
                self.entries.append(dict(id='%14', agent='codex', thread=THREAD, writable=True))
                self.states['%14'] = 'Idle'
                review_links.add(worker=parent['id'], reviewer='%14', reviewer_thread=THREAD,
                                 name='Parent work', agent=agent)
                self.observe(**{'%11': 'Working'})
                self.states['%11'] = 'Idle'  # the old turn ended before the notice was sent
                with own_time(review_links, time=lambda: 1000):
                    self.fail_to_limit()
                own = self.links()[1]
                own.update(seen_working=True)
                self.file.write_text(json.dumps([self.links()[0], own]))
                self.turn_source = server.review_turn_source
                for at, pending in ((999, True), (1001, False)):
                    write_records(transcript, [
                        message('user', 'A prompt beyond the review tail.'),
                        message('assistant', 'x' * CLIPPED_REVIEW_OUTPUT),
                        dict(message('assistant', 'Reply.'), timestamp=datetime.fromtimestamp(at, timezone.utc).isoformat()),
                    ])
                    self.assertEqual(self.turn_source(parent), ('unknown', None, at))
                    self.observe()
                    self.assertEqual(bool(self.links()[1].get('review_prompts')), pending)

    def test_escalate_during_the_tick_stops_the_pending_parent_notice(self):
        parent = self.add_parent()
        self.states[parent['id']] = 'Working'
        self.fail_to_limit()
        self.assertFalse(self.links()[0]['parent_stage']['sent'])
        status = self.status.side_effect

        def escalate_mid_tick(entry):
            if entry['id'] == '%13' and 'parent_stage' in self.links()[0]:
                review_links.escalate('%13', parent['id'], parent['thread'])
            return status(entry)
        self.status.side_effect = escalate_mid_tick
        self.send.reset_mock()
        self.observe(**{parent['id']: 'Idle'})
        self.assertFalse(any(call.args[0] is parent for call in self.send.call_args_list))
        self.observe()
        self.assert_mak_alert()
        self.assertTrue(self.links()[0]['paused'])

    def test_parent_can_also_be_the_reviewer(self):
        self.add_parent(entry=self.reviewer)
        self.fail_to_limit()
        self.observe()  # The parent has finished the final review.
        self.assert_parent_notice(self.reviewer)
        review_links.agent_prompt('%13', self.reviewer['id'], self.reviewer['thread'])
        self.request_review()
        self.assertTrue(self.links()[0]['awaiting'])
        self.alert.assert_not_called()

    def test_empty_round_limit_goes_to_parent_and_grant_stops_at_five_in_all(self):
        for next_mark in ('same tree', 'changed tree'):
            with self.subTest(next_mark=next_mark):
                self.add()
                self.alert.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent()
                self.work_mark = Mock(return_value='same tree')
                self.request_review()
                self.finish_review()
                self.fail_to_limit()
                self.assert_parent_notice(parent)
                self.assertEqual(self.links()[0]['empty_rounds'], 3)
                self.assertEqual(self.links()[0]['rounds'], 1)
                self.alert.assert_not_called()
                review_links.agent_prompt('%13', parent['id'], parent['thread'])
                self.work_mark.side_effect = [next_mark]
                self.request_review()  # 4 failed reviews so far: the grant leaves one under the cap of 5
                self.finish_review(FINDINGS)
                self.assert_mak_stop(rounds=5)

    def test_parent_stage_is_skipped_when_no_round_is_left_under_the_total_cap(self):
        for level in (3, 4, 5):
            with self.subTest(level=level):
                self.file.write_text('[]')
                self.add()
                self.alert.reset_mock()
                self.entries = [self.worker, self.reviewer]
                parent = self.add_parent(level=level)
                self.work_mark = Mock(return_value='same tree')
                self.request_review()
                links = self.links()
                links[0].update(rounds=2, empty_rounds=2)  # 4 failed reviews before this one
                self.file.write_text(json.dumps(links))
                self.finish_review(FINDINGS)  # the 5th failed review in all
                self.assert_mak_stop(rounds=3 if level == 3 else 5)
                self.assertFalse(any(call.args[0] is parent for call in self.send.call_args_list))

    def test_stored_limit_is_clamped_for_both_counters(self):
        for key in ('rounds', 'empty_rounds'):
            with self.subTest(key=key):
                self.assertTrue(review_links._counts_at_limit({'limit': 99}, {key: 5}))
                self.assertTrue(review_links._counts_at_limit({'limit': 99}, {key: 4}, key))
                self.assertFalse(review_links._counts_at_limit({'limit': 99}, {key: 4}))

    def test_escalation_alerts_on_next_tick_and_keeps_review_paused(self):
        parent = self.add_parent()
        self.fail_to_limit()
        self.assertEqual(review_links.escalated([self.worker, self.reviewer]), set(), 'the parent tab decides first')
        with self.assertRaisesRegex(ValueError, 'That tab has no review waiting on this tab.'):
            review_links.escalate('%13', '%14', PARENT_THREAD)
        review_links.escalate('%13', parent['id'], parent['thread'])
        self.assertEqual(review_links.escalated([self.worker, self.reviewer]), {'%13'})
        self.alert.assert_not_called()
        self.request_review()
        self.observe()
        self.assert_mak_alert()
        link, = self.links()
        self.assertTrue(link['paused'])
        self.assertNotIn('parent_stage', link)
        self.assertNotIn('escalate', link)
        # The alert is sent, but the review still waits on Mak: the helper tab stays shown until he answers.
        self.assertEqual(review_links.escalated([self.worker, self.reviewer]), {'%13'})

    def test_third_failed_review_asks_mak_with_the_findings_instead_of_a_third_fix(self):
        self.assertEqual(review_links.MAX_ROUNDS, 3)
        self.fail_to_limit()
        reviews = [call for call in self.send.call_args_list if call.args[0] == self.reviewer]
        to_worker = [call.args[1] for call in self.send.call_args_list if call.args[0] == self.worker]
        self.assertEqual(len(reviews), review_links.MAX_ROUNDS)
        self.assertEqual(len(to_worker), review_links.MAX_ROUNDS)
        fixes = [text for text in to_worker if 'Fix what it raises' in text]
        self.assertEqual(len(fixes), review_links.MAX_ROUNDS - 1)
        ask = to_worker[-1]
        self.assertNotIn('Fix what it raises', ask)
        self.assertIn(f'Stopped after {review_links.MAX_ROUNDS} review rounds', ask)
        self.assertIn('Still broken: the lock is not released.', ask)
        self.assertIn('Do not fix them in this turn.', ask)
        self.assertTrue(ask.rstrip().endswith('then end with "Needs you: yes".'))
        link = self.links()[0]
        self.assertTrue(link['paused'])
        self.assertFalse(link['awaiting'])
        self.assertEqual(link['rounds'], review_links.MAX_ROUNDS)
        self.alert.assert_called_once()
        title, body, session = self.alert.call_args.args
        self.assertEqual((title, session), ('Review needs you', '%13'))
        self.assertIn('Still broken: the lock is not released.', body)
        self.assertIn(f'after {review_links.MAX_ROUNDS} rounds', body)
        # The worker answers the ask (a turn caused by Tether, not a new task): no fourth review.
        self.request_review()
        self.observe()
        self.assertEqual(self.send.call_count, 2 * review_links.MAX_ROUNDS)
        self.assertTrue(self.links()[0]['paused'])
        self.assertEqual(review_links.paused(), {'%13': ['%12']})

    def test_an_approval_in_round_three_still_completes_the_review(self):
        for _ in range(review_links.MAX_ROUNDS - 1):
            self.request_review()
            self.finish_review()
        self.request_review()
        self.finish_review('APPROVED')
        self.assertIn('Approved by the reviewer tab', self.send.call_args.args[1])
        self.assertNotIn('paused', self.links()[0])
        self.alert.assert_not_called()

    def test_a_prompt_from_mak_restarts_a_paused_review(self):
        self.turn_source = Mock(return_value='user')
        self.fail_to_limit()
        self.request_review()  # the ask turn
        count = self.send.call_count
        self.request_review()  # Mak answers in the worker tab
        self.assertNotIn('rounds', self.links()[0])
        self.assertNotIn('paused', self.links()[0])
        self.assertEqual(review_links.paused(), {})
        self.assertEqual(self.send.call_count, count + 1)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertTrue(self.links()[0]['awaiting'])
        self.turn_source.assert_called_with(self.worker)

    def test_background_turns_do_not_restart_or_reset_rounds(self):
        # A Monitor event, a finished background shell or a scheduled job is not a new task from Mak.
        self.turn_source = Mock(return_value='background')
        self.request_review()
        self.finish_review()
        self.request_review()  # fix 1, reviewed
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 2)
        self.request_review()  # fix 2 (a feedback turn)
        self.finish_review()  # third failed review: ask Mak
        self.assertTrue(self.links()[0]['paused'])
        self.request_review()  # the ask turn
        count = self.send.call_count
        for _ in range(3):
            self.request_review()  # background turns
        self.assertEqual(self.send.call_count, count)
        self.assertTrue(self.links()[0]['paused'])
        self.assertEqual(self.links()[0]['rounds'], review_links.MAX_ROUNDS)

    def test_background_turn_below_the_limit_keeps_the_round_count(self):
        self.turn_source = Mock(return_value='background')
        self.request_review()
        self.finish_review()
        self.request_review()  # fix 1
        self.finish_review('APPROVED')  # forwarded; resets the count
        self.turn_source.return_value = 'review-feedback'
        self.request_review()  # approval turn
        self.turn_source.return_value = 'background'
        self.request_review()  # a background turn: reviewed, counts from zero
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.request_review()  # fix
        self.request_review()  # background turn while rounds is 1
        self.assertEqual(self.links()[0]['rounds'], 1)

    def test_a_prompt_from_another_agent_does_not_restart_a_paused_review(self):
        # An orchestrator tab that sends "continue" is not Mak: the review stays paused until Mak answers.
        self.turn_source = Mock(return_value='user')
        self.fail_to_limit()
        self.request_review()  # the ask turn
        count = self.send.call_count
        review_links.agent_prompt('%13')
        self.assertIsInstance(self.links()[0]['agent_prompt'], float)
        self.request_review()
        self.assertEqual(self.send.call_count, count)
        self.assertTrue(self.links()[0]['paused'])
        self.assertNotIn('agent_prompt', self.links()[0])
        self.request_review()  # then Mak answers
        self.assertNotIn('paused', self.links()[0])
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_agent_prompt_marks_only_links_of_that_worker(self):
        self.add(worker='%20', reviewer='%21')
        self.assertTrue(review_links.agent_prompt('%20'))
        self.assertFalse(review_links.agent_prompt('%99'))
        marks = {link['worker']: 'agent_prompt' in link for link in self.links()}
        self.assertEqual(marks, {'%13': False, '%20': True})

    def test_an_old_agent_prompt_does_not_block_maks_restart(self):
        self.turn_source = Mock(return_value='user')
        self.fail_to_limit()
        self.request_review()  # the ask turn
        review_links.agent_prompt('%13')
        link, = self.links()
        link['agent_prompt'] -= review_links.AGENT_PROMPT_TTL + 1
        self.file.write_text(json.dumps([link]))
        self.request_review()  # Mak answers much later
        self.assertNotIn('paused', self.links()[0])
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_an_agent_turn_longer_than_the_ttl_still_does_not_restart(self):
        self.turn_source = Mock(return_value='user')
        self.fail_to_limit()
        self.request_review()  # the ask turn
        count = self.send.call_count
        review_links.agent_prompt('%13')
        self.observe(**{'%13': 'Working'})
        later = time.time() + review_links.AGENT_PROMPT_TTL + 60
        with patch.object(review_links.time, 'time', return_value=later):
            self.observe(**{'%13': 'Working'})
            self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, count)
        self.assertTrue(self.links()[0]['paused'])
        self.assertEqual(self.links()[0]['rounds'], review_links.MAX_ROUNDS)

    def test_an_agent_prompt_during_a_running_turn_is_saved_on_its_snapshot(self):
        self.turn_source = Mock(return_value='user')
        self.fail_to_limit()
        self.request_review()  # the ask turn
        count = self.send.call_count
        self.observe(**{'%13': 'Working'})  # the snapshot is saved before the agent prompt arrives
        self.assertNotIn('agent', self.links()[0]['restart'])
        review_links.agent_prompt('%13')
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Working'})
        self.assertTrue(self.links()[0]['restart']['agent'])
        later = time.time() + review_links.AGENT_PROMPT_TTL + 60
        with patch.object(review_links.time, 'time', return_value=later):
            self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, count)
        self.assertTrue(self.links()[0]['paused'])

    def test_limit_reached_during_maks_turn_counts_only_that_turn(self):
        # Mak starts a new task at two failed rounds; the third failed review lands during that turn.
        self.turn_source = Mock(return_value='user')
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                self.add()
                self.worker['agent'] = agent
                self.states.update({'%13': 'Idle', '%12': 'Idle'})
                for _ in range(review_links.MAX_ROUNDS - 1):
                    self.request_review()
                    self.finish_review()
                self.request_review()  # fix 2: review 3 requested
                self.assertTrue(self.links()[0]['awaiting'])
                self.observe(**{'%13': 'Working'})  # Mak's new prompt
                self.finish_review()  # review 3 fails during that turn
                if agent == 'claude':  # a busy Claude tab takes the ask at once, so the link is paused mid-turn
                    self.assertTrue(self.links()[0]['paused'])
                self.observe(**{'%13': 'Idle'})
                self.observe()
                link = self.links()[0]
                self.assertEqual(link.get('rounds', 0), 1)
                self.assertNotIn('paused', link)
                self.assertEqual(review_links.paused(), {})

    def test_approval_before_limit_allows_feedback_on_new_work(self):
        for round_number in range(1, review_links.MAX_ROUNDS):
            self.request_review()
            self.finish_review()
            self.assertEqual(self.links()[0]['rounds'], round_number)
        self.request_review()
        self.finish_review('APPROVED')
        self.assertNotIn('rounds', self.links()[0])
        self.assertNotIn('paused', self.links()[0])
        self.request_review()  # the approval follow-up needs no review
        self.assertNotIn('approved_turn', self.links()[0])
        self.request_review()
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertEqual(self.send.call_count, 2 * review_links.MAX_ROUNDS + 2)

    def test_stored_awaiting_link_at_limit_pauses_without_feedback(self):
        self.request_review()
        link, = self.links()
        link['rounds'] = review_links.MAX_ROUNDS
        self.file.write_text(json.dumps([link]))
        self.send.reset_mock()
        self.finish_review()
        self.send.assert_not_called()
        link, = self.links()
        self.assertTrue(link['paused'])
        self.assertFalse(link['awaiting'])
        self.assertEqual(link['rounds'], review_links.MAX_ROUNDS)
        for key in ('wait_ticks', 'idle_ticks', 'reviewer_before', 'reviewer_seen_working', 'retries'):
            self.assertNotIn(key, link)

    def test_single_reviewer_approval_clears_rounds_below_the_cap(self):
        self.request_review()
        link, = self.links()
        link.update(rounds=review_links.MAX_ROUNDS - 1)
        self.file.write_text(json.dumps([link]))
        self.finish_review('APPROVED')
        self.assertNotIn('rounds', self.links()[0])
        self.assertNotIn('paused', self.links()[0])

    def test_feedback_turn_consumes_marker_once_and_does_not_reset_rounds(self):
        self.request_review()
        self.finish_review()
        self.observe(**{'%13': 'Working'})
        self.observe()
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.observe(**{'%13': 'Idle'})
        self.finish_review('APPROVED')
        self.assertNotIn('rounds', self.links()[0])

    def test_awaiting_link_consumes_other_reviewers_feedback_turn(self):
        self.add(reviewer='%21')
        reviewer = dict(self.reviewer, id='%21')
        self.states['%21'] = 'Idle'
        entries = [self.worker, self.reviewer, reviewer]
        self.observe(entries=entries, **{'%13': 'Working'})
        self.observe(entries=entries, **{'%13': 'Idle'})
        rows = self.rows()
        cap = next(row for row in rows if row.get('cap'))
        self.set_counts(cap, '%21', rounds=2)
        self.file.write_text(json.dumps(rows))
        self.observe(entries=entries, **{'%12': 'Working'})
        self.observe(entries=entries, **{'%12': 'Idle'})  # only the first reviewer sends feedback
        self.assertEqual([link['rounds'] for link in self.links()], [1, 2])
        self.observe(entries=entries, **{'%13': 'Working'})
        self.observe(entries=entries)
        self.observe(entries=entries, **{'%13': 'Approval'})
        self.observe(entries=entries, **{'%13': 'Working'})
        self.assertEqual([link['rounds'] for link in self.links()], [1, 2])
        self.assertNotIn('restart', self.links()[1])
        self.assertTrue(self.links()[1]['awaiting'])
        self.observe(entries=entries, **{'%13': 'Idle', '%21': 'Working'})
        self.observe(entries=entries, **{'%21': 'Idle'})
        self.assertTrue(self.links()[1]['paused'])
        self.assertEqual([link['rounds'] for link in self.links()], [1, 3])
        self.observe(entries=entries, **{'%13': 'Working'})  # the findings summary turn
        self.observe(entries=entries, **{'%13': 'Idle'})
        self.observe(entries=entries, **{'%13': 'Working'})  # a new user turn
        self.observe(entries=entries, **{'%13': 'Idle'})
        for link in self.links():
            self.assertNotIn('rounds', link)
            self.assertNotIn('paused', link)

    def test_new_user_turn_resets_rounds_even_while_awaiting_review(self):
        self.request_review()
        link, = self.links()
        link.update(rounds=3, paused=True)
        self.file.write_text(json.dumps([link]))
        self.observe(**{'%13': 'Working'})
        self.assertEqual(self.links()[0]['rounds'], 3)  # decided when the turn ends
        self.observe(**{'%13': 'Idle'})
        self.assertNotIn('rounds', self.links()[0])
        self.assertNotIn('paused', self.links()[0])
        self.assertTrue(self.links()[0]['awaiting'])

    def test_old_capped_link_resets_on_new_work(self):
        link, = self.links()
        link['rounds'] = review_links.MAX_ROUNDS
        self.file.write_text(json.dumps([link]))
        self.request_review()
        self.assertNotIn('rounds', self.links()[0])
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 1)

    def test_new_work_resets_all_links_and_not_each_working_tick(self):
        self.add(reviewer='%21')
        reviewer = dict(self.reviewer, id='%21')
        self.states['%21'] = 'Idle'
        self.file.write_text(json.dumps([dict(link, rounds=3, paused=True) for link in self.links()]))
        entries = [self.worker, self.reviewer, reviewer]
        self.observe(entries=entries, **{'%13': 'Working'})
        self.observe(entries=entries)
        self.assertEqual([link['rounds'] for link in self.links()], [3, 3])  # decided when the turn ends
        self.observe(entries=entries, **{'%13': 'Idle'})
        for link in self.links():
            self.assertNotIn('rounds', link)
            self.assertNotIn('paused', link)

    def test_two_reviewers_never_reset_each_others_feedback_rounds(self):
        self.add(reviewer='%21')
        reviewer = dict(self.reviewer, id='%21')
        self.states['%21'] = 'Idle'
        entries = [self.worker, self.reviewer, reviewer]
        turns = []
        def send(entry, text, state):
            self.states[entry['id']] = 'Working'
            turns.append(entry['id'])
        self.send.side_effect = send
        self.observe(entries=entries, **{'%13': 'Working'})
        self.observe(entries=entries, **{'%13': 'Idle'})
        for _ in range(50):
            if not turns:
                break
            pane = turns.pop(0)
            self.observe(entries=entries)
            self.observe(entries=entries, **{pane: 'Idle'})
        self.assertEqual(turns, [], 'automatic review loop did not stop')
        feedback = [call for call in self.send.call_args_list if call.args[0] == self.worker]
        counts = [link['rounds'] for link in self.links()]
        self.assertEqual(sum(counts), len(feedback))
        self.assertEqual(max(counts), review_links.MAX_ROUNDS)
        self.assertTrue(all(link['paused'] for link in self.links()))
        calls = self.send.call_count
        self.observe(entries=entries)
        self.observe(entries=entries)
        self.assertEqual(self.send.call_count, calls)

    def test_feedback_waits_for_worker_idle(self):
        for state in ('Working', 'Approval', 'Input needed', 'Unavailable'):
            with self.subTest(state=state):
                self.add()
                self.send.reset_mock()
                self.states['%12'] = 'Idle'
                self.request_review()
                self.observe(**{'%13': state})
                self.finish_review()
                for _ in range(1000):
                    self.observe()
                self.assertEqual(self.send.call_count, 1)
                self.assertTrue(self.links()[0]['awaiting'])
                self.observe(**{'%13': 'Idle'})
                self.assertEqual(self.send.call_count, 2)
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertEqual(self.links()[0]['rounds'], 1)

    def test_feedback_holds_for_1000_approval_ticks_and_uses_current_reply(self):
        self.request_review()
        self.observe(**{'%13': 'Approval'})
        self.finish_review()
        for _ in range(1000):
            self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0].get('rounds', 0), 0)
        self.reply.return_value = 'Newest feedback.'
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('Newest feedback.', self.send.call_args.args[1])

    def test_feedback_draft_and_runtime_errors_retry_without_counting_a_round(self):
        self.request_review()
        self.send.reset_mock()
        self.send.side_effect = [DraftError('draft'), RuntimeError('unavailable'), None]
        self.finish_review()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0].get('rounds', 0), 0)
        self.assertNotIn('%13', review_links._last_sent)
        self.observe()
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertNotIn('%13', review_links._last_sent)
        self.observe()
        self.assertEqual(review_links._last_sent['%13'], self.clock.return_value)
        self.assertEqual(self.send.call_count, 3)
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.assertNotIn('retries', self.links()[0])

    def test_feedback_retries_1000_failures_then_delivers(self):
        for failure in (DraftError('draft'), RuntimeError('unavailable')):
            with self.subTest(failure=failure):
                self.add()
                self.send.reset_mock(side_effect=True)
                self.request_review()
                self.send.reset_mock()
                self.send.side_effect = failure
                self.finish_review()
                for _ in range(999):
                    self.observe()
                self.assertTrue(self.links()[0]['awaiting'])
                self.assertEqual(self.send.call_count, 1000)
                self.assertEqual(self.links()[0].get('rounds', 0), 0)
                self.send.side_effect = None
                self.reply.return_value = 'Latest feedback.'
                self.observe()
                self.assertFalse(self.links()[0]['awaiting'])
                self.assertEqual(self.links()[0]['rounds'], 1)
                self.assertIn('Latest feedback.', self.send.call_args.args[1])

    def test_old_link_without_review_loop_fields_completes_a_round(self):
        link, = self.links()
        self.assertNotIn('awaiting', link)
        self.assertNotIn('reviewer_seen_working', link)
        self.assertNotIn('rounds', link)
        self.request_review()
        self.finish_review()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.links()[0]['rounds'], 1)

    def test_missing_worker_survives_grace_then_counts_loss(self):
        with own_time(review_links, time=Mock(return_value=1000)):
            self.add()
        original = self.links()
        for now in (1001, 1029):
            with own_time(review_links, time=Mock(return_value=now)):
                self.observe(entries=[self.reviewer])
            self.assertEqual(self.links(), original)
        with own_time(review_links, time=Mock(return_value=1030)):
            self.observe(entries=[self.reviewer])
        self.assertEqual(self.links()[0]['lost_ticks'], 1)
        self.status.assert_not_called()
        self.send.assert_not_called()
        self.reply.assert_not_called()

    def test_missing_reviewer_survives_within_grace(self):
        with own_time(review_links, time=Mock(return_value=1000)):
            self.add()
        original = self.links()
        with own_time(review_links, time=Mock(return_value=1001)):
            review_links.observe([self.worker], self.status, self.send, self.reply)
        self.assertEqual(self.links(), original)
        self.status.assert_not_called()
        self.send.assert_not_called()

    def test_changed_reviewer_thread_counts_loss_even_with_missing_worker(self):
        for entries in ([self.worker, dict(self.reviewer, thread='new-thread')],
                        [dict(self.reviewer, thread='new-thread')]):
            with own_time(review_links, time=Mock(return_value=1000)):
                self.add()
                review_links.observe(entries, self.status, self.send, self.reply)
            self.assertEqual(self.links()[0]['lost_ticks'], 1)
            self.assertEqual(self.links()[0]['lost_reason'], 'reviewer conversation changed')
        self.status.assert_not_called()
        self.send.assert_not_called()
        self.reply.assert_not_called()

    def test_malformed_created_defaults_without_stopping_other_links(self):
        link, = self.links()
        bad = dict(link, worker='%99', created='invalid')
        self.file.write_text(json.dumps([bad, link]))
        self.observe(**{'%13': 'Working'})
        self.assertNotIn('created', self.links()[0])
        self.assertEqual(self.links()[0]['lost_reason'], 'worker tab is gone')
        self.assertTrue(self.links()[1]['seen_working'])

    def test_worker_thread_is_pinned_once_known_and_changes_count_loss(self):
        for thread in (None, 'pending-123-1', 'not-a-uuid', WORKER_THREAD.upper()):
            self.worker['thread'] = thread
            self.observe(**{'%13': 'Working'})
            self.assertIsNone(self.links()[0]['worker_thread'])
        self.worker['thread'] = WORKER_THREAD
        self.observe()
        self.assertEqual(self.links()[0]['worker_thread'], WORKER_THREAD)
        for thread in ('pending-123-2', None, 'not-a-uuid'):
            self.worker['thread'] = thread
            self.observe()
            self.assertEqual(self.links()[0]['worker_thread'], WORKER_THREAD)
        self.worker['thread'] = THREAD
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.links()[0]['lost_ticks'], 1)
        self.assertEqual(self.links()[0]['lost_reason'], 'worker conversation changed')
        self.send.assert_not_called()

    def test_reply_is_cut_to_1500_and_claude_label_is_used(self):
        review_links.add(worker='%13', reviewer='%12', reviewer_thread=THREAD, name='task', agent='claude')
        self.reply.return_value = 'x' * 4001
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        text = self.send.call_args.args[1]
        self.assertIn('The Claude tab “task”', text)
        self.assertIn('\n\n' + 'x' * 1500 + '\n\nTranscript:', text)
        self.assertNotIn('x' * 4001, text)

    def test_bad_link_or_callback_does_not_stop_other_links(self):
        self.file.write_text(json.dumps([{}, *self.links()]))
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()
        with patch.object(review_links, '_read', side_effect=OSError('disk')):
            self.observe()
        with patch.object(review_links, '_write', side_effect=OSError('disk')):
            self.observe(**{'%13': 'Working'})
        self.status.side_effect = RuntimeError('bad pane')
        self.observe()

    def test_callback_can_add_link_without_deadlock_or_lost_update(self):
        self.observe(**{'%13': 'Working'})
        def send(*args):
            self.assertFalse(review_links._lock.locked())
            review_links.add(worker='%20', reviewer='%12', reviewer_thread=THREAD, name='other', agent='claude')
        self.send.side_effect = send
        self.observe(**{'%13': 'Idle'})
        self.assertEqual([link['worker'] for link in self.links()], ['%13', '%20'])
        self.assertFalse(self.links()[0]['seen_working'])

    def test_store_survives_reloading_and_no_change_does_not_write(self):
        self.observe(**{'%13': 'Working'})
        saved = self.file.read_text()
        with patch.object(review_links, '_write') as write:
            self.observe()
        write.assert_not_called()
        self.assertEqual(self.file.read_text(), saved)
        self.observe(**{'%13': 'Idle'})
        self.send.assert_called_once()

    def test_bad_observations_require_30_ticks_and_alert_once(self):
        cases = (
            ([self.reviewer], 'worker tab is gone'),
            ([self.worker], 'reviewer tab is gone'),
            ([self.worker, dict(self.reviewer, thread=WORKER_THREAD)], 'reviewer conversation changed'),
            ([dict(self.worker, thread=THREAD), self.reviewer], 'worker conversation changed'),
        )
        self.assertEqual(review_links.LOSS_TICKS, 30)
        for entries, reason in cases:
            with self.subTest(reason=reason):
                self.add(seen_working=True)
                link, = self.links()
                link.update(created=0, worker_thread=WORKER_THREAD, awaiting=True)
                self.file.write_text(json.dumps([link]))
                self.alert.reset_mock()
                for tick in range(1, 30):
                    self.observe(entries=entries)
                    self.assertEqual(self.links()[0]['lost_ticks'], tick)
                    self.assertEqual(self.links()[0]['lost_reason'], reason)
                    self.alert.assert_not_called()
                with patch('review_links.sys.stderr', new_callable=io.StringIO) as stderr:
                    self.observe(entries=entries)
                self.assertEqual(self.links(), [])
                self.assertEqual(stderr.getvalue(),
                    f'review link removed: worker=%13 reviewer=%12 name=Fix the bug reason={reason}\n')
                self.alert.assert_called_once_with('Review link lost',
                    f'“Fix the bug”: {reason}. Tether no longer reviews this tab.', '%13')
                self.observe(entries=entries)
                self.assertEqual(self.alert.call_count, 1)
        self.status.assert_not_called()
        self.send.assert_not_called()
        self.reply.assert_not_called()

    def test_healthy_observation_resets_loss_without_extra_writes(self):
        link, = self.links()
        link.update(created=0, worker_thread=WORKER_THREAD)
        self.file.write_text(json.dumps([link]))
        for _ in range(29):
            self.observe(entries=[self.reviewer])
        self.observe()
        self.assertNotIn('lost_ticks', self.links()[0])
        self.assertNotIn('lost_reason', self.links()[0])
        with patch.object(review_links, '_write') as write:
            self.observe()
        write.assert_not_called()
        self.observe(entries=[self.reviewer])
        self.assertEqual(self.links()[0]['lost_ticks'], 1)
        self.alert.assert_not_called()

    def test_failed_discovery_does_not_read_or_change_links(self):
        original = self.file.read_text()
        for entries in ([], [{'id': 'saved'}]):
            with patch.object(review_links, '_read') as read:
                self.observe(entries=entries)
            read.assert_not_called()
            self.assertEqual(self.file.read_text(), original)
        self.status.assert_not_called()
        self.send.assert_not_called()
        self.reply.assert_not_called()

    def test_start_wait_expiry_alert_uses_reason_from_step_copy(self):
        self.add(reviewer_thread=None)
        self.reviewer['thread'] = None
        with own_time(review_links, time=Mock(return_value=self.links()[0]['created'] + 301)), \
                patch('review_links.sys.stderr', new_callable=io.StringIO) as stderr:
            self.observe()
        self.assertEqual(self.links(), [])
        self.assertIn('reason=reviewer never started', stderr.getvalue())
        self.alert.assert_called_once_with('Review link lost',
            '“Fix the bug”: reviewer never started. Tether no longer reviews this tab.', '%13')

    def test_pause_alerts_once_and_explicit_remove_does_not_alert(self):
        self.add(seen_working=True)
        link, = self.links()
        link['rounds'] = review_links.MAX_ROUNDS
        self.file.write_text(json.dumps([link]))
        self.observe()
        self.alert.assert_called_once_with('Review paused',
            '“Fix the bug” reached the round limit. Answer in that tab to restart reviews.', '%13')
        self.observe()
        self.assertTrue(review_links.remove('%13'))
        self.observe()
        self.assertEqual(self.alert.call_count, 1)

    def test_alert_failure_keeps_store_changes_and_other_alerts(self):
        link, = self.links()
        link.update(created=0, lost_ticks=29)
        self.file.write_text(json.dumps([link, dict(link, worker='%20')]))
        self.alert.side_effect = RuntimeError('push failed')
        with patch('review_links.sys.stderr', new_callable=io.StringIO):
            self.observe(entries=[self.reviewer])
        self.assertEqual(self.links(), [])
        self.assertEqual(self.alert.call_count, 2)
        self.assertEqual([call.args[2] for call in self.alert.call_args_list], ['%13', '%20'])

    def test_failed_store_write_never_alerts(self):
        link, = self.links()
        link.update(created=0, lost_ticks=29)
        self.file.write_text(json.dumps([link]))
        with patch.object(review_links, '_write', side_effect=OSError('disk')):
            self.observe(entries=[self.reviewer])
        self.assertEqual(self.links(), [link])
        self.alert.assert_not_called()

    def test_approval_followup_is_skipped_then_next_turn_is_reviewed(self):
        self.request_review()
        self.finish_review('APPROVED')
        self.send.reset_mock()
        self.observe()  # approval has not started yet
        self.assertTrue(self.links()[0]['approved_turn'])
        self.request_review()
        self.send.assert_not_called()
        self.assertFalse(self.links()[0]['seen_working'])
        self.assertNotIn('approved_turn', self.links()[0])
        self.assertNotIn('approved_before', self.links()[0])
        self.request_review()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_approval_followup_shorter_than_tick_is_skipped(self):
        for agent in ('codex', 'antigravity'):
            with self.subTest(agent=agent):
                self.add()
                self.worker['agent'] = agent
                self.turn_source = lambda entry: ('review-feedback', None, None)
                self.request_review()
                self.finish_review('APPROVED')
                self.send.reset_mock()
                self.reply.return_value = f'Approved next step is done in {agent}.'
                self.observe()
                self.send.assert_not_called()
                self.assertNotIn('approved_turn', self.links()[0])
                self.assertNotIn('approved_before', self.links()[0])
                self.request_review()
                self.send.assert_called_once()
                self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_approval_retries_draft_and_runtime_errors(self):
        self.request_review()
        self.send.reset_mock()
        self.send.side_effect = [DraftError('draft'), RuntimeError('gone'), None]
        self.finish_review('APPROVED')
        for _ in range(2):
            self.assertTrue(self.links()[0]['awaiting'])
            self.assertNotIn('approved_turn', self.links()[0])
            self.assertNotIn('approved_state', self.links()[0])
            self.assertNotIn('%13', review_links._last_sent)
            self.observe()
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertTrue(self.links()[0]['approved_turn'])
        self.assertEqual(self.send.call_count, 3)

    def test_approval_uses_feedback_delivery_gate(self):
        self.request_review()
        self.observe(**{'%13': 'Approval'})
        self.finish_review('APPROVED')
        self.assertTrue(self.links()[0]['awaiting'])
        self.observe(**{'%13': 'Working'})  # Codex cannot accept a busy send
        self.assertTrue(self.links()[0]['awaiting'])
        self.worker['agent'] = 'claude'
        review_links._last_sent['%13'] = self.clock.return_value
        self.observe(elapsed=5)
        self.assertTrue(self.links()[0]['awaiting'])
        self.observe(elapsed=1)
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertTrue(self.links()[0]['worker_busy_send'])
        self.send.assert_called_with(self.worker,
            '[Tether review] Approved by the reviewer tab (%12):\n\nAPPROVED\n\n'
            'The review is complete. Do the next step your instructions give for an approved review, then stop.',
            'Working')
        self.observe(**{'%13': 'Idle'})
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.assertNotIn('approved_turn', self.links()[0])
        self.request_review()
        self.assertEqual(self.send.call_count, 3)

    def test_empty_turns_use_own_limit_and_alert(self):
        self.work_mark = Mock(return_value='same tree')
        self.request_review()
        self.finish_review()
        self.assertEqual(self.links()[0]['rounds'], 1)
        for count in range(1, review_links.MAX_ROUNDS + 1):
            self.request_review()
            self.assertTrue(self.links()[0]['empty_turn'])
            self.finish_review()
            self.assertEqual(self.links()[0]['rounds'], 1)
            self.assertEqual(self.links()[0]['empty_rounds'], count)
            self.assertNotIn('empty_turn', self.links()[0])
        self.assertTrue(self.links()[0]['paused'])  # the third empty review asked Mak
        self.request_review()  # the ask turn: no review
        self.assertEqual(self.work_mark.call_count, 4)
        self.alert.assert_called_once()
        self.assertEqual(self.alert.call_args.args[0], 'Review needs you')
        self.observe()
        self.assertEqual(self.alert.call_count, 1)
        self.observe(**{'%13': 'Working'})  # a new prompt from Mak resets both limits when its turn ends
        self.assertEqual(self.links()[0]['empty_rounds'], review_links.MAX_ROUNDS)
        self.observe(**{'%13': 'Idle'})
        self.assertNotIn('rounds', self.links()[0])
        self.assertNotIn('empty_rounds', self.links()[0])
        self.assertEqual(self.links()[0]['work_mark'], 'same tree')

    def test_changed_or_unknown_mark_counts_as_work(self):
        for mark in ('new tree', None):
            with self.subTest(mark=mark):
                self.add()
                self.work_mark = Mock(return_value='old tree')
                self.request_review()
                self.finish_review()
                self.work_mark.return_value = mark
                self.request_review()
                self.assertNotIn('empty_turn', self.links()[0])
                self.assertEqual(self.links()[0]['work_mark'], mark)
                self.finish_review()
                self.assertEqual(self.links()[0]['rounds'], 2)
                self.assertNotIn('empty_rounds', self.links()[0])

    def test_work_mark_is_only_stored_on_successful_request(self):
        self.work_mark = Mock(return_value='tree')
        self.observe(**{'%13': 'Working'})
        self.work_mark.assert_not_called()
        self.observe(**{'%13': 'Idle', '%12': 'Approval'})
        self.work_mark.assert_not_called()
        self.send.side_effect = DraftError('draft')
        self.observe(**{'%12': 'Idle'})
        self.work_mark.assert_called_once_with(self.worker)
        self.assertNotIn('work_mark', self.links()[0])
        self.send.side_effect = None
        self.observe()
        self.assertEqual(self.links()[0]['work_mark'], 'tree')
        self.finish_review()
        self.assertEqual(self.work_mark.call_count, 2)

    def test_awaiting_empty_round_limit_pauses_and_late_approval_keeps_it_paused(self):
        self.request_review()
        link, = self.links()
        link.update(empty_rounds=review_links.MAX_ROUNDS)
        self.file.write_text(json.dumps([link]))
        self.finish_review()
        self.assertTrue(self.links()[0]['paused'])
        self.assertEqual(self.alert.call_count, 1)
        link, = self.links()
        link.update(awaiting=True, reviewer_seen_working=True, rounds=2, work_mark='tree')
        self.file.write_text(json.dumps([link]))
        self.send.reset_mock()
        self.finish_review('APPROVED')
        self.send.assert_called_once()
        self.assertIn('Approved by the reviewer tab', self.send.call_args.args[1])
        link, = self.links()
        self.assertFalse(set(review_links.COUNTS) & link.keys())
        self.assertTrue(link['paused'])
        self.assertEqual(link['work_mark'], 'tree')
        self.assertEqual(self.alert.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'Mak or the parent'):
            review_links.check_cap('%13', WORKER_THREAD)

    def test_notice_monitor_fake_pane_tick_preserves_seeded_link(self):
        isolate_notice_monitor(self)
        link, = self.links()
        link['created'] = 0
        self.file.write_text(json.dumps([link]))
        entry = {'id': '%1', 'agent': 'claude', 'thread': 'conversation', 'owner_pid': 1, 'writable': True}
        with patch.object(server, 'sessions', return_value=[entry]), \
                patch.object(server, 'live_status', return_value={'state': 'Idle'}), \
                own_time(server, sleep=Mock(side_effect=StopIteration)):
            with self.assertRaises(StopIteration):
                server.monitor_notices()
        self.assertEqual(self.links(), [dict(link, lost_ticks=1, lost_reason='worker tab is gone')])

    # B205: reviews bound to a commit

    HASH = '1f0e' * 10
    NEXT = '2a9d' * 10

    def bind(self, commit=None, *, dirty=False, rules=True, mark='worktree fingerprint', **scope_fields):
        """The worker's folder is at `commit`. The request and the approval check read it from here."""
        self.head = commit or self.HASH
        self.scope = lambda entry: {'commit': self.head, 'dirty': dirty, 'folder': '/work/my tree', 'rules': rules,
                                    'mark': mark, **scope_fields}
        self.commit = lambda entry: self.head
        # As in the server: the reviewer's reply is read at the transcript anchor of the request.
        self.anchored_reply = None
        self.review_reply = lambda entry, request_id: self.anchored_reply

    def test_request_names_the_commit_the_rules_and_the_approval_form(self):
        self.bind()
        self.request_review()
        text = self.send.call_args.args[1]
        request_id = self.links()[0]['request_id']
        self.assertIn(f"Review id: {request_id}. Commit under review: {self.HASH} in '/work/my tree'. "
                      "Read the review rules first: `git -C '/work/my tree' show origin/main:REVIEW.md`. "
                      f'An approval counts only for this commit: start it with `APPROVED {self.HASH}` (the full hash). '
                      'Its last reply:\n\nI fixed it.', text)
        self.assertEqual(self.links()[0]['review_commit'], self.HASH)
        self.assertTrue(self.links()[0]['review_clean'])
        self.assertEqual(re.match(REVIEW_REQUEST, text).groups()[:5],
                         ('Codex', 'Fix the bug', '%13', 'I fixed it.', '/project/worker.jsonl'))

    def test_request_lists_checklist_rows_and_still_matches_the_review_card(self):
        self.bind(checklist={'status': 'hit', 'rows': [
            {'id': 'R3', 'title': 'Launch tabs', 'check': 'Start a tab.\r\nCheck the prompt.', 'incident': 'B188'},
            {'id': 'R4', 'title': 'Check Its last reply', 'check': 'Keep it in the card.', 'incident': 'B205'},
        ]}, ui=['ui/index.html'])
        self.request_review()
        text = self.send.call_args.args[1]
        self.assertIn('Reliability checklist: this diff hits 2 row(s) of origin/main:docs/reliability-checklist.md. '
                      'Confirm each check in your reply:\n'
                      '- R3 Launch tabs: Start a tab. Check the prompt. (Incident: B188)\n', text)
        self.assertIn('- R4 Check Its last reply\\: Keep it in the card. (Incident: B205)\n'
                      'This diff changes the UI (1 file(s), for example ui/index.html). ', text)
        self.assertIn('check them against the claims.\nIts last reply:', text)
        self.assertEqual(re.match(REVIEW_REQUEST, text).groups()[:5],
                         ('Codex', 'Fix the bug', '%13', 'I fixed it.', '/project/worker.jsonl'))

    def test_request_explains_unknown_ui_paths_and_omits_empty_or_missing_lists(self):
        sentence = ('Tether could not list the changed UI files. Check the diff for UI changes yourself; '
                    'a UI change needs before and after screenshots at 320 px and 1280 px. ')
        for fields in ({'ui': None}, {'ui': []}, {}):
            with self.subTest(fields=fields):
                self.add()
                self.bind(**fields)
                self.request_review()
                text = self.send.call_args.args[1]
                expected = sentence if fields == {'ui': None} else ''
                self.assertIn(f'`APPROVED {self.HASH}` (the full hash). {expected}Its last reply:', text)

    def test_request_replaces_binding_controls_and_passes_the_terminal_guard(self):
        # The second and third sets: review round 2, finding 5 (direction marks and other format characters).
        for characters in ('\x1b\x00\x7f\u202e', '\t\x85\u2028\u2029\u2066\u2069', '\u200f\u200e\u061c',
                           '\u200b\ufeff\u2060\ue000'):
            with self.subTest(characters=repr(characters)):
                self.add()
                self.bind(folder=f'/work/tree{characters}', ui=[f'ui/src/App{characters}.tsx'],
                          checklist={'status': 'hit', 'rows': [
                              dict(id='R1', title=f'Safe{characters} title', check='Check', incident='B1'),
                          ]})
                self.request_review()
                text = self.send.call_args.args[1]
                replaced = '?' * len(characters)
                self.assertIn(f"'/work/tree{replaced}'", text)
                self.assertIn(f'Safe{replaced} title', text)
                self.assertIn(f'ui/src/App{replaced}.tsx', text)
                self.assertTrue(all(character not in text for character in characters))
                self.assertEqual(re.match(REVIEW_REQUEST, text).groups()[:5],
                                 ('Codex', 'Fix the bug', '%13', 'I fixed it.', '/project/worker.jsonl'))
                run = Mock(side_effect=RuntimeError('Reached mocked tmux'))
                with self.assertRaisesRegex(RuntimeError, 'Reached mocked tmux'):
                    terminal_input.submit(self.reviewer, text, run=run)

    def test_request_caps_checklist_rows_and_line_length(self):
        rows = [dict(id=f'R{i}', title='Title', check='x' * 400, incident='B10') for i in range(1, 11)]
        self.bind(checklist={'status': 'hit', 'rows': rows})
        self.request_review()
        text = self.send.call_args.args[1]
        self.assertIn('hits 10 row(s)', text)
        lines = [line for line in text.splitlines() if line.startswith('- R')]
        self.assertEqual([line.split()[1] for line in lines], [f'R{i}' for i in range(1, 9)])
        self.assertTrue(all(len(line) == 400 and line.endswith('…') for line in lines))
        self.assertIn("- and 2 more: run python3 scripts/checklist.py --repo '/work/my tree'\nIts last reply:", text)

    def test_request_explains_an_unchecked_checklist_before_it_asks_for_screenshots(self):
        reason = 'x' * 250
        self.bind(checklist={'status': 'unchecked', 'reason': 'timed out\n' + reason},
                  ui=['server/app.js', 'ui/src/Composer.tsx'])
        self.request_review()
        self.assertIn(f'Reliability checklist: not checked (timed out {reason[:190]}). '
                      'This diff changes the UI (2 file(s), for example server/app.js). '
                      'Do not approve without before and after screenshots at 320 px and 1280 px for each changed '
                      "screen: the worker's reply must name the folder that holds them. Open the screenshots and "
                      'check them against the claims. Its last reply:', self.send.call_args.args[1])

    def test_request_omits_clear_absent_and_malformed_checklist_results(self):
        results = ({'status': 'clear'}, {'status': 'absent'}, 'hit', {'status': []},
                   {'status': 'hit', 'rows': [{}]}, {'status': 'unchecked', 'reason': 42},
                   {'status': 'hit', 'rows': [dict(id='R1', title=' ', check='Check', incident='B1')]})
        for result in results:
            with self.subTest(result=result):
                self.add()
                self.bind(checklist=result, ui=[])
                self.send.reset_mock()
                self.request_review()
                self.send.assert_called_once()
                self.assertIn(f'`APPROVED {self.HASH}` (the full hash). Its last reply:', self.send.call_args.args[1])

    def test_request_says_uncommitted_changes_and_omits_rules_the_repository_does_not_have(self):
        self.bind(dirty=True, rules=False)
        self.request_review()
        text = self.send.call_args.args[1]
        self.assertIn(f"Commit under review: {self.HASH} in '/work/my tree' plus uncommitted changes. An approval", text)
        self.assertNotIn('REVIEW.md', text)
        self.assertFalse(self.links()[0]['review_clean'])

    def test_unreadable_folder_sends_the_unbound_request_and_forwards_approval_as_before(self):
        for scope in (lambda entry: None, Mock(side_effect=OSError('git')), lambda entry: {'dirty': False}):
            with self.subTest(scope=scope):
                self.add()
                self.send.reset_mock()
                self.scope = scope
                self.commit = Mock(return_value=None)
                self.request_review()
                self.assertNotIn('Commit under review', self.send.call_args.args[1])
                self.assertNotIn('review_commit', self.links()[0])
                self.finish_review('APPROVED')
                self.send.assert_called_with(self.worker,
                    '[Tether review] Approved by the reviewer tab (%12):\n\nAPPROVED\n\n'
                    'The review is complete. Do the next step your instructions give for an approved review, then stop.',
                    'Idle')
                self.assertNotIn('approved_commit', self.links()[0])
                self.commit.assert_not_called()  # no commit named, none to compare

    def test_unbound_approval_that_names_a_commit_the_tab_is_not_at_is_not_forwarded(self):
        # Review round 2, finding 1: the request was not bound, git answers again, the reply approves an old commit.
        self.scope = lambda entry: None
        self.head = self.NEXT
        self.commit = lambda entry: self.head
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.links()[0]['stale_commit'], self.HASH)
        self.observe()
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f'Review id: {self.links()[0]["request_id"]}. Your approval of {self.HASH[:12]} was not '
                      'forwarded: this tab is not at that commit. Its last reply:', self.send.call_args.args[1])
        self.finish_review(f'APPROVED {self.NEXT}')  # names the commit the tab is at: forwarded, not recorded
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertNotIn('approved_commit', self.links()[0])

    def test_approval_of_the_current_clean_commit_is_forwarded_and_recorded(self):
        for reply in (f'APPROVED {self.HASH}', f'APPROVED `{self.HASH}`: fine',
                      f'Approved for commit {self.HASH.upper()}. Tests pass.'):
            with self.subTest(reply=reply):
                self.add()
                self.send.reset_mock()
                self.bind()
                self.request_review()
                with patch.object(review_links.time, 'time', return_value=1700000000.9):
                    self.finish_review(reply)
                self.send.assert_called_with(self.worker,
                    f'[Tether review] Approved by the reviewer tab (%12) for commit {self.HASH}:\n\n'
                    + reply + f'\n\nThe review is complete. The approval is for commit {self.HASH} only. '
                    "Before you push, deploy or merge, check that HEAD in '/work/my tree' "
                    f"(`git -C '/work/my tree' rev-parse HEAD`) is {self.HASH}. "
                    'If HEAD is different or you cannot read it, do not act on this approval. '
                    'Stop and wait for the next review. If HEAD is that hash, do the next step your instructions '
                    'give for an approved review, then stop.', 'Idle')
                self.assertEqual(review_links.prompt_source(self.send.call_args.args[1]), 'review-feedback')
                link = self.links()[0]
                self.assertEqual((link['approved_commit'], link['approved_at']), (self.HASH, 1700000000))
                self.assertEqual(review_links.approved_commits(),
                                 {'%13': {'%12': {'commit': self.HASH, 'at': 1700000000}}})

    def test_clean_approval_uses_the_worktree_when_the_bound_folder_is_missing(self):
        commit = '1f0e' * 16
        self.bind(commit, folder=None)
        self.request_review()
        self.scope = lambda entry: {'commit': commit, 'dirty': False, 'folder': '/work/a different tree'}
        self.finish_review(f'APPROVED {commit}')
        text = self.send.call_args.args[1]
        self.assertIn(f'for commit {commit}:', text)
        self.assertIn(f"HEAD in this tab's worktree (`git rev-parse HEAD`) is {commit}.", text)
        self.assertNotIn('/work/a different tree', text)
        self.assertEqual(review_links.prompt_source(text), 'review-feedback')

    def test_approval_of_uncommitted_work_is_forwarded_but_not_recorded_as_an_approved_commit(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.links()[0]['approved_commit'], self.HASH)
        self.bind(dirty=True)
        self.request_review()  # an edit after the approval requests another review
        self.finish_review(f'APPROVED {self.HASH}')
        text = self.send.call_args.args[1]
        self.assertIn(f'(%12) for commit {self.HASH} and its uncommitted changes:', text)
        self.assertIn('Tether asks the reviewer to confirm that commit.', text)
        self.assertNotIn('rev-parse HEAD', text)
        self.assertEqual(review_links.prompt_source(text), 'review-feedback')
        self.assertNotIn('approved_commit', self.links()[0])
        self.assertEqual(review_links.approved_commits(), {})

    def test_approval_for_an_older_commit_is_not_forwarded_and_the_new_commit_is_reviewed(self):
        self.bind()
        self.request_review()
        self.head = self.NEXT  # the worker committed while the reviewer read the old commit
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_count, 1)  # nothing reached the worker
        link = self.links()[0]
        self.assertEqual((link['stale_commit'], link['stale_rounds']), (self.HASH, 1))
        self.assertFalse(link['awaiting'])
        self.assertNotIn('approved_turn', link)
        self.assertNotIn('approved_commit', link)
        self.assertNotIn('rounds', link)
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])
        self.assertIn(f'Your approval of {self.HASH[:12]} was not forwarded: this tab is not at that commit. '
                      'Its last reply:', self.send.call_args.args[1])
        link = self.links()[0]
        self.assertEqual(link['review_commit'], self.NEXT)
        self.assertNotIn('stale_commit', link)
        self.finish_review(f'APPROVED {self.NEXT}')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn(f'for commit {self.NEXT}:', self.send.call_args.args[1])
        self.assertEqual(self.links()[0]['approved_commit'], self.NEXT)
        self.assertNotIn('stale_rounds', self.links()[0])

    def test_approval_that_names_another_commit_is_not_forwarded(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.NEXT}: looks right')  # the tab never left HASH
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.links()[0]['stale_commit'], self.NEXT)
        self.observe()
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f'Your approval of {self.NEXT[:12]} was not forwarded', self.send.call_args.args[1])

    def test_findings_for_an_older_commit_still_reach_the_worker(self):
        self.bind()
        self.request_review()
        self.head = self.NEXT
        self.finish_review('The lock is not released.')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('Feedback from the reviewer tab (%12)', self.send.call_args.args[1])
        self.assertEqual(self.links()[0]['rounds'], 1)

    def test_approval_must_name_the_commit_right_after_approved(self):
        # Review round 1, findings 1 and 3: a wrong hash of letters only, a hash later in the line, or no hash.
        # Review round 6, finding 3: a short hash can match two commits, so only the full hash names the commit.
        short = 'Your approval was not forwarded: it did not name the full commit hash right after APPROVED. '
        cases = {'APPROVED deadbeefcafe': short, f'APPROVED {self.HASH[:12]}': short, f'APPROVED {self.HASH[:39]}': short,
                 f'APPROVED {self.NEXT}': f'Your approval of {self.NEXT[:12]} was not forwarded: this tab is not at that commit. ',
                 f'APPROVED for the older commit {self.NEXT[:12]}':
                     'Your approval was not forwarded: it did not name the full commit hash right after APPROVED. ',
                 'APPROVED': 'Your approval was not forwarded: it did not name the full commit hash right after APPROVED. ',
                 f'APPROVED. I read {self.HASH[:12]}.':
                     'Your approval was not forwarded: it did not name the full commit hash right after APPROVED. '}
        for reply, note in cases.items():
            with self.subTest(reply=reply):
                self.add()
                self.send.reset_mock()
                self.bind()
                self.request_review()
                self.finish_review(reply)
                self.assertEqual(self.send.call_count, 1)  # nothing reached the worker
                self.assertNotIn('approved_commit', self.links()[0])
                self.assertEqual(self.links()[0]['stale_rounds'], 1)
                self.observe()
                self.assertEqual(self.send.call_args.args[0], self.reviewer)
                self.assertIn(note + 'Its last reply:', self.send.call_args.args[1])

    def test_latest_reply_of_a_stalled_bound_review_needs_the_commit_and_is_never_commit_proof(self):
        # Review round 1, finding 3 and round 2, finding 2: "Send latest reply" has no transcript anchor. The reply
        # may answer an earlier request (for example a review of the same commit with uncommitted changes).
        for reply, forwarded in (('APPROVED', False), (f'APPROVED {self.HASH}', True)):
            with self.subTest(reply=reply):
                self.add()
                self.bind()
                self.reply.return_value = reply
                request_id = self.stall_review()
                self.assertEqual(self.links()[0]['review_commit'], self.HASH)
                self.assertTrue(self.links()[0]['review_clean'])
                review_links.resume_stalled('%13', '%12', request_id, 'latest',
                                            last_reply=self.reply, reviewer_entry=self.reviewer)
                self.send.reset_mock()
                self.observe(**{'%12': 'Idle'})
                self.assertNotIn('approved_commit', self.links()[0])
                self.assertEqual(review_links.approved_commits(), {})
                if forwarded:
                    self.assertEqual(self.send.call_args.args[0], self.worker)
                    self.assertIn(f'for commit {self.HASH}:', self.send.call_args.args[1])
                else:
                    self.send.assert_not_called()
                    self.assertEqual(self.links()[0]['stale_commit'], '')

    def test_bound_approval_waits_until_a_working_claude_worker_is_idle(self):
        # Review round 1, finding 4: a working tab can commit before it reads a queued approval.
        self.worker['agent'] = 'claude'
        self.bind()
        self.request_review()
        self.commit = Mock(side_effect=lambda entry: self.head)
        self.reply.return_value = self.anchored_reply = f'APPROVED {self.HASH}'
        self.observe(**{'%12': 'Working'})
        self.observe(**{'%12': 'Idle', '%13': 'Working'})
        self.observe()
        self.assertEqual(self.send.call_count, 1)  # the approval is not queued into the running turn
        self.commit.assert_not_called()
        self.assertTrue(self.links()[0]['awaiting'])
        self.head = self.NEXT  # the running turn committed
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.links()[0]['stale_commit'], self.HASH)
        self.assertNotIn('approved_commit', self.links()[0])
        self.observe()
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])

    def test_turn_that_starts_during_the_commit_read_holds_the_approval(self):
        # Review round 3, finding 2: idle at the first check, working when the commit read returns.
        self.worker['agent'] = 'claude'
        self.bind()
        self.request_review()
        live = {'state': 'Idle'}
        def read_commit(entry):
            live['state'] = 'Working'  # the user sends a prompt while git runs
            return self.head
        self.commit = read_commit
        self.fresh = lambda entry: dict(live)
        self.finish_review(f'APPROVED {self.HASH}')
        self.observe()
        self.assertEqual(self.send.call_count, 1)  # only the request: the approval waits
        self.assertTrue(self.links()[0]['awaiting'])
        self.assertNotIn('approved_commit', self.links()[0])

    def test_commit_that_moves_before_delivery_gets_a_review_of_the_new_commit(self):
        # Review round 4, finding 3: HEAD moved during the fresh state read. The text for the old commit went out,
        # and the review counted as complete with no review of the new commit.
        self.bind()
        self.request_review()
        request_id = self.links()[0]['request_id']
        def fresh(entry):
            self.head = self.NEXT
            return {'state': 'Idle'}
        self.fresh = fresh
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        link = self.links()[0]
        self.assertNotIn('approved_turn', link)
        self.assertNotIn('approved_commit', link)
        self.assertEqual((link['stale_commit'], link['stale_rounds']), (self.HASH, 1))
        self.assertEqual(self.send.call_count, 2)  # approval and request only
        self.assertEqual(link['stale_correction'],
                         dict(commit=self.HASH, now=self.NEXT, request=request_id, requested=False))
        self.fresh = None
        self.anchored_reply = None
        self.observe(elapsed=review_links.SEND_GAP - 1)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        request = self.send.call_args.args[1]
        self.assertIn(f'Commit under review: {self.NEXT} in', request)
        # Review round 5, finding 1: the approval did reach the tab; the request must not say it was not forwarded.
        self.assertIn(f'Your approval of {self.HASH[:12]} reached the tab after it had moved to another commit, so it '
                      'does not count for the current work. Its last reply:', request)
        self.assertNotIn('was not forwarded', request)
        self.assertNotIn('stale_delivered', self.links()[0])
        self.observe(elapsed=1)
        self.send.assert_called_with(self.worker,
            f'[Tether review] Correction: the approval above is for commit {self.HASH} only. '
            f'This tab is now at {self.NEXT}. Do not push, deploy or merge on that approval. '
            'Tether asked the reviewer to review the current commit.', 'Idle')
        self.assertNotIn('stale_correction', self.links()[0])
        self.assertEqual(self.links()[0]['stale_rounds'], 1)
        self.observe()
        self.assertEqual(self.send.call_count, 4)

    def test_commit_unreadable_after_delivery_asks_for_confirmation(self):
        for failure in (None, subprocess.TimeoutExpired('git', 5)):
            with self.subTest(failure=failure):
                self.add()
                self.send.reset_mock()
                self.bind()
                self.request_review()
                self.commit = Mock(side_effect=[self.HASH, failure])
                self.finish_review(f'APPROVED {self.HASH}')
                self.assertEqual(self.send.call_count, 2)
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertEqual(review_links.approved_commits(), {})

                scope = self.scope
                self.scope = lambda entry: None
                with own_time(review_links, time=lambda: 1000):
                    self.observe()  # the retry waits for a readable scope
                self.assertEqual(self.send.call_count, 3)
                self.send.assert_called_with(self.worker,
                    f'[Tether review] Correction: the approval above is for commit {self.HASH} only. '
                    "Tether could not read this tab's commit after the approval, so the approval cannot be checked. "
                    'Do not push, deploy or merge on that approval. Wait for the next review.', 'Idle')
                self.scope = scope  # git can read the same commit again
                with own_time(review_links, time=lambda: 1002):
                    self.observe()
                self.assertEqual(self.send.call_count, 4)
                self.assertEqual(self.send.call_args.args[0], self.reviewer)
                text = self.send.call_args.args[1]
                self.assertIn(f'Commit under review: {self.HASH} in', text)
                self.assertIn(f'Your approval of {self.HASH[:12]} reached the tab. Tether could not read the tab\'s '
                              'commit after delivery. Review the current commit and confirm it if it passes.', text)
                self.assertNotIn('moved to another commit', text)

    def test_commit_unreadable_after_delivery_at_the_round_limit_asks_the_user(self):
        self.bind()
        self.request_review()
        for _ in range(review_links.MAX_ROUNDS):
            self.commit = Mock(side_effect=[self.HASH, None])
            self.finish_review(f'APPROVED {self.HASH}')
            self.observe()
        self.assertEqual(self.send.call_count, 3 * review_links.MAX_ROUNDS)
        self.assertEqual(self.send.call_args.args[0], self.worker)
        text = self.send.call_args.args[1]
        self.assertTrue(text.endswith('The review is paused: Mak decides.'))
        self.assertEqual(review_links.paused(), {'%13': ['%12']})
        self.alert.assert_called_once_with('Review needs you',
            '“Fix the bug”: 3 approvals in a row could not be verified for the current commit. '
            'Tether could not read the tab\'s commit for the latest approval. Answer in that tab to restart reviews.',
            '%13')
        self.turn_source = lambda entry: (review_links.prompt_source(text), None, None)
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})
        self.assertEqual(self.links()[0]['stale_rounds'], review_links.MAX_ROUNDS)
        self.assertTrue(self.links()[0]['paused'])
        self.assertNotIn('restart', self.links()[0])

    def test_correction_uses_each_workers_send_and_transcript_rules(self):
        for agent in ('claude', 'codex', 'antigravity'):
            with self.subTest(agent=agent):
                self.add(agent=agent)
                self.bind()
                self.worker['agent'] = agent
                self.request_review()
                self.commit = Mock(side_effect=[self.HASH, self.NEXT])
                self.finish_review(f'APPROVED {self.HASH}')
                rows = self.rows()
                cap = next(row for row in rows if row.get('cap'))
                self.set_counts(cap, '%12', rounds=2, stale_rounds=1)
                self.file.write_text(json.dumps(rows))
                approved = self.send.call_count
                self.head = self.NEXT
                self.anchored_reply = None
                self.observe(**{'%13': 'Working'})
                if agent == 'claude':
                    self.assertEqual(self.send.call_count, approved + 1)
                    self.assertEqual(self.send.call_args.args[2], 'Working')
                    self.assertTrue(self.send.call_args.args[1].endswith('Wait for the next review.'))
                else:
                    self.assertEqual(self.send.call_count, approved)
                    self.observe(**{'%13': 'Idle'})
                    self.assertEqual(self.send.call_count, approved + 2)
                    self.assertEqual(self.send.call_args.args[2], 'Idle')
                text = self.send.call_args.args[1]
                entry = self.worker
                if agent == 'claude':
                    records = [{'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': text}},
                               ASSISTANT]
                elif agent == 'codex':
                    records = [codex('user', text), codex('assistant', 'Done')]
                else:
                    records = [
                        dict(type='USER_INPUT', step_index=1, content=f'<USER_REQUEST>{text}</USER_REQUEST>'),
                        dict(type='AGENT_RESPONSE', step_index=2, content='Done'),
                    ]
                with patch.object(server, 'notification_records', return_value=records):
                    self.assertEqual(server.review_turn_source(entry)[0], 'review-correction')
                    self.turn_source = server.review_turn_source
                    self.observe(**{'%13': 'Working'})
                    self.observe(**{'%13': 'Idle'})
                    self.observe()
                    self.turn_source = None
                link = self.links()[0]
                self.assertEqual((link['rounds'], link['stale_rounds']), (2, 1))
                for key in ('stale_correction', 'restart', 'feedback_turn', 'review_prompts'):
                    self.assertNotIn(key, link)
                self.assertEqual(self.send.call_count, approved + 2)

    def test_unmatched_correction_turn_does_not_block_later_reviews(self):
        # Antigravity gives no prompt start time, so a correction turn can end with the source read as 'user'.
        for agent in ('antigravity', 'codex'):
            with self.subTest(agent=agent):
                self.add(agent=agent)
                self.bind()
                self.worker['agent'] = agent
                self.request_review()
                self.commit = Mock(side_effect=[self.HASH, self.NEXT])
                self.finish_review(f'APPROVED {self.HASH}')
                self.head = self.NEXT
                self.anchored_reply = None
                self.observe(elapsed=0)
                self.observe()
                self.assertTrue(self.send.call_args.args[1].startswith('[Tether review] Correction'))
                self.turn_source = lambda entry: ('user', None, None)
                self.observe(**{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.assertFalse(any(marker[0] == 'review-correction'
                                     for marker in self.links()[0].get('review_prompts', ())))
                self.turn_source = None
                self.finish_review('Needs a regression test.')
                before = self.send.call_count
                for state in ('Working', 'Idle', 'Working', 'Idle'):
                    self.observe(**{'%13': state})
                sent = [call.args[0]['id'] for call in self.send.call_args_list[before:]]
                self.assertIn('%12', sent)  # the fix turn is reviewed again

    def test_newer_verdict_retires_a_pending_correction(self):
        for reply in (f'APPROVED {self.NEXT}', 'The race still exists.'):
            with self.subTest(reply=reply):
                self.add()
                self.bind()
                self.request_review()
                self.commit = Mock(side_effect=[self.HASH, self.NEXT])
                self.finish_review(f'APPROVED {self.HASH}')
                self.head = self.NEXT
                self.commit = lambda entry: self.head
                self.observe(elapsed=0)  # send the new request while the worker's send gap still applies
                self.assertTrue(self.links()[0]['stale_correction']['requested'])
                self.anchored_reply = reply
                self.observe()  # both could be sent now; the verdict supersedes the correction
                self.assertEqual(self.send.call_args.args[0], self.worker)
                self.assertIn(reply, self.send.call_args.args[1])
                self.assertNotIn('stale_correction', self.links()[0])
                self.observe()
                self.assertEqual(self.send.call_count, 4)
                self.send.reset_mock()

    def test_pending_correction_send_checks_the_link_after_reading_status(self):
        self.bind()
        self.request_review()
        self.commit = Mock(side_effect=[self.HASH, None])
        self.finish_review(f'APPROVED {self.HASH}')
        self.scope = lambda entry: None

        def stopped(entry):
            # The worker status read for the correction comes after the normal review step.
            if self.status.call_count == 3:
                review_links.remove('%13', '%12')
            return {'state': 'Idle'}

        self.status.reset_mock()
        self.status.side_effect = stopped
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(review_links.reviewed_by(), {})

    def test_overlapping_ticks_cannot_deliver_the_same_correction(self):
        self.bind()
        self.request_review()
        self.commit = Mock(side_effect=[self.HASH, None])
        self.finish_review(f'APPROVED {self.HASH}')
        self.scope = lambda entry: None

        def overlap(entry, text, state):
            self.send.side_effect = None
            self.observe()

        self.send.side_effect = overlap
        self.observe()
        self.assertEqual(self.send.call_count, 3)
        self.assertNotIn('stale_correction', self.links()[0])
        self.assertEqual(self.links()[0]['stale_rounds'], 1)

    def test_sanitizer_drops_malformed_stale_corrections(self):
        valid = dict(commit=self.HASH, now=self.NEXT, request='1234abcd', requested=False)
        row = self.links()[0]
        for correction in ([], dict(valid, now=self.HASH), dict(valid, now=self.NEXT[:12]),
                           dict(valid, commit=123), dict(valid, request='bad'), dict(valid, requested=1),
                           {key: value for key, value in valid.items() if key != 'now'}):
            with self.subTest(correction=correction):
                self.assertNotIn('stale_correction', review_links._sanitize(dict(row, stale_correction=correction)))
        self.assertEqual(review_links._sanitize(dict(row, stale_correction=valid))['stale_correction'], valid)

    def test_unbound_approval_that_names_a_commit_needs_a_readable_commit(self):
        # Review round 5, finding 2: the request was not bound and git cannot read HEAD at the reply.
        self.scope = lambda entry: None
        self.commit = Mock(return_value=None)
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        for _ in range(review_links.VERIFY_TICKS):
            self.observe()
        self.assertTrue(all(call.args[0] == self.reviewer for call in self.send.call_args_list))  # never the worker
        self.assertIn(f'Your approval of {self.HASH[:12]} was not forwarded because Tether could not read the tab\'s '
                      'commit. Check the current commit before approving this review.', self.send.call_args.args[1])
        self.add()
        self.send.reset_mock()
        self.request_review()
        self.finish_review('APPROVED')  # names no commit: forwarded as before
        self.assertEqual(self.send.call_args.args[0], self.worker)

    def test_tree_that_became_dirty_during_the_review_gives_no_proof(self):
        # Review round 5, finding 3: clean at the request, edited during the review, HEAD in place.
        self.bind()
        self.request_review()
        self.scope = lambda entry: {'commit': self.HASH, 'dirty': True, 'folder': '/work/my tree', 'rules': True}
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertNotIn('approved_commit', self.links()[0])
        for unknown in (lambda entry: None, lambda entry: {'commit': self.HASH}, Mock(side_effect=OSError('git'))):
            with self.subTest(scope=unknown):
                self.add()
                self.bind()
                self.request_review()
                self.scope = unknown  # a status that cannot be read is not clean
                self.finish_review(f'APPROVED {self.HASH}')
                self.assertNotIn('approved_commit', self.links()[0])

    def test_one_failed_read_does_not_lose_the_confirmation_of_the_commit(self):
        # Review round 5, finding 4.
        for short_turn in (False, True):
            with self.subTest(short_turn=short_turn):
                self.add()
                self.bind(dirty=True)
                self.request_review()
                self.finish_review(f'APPROVED {self.HASH}')
                count = self.send.call_count
                self.bind(self.NEXT)
                working, fails = self.scope, [True]
                self.scope = lambda entry: None if fails and fails.pop() else working(entry)
                if short_turn:
                    self.reply.return_value = 'Committed.'
                    self.observe()
                else:
                    self.request_review()
                self.assertEqual(self.send.call_count, count)  # the first read failed
                self.assertEqual(self.links()[0]['approved_dirty'], self.HASH)
                self.observe()
                self.observe()
                self.assertEqual(self.send.call_count, count + 1)
                self.assertIn(f'You approved this work as commit {self.HASH[:12]} plus', self.send.call_args.args[1])

    def test_status_timeout_does_not_lose_the_confirmation_of_the_commit(self):
        # Review round 6, finding 1: HEAD was readable and the status was not; that counted as dirty.
        self.bind(dirty=True)
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        count = self.send.call_count
        self.bind(self.NEXT)
        clean, fails = self.scope, [True]
        self.scope = lambda entry: {**clean(entry), 'dirty': None} if fails and fails.pop() else clean(entry)
        self.request_review()
        self.assertEqual(self.send.call_count, count)
        self.assertEqual(self.links()[0]['approved_dirty'], self.HASH)
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, count + 1)
        self.assertIn(f'You approved this work as commit {self.HASH[:12]} plus', self.send.call_args.args[1])

    def test_unknown_status_is_never_clean(self):
        self.bind(dirty=None)
        self.request_review()
        self.assertIn(f"Commit under review: {self.HASH} in '/work/my tree' (Tether could not read the status of its "
                      'files). ', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['review_clean'])
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertNotIn('approved_commit', self.links()[0])

    def test_proof_read_at_another_commit_gives_no_proof(self):
        # The pre-send scope agrees with HEAD. Only the post-send proof read sees a different commit.
        self.bind()
        self.request_review()
        before = self.scope(self.worker)
        self.scope = Mock(side_effect=[before, dict(before, commit=self.NEXT)])
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertNotIn('approved_commit', self.links()[0])

    def test_link_removed_during_the_scope_read_sends_no_request(self):
        # Review round 5, finding 5.
        def scope(entry):
            review_links.remove('%13', '%12')
            return {'commit': self.HASH, 'dirty': False, 'folder': '/work', 'rules': False}
        self.scope = scope
        self.request_review()
        self.observe()
        self.send.assert_not_called()
        self.assertEqual(self.links(), [])

    def test_work_approved_while_uncommitted_is_reviewed_again_when_the_tab_commits_it(self):
        # Agents usually commit after the approval. That commit has no approval, so auto-merge finds nothing.
        self.bind(dirty=True)
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertIn('The review is complete. It covered uncommitted changes, so no commit is approved yet: commit '
                      'the work in this turn, and Tether asks the reviewer to confirm that commit. Do the next step',
                      self.send.call_args.args[1])
        self.assertEqual(self.links()[0]['approved_dirty'], self.HASH)
        self.bind(self.NEXT)  # the follow-up turn commits: a new commit and a clean tree
        self.request_review()
        text = self.send.call_args.args[1]
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f'Commit under review: {self.NEXT} in', text)
        self.assertIn(f'You approved this work as commit {self.HASH[:12]} plus uncommitted changes. The tab has '
                      'committed since: check that this commit holds that work and nothing else. Its last reply:', text)
        self.finish_review(f'APPROVED {self.NEXT}')
        link = self.links()[0]
        self.assertEqual(link['approved_commit'], self.NEXT)
        self.assertNotIn('approved_dirty', link)
        self.assertNotIn('confirm_commit', link)
        self.assertNotIn('uncommitted changes', self.send.call_args.args[1])
        count = self.send.call_count
        self.request_review()  # the follow-up leaves the approved work as it was
        self.assertEqual(self.send.call_count, count)

    def test_clean_approval_then_new_commit_requests_review_on_normal_and_short_turns(self):
        for short in (False, True):
            with self.subTest(short=short):
                self.add()
                self.bind()
                self.request_review()
                self.finish_review(f'APPROVED {self.HASH}')
                count = self.send.call_count
                self.bind(self.NEXT)
                if short:
                    self.reply.return_value = 'Committed.'
                    self.observe()
                    self.observe()
                else:
                    self.request_review()
                self.assertEqual(self.send.call_count, count + 1)
                self.assertEqual(self.send.call_args.args[0], self.reviewer)
                self.assertIn(f'Commit under review: {self.NEXT}', self.send.call_args.args[1])
                self.assertNotIn('You approved this work as commit', self.send.call_args.args[1])

    def test_edit_during_approval_delivery_is_reviewed(self):
        self.bind(mark='before delivery')
        live = self.scope(self.worker)
        self.scope = lambda entry: live
        self.request_review()

        def send(entry, text, state):
            if entry == self.worker:
                live.update(dirty=True, mark='after delivery')

        self.send.side_effect = send
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertNotIn('approved_commit', self.links()[0])
        self.request_review()
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn('plus uncommitted changes', self.send.call_args.args[1])

    def test_late_push_only_turn_checks_scope_once_and_clears_follow_up(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        finish = self.links()[0]['approved_state']['at']
        self.turn_source = Mock(return_value=('user', finish - 1, finish))
        self.scope = Mock(wraps=self.scope)
        for _ in range(review_links.VERIFY_TICKS * 2):
            self.observe()
        self.scope.assert_not_called()
        self.assertTrue(self.links()[0]['approved_turn'])  # kept for a follow-up turn that starts later
        self.assertEqual(self.send.call_count, 2)
        # It finishes between polls, keeps the same reply text, and only pushes.
        self.turn_source.return_value = ('review-feedback', finish + 1, finish + 2)
        self.observe()
        self.observe()
        self.scope.assert_called_once()
        self.assertEqual(self.send.call_count, 2)
        self.assertNotIn('approved_turn', self.links()[0])
        self.assertEqual(self.links()[0]['follow_up_checked_at'], finish + 2)
        self.request_review()  # the next turn is reviewed as usual
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_untimed_review_notice_checks_scope_once_and_keeps_follow_up(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.turn_source = Mock(return_value='review-notice')
        self.reply.return_value = 'Handled a review notice.'
        self.scope = Mock(wraps=self.scope)
        self.observe()
        self.observe()
        self.scope.assert_called_once()
        self.assertTrue(self.links()[0]['approved_turn'])

        self.turn_source.return_value = 'review-feedback'
        self.reply.return_value = 'Pushed the approved commit.'
        self.observe()
        self.assertEqual(self.scope.call_count, 2)
        self.assertNotIn('approved_turn', self.links()[0])
        self.assertEqual(self.send.call_count, 2)

    def test_follow_up_detects_changes_in_the_approved_folder(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        bound = self.scope(self.worker)
        other = dict(bound, commit='c' * 40, folder='/work/other')
        self.scope = lambda entry: bound if entry.get('review_folder') == bound['folder'] else other
        self.request_review()
        self.assertEqual(self.send.call_count, 2)

        # Only the approved folder changed. The request names that folder.
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        bound = dict(bound, commit=self.NEXT)
        other = dict(other, commit=self.HASH)
        self.scope = lambda entry: bound if entry.get('review_folder') == bound['folder'] else other
        self.reviewer['writable'] = False
        self.request_review()
        self.assertEqual(self.send.call_count, 4)
        self.reviewer['writable'] = True
        self.observe()
        self.assertEqual(self.send.call_count, 5)
        self.assertIn(f"Commit under review: {self.NEXT} in '/work/my tree'", self.send.call_args.args[1])

    def test_clean_approval_then_dirty_tree_requests_review(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        count = self.send.call_count
        self.bind(dirty=True)
        self.request_review()
        self.assertEqual(self.send.call_count, count + 1)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f"Commit under review: {self.HASH} in '/work/my tree' plus uncommitted changes",
                      self.send.call_args.args[1])

    def test_dirty_approval_then_further_edit_requests_review(self):
        self.bind(dirty=True, mark='approved tree')
        self.work_mark = Mock(return_value='unchanged empty-turn mark')
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        count = self.send.call_count
        self.bind(dirty=True, mark='edited tree')
        self.request_review()
        self.assertEqual(self.send.call_count, count + 1)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f'Commit under review: {self.HASH}', self.send.call_args.args[1])
        self.assertNotIn('You approved this work as commit', self.send.call_args.args[1])

    def test_saved_approval_with_an_old_mark_still_tracks_the_commit(self):
        for moved in (False, True):
            with self.subTest(moved=moved):
                self.add()
                self.bind(dirty=True)
                self.request_review()
                self.finish_review(f'APPROVED {self.HASH}')
                rows = self.rows()
                link = next(row for row in rows if 'reviewer' in row)
                link['approved_state'] = dict(commit=self.HASH, dirty=True, mark='old format digest')
                self.file.write_text(json.dumps(rows))

                self.bind(self.NEXT if moved else self.HASH, dirty=True, mark='scope digest')
                count = self.send.call_count
                self.request_review()
                self.assertEqual(self.send.call_count, count + int(moved))

    def test_unbound_approval_requests_review_only_for_a_changed_mark(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.add()
                self.scope = Mock(return_value=None)
                self.request_review()
                # Git was unreadable at the request. It recovered before the unbound verdict.
                live = dict(commit=self.HASH, dirty=True, mark='reviewed tree', folder='/work/unbound')
                self.scope.return_value = live
                self.finish_review('APPROVED')
                count = self.send.call_count
                if changed:
                    self.scope.return_value = dict(live, mark='edited tree')
                self.request_review()
                self.assertEqual(self.send.call_count, count + int(changed))
                if changed:
                    self.assertEqual(self.send.call_args.args[0], self.reviewer)

    def test_clean_approval_then_push_with_same_commit_and_mark_skips_review(self):
        self.bind(mark='same tree')
        self.request_review()
        with own_time(review_links, time=lambda: 1000.5):
            self.finish_review(f'APPROVED {self.HASH}')
        count = self.send.call_count
        self.reply.return_value = 'Pushed.'
        self.request_review()
        self.observe()
        self.assertEqual(self.send.call_count, count)
        self.assertNotIn('approved_turn', self.links()[0])
        self.assertEqual(self.links()[0]['approved_state'],
                         dict(commit=self.HASH, dirty=False, mark='same tree', folder='/work/my tree', at=1000.5))

    def test_push_that_updates_the_base_keeps_the_same_work(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'REVIEW_SCOPES', {}):
            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                       '-c', 'commit.gpgsign=false', *args], cwd=folder, capture_output=True,
                                      check=True, text=True).stdout.strip()
            git('init')
            git('commit', '--allow-empty', '-m', 'Base')
            git('commit', '--allow-empty', '-m', 'Reviewed work')
            head = git('rev-parse', 'HEAD')
            self.bind(head)
            self.worker['review_folder'] = folder
            self.scope = server.review_scope_of
            for dirty in (False, True):
                with self.subTest(dirty=dirty):
                    self.add()
                    git('update-ref', 'refs/remotes/origin/main', 'HEAD^')
                    if dirty:
                        (Path(folder) / 'notes.txt').write_text('Reviewed draft\n')
                    self.request_review()
                    self.finish_review(f'APPROVED {head}')
                    count = self.send.call_count
                    mark = self.scope(self.worker)['mark']
                    git('update-ref', 'refs/remotes/origin/main', 'HEAD')  # the local effect of a push to main
                    self.assertNotEqual(mark, self.scope(self.worker)['mark'])
                    self.request_review()
                    self.assertEqual(self.send.call_count, count)
                    self.assertFalse(review_links.unreviewed_work(self.worker, self.reviewer, self.scope))

    def test_forwarded_feedback_replaces_the_approval_as_idle_close_baseline(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.bind(self.NEXT)
        self.request_review()
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])
        self.assertTrue(review_links.unreviewed_work(self.worker, self.reviewer, self.scope))
        for _ in range(30):  # the reviewer never replies to the request
            self.observe()
        stalled = self.links()[0]['stalled']
        self.assertTrue(review_links.unreviewed_work(self.worker, self.reviewer, self.scope))
        review_links.resume_stalled('%13', '%12', stalled, 'resend')
        self.observe()
        self.finish_review('Needs a regression test.')
        self.assertFalse(review_links.unreviewed_work(self.worker, self.reviewer, self.scope))
        self.bind(self.HASH)  # the worker moved again and nobody reviewed it
        self.assertTrue(review_links.unreviewed_work(self.worker, self.reviewer, self.scope))

    def test_dirty_approval_reviews_partial_commit_and_skips_unreadable_or_unchanged_tree(self):
        for after in ('partial commit', 'unreadable', 'unchanged'):
            with self.subTest(after=after):
                self.add()
                self.bind(dirty=True)
                self.request_review()
                self.finish_review(f'APPROVED {self.HASH}')
                count = self.send.call_count
                if after == 'unreadable':
                    self.scope = lambda entry: None  # unreadable
                elif after == 'partial commit':
                    self.bind(self.NEXT, dirty=True)  # committed a part, more changes are open
                self.request_review()  # the follow-up turn
                if after == 'unreadable':
                    for _ in range(review_links.VERIFY_TICKS):  # git stays unreadable: Tether stops asking
                        self.observe()
                if after == 'partial commit':
                    self.assertEqual(self.send.call_count, count + 1)
                    self.assertEqual(self.send.call_args.args[0], self.reviewer)
                    self.assertIn(f'Commit under review: {self.NEXT}', self.send.call_args.args[1])
                    self.assertNotIn('You approved this work as commit', self.send.call_args.args[1])
                else:
                    self.assertEqual(self.send.call_count, count)
                self.assertNotIn('approved_dirty', self.links()[0])
                self.assertNotIn('confirm_ticks', self.links()[0])
                if after != 'partial commit':
                    self.bind(dirty=True)
                    self.request_review()  # the turn after it is reviewed as usual
                    self.assertEqual(self.send.call_count, count + 1)

    def test_commit_in_a_follow_up_turn_shorter_than_a_tick_is_reviewed(self):
        self.bind(dirty=True)
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        count = self.send.call_count
        self.bind(self.NEXT)
        self.reply.return_value = 'Committed.'  # the worker replied; no Working state was seen
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, count + 1)
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])

    def test_commit_that_moves_during_the_send_gives_no_proof(self):
        self.bind()
        self.request_review()
        self.send.side_effect = lambda entry, text, state: setattr(self, 'head', self.NEXT) if entry is self.worker else None
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn(f'for commit {self.HASH}:', self.send.call_args.args[1])  # the text names the commit
        self.assertNotIn('approved_commit', self.links()[0])
        self.assertEqual(review_links.approved_commits(), {})

    def test_bound_approval_needs_a_fresh_idle_state_not_the_cached_one(self):
        # Review round 2, finding 3: the cached state said Idle while the tab was working.
        self.worker['agent'] = 'claude'
        self.bind()
        self.request_review()
        live = {'state': 'Working'}
        self.fresh = Mock(side_effect=lambda entry: dict(live))
        self.commit = Mock(side_effect=lambda entry: self.head)
        self.finish_review(f'APPROVED {self.HASH}')  # the cached state of the worker is Idle
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.fresh.assert_called_with(self.worker)
        self.assertTrue(self.links()[0]['awaiting'])
        self.fresh = Mock(side_effect=OSError('pane closed'))  # an unreadable state is not Idle
        self.observe()
        self.assertEqual(self.send.call_count, 1)
        live['state'] = 'Idle'
        order = []
        self.fresh = Mock(side_effect=lambda entry: order.append('state') or dict(live))
        self.commit = Mock(side_effect=lambda entry: order.append('commit') or self.head)
        self.send.side_effect = lambda *args: order.append('send')
        self.observe()
        # Review round 3, finding 2: the state is read after the commit, right before the send; the commit is read
        # once more after the send, for the proof.
        self.assertEqual(order, ['commit', 'state', 'send', 'commit'])
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertEqual(self.links()[0]['approved_commit'], self.HASH)

    def test_unbound_approval_still_reaches_a_working_claude_worker(self):
        self.worker['agent'] = 'claude'
        self.request_review()
        self.reply.return_value = 'APPROVED'
        self.observe(**{'%12': 'Working'})
        self.observe(**{'%12': 'Idle', '%13': 'Working'})
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertEqual(self.send.call_args.args[2], 'Working')

    def test_failed_scope_read_does_not_drop_the_binding_of_a_bound_link(self):
        # Review round 1, finding 2: stale approval of A, then a failed read, then a second approval of A at B.
        self.bind()
        self.request_review()
        self.head = self.NEXT
        self.finish_review(f'APPROVED {self.HASH}')
        working = self.scope
        self.scope = Mock(return_value=None)  # git does not answer on the retry
        with own_time(review_links, time=Mock(return_value=1000)) as clock:
            self.observe()
            waiting = self.links()[0]
            for now in (1000, 1001):
                clock.return_value = now
                self.observe()
                self.assertEqual(self.links()[0], waiting)
            self.scope.assert_called_once_with(self.worker)
            self.assertEqual(waiting['scope_since'], 1000)
            self.assertEqual(waiting['scope_retry_at'], 1002)
            self.assertEqual(self.send.call_count, 1)
            self.assertEqual(waiting['stale_commit'], self.HASH)
            self.scope = working
            clock.return_value = 1002
            self.observe()
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])
        self.assertTrue({'scope_ticks', 'scope_since', 'scope_retry_at'}.isdisjoint(self.links()[0]))
        self.finish_review(f'APPROVED {self.HASH}')  # the reviewer repeats the old approval
        self.assertTrue(all(call.args[0] == self.reviewer for call in self.send.call_args_list))

    def test_legacy_scope_count_starts_a_new_window_and_clock_rollback_allows_a_read(self):
        self.bind()
        self.request_review()
        self.head = self.NEXT
        self.finish_review(f'APPROVED {self.HASH}')
        rows = self.rows()
        next(row for row in rows if review_links._is_link(row))['scope_ticks'] = 29
        self.file.write_text(json.dumps(rows))
        readable = self.scope(self.worker)
        self.scope = Mock(return_value=None)
        self.send.reset_mock()
        with own_time(review_links, time=Mock(return_value=1000)) as clock:
            self.observe()
            link = self.links()[0]
            self.assertEqual(link['review_commit'], self.HASH)
            self.assertEqual(link['scope_ticks'], 1)
            self.assertEqual(link['scope_since'], 1000)
            self.assertEqual(link['scope_retry_at'], 1002)
            self.send.assert_not_called()
            self.alert.assert_not_called()

            clock.return_value = 987  # exactly 15 seconds before the scheduled read
            self.observe()
            self.scope.assert_called_once_with(self.worker)
            clock.return_value = 986
            self.scope.return_value = readable
            self.send.side_effect = RuntimeError('reviewer has a draft')
            self.observe()
            self.assertEqual(self.scope.call_count, 2)
            self.send.assert_called_once()
            link = self.links()[0]
            self.assertFalse(link['awaiting'])
            self.assertTrue({'scope_ticks', 'scope_since', 'scope_retry_at'}.isdisjoint(link))

    def test_clock_rollback_cannot_stretch_the_scope_wait(self):
        self.bind()
        self.request_review()
        self.head = self.NEXT
        self.finish_review(f'APPROVED {self.HASH}')
        self.scope = Mock(return_value=None)
        with own_time(review_links, time=Mock(return_value=1000)) as clock:
            self.observe()
            self.assertEqual(self.links()[0]['scope_since'], 1000)
            clock.return_value = 500  # the clock moves back by 500 s
            self.observe()
            self.assertEqual(self.links()[0]['scope_since'], 500)
            for now in range(515, 560, 15):
                clock.return_value = now
                self.observe()
                self.assertEqual(self.links()[0]['review_commit'], self.HASH)
            clock.return_value = 560
            self.observe()
            link = self.links()[0]
            self.assertNotIn('review_commit', link)
            self.assertTrue({'scope_ticks', 'scope_since', 'scope_retry_at'}.isdisjoint(link))
            self.alert.assert_called()

    def test_sanitizer_validates_scope_retry_timestamps(self):
        for field in ('scope_since', 'scope_retry_at'):
            for value in (True, None, '1000', [], -1, -0.25, float('inf'), float('-inf'), float('nan')):
                with self.subTest(field=field, value=value):
                    self.assertNotIn(field, review_links._sanitize({'worker': '%13', field: value}))
            for value in (0, 1000, 1000.25, 10 ** 400):
                with self.subTest(field=field, value=value):
                    self.assertEqual(review_links._sanitize({'worker': '%13', field: value})[field], value)

    def test_commit_unreadable_for_long_pauses_the_review_and_asks_the_user(self):
        # Review round 2, finding 1: a timeout cannot show that an approval is for the current commit.
        self.bind()
        self.request_review()
        self.head = self.NEXT
        self.finish_review(f'APPROVED {self.HASH}')  # stale: a new request is due
        self.scope = Mock(return_value=None)
        self.send.reset_mock()
        with own_time(review_links, time=Mock(return_value=1000)) as clock:
            for now, retry_at in ((1000, 1002), (1002, 1006), (1006, 1014), (1014, 1029),
                                  (1029, 1044), (1044, 1059), (1059, 1060)):
                clock.return_value = now
                self.observe()
                self.assertEqual(self.links()[0]['scope_retry_at'], retry_at)
            self.assertEqual(self.scope.call_count, 7)
            self.send.assert_not_called()
            self.alert.assert_not_called()
            clock.return_value = 1060
            self.observe()
        self.assertEqual(self.scope.call_count, 8)
        self.send.assert_not_called()  # no unbound request went out
        link = self.links()[0]
        self.assertTrue(link['paused'])
        for key in ('review_commit', 'review_clean', 'review_folder', 'stale_commit',
                    'scope_ticks', 'scope_since', 'scope_retry_at'):
            self.assertNotIn(key, link)
        self.alert.assert_called_once_with('Review needs you',
            '“Fix the bug”: Tether cannot read the commit in the folder of that tab, so it cannot tie the review '
            'to a commit. Answer in that tab to restart reviews.', '%13')
        self.observe()
        self.observe(**{'%13': 'Working'})  # a background turn does not restart
        self.turn_source = lambda entry: 'background'
        self.observe(**{'%13': 'Idle'})
        self.send.assert_not_called()
        self.turn_source = lambda entry: 'user'  # the user answers: reviews restart, without the old binding
        self.request_review()
        self.send.assert_called_once()
        self.assertNotIn('Commit under review', self.send.call_args.args[1])
        self.assertNotIn('unreadable', self.links()[0])
        self.assertNotIn('paused', self.links()[0])
        self.commit = lambda entry: self.NEXT  # git answers again
        self.finish_review(f'APPROVED {self.HASH}')  # the reviewer repeats the old approval
        self.assertTrue(all(call.args[0] == self.reviewer for call in self.send.call_args_list))
        self.assertEqual(self.links()[0]['stale_commit'], self.HASH)

    def test_store_change_during_a_step_keeps_the_approval_state_and_sends_once(self):
        # Review round 1, finding 5: agent_prompt() ran during the commit read; the step's result was dropped and
        # the next tick sent the same approval again.
        self.bind()
        self.request_review()
        def read_commit(entry):
            review_links.agent_prompt('%13')
            return self.HASH
        self.commit = read_commit
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual([call.args[0] for call in self.send.call_args_list], [self.reviewer, self.worker])
        link = self.links()[0]
        self.assertFalse(link['awaiting'])
        self.assertEqual(link['approved_commit'], self.HASH)
        self.assertIn('agent_prompt', link)  # the other writer's change is kept too
        self.commit = lambda entry: self.HASH
        self.observe()
        self.observe()
        self.assertEqual(self.send.call_count, 2)

    def test_merge_keeps_the_steps_fields_and_a_newer_agent_prompt(self):
        old = {'worker': '%1', 'awaiting': True, 'request_id': 'a', 'agent_prompt': 1}
        updated = {'worker': '%1', 'awaiting': False, 'rounds': 1}  # the step also consumed agent_prompt
        self.assertEqual(review_links._merge(old, updated, {**old, 'stalled': 'x'}),
                         {'worker': '%1', 'awaiting': False, 'rounds': 1, 'stalled': 'x'})
        # The same field changed by both: the step's value records a send that happened, so it wins.
        self.assertEqual(review_links._merge(old, updated, {**old, 'awaiting': 'other', 'request_id': 'b'}),
                         {'worker': '%1', 'awaiting': False, 'rounds': 1})
        # A newer agent_prompt mark is kept: it belongs to a prompt sent after the step read the link.
        self.assertEqual(review_links._merge(old, updated, {**old, 'agent_prompt': 9})['agent_prompt'], 9)

    def test_agent_prompt_refreshed_during_a_step_does_not_send_the_approval_twice(self):
        # Review round 2, finding 4: the step consumes agent_prompt at the end of the worker's turn; another agent
        # refreshed it during the commit read. Both changed the same field, and the step's result was dropped.
        self.bind()
        review_links.agent_prompt('%13')
        self.request_review()
        def read_commit(entry):
            review_links.agent_prompt('%13')
            return self.HASH
        self.commit = read_commit
        with patch.object(review_links.time, 'time', return_value=time.time() + 5):
            self.finish_review(f'APPROVED {self.HASH}')
            self.commit = lambda entry: self.HASH
            self.observe()
            self.observe()
        self.assertEqual([call.args[0] for call in self.send.call_args_list], [self.reviewer, self.worker])
        self.assertEqual(self.links()[0]['approved_commit'], self.HASH)
        self.assertFalse(self.links()[0]['awaiting'])

    def test_link_removed_during_the_commit_read_sends_nothing(self):
        # Review round 2, finding 4: the link was removed during the read and the approval still went out.
        self.bind()
        self.request_review()
        def read_commit(entry):
            review_links.remove('%13', '%12')
            return self.HASH
        self.commit = read_commit
        self.finish_review(f'APPROVED {self.HASH}')
        self.observe()
        self.assertEqual(self.send.call_count, 1)  # only the request
        self.assertEqual(self.links(), [])

    def test_a_replaced_link_is_not_merged_with_the_step_of_the_old_one(self):
        self.bind()
        self.request_review()
        def read_commit(entry):
            self.add()  # the user links the pair again during the step: a new link with a new creation time
            return self.HASH
        self.commit = read_commit
        with patch.object(review_links.time, 'time', return_value=time.time() + 100):
            self.finish_review(f'APPROVED {self.HASH}')
        link, = self.links()
        self.assertNotIn('approved_commit', link)
        self.assertNotIn('awaiting', link)

    def test_findings_end_a_run_of_approvals_that_did_not_match(self):
        # Review round 1, finding 7: a mismatch, findings, then 2 mismatches are not 3 mismatches in a row.
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.NEXT}')
        self.observe()
        self.assertEqual(self.links()[0]['stale_rounds'], 1)
        self.finish_review('The lock is not released.')
        self.assertNotIn('stale_rounds', self.links()[0])
        self.request_review()
        for _ in range(2):
            self.finish_review(f'APPROVED {self.NEXT}')
            self.observe()
        link = self.links()[0]
        self.assertEqual(link['stale_rounds'], 2)
        self.assertNotIn('paused', link)
        self.alert.assert_not_called()

    def test_findings_at_the_round_limit_end_a_run_of_approvals_that_did_not_match(self):
        # Review round 9: two mismatches, then the findings that reach the round limit. The count stayed, so after
        # a restart one more mismatch paused the review as "3 in a row".
        self.bind()
        self.request_review()
        for _ in range(2):
            self.finish_review(f'APPROVED {self.NEXT}')
            self.observe()
        self.assertEqual(self.links()[0]['stale_rounds'], 2)
        link, = self.links()
        link['rounds'] = review_links.MAX_ROUNDS - 1
        self.file.write_text(json.dumps([link]))
        self.finish_review('Still broken.')
        link = self.links()[0]
        self.assertTrue(link['paused'])
        self.assertNotIn('stale_rounds', link)
        self.turn_source = lambda entry: 'user'
        self.request_review()  # the worker's reply to the stop message
        count = self.send.call_count
        self.request_review()  # the user restarts
        self.assertEqual(self.send.call_count, count + 1)
        self.finish_review(f'APPROVED {self.NEXT}')
        self.observe()
        link = self.links()[0]
        self.assertEqual(link['stale_rounds'], 1)
        self.assertNotIn('paused', link)

    def test_three_approvals_that_do_not_match_pause_the_review_and_alert(self):
        self.bind()
        self.request_review()
        for _ in range(review_links.MAX_ROUNDS):
            self.finish_review(f'APPROVED {self.NEXT}')
            self.observe()
        link = self.links()[0]
        self.assertEqual(link['stale_rounds'], review_links.MAX_ROUNDS)
        self.assertTrue(link['paused'])
        self.assertEqual(self.send.call_count, review_links.MAX_ROUNDS)  # 3 requests, no 4th, nothing to the worker
        self.alert.assert_called_once_with('Review needs you',
            '“Fix the bug”: 3 approvals in a row could not be verified for the current commit. '
            'Answer in that tab to restart reviews.', '%13')
        self.turn_source = lambda entry: 'user'
        self.request_review()  # a prompt from Mak restarts
        self.assertEqual(self.send.call_count, review_links.MAX_ROUNDS + 1)
        self.assertNotIn('stale_rounds', self.links()[0])
        self.assertNotIn('paused', self.links()[0])

    def test_dirty_approval_resets_rounds_before_commit_confirmation(self):
        for stopped_sibling in (False, True):
            with self.subTest(stopped_sibling=stopped_sibling):
                self.file.write_text('[]')
                self.add(worker_thread=WORKER_THREAD)
                link, = self.links()
                self.file.write_text(json.dumps([dict(link, rounds=2)]))
                if stopped_sibling:
                    self.add(reviewer='%21', worker_thread=WORKER_THREAD, reset=False)
                    self.assertTrue(review_links.remove('%13', '%21'))

                self.bind(dirty=True)
                self.turn_source = lambda _: 'background'
                self.observe(**{'%13': 'Working'})
                self.observe(**{'%13': 'Idle'})
                self.finish_review(f'APPROVED {self.HASH}')
                self.assertEqual(self.links()[0]['approved_dirty'], self.HASH)
                self.bind(commit=self.NEXT)
                self.request_review()  # this turn commits the previously approved work
                self.assertIn('check that this commit holds that work', self.send.call_args.args[1])

                self.send.reset_mock()
                self.alert.reset_mock()
                self.finish_review(FINDINGS)
                self.send.assert_called_once()
                self.assertIn('Feedback from the reviewer tab', self.send.call_args.args[1])
                self.assertEqual(self.links()[0]['rounds'], 1)
                self.alert.assert_not_called()

    def test_approval_delivered_while_stopping_the_reviewer_clears_the_saved_cap(self):
        self.bind(dirty=True)
        self.add(worker_thread=WORKER_THREAD)
        for _ in range(2):
            self.request_review()
            self.finish_review(FINDINGS)
        self.request_review()
        self.send.side_effect = lambda entry, *_: review_links.remove('%13') if entry == self.worker else None
        self.finish_review(f'APPROVED {self.HASH}')
        saved, = self.links()
        self.assertIn('saved_at', saved)
        self.assertNotIn('rounds', saved)

    def test_third_mismatched_approval_and_sibling_pause_alert_once(self):
        for reverse_order in (False, True):
            with self.subTest(reverse_order=reverse_order):
                self.file.write_text('[]')
                self.alert.reset_mock()
                self.bind()
                self.add(worker_thread=WORKER_THREAD, seen_working=True)
                self.add(reviewer='%21', worker_thread=WORKER_THREAD, seen_working=True, reset=False)
                self.entries = [self.worker, self.reviewer, dict(self.reviewer, id='%21')]
                self.states['%21'] = 'Idle'
                if reverse_order:
                    self.file.write_text(json.dumps(self.rows()[::-1]))
                replies = {}
                self.review_reply = lambda entry, _: replies.get(entry['id'])
                self.observe()
                for round_number in range(1, 4):
                    replies['%12'] = f'APPROVED {self.NEXT}'
                    self.observe()
                    replies.clear()
                    if round_number < 3:
                        self.observe()
                replies['%21'] = FINDINGS
                self.observe()
                self.assertTrue(all(link.get('paused') for link in self.links()))
                self.alert.assert_called_once_with('Review needs you',
                    '“Fix the bug”: 3 approvals in a row could not be verified for the current commit. '
                    'Answer in that tab to restart reviews.', '%13')

    def test_unreadable_commit_at_approval_waits_then_requests_verification(self):
        self.bind()
        self.request_review()
        self.commit = Mock(return_value=None)
        self.finish_review(f'APPROVED {self.HASH}')
        for _ in range(review_links.VERIFY_TICKS - 2):
            self.observe()
        self.assertEqual(self.send.call_count, 1)
        self.assertTrue(self.links()[0]['awaiting'])
        self.commit = lambda entry: self.HASH  # git answers again in time
        self.observe()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertEqual(self.links()[0]['approved_commit'], self.HASH)
        self.assertNotIn('verify_ticks', self.links()[0])

        self.add()
        self.send.reset_mock()
        self.bind()
        self.request_review()
        self.commit = Mock(side_effect=OSError('git'))
        self.finish_review(f'APPROVED {self.HASH}')
        for _ in range(review_links.VERIFY_TICKS):
            self.observe()
        self.assertTrue(all(call.args[0] == self.reviewer for call in self.send.call_args_list))  # never the worker
        self.assertNotIn('approved_commit', self.links()[0])
        self.assertEqual(self.links()[0]['stale_rounds'], 1)
        self.assertIn(f'Your approval of {self.HASH[:12]} was not forwarded because Tether could not read the tab\'s '
                      'commit. Check the current commit before approving this review.', self.send.call_args.args[1])

    def test_link_from_before_the_restart_without_a_commit_forwards_approval(self):
        self.request_review()  # no scope: the stored link has no review_commit
        self.bind()
        self.head = self.NEXT
        self.finish_review('APPROVED')
        self.assertIn('Approved by the reviewer tab (%12):', self.send.call_args.args[1])

    def test_named_commit_reads_only_a_hash_right_after_approved(self):
        cases = {'APPROVED': None, 'APPROVED fine': None, 'APPROVED 1f0e1f0e1f0e': '1f0e1f0e1f0e',
                 'approved: `1F0E1F0`.': '1f0e1f0', 'APPROVED for commit 1f0e1f0e — ok': '1f0e1f0e',
                 'APPROVED **1f0e1f0e1f0e**': '1f0e1f0e1f0e', 'APPROVED deadbeefcafe': 'deadbeefcafe',
                 'APPROVED 1f0e1': None, 'APPROVED for the older commit 2a9d2a9d2a9d': None,
                 'APPROVED ' + 'ab12' * 16: 'ab12' * 16,
                 'APPROVED. Earlier I read 1f0e1f0e1f0e.': None, 'APPROVED 1f0e1f0e1f0ezz': None}
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(review_links._named_commit(reply), expected)


class ServerWatcherTests(unittest.TestCase):
    def test_review_level_resolves_worker_project_or_global(self):
        worker = {'id': '%13'}
        pid = 'a1b2c3d4e5f6'
        with patch.object(server.codex_level, '_tmux') as tmux, \
                patch.object(server.autonomy, 'resolve', return_value={'level': 4}) as resolve:
            for code, value, expected in ((0, pid, pid), (0, 'unknown', None), (1, pid, None)):
                with self.subTest(code=code, value=value):
                    tmux.return_value = subprocess.CompletedProcess([], code, stdout=value + '\n')
                    self.assertEqual(server.review_level(worker), 4)
                    tmux.assert_called_with('display-message', '-p', '-t', '%13', '#{@project}')
                    resolve.assert_called_with(expected)
            resolve.side_effect = OSError('No autonomy settings')
            self.assertEqual(server.review_level(worker), 0)
            tmux.side_effect = OSError('Could not read project')
            self.assertEqual(server.review_level(worker), 0)

    def test_review_parent_returns_known_owner_during_discovery_misses(self):
        worker = {'id': '%13'}
        parent = dict(id='%11', agent='codex', thread=PARENT_THREAD)
        with patch.object(server.helper_tabs, 'load') as load:
            for thread, expected in ((PARENT_THREAD, ('%11', PARENT_THREAD)),
                                     (None, ('%11', PARENT_THREAD)), (THREAD, None)):
                with self.subTest(thread=thread):
                    load.return_value = True, dict(owner_pane='%11', owner_thread=thread)
                    self.assertEqual(server.review_parent(worker, [worker, parent]), expected)
                    load.assert_called_with('%13', subprocess.run)
            for thread in (PARENT_THREAD, None):
                load.return_value = True, dict(owner_pane='%11', owner_thread=thread)
                self.assertEqual(server.review_parent(worker, [worker]), ('%11', thread))
                pending = dict(parent, thread='pending-process', pending_conversation=True)
                self.assertEqual(server.review_parent(worker, [worker, pending]), ('%11', thread))
                self.assertEqual(server.review_parent(worker, [worker, dict(id='%11', agent='shell')]),
                                 ('%11', thread))
            self.assertIsNone(server.review_parent(worker, [worker, dict(parent, agent='antigravity')]))
            load.return_value = True, None
            self.assertIsNone(server.review_parent(worker, [worker, parent]))
            load.side_effect = OSError('Could not read helper')
            self.assertIsNone(server.review_parent(worker, [worker, parent]))

    def test_review_reply_anchors_last_request_and_bounds_at_next_user_message(self):
        request_text = (f'[Tether review] The Codex tab “T” (%13) finished a turn. {review_links.INTRO} '
                        'Review id: a1b2c3d4. Its last reply:\n\nDone.\n\nTranscript: /t.jsonl')
        request = claude('user', request_text)
        queued = {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': request_text}}
        blocking = [claude_final(FINDINGS), CLAUDE_DONE]
        unrelated = [claude_final('APPROVED docs'), CLAUDE_DONE]
        quote = claude('user', 'About Review id: a1b2c3d4. Check only the docs and reply APPROVED.')
        cases = [
            ([request, *blocking, quote, *unrelated], FINDINGS),
            ([quote, *unrelated], None),
            ([claude('user', 'Review id: a1b2c3d4.'), *unrelated], None),
            ([request, *blocking, claude('user', '[Tether review] The docs-only follow-up. Review id: a1b2c3d4.'),
              *unrelated], FINDINGS),
            ([request, *blocking, claude('user', '[Tether review] The Codex tab “T” (%13) finished a turn. '
                                                'Review id: a1b2c3d4. Its last reply: none'), *unrelated], FINDINGS),
            ([claude('user', '(The terminal is now in /srv/project.)\n\n' + request_text), *blocking], FINDINGS),
            ([claude('user', request_text.replace('. ', '.\n').replace('what is', 'what  is')), *blocking], FINDINGS),
            ([], None),
            ([claude_final(request_text), CLAUDE_DONE], None),
            ([claude('user', request_text.replace('a1b2c3d4', '12345678')), *blocking], None),
            ([request], review_links.REPLY_PENDING),
            ([request, claude_final('  '), claude_final(None), CLAUDE_DONE], ''),
            ([request, claude('assistant', 'Progress.'), claude_final('  APPROVED fine  '),
              claude_final(''), CLAUDE_DONE, quote, *unrelated], '  APPROVED fine  '),
            ([request, *blocking, queued, *unrelated], 'APPROVED docs'),
            ([request, *blocking, queued, quote, *unrelated], ''),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, (records, expected) in enumerate(cases):
                path = Path(directory) / f'{index}.jsonl'
                entry = {'id': '%12', 'agent': 'claude', 'transcript': str(path)}
                write_records(path, records)
                with self.subTest(records=records):
                    self.assertEqual(server.review_reply(entry, 'a1b2c3d4'), expected)
            entry['transcript'] = str(Path(directory) / 'missing.jsonl')
            self.assertIsNone(server.review_reply(entry, 'a1b2c3d4'))
        with patch.object(server.history, 'read', side_effect=RuntimeError('no history')):
            self.assertIsNone(server.review_reply(entry, 'a1b2c3d4'))

    def test_last_reply_uses_latest_nonempty_assistant_text(self):
        messages = [{'role': 'assistant', 'text': 'old'}, {'role': 'assistant', 'text': 'latest'},
                    {'role': 'user', 'text': 'question'}, {'role': 'assistant', 'text': '  '},
                    {'role': 'assistant', 'text': None}, {'role': 'assistant', 'kind': 'actions', 'text': 'Ran tests'},
                    {'role': 'assistant', 'kind': 'notice', 'text': 'A background task finished'},
                    {'role': 'assistant', 'phase': 'commentary', 'text': 'I will check.'},
                    {'role': 'assistant', 'kind': 'advisor', 'text': 'Asked the advisor'}]
        with patch.object(server, 'snapshot', return_value={'messages': messages}):
            self.assertEqual(server.last_reply({}), 'latest')
        for snapshot in ({'messages': []}, {}):
            with patch.object(server, 'snapshot', return_value=snapshot):
                self.assertEqual(server.last_reply({}), '')
        with patch.object(server, 'snapshot', side_effect=RuntimeError('no history')):
            self.assertEqual(server.last_reply({}), '')

    def test_monitor_runs_watcher_on_each_loop(self):
        isolate_notice_monitor(self)
        entries = [{'id': '%12'}]
        with patch.object(server, 'sessions', return_value=entries), \
                patch.object(server.review_links, 'observe') as observe, \
                own_time(server, sleep=Mock(side_effect=[None, StopIteration])):
            with self.assertRaises(StopIteration):
                server.monitor_notices()
        self.assertEqual(observe.call_count, 2)
        observe.assert_called_with(entries, server.shared_status, server.review_send, server.last_reply,
                                   alert=server.review_alert, work_mark=server.review_work_mark,
                                   review_reply=server.review_reply, turn_source=server.review_turn_source,
                                   level=server.review_level, parent=ANY,
                                   scope=server.review_scope_of, commit=server.review_commit,
                                   fresh=server.fresh_status, folder=server.review_folder)
        with patch.object(server, 'review_parent') as parent:
            observe.call_args.kwargs['parent'](entries[0])
            parent.assert_called_once_with(entries[0], entries)

    def test_session_discovery_exception_skips_watcher_and_loop_recovers(self):
        isolate_notice_monitor(self)
        entries = [{'id': '%12'}]
        with patch.object(server, 'sessions', side_effect=[KeyError('pane'), entries]) as sessions, \
                patch.object(server.review_links, 'observe') as observe, \
                own_time(server, sleep=Mock(side_effect=[None, StopIteration])) as clock, \
                patch('server.sys.stderr', new_callable=io.StringIO) as stderr:
            with self.assertRaises(StopIteration):
                server.monitor_notices()
        self.assertEqual(sessions.call_count, 2)
        observe.assert_called_once_with(entries, server.shared_status, server.review_send, server.last_reply,
                                        alert=server.review_alert, work_mark=server.review_work_mark,
                                        review_reply=server.review_reply, turn_source=server.review_turn_source,
                                        level=server.review_level, parent=ANY,
                                        scope=server.review_scope_of, commit=server.review_commit,
                                        fresh=server.fresh_status, folder=server.review_folder)
        self.assertEqual([call.args for call in clock.call_args_list], [(2,), (2,)])
        self.assertEqual(stderr.getvalue(), "session discovery failed: KeyError('pane')\n")

    def test_review_alert_uses_native_payload_keys(self):
        guard_subprocess(self)
        with patch.object(server, 'sessions', return_value=[]), patch.object(server, 'notify') as notify:
            server.review_alert('Review link lost', 'Tab vanished.', '%13')
        notify.assert_called_once_with({
            'title': 'Review link lost', 'body': 'Tab vanished.', 'tag': 'review-%13', 'session': '%13',
            'priority': 'urgent', 'silent': False, 'url': '/mobile/#session=%2513',
        }, True)

    def test_review_alert_includes_review_actions_only_when_given(self):
        guard_subprocess(self)
        review = {'worker': '%13', 'reviewer': '%12', 'id': 'a1b2c3d4'}
        with patch.object(server, 'sessions', return_value=[]), patch.object(server, 'notify') as notify:
            server.review_alert('Review stalled', 'Reply not found.', '%13', review=review)
        notify.assert_called_once_with({
            'title': 'Review stalled', 'body': 'Reply not found.', 'tag': 'review-%13', 'session': '%13',
            'priority': 'urgent', 'silent': False, 'url': '/mobile/#session=%2513', 'review': review,
        }, True)

    def test_work_mark_tracks_tree_and_head_in_real_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(['git', *args], cwd=folder, capture_output=True, check=True)
            git('init')
            path = Path(folder) / 'work.txt'
            path.write_text('initial\n')
            git('add', 'work.txt')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-m', 'Initial')
            entry = {'cwd': folder}
            initial = server.review_work_mark(entry)
            self.assertIsInstance(initial, str)
            self.assertEqual(initial, server.review_work_mark(entry))
            path.write_text('edited\n')
            edited = server.review_work_mark(entry)
            self.assertNotEqual(initial, edited)
            git('add', 'work.txt')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-m', 'Edit')
            committed = server.review_work_mark(entry)
            self.assertNotEqual(initial, committed)
            self.assertNotEqual(edited, committed)
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(server.review_work_mark({'cwd': folder}))
        self.assertIsNone(server.review_work_mark({}))

    def test_work_mark_uses_bounded_read_only_git_commands_and_handles_errors(self):
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'output')) as run:
            self.assertIsInstance(server.review_work_mark({'cwd': '/project'}), str)
        self.assertEqual([call.args[0] for call in run.call_args_list],
            [['git', 'rev-parse', 'HEAD'], ['git', 'status', '--porcelain'], ['git', 'diff', 'HEAD']])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['cwd'], '/project')
            self.assertEqual(call.kwargs['timeout'], 5)
            self.assertEqual(call.kwargs['env']['GIT_OPTIONAL_LOCKS'], '0')
            self.assertTrue(call.kwargs['capture_output'])
        for failure in (OSError('missing'), subprocess.TimeoutExpired('git', 5)):
            with patch.object(server.subprocess, 'run', side_effect=failure):
                self.assertIsNone(server.review_work_mark({'cwd': '/project'}))
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'')):
            self.assertIsNone(server.review_work_mark({'cwd': '/project'}))

    def test_scope_reads_head_dirty_state_and_rules_in_a_real_repository(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'work'
            def git(*args, cwd=folder):
                env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
                return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                       '-c', 'commit.gpgsign=false', *args], cwd=cwd, capture_output=True, check=True,
                                      text=True, timeout=5, env=env).stdout.strip()
            git('init', '-b', 'main', str(folder), cwd=root)
            self.assertIsNone(review_scope.scope(folder))  # no commit yet
            (folder / 'work.txt').write_text('one\n')
            git('add', '.')
            git('commit', '-m', 'One')
            first = git('rev-parse', 'HEAD')
            self.assertEqual(review_scope.scope(folder),
                             {'commit': first, 'dirty': False, 'folder': str(folder), 'rules': False, 'ui': [],
                              'checklist': {'status': 'absent', 'rows': [], 'paths': [],
                                            'reason': 'origin/main is not a readable commit'}})
            (folder / 'REVIEW.md').write_text('rules\n')
            self.assertTrue(review_scope.scope(folder)['dirty'])  # an untracked file
            git('add', '.')
            git('commit', '-m', 'Rules')
            self.assertFalse(review_scope.scope(folder)['rules'])  # on the branch only: main does not have it yet
            second = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/main', second)
            self.assertEqual(review_scope.scope(folder),
                             {'commit': second, 'dirty': False, 'folder': str(folder), 'rules': True, 'ui': [],
                              'checklist': {'status': 'absent', 'rows': [], 'paths': [], 'base': second,
                                            'reason': 'origin/main has no docs/reliability-checklist.md'}})
            self.assertEqual(review_scope.head(folder), second)
            (folder / 'work.txt').write_text('two\n')
            changed_ui = ['server/app.js', 'server/index.html', 'ui/index.html', 'ui/package.json',
                          'ui/public/logo.png', 'ui/src/App.tsx', 'ui/vite.config.ts']
            for name in changed_ui + ['ui/test_cards.js', 'ui/tests/cards.js', 'docs/guide.md', 'server/app.json']:
                file = folder / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('new\n')
            target = review_scope.scope(folder)
            self.assertTrue(target['dirty'])
            self.assertEqual(target['checklist']['status'], 'absent')
            self.assertIn('ui/tests/cards.js', target['checklist']['paths'])
            self.assertEqual(target['ui'], changed_ui)
            self.assertEqual(server.review_commit({'cwd': str(folder)}), second)
            self.assertEqual(server.review_scope_of({'cwd': str(folder)})['commit'], second)
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(review_scope.scope(folder))
            self.assertIsNone(review_scope.head(folder))
        self.assertIsNone(review_scope.head(None))
        self.assertIsNone(server.review_scope_of({}))

    def test_scope_uses_bounded_read_only_git_commands_and_handles_errors(self):
        done = subprocess.CompletedProcess([], 0, (('1f0e' * 10) + '\n').encode())
        status = subprocess.CompletedProcess([], 0, ('# branch.oid ' + '1f0e' * 10 + '\n# branch.head main\n').encode())
        missing = subprocess.CompletedProcess([], 1, b'')
        root = subprocess.CompletedProcess([], 0, b'/project\n')
        none = subprocess.CompletedProcess([], 0, b'')
        results = [status, done, missing, root, missing, none, none, none, none]
        with patch.object(review_scope.subprocess, 'run', side_effect=results) as run:
            self.assertEqual(review_scope.scope('/project'),
                             {'commit': '1f0e' * 10, 'dirty': False, 'folder': '/project', 'rules': True, 'ui': [],
                              'checklist': {'status': 'absent', 'rows': [], 'paths': [],
                                            'reason': 'origin/main is not a readable commit'}})
        # Review round 6, finding 2: one command gives the commit and the status, so they are from the same moment.
        self.assertEqual([call.args[0] for call in run.call_args_list],
            [['git', 'status', '--porcelain=v2', '--branch', '--untracked-files=normal'],
             ['git', 'cat-file', '-e', 'origin/main:REVIEW.md'],
             ['git', 'rev-parse', '--verify', '--quiet', 'origin/main'],
             ['git', 'rev-parse', '--show-toplevel'],
             ['git', 'rev-parse', '--verify', '--quiet', 'origin/main'],
             ['git', 'diff', '--name-only', '-z', '--no-renames', '--no-relative', 'HEAD', '--', *review_scope.UI_SPECS],
             ['git', 'diff', '--cached', '--name-only', '-z', '--no-renames', '--no-relative', 'HEAD', '--',
              *review_scope.UI_SPECS],
             ['git', 'ls-files', '--others', '--exclude-standard', '-z', '--', *review_scope.UI_SPECS],
             ['git', 'ls-files', '-z', '--', *review_scope.UI_SPECS]])
        changed = subprocess.CompletedProcess([], 0, status.stdout + b'1 .M N... 100644 100644 100644 a b work.txt\n')
        with patch.object(review_scope.subprocess, 'run', return_value=changed):
            self.assertIs(review_scope.scope('/project')['dirty'], True)
        untracked = subprocess.CompletedProcess([], 0, status.stdout + b'? new.txt\n')
        with patch.object(review_scope.subprocess, 'run', return_value=untracked):
            self.assertIs(review_scope.scope('/project')['dirty'], True)
        # Review round 7: git exits with 0 and a warning when it cannot read a folder; that status is partial.
        partial = subprocess.CompletedProcess([], 0, status.stdout, b"warning: could not open directory 'x/': Permission denied\n")
        def warned(args, **options):
            return partial if args[1] == 'status' else done
        with patch.object(review_scope.subprocess, 'run', side_effect=warned):
            found = review_scope.scope('/project')
        self.assertEqual((found['commit'], found['dirty']), ('1f0e' * 10, None))

        for call in run.call_args_list:
            self.assertEqual(call.kwargs['cwd'], '/project')
            self.assertEqual(call.kwargs['timeout'], 5)
            self.assertEqual(call.kwargs['env']['GIT_OPTIONAL_LOCKS'], '0')
        for failure in (OSError('missing'), subprocess.TimeoutExpired('git', 5)):
            with patch.object(review_scope.subprocess, 'run', side_effect=failure):
                self.assertIsNone(review_scope.scope('/project'))
                self.assertIsNone(review_scope.head('/project'))
        # HEAD is readable but the status is not: the review stays bound, and the tree does not count as clean.
        def status_fails(args, **options):
            if args[1] == 'status':
                raise subprocess.TimeoutExpired('git', 5)
            return done
        with patch.object(review_scope.subprocess, 'run', side_effect=status_fails):
            found = review_scope.scope('/project')
        self.assertEqual((found['commit'], found['dirty']), ('1f0e' * 10, None))  # unknown, never clean
        sha256 = subprocess.CompletedProcess([], 0, ('ab12' * 16 + '\n').encode())
        with patch.object(review_scope.subprocess, 'run', return_value=sha256):
            self.assertEqual(review_scope.head('/project'), 'ab12' * 16)
        with patch.object(review_scope.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, b'1f0e1f0e\n')):
            self.assertIsNone(review_scope.head('/project'))  # a short hash is not a commit id


    def test_scope_with_an_unreadable_folder_in_a_real_repository_is_not_clean(self):
        import review_scope
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                       '-c', 'commit.gpgsign=false', *args], cwd=folder, capture_output=True, check=True)
            git('init', '-b', 'main')
            (Path(folder) / 'work.txt').write_text('one\n')
            git('add', '.')
            git('commit', '-m', 'One')
            self.assertIs(review_scope.scope(folder)['dirty'], False)
            hidden = Path(folder) / 'hidden'
            hidden.mkdir()
            (hidden / 'new.txt').write_text('x\n')
            hidden.chmod(0)
            try:
                if os.access(hidden, os.R_OK):
                    self.skipTest('this user can read every folder')
                self.assertIsNone(review_scope.scope(folder)['dirty'])
            finally:
                hidden.chmod(0o700)  # before the temporary folder is removed
            self.assertIs(review_scope.scope(folder)['dirty'], True)

    def test_scope_counts_untracked_files_as_dirty_when_the_repository_hides_them(self):
        import review_scope
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                       '-c', 'commit.gpgsign=false', *args], cwd=folder, capture_output=True, check=True)
            git('init', '-b', 'main')
            (Path(folder) / 'work.txt').write_text('one\n')
            git('add', '.')
            git('commit', '-m', 'One')
            git('config', 'status.showUntrackedFiles', 'no')
            self.assertFalse(review_scope.scope(folder)['dirty'])
            (Path(folder) / 'untracked.txt').write_text('new\n')
            self.assertTrue(review_scope.scope(folder)['dirty'])

    def test_checklist_reads_base_rows_in_a_real_repository_and_bounds_the_checker(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)

            def git(*args):
                subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=Test', '-c', 'commit.gpgsign=false',
                                *args], cwd=root, check=True, capture_output=True, timeout=5,
                               env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})

            git('init', '-q')
            rules = folder / 'docs' / 'reliability-checklist.md'
            rules.parent.mkdir()
            rules.write_text('## R1: Work file\n- Paths: `^work\\.txt$`\n- Check: Open the file.\n- Incident: B1\n')
            git('add', '.')
            git('commit', '-qm', 'Base')
            with patch.object(review_scope.subprocess, 'run', wraps=subprocess.run) as run:
                self.assertEqual(review_scope.checklist(folder)['status'], 'absent')
                run.assert_called_once()
            git('update-ref', 'refs/remotes/origin/main', 'HEAD')
            (folder / 'other.txt').write_text('different file\n')
            clear = review_scope.checklist(folder)
            self.assertEqual((clear['status'], clear['rows'], clear['paths']), ('clear', [], ['other.txt']))
            (folder / 'work.txt').write_text('changed work\n')
            rules.write_text('# Branch removes the rules\n')
            hit = review_scope.checklist(folder)
            self.assertEqual(hit['status'], 'hit')
            self.assertEqual([(row['id'], row['files']) for row in hit['rows']], [('R1', ['work.txt'])])

            real_run = subprocess.run

            def time_out_checker(command, **options):
                if command[0] == 'git':
                    return real_run(command, **options)
                self.assertEqual(command, [review_scope.sys.executable, str(review_scope.CHECKER),
                                           '--json', '--repo', root])
                self.assertEqual(options['timeout'], 15)
                self.assertEqual(options['env']['GIT_OPTIONAL_LOCKS'], '0')
                self.assertTrue(options['capture_output'])
                raise subprocess.TimeoutExpired(command, 15)

            with patch.object(review_scope.subprocess, 'run', side_effect=time_out_checker):
                self.assertEqual(review_scope.checklist(folder),
                                 {'status': 'unchecked', 'rows': [], 'paths': [], 'reason': 'checker timed out'})

    def test_checklist_marks_bad_subprocess_results_unchecked(self):
        results = (subprocess.CompletedProcess([], 0, b''), subprocess.CompletedProcess([], 1, b'[]'),
                   subprocess.CompletedProcess([], 2, b'{"status":"unknown"}'),
                   subprocess.CompletedProcess([], 3, b'{"status":"clear"}'), OSError('unavailable'))
        base = subprocess.CompletedProcess([], 0, b'1f0e' * 10 + b'\n')
        with patch.object(review_scope, 'git_result', return_value=base):
            for result in results:
                with self.subTest(result=result), patch.object(review_scope.subprocess, 'run', side_effect=[result]):
                    actual = review_scope.checklist('/project')
                    self.assertEqual((actual['status'], actual['rows'], actual['paths']), ('unchecked', [], []))
                    self.assertTrue(actual['reason'])

    def test_failed_base_ref_lookup_is_unchecked_and_ui_paths_are_unknown(self):
        failures = (subprocess.TimeoutExpired('git', 5), subprocess.CompletedProcess([], 128, b''))
        root = subprocess.CompletedProcess([], 0, b'/project\n')
        for failure in failures:
            with self.subTest(failure=failure):
                with patch.object(review_scope.subprocess, 'run', side_effect=[failure]):
                    result = review_scope.checklist('/project')
                self.assertEqual(result['status'], 'unchecked')
                self.assertIn('origin/main', result['reason'])
                with patch.object(review_scope.subprocess, 'run', side_effect=[root, failure]):
                    self.assertIsNone(review_scope.ui_paths('/project'))

    def test_ui_paths_are_unknown_when_either_file_listing_fails(self):
        root = subprocess.CompletedProcess([], 0, b'/project\n')
        base = subprocess.CompletedProcess([], 0, b'1f0e' * 10 + b'\n')
        diff = subprocess.CompletedProcess([], 0, b'ui/index.html\x00')
        fail = subprocess.CompletedProcess([], 128, b'')
        for results in ([root, base, base, fail], [root, base, base, diff, subprocess.TimeoutExpired('git', 5)]):
            with self.subTest(results=results):
                with patch.object(review_scope.subprocess, 'run', side_effect=results):
                    self.assertIsNone(review_scope.ui_paths('/project'))

    def test_scope_is_computed_once_per_work_state_while_a_request_is_retried(self):
        # Each failed delivery repeats the request step, and the scope with it. The checklist subprocess and the
        # UI listing must not run on each monitor tick.
        entry = {'id': '%13', 'cwd': '/project'}
        marks = ['m1']
        found = {'commit': 'c' * 40, 'dirty': False, 'folder': '/project', 'rules': False,
                 'checklist': {'status': 'clear', 'rows': [], 'paths': []}, 'ui': []}
        with patch.object(server, 'REVIEW_SCOPES', {}), \
                patch.object(server, 'agent_folder', return_value='/project'), \
                patch.object(server.review_scope, 'fingerprint', side_effect=lambda folder: marks[0]) as mark, \
                patch.object(server.review_scope, 'scope', side_effect=lambda folder: dict(found)) as scope, \
                patch.object(server.time, 'monotonic', return_value=1000.0) as clock:
            for _ in range(50):
                self.assertEqual(server.review_scope_of(entry), dict(found, mark='m1'))
            self.assertEqual(scope.call_count, 1)
            mark.assert_called_with('/project')
            server.review_scope_of(entry)['commit'] = 'changed by the caller'
            self.assertEqual(server.review_scope_of(entry)['commit'], 'c' * 40)
            marks[0] = 'm2'  # a commit or an edit
            self.assertEqual(server.review_scope_of(entry)['mark'], 'm2')
            self.assertEqual(scope.call_count, 2)
            clock.return_value += server.REVIEW_SCOPE_AGE  # origin/main may have moved
            server.review_scope_of(entry)
            self.assertEqual(scope.call_count, 3)
            marks[0] = None  # git cannot read the work: nothing is reused
            server.review_scope_of(entry)
            server.review_scope_of(entry)
            self.assertEqual(scope.call_count, 5)
            self.assertNotIn('%13', server.REVIEW_SCOPES)
            marks[0] = 'm3'
            scope.side_effect = lambda folder: None  # no readable commit: not cached
            self.assertIsNone(server.review_scope_of(entry))
            self.assertIsNone(server.review_scope_of(entry))
            self.assertEqual(scope.call_count, 7)

    def make_repository(self, root, *, origin=True, files=(('server/app.py', 'x = 1\n'),)):
        folder = Path(root) / 'work'
        def git(*args):
            return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                   '-c', 'commit.gpgsign=false', *args], cwd=folder if folder.exists() else root,
                                  capture_output=True, check=True, text=True).stdout.strip()
        git('init', '-q', '-b', 'main', str(folder))
        for name, text in files:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text)
        git('add', '-A')
        git('commit', '-q', '-m', 'Start')
        if origin:
            git('init', '-q', '--bare', '-b', 'main', str(Path(root) / 'origin.git'))
            git('remote', 'add', 'origin', str(Path(root) / 'origin.git'))
            git('push', '-q', 'origin', 'main')
        return folder, git

    def test_fingerprint_changes_with_everything_a_scope_reads(self):
        # Review round 2, finding 1: the cache key missed an edit of an untracked file and a new file in an
        # untracked folder, so a retried request went out with an old checklist result and UI list.
        with tempfile.TemporaryDirectory() as root:
            folder, git = self.make_repository(root)
            seen = [review_scope.fingerprint(folder)]
            self.assertIsInstance(seen[0], str)
            self.assertEqual(review_scope.fingerprint(folder), seen[0])  # nothing changed
            self.assertEqual(review_scope.fingerprint(folder / 'server'), seen[0])  # a subfolder is the same work
            def changed(step):
                found = review_scope.fingerprint(folder)
                self.assertNotIn(found, seen, step)
                seen.append(found)
            (folder / 'tools').mkdir()
            (folder / 'tools/stop.sh').write_text('echo stop\n')
            changed('new untracked file')
            (folder / 'tools/stop.sh').write_text('tmux kill-server\n')
            changed('untracked file edited')
            # Review round 3, finding 1: same size, modification time put back (cp -p, touch -r).
            before = os.stat(folder / 'tools/stop.sh')
            time.sleep(0.02)
            (folder / 'tools/stop.sh').write_text('tmux kill-SERVER\n')
            os.utime(folder / 'tools/stop.sh', ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(os.stat(folder / 'tools/stop.sh').st_mtime_ns, before.st_mtime_ns)
            changed('same size and modification time')
            (folder / 'ui/src').mkdir(parents=True)
            (folder / 'ui/src/a.tsx').write_text('a\n')
            changed('untracked folder')
            (folder / 'ui/src/new.tsx').write_text('b\n')
            changed('new file in an untracked folder')
            (folder / 'server/app.py').write_text('x = 2\n')
            changed('tracked edit')
            git('add', 'server/app.py')
            changed('staged')
            (folder / 'server/app.py').write_text('x = 1\n')
            changed('working copy differs from the index')
            git('commit', '-q', '-m', 'Next')
            changed('commit')
            git('push', '-q', 'origin', 'main')
            changed('origin/main moved')
        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(review_scope.fingerprint(plain))
        self.assertIsNone(review_scope.fingerprint(None))
        with patch.object(review_scope.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 5)):
            self.assertIsNone(review_scope.fingerprint('/project'))

    def test_fingerprint_matches_the_buffered_digest(self):
        for origin in (True, False):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as root:
                folder, git = self.make_repository(root, origin=origin)
                tracked = folder / 'server/app.py'
                tracked.write_text('x = 2\n')
                git('add', 'server/app.py')
                tracked.write_text('x = 3\n')
                (folder / 'untracked file').write_text('untracked\n')

                def buffered(*args):
                    return subprocess.run(['git', *args], cwd=folder, capture_output=True)

                base = buffered('rev-parse', '--verify', '--quiet', 'origin/main')
                expected_base = hashlib.sha256(base.stdout + bytes([base.returncode & 255])).hexdigest()
                expected = hashlib.sha256()
                for command in (('rev-parse', 'HEAD'), ('status', '--porcelain', '-z', '--untracked-files=all'),
                                ('diff', '--no-ext-diff', '--no-textconv', '--text', 'HEAD'),
                                ('diff', '--no-ext-diff', '--no-textconv', '--text', '--cached', 'HEAD'),
                                ('ls-files', '--others', '--exclude-standard', '-z')):
                    result = buffered(*command)
                    self.assertEqual((result.returncode, result.stderr), (0, b''))
                    expected.update(result.stdout + b'\0\0')
                for name in result.stdout.split(b'\0'):
                    if name:
                        found = os.lstat(os.path.join(os.fsencode(folder), name))
                        expected.update(b'%d:%d:%d:%d;' %
                                        (found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_ino))
                self.assertEqual(review_scope.fingerprint(folder), f'{expected.hexdigest()}:{expected_base}')

    def test_fingerprint_reads_large_diffs_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as root:
            folder, git = self.make_repository(root)
            large = folder / 'large.txt'
            large.write_bytes((b'x' * 63 + b'\n') * (48 * 1024))
            staged_size = large.stat().st_size
            git('add', 'large.txt')
            with large.open('ab') as file:
                file.write(b'worktree change\n')
            total = 0

            def read(fd, size):
                nonlocal total
                self.assertLessEqual(size, 64 * 1024)
                chunk = os.read(fd, size)
                total += len(chunk)
                return chunk

            with patch.object(review_scope, 'os', Mock(wraps=os, environ=os.environ, read=read)):
                self.assertIsInstance(review_scope.fingerprint(folder), str)
            self.assertGreater(total, 2 * staged_size)

    def test_fingerprint_rejects_failed_streamed_commands(self):
        with tempfile.TemporaryDirectory() as root:
            folder, _ = self.make_repository(root)
            popen = subprocess.Popen
            hash_command = review_scope._hash_command
            for case, script, exit_code, timed_out in (
                ('start failure', None, None, False),
                ('stderr', 'import sys; sys.stderr.buffer.write(b"x" * (1024 * 1024)); print("partial")', 0, False),
                ('nonzero exit', 'print("partial"); raise SystemExit(7)', 7, False),
                ('busy pipes', 'import os\nwhile True: os.write(1, b"x" * 65536)', -signal.SIGKILL, True),
                ('closed pipes', 'import os; os.close(1); os.close(2); fd, writer = os.pipe(); os.read(fd, 1)',
                 -signal.SIGKILL, True),
            ):
                child = None

                def start(argv, **kwargs):
                    nonlocal child
                    if argv[:2] != ['git', 'diff']:
                        return popen(argv, **kwargs)
                    if script is None:
                        raise OSError('could not start git')
                    # Let a broken timeout fail the test instead of leaving an endless fake command.
                    child = popen([sys.executable, '-c', 'import signal; signal.alarm(5)\n' + script], **kwargs)
                    return child

                def run_hash(*args):
                    with patch.object(review_scope, 'TIMEOUT', 0.2 if timed_out else review_scope.TIMEOUT):
                        return hash_command(*args)

                with self.subTest(case=case), patch.object(subprocess, 'Popen', start), \
                        patch.object(review_scope, '_hash_command', run_hash):
                    self.assertIsNone(review_scope.fingerprint(folder))
                    if script is not None:
                        self.assertIsNotNone(child)
                        self.assertEqual(child.returncode, exit_code)
                        self.assertTrue(child.stdout.closed)
                        self.assertTrue(child.stderr.closed)
                        with self.assertRaises(ChildProcessError):
                            os.waitpid(child.pid, os.WNOHANG)

    def test_cached_scope_is_not_reused_after_an_untracked_edit_in_a_real_repository(self):
        with tempfile.TemporaryDirectory() as root:
            folder, _ = self.make_repository(root, files=(('docs/reliability-checklist.md',
                '## R1: Stop\n- Paths: `\\.sh$`\n- Text: `kill-server`\n- Check: c\n- Incident: i\n'),))
            (folder / 'stop.sh').write_text('echo stop\n')
            (folder / 'ui').mkdir()
            (folder / 'ui/notes.txt').write_text('n\n')
            entry = {'id': '%13', 'cwd': str(folder)}
            with patch.object(server, 'REVIEW_SCOPES', {}), patch.object(server, 'agent_folder', return_value=str(folder)):
                first = server.review_scope_of(entry)
                self.assertEqual((first['checklist']['status'], first['ui']), ('clear', []))
                (folder / 'stop.sh').write_text('tmux kill-server\n')
                (folder / 'ui/src').mkdir()
                (folder / 'ui/src/new.tsx').write_text('x\n')
                second = server.review_scope_of(entry)
                self.assertEqual(second['checklist']['status'], 'hit')
                self.assertEqual(second['ui'], ['ui/src/new.tsx'])

    def test_scope_marks_track_staged_and_untracked_edits_with_the_same_head_and_status(self):
        with tempfile.TemporaryDirectory() as root, patch.object(server, 'REVIEW_SCOPES', {}):
            folder, git = self.make_repository(root)
            tracked = folder / 'server/app.py'
            untracked = folder / 'notes.txt'
            entry = {'id': '%13', 'review_folder': str(folder)}
            tracked.write_text('x = 2\n')
            git('add', 'server/app.py')
            untracked.write_text('notes\n')
            # The working copy and status stay identical. Only the staged content changes.
            tracked.write_text('x = 3\n')
            status = git('status', '--porcelain')
            initial = server.review_scope_of(entry)
            tracked.write_text('x = 4\n')
            git('add', 'server/app.py')
            tracked.write_text('x = 3\n')
            self.assertEqual(git('status', '--porcelain'), status)
            staged = server.review_scope_of(entry)
            untracked.write_text('more notes\n')
            self.assertEqual(git('status', '--porcelain'), status)
            edited = server.review_scope_of(entry)
            self.assertEqual({(scope['commit'], scope['dirty']) for scope in (initial, staged, edited)},
                             {(initial['commit'], True)})
            self.assertEqual(len({scope.get('mark') for scope in (initial, staged, edited)}), 3)

    def test_ui_paths_without_a_base_ref_lists_what_git_can_see_or_says_unknown(self):
        # Review round 2, finding 3: no origin/main gave [] before the untracked files were listed.
        with tempfile.TemporaryDirectory() as root:
            folder, git = self.make_repository(root, origin=False)
            self.assertEqual(review_scope.ui_paths(folder), [])  # no UI files in this repository: nothing to ask
            (folder / 'ui/src').mkdir(parents=True)
            (folder / 'ui/src/new.tsx').write_text('x\n')
            self.assertEqual(review_scope.ui_paths(folder), ['ui/src/new.tsx'])
            git('add', '-A')
            self.assertEqual(review_scope.ui_paths(folder), ['ui/src/new.tsx'])  # staged
            git('commit', '-q', '-m', 'UI')
            # Committed, and no base to compare with: a UI repository with an unknown diff.
            self.assertIsNone(review_scope.ui_paths(folder))
            (folder / 'ui/src/new.tsx').write_text('y\n')
            self.assertEqual(review_scope.ui_paths(folder), ['ui/src/new.tsx'])

    def test_ui_paths_with_a_base_ref_include_staged_changes(self):
        with tempfile.TemporaryDirectory() as root:
            folder, git = self.make_repository(root, files=(('ui/src/a.tsx', 'a\n'), ('ui/test_a.cjs', 't\n')))
            self.assertEqual(review_scope.ui_paths(folder), [])
            (folder / 'ui/src/a.tsx').write_text('staged\n')
            (folder / 'ui/test_a.cjs').write_text('changed test\n')
            git('add', '-A')
            (folder / 'ui/src/a.tsx').write_text('a\n')  # the working copy is back; the next commit still changes it
            self.assertEqual(review_scope.ui_paths(folder), ['ui/src/a.tsx'])
            # Review round 3, finding 2: a lock file change alone changes the shipped UI.
            git('commit', '-q', '-m', 'UI')
            git('checkout', '-q', '--', '.')  # the working copy equals the commit again
            git('push', '-q', 'origin', 'HEAD:main')
            self.assertEqual(review_scope.ui_paths(folder), [])
            (folder / 'ui/package-lock.json').write_text('{}\n')
            self.assertEqual(review_scope.ui_paths(folder), ['ui/package-lock.json'])
            git('add', '-A')
            git('commit', '-q', '-m', 'Lock')
            self.assertEqual(review_scope.ui_paths(folder), ['ui/package-lock.json'])
            # Review round 4, finding 1: the commit holds the UI change; a staged revert must not hide it.
            git('rm', '-q', '-f', 'ui/package-lock.json')
            self.assertEqual(review_scope.ui_paths(folder), ['ui/package-lock.json'])
            git('reset', '-q', '--hard')
            # Review round 4, finding 3: the React layouts import the server's manifest.
            (folder / 'server').mkdir(exist_ok=True)
            (folder / 'server/ui_manifest.json').write_text('{}\n')
            self.assertEqual(review_scope.ui_paths(folder), ['server/ui_manifest.json', 'ui/package-lock.json'])

    def test_fingerprint_changes_for_a_tracked_edit_when_git_converts_the_text(self):
        # Review round 4, finding 2: with a textconv driver the diff, and so the digest, stayed the same.
        with tempfile.TemporaryDirectory() as root:
            folder, git = self.make_repository(root, files=(('ui/src/App.tsx', 'one\n'), ('.gitattributes', '*.tsx diff=same\n')))
            git('config', 'diff.same.textconv', 'echo same #')
            first = review_scope.fingerprint(folder)
            (folder / 'ui/src/App.tsx').write_text('const page = lazy(() => import("./Page"))\n')
            second = review_scope.fingerprint(folder)
            self.assertNotEqual(first, second)
            git('add', '-A')
            (folder / 'ui/src/App.tsx').write_text('one\n')
            self.assertNotIn(review_scope.fingerprint(folder), (first, second))  # staged text differs


class ApprovalTurnFixture(unittest.TestCase):
    def setUp(self):
        private = self.enterContext(tempfile.TemporaryDirectory(prefix="b286-"))
        self.root = Path(private)
        self.store = self.root / "review-links.json"
        self.now = 1000.0
        self.worker = {
            "id": "%13", "agent": "claude", "thread": WORKER_THREAD, "writable": True,
        }
        self.reviewer = {
            "id": "%12", "agent": "codex", "thread": THREAD, "writable": True,
        }
        self.send = Mock()
        self.verdict = None
        self.turn_source = ("user", 980.0, 990.0)
        self.turn_started_at = None
        self.next_turn_source = "user"
        self.enterContext(patch.object(review_links, "FILE", self.store))
        self.enterContext(patch.object(review_links, "_last_sent", {}))
        self.enterContext(patch.object(review_links, "_feedback_in_flight", {}))
        self.enterContext(patch.object(review_links.audit, "record"))
        self.enterContext(own_time(review_links, time=lambda: self.now, monotonic=lambda: self.now))
        review_links.add(
            worker=self.worker["id"], reviewer=self.reviewer["id"],
            worker_thread=WORKER_THREAD, reviewer_thread=THREAD,
            name="B286 test", agent="claude",
        )

    def observe(self, worker_state="Idle"):
        self.now += 10
        if worker_state == "Working" and self.turn_started_at is None:
            self.turn_started_at = self.now
        elif worker_state == "Idle" and self.turn_started_at is not None:
            self.turn_source = (self.next_turn_source, self.turn_started_at, self.now - 1)
            self.turn_started_at = None
            self.next_turn_source = "user"

        def status(entry):
            if entry["id"] == self.worker["id"]:
                return {"state": worker_state}
            return {"state": "Idle"}

        review_links.observe(
            [self.worker, self.reviewer], status, self.send,
            lambda entry: "Done." if entry["id"] == self.worker["id"] else "Ready.",
            work_mark=self.work_mark, scope=self.scope, commit=self.commit, fresh=status,
            review_reply=lambda entry, request_id: self.verdict,
            turn_source=lambda entry: self.turn_source, folder=getattr(self, 'folder', None),
        )

    def worker_turn(self):
        self.observe("Working")
        self.observe()

    def deliver_approval(self, commit):
        sent_before = self.send.call_count
        self.verdict = f"APPROVED {commit}"
        self.observe()
        self.verdict = None
        new_messages = self.send.call_args_list[sent_before:]
        if any(call.args[0] == self.worker for call in new_messages):
            self.next_turn_source = "review-feedback"

    def assert_follow_up_requested(self):
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        return self.send.call_args.args[1]

    def reviewer_should_stay_open(self):
        return review_links.unreviewed_work(self.worker, self.reviewer, self.scope)


class ApprovalTurnWorktreeTests(ApprovalTurnFixture):
    def setUp(self):
        super().setUp()
        self.start = self.root / "start"
        self.reviewed = self.root / "reviewed worktree"
        self.gate_log = self.root / "gate.log"
        self.git("init", "-q", "-b", "main", str(self.start), cwd=self.root)
        self.git("commit", "-qm", "Start", "--allow-empty")
        self.git("worktree", "add", "-qb", "reviewed", str(self.reviewed))
        self.commit_in(self.reviewed, "Reviewed commit")
        self.worker.update(cwd=str(self.start), process_start="1234")
        self.enterContext(patch.object(server.paths, "COMMIT_GATE_LOG", self.gate_log))
        self.enterContext(patch.object(server.agents, "started_ms", return_value=900_000))
        self.enterContext(patch.object(server, "REVIEW_GATE_TAIL", (None, {})))
        self.enterContext(patch.object(server, "REVIEW_SCOPES", {}))
        self.scope = server.review_scope_of
        self.commit = server.review_commit
        self.folder = server.review_folder
        self.work_mark = server.review_work_mark

    def git(self, *args, cwd=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(
            [
                "git", "-c", "user.name=B286 Test", "-c", "user.email=test@example.invalid",
                "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", *args,
            ],
            cwd=cwd or self.start, env=env, capture_output=True, check=True,
            text=True, timeout=10,
        ).stdout.strip()

    def record_gate_check(self, folder, previous_head):
        record = {
            "event": "check", "via": "commit-msg", "source": "claude",
            "session": WORKER_THREAD, "folder": str(folder), "at": self.now + 0.1,
            "head": previous_head,
        }
        with self.gate_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")

    def commit_in(self, folder, message):
        previous_head = self.git("rev-parse", "HEAD", cwd=folder)
        self.record_gate_check(folder, previous_head)
        self.git("commit", "-qm", message, "--allow-empty", cwd=folder)
        return self.git("rev-parse", "HEAD", cwd=folder)

    def approve_reviewed_worktree(self, *, earlier_gate_points_to_start=False):
        self.worker_turn()
        self.assertEqual(self.send.call_count, 1)
        self.assertIn(f"in {shlex.quote(str(self.reviewed))}", self.send.call_args.args[1])
        if earlier_gate_points_to_start:
            self.record_gate_check(self.start, self.git("rev-parse", "HEAD"))
        head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        self.deliver_approval(head)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.worker)
        return head

    def make_tracked_file(self):
        path = self.reviewed / "tracked.txt"
        path.write_text("Committed content.\n", encoding="utf-8")
        self.git("add", path.name, cwd=self.reviewed)
        self.commit_in(self.reviewed, "Add tracked file")
        return path

    def another_worktree(self):
        folder = self.root / "third worktree"
        self.git("worktree", "add", "-qb", "third", str(folder), "reviewed")
        return folder

    # Round 1 real Git cases.
    def test_unchanged_follow_up_skips_review_after_gate_worktree_changes(self):
        self.approve_reviewed_worktree(earlier_gate_points_to_start=True)
        self.worker_turn()
        self.assertEqual(self.send.call_count, 2)

    def test_tracked_edit_in_approved_worktree_requests_review(self):
        path = self.make_tracked_file()
        self.git("merge", "--ff-only", "reviewed")
        self.approve_reviewed_worktree(earlier_gate_points_to_start=True)
        self.observe("Working")
        path.write_text("Follow-up edit.\n", encoding="utf-8")
        self.observe()
        self.assert_follow_up_requested()

    def test_idle_guard_keeps_reviewer_for_new_edit_in_approved_worktree(self):
        path = self.make_tracked_file()
        self.git("merge", "--ff-only", "reviewed")
        self.approve_reviewed_worktree(earlier_gate_points_to_start=True)
        path.write_text("New work for review.\n", encoding="utf-8")
        self.assertTrue(self.reviewer_should_stay_open())

    def test_staged_only_edit_requests_review(self):
        path = self.make_tracked_file()

        def stage_and_restore_working_copy(text):
            path.write_text(text, encoding="utf-8")
            self.git("add", path.name, cwd=self.reviewed)
            path.write_text("Committed content.\n", encoding="utf-8")

        stage_and_restore_working_copy("Reviewed staged content.\n")
        staged = self.git("diff", "--cached", "HEAD", cwd=self.reviewed)
        status = self.git("status", "--porcelain", cwd=self.reviewed)
        working_diff = self.git("diff", "HEAD", cwd=self.reviewed)
        self.approve_reviewed_worktree()
        self.observe("Working")
        stage_and_restore_working_copy("Different staged content.\n")
        self.assertNotEqual(self.git("diff", "--cached", "HEAD", cwd=self.reviewed), staged)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.reviewed), status)
        self.assertEqual(self.git("diff", "HEAD", cwd=self.reviewed), working_diff)
        self.observe()
        self.assert_follow_up_requested()

    def test_edit_to_existing_untracked_file_requests_review(self):
        path = self.reviewed / "untracked.txt"
        path.write_text("Reviewed content.\n", encoding="utf-8")
        self.approve_reviewed_worktree()
        self.observe("Working")
        path.write_text("New untracked content.\n", encoding="utf-8")
        self.observe()
        self.assert_follow_up_requested()

    def test_idle_guard_keeps_reviewer_after_edit_to_already_dirty_tree(self):
        path = self.make_tracked_file()
        path.write_text("Reviewed draft.\n", encoding="utf-8")
        self.approve_reviewed_worktree()
        path.write_text("New draft for review.\n", encoding="utf-8")
        self.assertTrue(self.reviewer_should_stay_open())

    # Round 2 worktree routing and partial Git read cases.
    def test_request_names_worktree_with_detected_edit(self):
        self.git("merge", "--ff-only", "reviewed")
        self.approve_reviewed_worktree(earlier_gate_points_to_start=True)
        self.observe("Working")
        path = self.reviewed / "notes.txt"
        path.write_text("New work in the approved worktree.\n", encoding="utf-8")
        self.observe()
        text = self.assert_follow_up_requested()
        expected_folder = shlex.quote(str(self.reviewed))
        self.assertIn(f"in {expected_folder} plus uncommitted changes", text)

    def test_new_commit_in_another_worktree_after_clean_approval_requests_review(self):
        folder = self.another_worktree()
        self.approve_reviewed_worktree()
        self.observe("Working")
        head = self.commit_in(folder, "Follow-up commit in another worktree")
        self.observe()
        self.observe()
        text = self.assert_follow_up_requested()
        self.assertIn(f"Commit under review: {head} in {shlex.quote(str(folder))}", text)

    def test_pending_and_stalled_review_in_another_worktree_keeps_reviewer(self):
        folder = self.another_worktree()
        self.approve_reviewed_worktree()
        self.worker_turn()
        self.assertEqual(self.send.call_count, 2)
        self.observe("Working")
        head = self.commit_in(folder, "Commit in another worktree")
        self.observe()
        text = self.assert_follow_up_requested()
        self.assertIn(f"Commit under review: {head} in {shlex.quote(str(folder))}", text)
        pending_keeps_reviewer = self.reviewer_should_stay_open()

        for _ in range(30):
            self.observe()
        rows = json.loads(self.store.read_text(encoding="utf-8"))
        link = next(row for row in rows if row.get("reviewer") == self.reviewer["id"])
        self.assertTrue(link.get("stalled"))
        stalled_keeps_reviewer = self.reviewer_should_stay_open()
        with self.subTest(phase="pending"):
            self.assertTrue(pending_keeps_reviewer)
        with self.subTest(phase="stalled"):
            self.assertTrue(stalled_keeps_reviewer)

    def test_fingerprint_read_recovers_and_requests_review_of_dirty_edit(self):
        path = self.reviewed / "notes.txt"
        path.write_text("Reviewed draft.\n", encoding="utf-8")
        self.approve_reviewed_worktree()
        self.observe("Working")
        path.write_text("A changed untracked draft after approval.\n", encoding="utf-8")
        with patch.object(server.review_scope, "fingerprint", return_value=None):
            self.observe()
        self.observe()
        self.observe()
        self.assert_follow_up_requested()

    def test_follow_up_commit_is_reviewed_after_one_missing_approval_fingerprint(self):
        self.worker_turn()
        approved_head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        review_scope = server.review_scope
        fingerprint = review_scope.fingerprint
        first_read = True

        def miss_first_read(folder):
            nonlocal first_read
            if first_read:
                first_read = False
                return None
            return fingerprint(folder)

        with patch.object(review_scope, "fingerprint", side_effect=miss_first_read):
            self.deliver_approval(approved_head)
        # A watcher that defers an incomplete verdict can retry the same reply.
        self.deliver_approval(approved_head)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.worker)

        self.observe("Working")
        head = self.commit_in(self.reviewed, "Follow-up commit")
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        text = self.assert_follow_up_requested()
        self.assertIn(f"Commit under review: {head}", text)
        self.assertTrue(self.reviewer_should_stay_open())

    def test_follow_up_reads_git_status_again_after_one_failed_read(self):
        self.approve_reviewed_worktree()
        self.observe("Working")
        head = self.commit_in(self.reviewed, "Follow-up commit")
        review_scope = server.review_scope
        git = review_scope.git
        status_reads = 0

        def fail_first_status_read(folder, *args, **kwargs):
            nonlocal status_reads
            if args[:2] == ("status", "--porcelain=v2"):
                status_reads += 1
                if status_reads == 1:
                    return None
            return git(folder, *args, **kwargs)

        with patch.object(review_scope, "git", side_effect=fail_first_status_read):
            for _ in range(review_links.VERIFY_TICKS + 1):
                self.observe()
        self.assertGreater(status_reads, 1, "Retry must read Git status again")
        self.assertIn(f"Commit under review: {head}", self.assert_follow_up_requested())

    def approve_after_one_failed_read(self, owner, attribute):
        self.worker_turn()
        head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        read = getattr(owner, attribute)
        first_read = True

        def fail_first_read(*args, **kwargs):
            nonlocal first_read
            if first_read:
                first_read = False
                return None
            return read(*args, **kwargs)

        with patch.object(owner, attribute, side_effect=fail_first_read):
            self.deliver_approval(head)
        # Allow the watcher to defer the verdict until the read recovers.
        self.deliver_approval(head)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.worker)
        return head

    def test_follow_up_commit_is_reviewed_after_one_failed_approval_scope_read(self):
        self.approve_after_one_failed_read(self, "scope")
        self.observe("Working")
        head = self.commit_in(self.reviewed, "Follow-up commit")
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        self.assertIn(f"Commit under review: {head}", self.assert_follow_up_requested())

    def test_dirty_to_clean_follow_up_is_reviewed_without_saved_fingerprint(self):
        draft = self.reviewed / "draft.txt"
        draft.write_text("Reviewed uncommitted draft.\n", encoding="utf-8")
        head = self.approve_after_one_failed_read(server.review_scope, "fingerprint")
        self.observe("Working")
        draft.unlink()
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        self.assertIn(f"Commit under review: {head}", self.assert_follow_up_requested())

    def test_named_approval_of_unbound_request_can_review_a_follow_up_commit(self):
        self.worker["cwd"] = str(self.reviewed)
        self.gate_log.write_text("", encoding="utf-8")
        approved_head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        self.assertEqual(self.commit(self.worker), approved_head)

        with patch.object(self, "scope", return_value=None):
            self.worker_turn()
            self.assertEqual(self.send.call_count, 1)
            self.assertNotIn("Commit under review:", self.send.call_args.args[1])
            self.deliver_approval(approved_head)
        # The watcher can defer approval until it can read the full scope.
        self.deliver_approval(approved_head)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.worker)

        self.observe("Working")
        self.git("commit", "-qm", "Follow-up commit", "--allow-empty", cwd=self.reviewed)
        new_head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        folder = shlex.quote(str(self.reviewed))
        self.assertIn(f"Commit under review: {new_head} in {folder}", self.assert_follow_up_requested())

    def approve_named_without_scope(self):
        head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        self.assertEqual(self.commit(self.worker), head)
        with patch.object(self, "scope", return_value=None):
            self.worker_turn()
            self.assertEqual(self.send.call_count, 1)
            self.assertNotIn("Commit under review:", self.send.call_args.args[1])
            self.deliver_approval(head)
        # The watcher can retry an approval after it recovers the scope.
        self.deliver_approval(head)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.worker)
        return head

    def test_unchanged_push_after_named_approval_of_unbound_request_skips_review(self):
        self.worker["cwd"] = str(self.reviewed)
        self.gate_log.write_text("", encoding="utf-8")
        remote = self.root / "local-remote.git"
        self.git("init", "-q", "--bare", str(remote), cwd=self.root)
        self.git("remote", "add", "local-test", str(remote), cwd=self.reviewed)
        head = self.approve_named_without_scope()
        before = server.review_scope.fingerprint(self.reviewed)
        self.observe("Working")
        self.git("push", "-q", "local-test", "HEAD:refs/heads/main", cwd=self.reviewed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.reviewed), head)
        self.assertEqual(server.review_scope.fingerprint(self.reviewed), before)
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        self.assertEqual(self.send.call_count, 2)

    def test_named_approval_keeps_the_worktree_used_to_verify_its_hash(self):
        self.approve_named_without_scope()
        self.observe("Working")
        # This commit uses Git directly and leaves the older gate record in place.
        self.git("commit", "-qm", "Follow-up work", "--allow-empty", cwd=self.reviewed)
        head = self.git("rev-parse", "HEAD", cwd=self.reviewed)
        self.assertEqual(self.commit(self.worker), head)
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        self.assertIn(f"Commit under review: {head} in {shlex.quote(str(self.reviewed))}",
                      self.assert_follow_up_requested())

    def test_named_approval_reviews_commit_in_verified_worktree_after_gate_log_rotates(self):
        # Both worktrees start at the approved commit. The gate record identifies the reviewed one.
        self.git("merge", "--ff-only", "reviewed")
        self.assertEqual(server.review_folder(self.worker), str(self.reviewed))
        self.approve_named_without_scope()

        self.observe("Working")
        head = self.commit_in(self.reviewed, "Follow-up commit in the verified worktree")
        # Rotation moves the records out of the file read by the worktree resolver.
        self.gate_log.replace(self.gate_log.with_suffix(".log.1"))
        self.gate_log.touch()
        for _ in range(review_links.VERIFY_TICKS + 1):
            self.observe()
        folder = shlex.quote(str(self.reviewed))
        self.assertIn(f"Commit under review: {head} in {folder}", self.assert_follow_up_requested())

    def test_rejected_gate_check_preserves_review_of_worktree_with_edit(self):
        alternate = self.another_worktree()
        head = self.approve_reviewed_worktree()
        self.observe("Working")
        record = {
            "event": "check", "via": "commit-msg", "source": "claude",
            "session": self.worker["thread"], "folder": str(alternate),
            "at": self.now + 0.1, "head": head, "verdict": "block",
        }
        with self.gate_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
        path = self.reviewed / "notes.txt"
        path.write_text("Actual follow-up edit in the reviewed worktree.\n", encoding="utf-8")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=alternate), head)
        self.assertEqual(self.git("status", "--porcelain", cwd=alternate), "")
        self.observe()
        folder = shlex.quote(str(self.reviewed))
        text = self.assert_follow_up_requested()
        self.assertIn(f"Commit under review: {head} in {folder} plus uncommitted changes", text)

    def test_idle_close_skips_the_checklist_subprocess(self):
        from test_helper_close import MockPanes

        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.reviewed)
        self.approve_reviewed_worktree()
        self.worker_turn()
        self.assertEqual(self.send.call_count, 2)

        self.enterContext(patch.object(undo, "capture_safe"))
        self.enterContext(patch.object(undo, "record_tab_close"))
        self.enterContext(patch.object(helper_close.auto_resume, "forget_pane"))
        self.enterContext(patch.object(helper_close.groups, "closing", return_value=nullcontext()))
        panes = MockPanes(self.worker, self.reviewer)
        entries = [self.worker, self.reviewer]
        lock = threading.Lock()
        checker = str(server.review_scope.CHECKER)
        launches = []
        popen = subprocess.Popen

        def idle(entry):
            return {"state": "Idle"}

        def record_launch(args, *positional, **kwargs):
            if len(args) > 1 and str(args[1]) == checker:
                phase = "under the send lock" if lock.locked() else "before the send lock"
                launches.append(phase)
            return popen(args, *positional, **kwargs)

        closer = server.HELPER_CLOSER
        with patch.multiple(closer, now=lambda: self.now, active={}, since={}, last_sweep=float("-inf")):
            closer.observe(entries, idle, panes, lock=lock, fresh=idle, log=lambda text: None)
            self.now += helper_close.IDLE + 1
            with patch.object(subprocess, "Popen", side_effect=record_launch):
                closer.observe(entries, idle, panes, lock=lock, fresh=idle, log=lambda text: None)

        self.assertEqual(panes.closed, [self.reviewer["id"]])
        self.assertEqual(launches, [], "Idle close must skip the checklist subprocess")



class ApprovalTurnTimingFixture(ApprovalTurnFixture):
    HEAD = "a" * 40
    NEXT = "b" * 40

    def setUp(self):
        super().setUp()
        self.live = {
            "commit": self.HEAD, "dirty": False, "mark": "original work",
            "folder": str(self.root / "reviewed worktree"),
        }
        self.scope = lambda entry: dict(self.live)
        self.commit = lambda entry: self.live["commit"]
        self.work_mark = lambda entry: self.live["mark"]

    def approve_current_work(self):
        self.worker_turn()
        self.deliver_approval(self.HEAD)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], self.worker)

class ApprovalTurnTimingTests(ApprovalTurnTimingFixture):
    def test_late_short_commit_with_same_reply_requests_review(self):
        self.approve_current_work()
        for _ in range(6):
            self.observe()
        self.live["commit"] = self.NEXT
        self.turn_source = ("review-feedback", self.now + 1, self.now + 2)
        self.observe()
        self.observe()
        text = self.assert_follow_up_requested()
        self.assertIn(f"Commit under review: {self.NEXT}", text)

    def test_dirty_edit_requests_review_after_missing_mark_recovers(self):
        self.live["dirty"] = True
        self.approve_current_work()
        self.observe("Working")
        self.live["mark"] = "new work with the same HEAD and status"
        first_read = True

        def scope(entry):
            nonlocal first_read
            snapshot = dict(self.live)
            if first_read:
                first_read = False
                snapshot["mark"] = None
            return snapshot

        self.scope = scope
        self.observe()
        self.observe()
        self.observe()
        self.assert_follow_up_requested()

    def test_approval_is_withheld_when_pre_send_scope_sees_new_head(self):
        self.worker_turn()

        def read_commit(entry):
            old_head = self.live["commit"]
            self.live["commit"] = self.NEXT
            return old_head

        self.commit = read_commit
        self.deliver_approval(self.HEAD)
        approvals = [
            call.args[1] for call in self.send.call_args_list
            if call.args[0] == self.worker and "Approved by the reviewer tab" in call.args[1]
        ]
        self.assertEqual(approvals, [])

    def test_late_push_skips_review_after_an_earlier_background_turn_finishes(self):
        self.approve_current_work()
        sent_at = self.now
        self.turn_source = ("background", sent_at - 1, sent_at + 1)
        self.observe()
        self.worker_turn()
        self.assertEqual(self.send.call_count, 2)

    def test_unchanged_approval_turn_with_only_a_finish_time_allows_idle_close(self):
        self.approve_current_work()
        approval = self.send.call_args.args[1]
        self.observe("Working")
        finish_time = datetime.fromtimestamp(self.now + 1, timezone.utc).isoformat()
        transcript = self.root / "worker.jsonl"
        records = (
            {"type": "user", "message": {"content": approval}},
            {"type": "assistant", "timestamp": finish_time, "message": {
                "content": [{"type": "text", "text": "Pushed."}],
            }},
        )
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.worker["transcript"] = str(transcript)
        self.turn_started_at = None  # Use the transcript here instead of the fixture's clock.
        self.turn_source = server.review_turn_source(self.worker)
        self.observe()
        self.assertEqual(self.send.call_count, 2)

        self.now += helper_close.IDLE
        self.observe()
        self.assertFalse(self.reviewer_should_stay_open(),
                         f"The completed approval turn {self.turn_source!r} should allow idle close")

    def test_clipped_untimed_approval_turn_without_a_marker_allows_idle_close(self):
        self.approve_current_work()
        approval = self.send.call_args.args[1]
        rows = json.loads(self.store.read_text())
        next(row for row in rows if row.get("cap")).pop("feedback_turn")
        self.store.write_text(json.dumps(rows))
        self.observe("Working")
        transcript = self.root / "worker.jsonl"
        write_records(transcript, [
            claude("user", approval), claude("assistant", "x" * CLIPPED_REVIEW_OUTPUT), claude("assistant", "Pushed."),
        ])
        self.worker["transcript"] = str(transcript)
        self.turn_started_at = None  # Use the transcript here instead of the fixture's clock.
        self.turn_source = server.review_turn_source(self.worker)
        self.assertEqual(self.turn_source, ("unknown", None, None))
        self.observe()
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse(self.reviewer_should_stay_open())

    def assert_late_push_skips_review_after_earlier_untimed_prompt(self, prompt):
        self.worker_turn()
        transcript = self.root / "worker.jsonl"
        self.worker["transcript"] = str(transcript)
        earlier_prompt = {
            "type": "attachment",
            "attachment": {"type": "queued_command", "prompt": prompt},
        }
        transcript.write_text(json.dumps(earlier_prompt) + "\n", encoding="utf-8")
        self.deliver_approval(self.HEAD)
        self.assertEqual(self.send.call_count, 2)

        finished = {
            "type": "assistant",
            "timestamp": datetime.fromtimestamp(self.now + 1, timezone.utc).isoformat(),
            "message": {"content": [{"type": "text", "text": "Status checked."}]},
        }
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(finished) + "\n")
        self.turn_source = server.review_turn_source(self.worker)
        self.observe()
        self.worker_turn()  # The approval starts now and sends an unchanged push.
        self.assertEqual(self.send.call_count, 2)

    def test_earlier_untimed_user_prompt_preserves_approval_for_late_push(self):
        self.assert_late_push_skips_review_after_earlier_untimed_prompt("Check the status without editing files.")

    def test_earlier_untimed_review_feedback_preserves_approval_for_late_push(self):
        prompt = ("[Tether review] Feedback from the reviewer tab (%21):\n\nCheck the status.\n\n"
                  "Fix what it raises, then stop and wait.")
        self.assert_late_push_skips_review_after_earlier_untimed_prompt(prompt)

    def test_late_push_skips_review_after_an_unrelated_review_notice(self):
        self.approve_current_work()
        sent_at = self.now
        self.turn_source = ("review-notice", sent_at + 1, sent_at + 2)
        self.observe()
        self.worker_turn()
        self.assertEqual(self.send.call_count, 2)

    def test_known_clean_baseline_detects_dirty_follow_up_without_saved_mark(self):
        self.live["mark"] = None
        self.approve_current_work()
        self.live.update(dirty=True, mark="first readable mark")
        self.worker_turn()
        self.assertIn("plus uncommitted changes", self.assert_follow_up_requested())

    def test_change_during_an_unrelated_review_notice_requests_review(self):
        self.approve_current_work()
        sent_at = self.now
        self.live["commit"] = self.NEXT
        self.turn_source = ("review-notice", sent_at + 1, sent_at + 2)
        self.observe()
        self.assertIn(f"Commit under review: {self.NEXT}", self.assert_follow_up_requested())

    def test_first_idle_checks_change_when_approval_was_queued_during_work(self):
        self.live["commit"] = None
        self.worker_turn()
        self.observe("Working")
        self.verdict = "APPROVED"
        self.observe("Working")
        self.assertEqual(self.send.call_args.args[0], self.worker)

        self.live.update(commit=self.HEAD, dirty=True, mark="new edit")
        self.observe()
        self.assertIn("plus uncommitted changes", self.assert_follow_up_requested())

    def test_earlier_feedback_keeps_approval_until_a_later_user_turn_finishes(self):
        self.approve_current_work()
        sent_at = self.now
        self.scope = Mock(wraps=self.scope)
        self.turn_source = ("review-feedback", sent_at - 1, sent_at + 1)
        self.observe()
        self.observe()
        self.scope.assert_called_once()
        self.assertTrue(self.reviewer_should_stay_open())

        self.turn_source = ("user", sent_at + 2, sent_at + 3)
        self.observe()
        self.assertEqual(self.scope.call_count, 2)
        self.assertFalse(self.reviewer_should_stay_open())
        self.assertEqual(self.send.call_count, 2)


if __name__ == '__main__':
    unittest.main()
