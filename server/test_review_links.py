"""Watcher transitions use isolated stores and mocked sends, never live panes."""
import io
import json
import time
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import review_links
import server
from own_time import own_time
from terminal_input import DraftError


THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'
WORKER_THREAD = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'


class ReviewLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.file = Path(temporary.name) / 'cache' / 'review-links.json'
        self.enterContext(patch.object(review_links, 'FILE', self.file))
        self.enterContext(patch.object(review_links, '_last_sent', {}))
        self.enterContext(patch.object(review_links, '_feedback_turns', {}))
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
        self.reply = Mock(return_value='I fixed it.')
        self.add()

    def add(self, **extra):
        review_links.add(**dict({'worker': '%13', 'reviewer': '%12', 'reviewer_thread': THREAD, 'name': 'Fix the bug', 'agent': 'codex'}, **extra))

    def links(self):
        return json.loads(self.file.read_text())

    def observe(self, *, entries=None, elapsed=6, **states):
        self.clock.return_value += elapsed
        self.states.update(states)
        review_links.observe(entries if entries is not None else [self.worker, self.reviewer],
                             self.status, self.send, self.reply, alert=self.alert, work_mark=self.work_mark,
                             review_reply=self.review_reply, turn_source=self.turn_source,
                             scope=self.scope, commit=self.commit, fresh=self.fresh)

    def test_idle_review_forwards_anchored_reply_before_unrelated_turn(self):
        self.review_reply = server.review_reply
        self.request_review()
        request = self.send.call_args.args[1]
        self.send.reset_mock()
        self.reply.return_value = 'Unrelated reply'
        messages = [
            {'role': 'user', 'text': request},
            {'role': 'assistant', 'text': 'APPROVED fine'},
            {'role': 'user', 'text': 'Other request'},
            {'role': 'assistant', 'text': 'Unrelated reply'},
        ]
        with patch.object(server, 'snapshot', return_value={'messages': messages}):
            self.observe()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], self.worker)
        self.assertIn('APPROVED fine', self.send.call_args.args[1])
        self.assertNotIn('Unrelated reply', self.send.call_args.args[1])
        self.assertFalse(self.links()[0]['awaiting'])
        self.assertNotIn('request_id', self.links()[0])

    def test_busy_review_forwards_queued_request_reply_on_first_idle(self):
        self.review_reply = server.review_reply
        self.observe(**{'%12': 'Working'})
        self.request_review()
        request = self.send.call_args.args[1]
        self.assertEqual(self.send.call_args.args[2], 'Working')
        self.assertTrue(self.links()[0]['reviewer_busy_send'])
        self.send.reset_mock()
        self.reply.return_value = 'Unrelated reply'
        messages = [
            {'role': 'user', 'text': 'Existing task'},
            {'role': 'user', 'text': request, 'delivery': 'queued'},
            {'role': 'assistant', 'text': 'APPROVED mid-turn'},
            {'role': 'user', 'text': 'Notice'},
            {'role': 'assistant', 'text': 'Unrelated reply'},
        ]
        with patch.object(server, 'snapshot', return_value={'messages': messages}):
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
        self.assertEqual(updated, self.links()[0])
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
                self.assertEqual(updated, self.links()[0])
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

    def test_paused_reads_once_under_lock_and_skips_unpaused_or_malformed_rows(self):
        link, = self.links()
        paused = dict(link, paused=True)
        links = [link, paused, dict(paused), {'worker': '%20', 'reviewer': '%21', 'paused': True},
                 {'worker': '%20', 'reviewer': '%22', 'paused': False}, None, [], {},
                 {'worker': [], 'reviewer': '%12', 'paused': True},
                 {'worker': '%30', 'reviewer': '%12\n', 'paused': True}]
        def read():
            self.assertTrue(review_links._lock.locked())
            return links
        with patch.object(review_links, '_read', side_effect=read) as read_store:
            self.assertEqual(review_links.paused(), {'%13': ['%12'], '%20': ['%21']})
        read_store.assert_called_once_with()

    def test_remove_drops_every_matching_link_and_keeps_other_rows_under_lock(self):
        link, = self.links()
        other = dict(link, worker='%20')
        self.file.write_text(json.dumps([link, other, dict(link, reviewer='%21'), None, {}]))
        write_store = review_links._write
        def write(links):
            self.assertTrue(review_links._lock.locked())
            write_store(links)
        with patch.object(review_links, '_write', side_effect=write) as write_mock:
            self.assertTrue(review_links.remove('%13'))
        write_mock.assert_called_once_with([other, None, {}])
        self.assertEqual(self.links(), [other, None, {}])

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
        link, = self.links()
        self.assertEqual(set(link), {'worker', 'reviewer', 'reviewer_thread', 'name', 'agent',
                                     'created', 'seen_working', 'worker_thread'})
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

    def request_review(self):
        self.observe(**{'%13': 'Working'})
        self.observe(**{'%13': 'Idle'})

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

    def fail_to_limit(self, findings='Still broken: the lock is not released.'):
        """Run reviews until the limit. Every review still finds a blocking problem."""
        for _ in range(review_links.MAX_ROUNDS):
            self.request_review()
            self.finish_review(findings)

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
        self.request_review()  # approval turn
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

    def test_approval_removes_existing_pause_and_rounds(self):
        self.request_review()
        link, = self.links()
        link.update(rounds=review_links.MAX_ROUNDS, paused=True)
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
        first, second = self.links()
        second['rounds'] = review_links.MAX_ROUNDS
        self.file.write_text(json.dumps([first, second]))
        self.observe(entries=entries, **{'%12': 'Working'})
        self.observe(entries=entries, **{'%12': 'Idle'})  # only the first reviewer sends feedback
        self.assertEqual(self.links()[0]['rounds'], 1)
        self.observe(entries=entries, **{'%13': 'Working'})
        self.observe(entries=entries)
        self.observe(entries=entries, **{'%13': 'Approval'})
        self.observe(entries=entries, **{'%13': 'Working'})
        self.assertEqual([link['rounds'] for link in self.links()], [1, 3])
        self.assertTrue(self.links()[1]['awaiting'])
        self.observe(entries=entries, **{'%13': 'Idle', '%21': 'Working'})
        self.observe(entries=entries, **{'%21': 'Idle'})
        self.assertTrue(self.links()[1]['paused'])
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
        self.assertEqual(len(feedback), 2 * review_links.MAX_ROUNDS)
        self.assertEqual([link['rounds'] for link in self.links()], [3, 3])
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

    def test_malformed_created_is_skipped_without_stopping_other_links(self):
        link, = self.links()
        bad = dict(link, worker='%99', created='invalid')
        self.file.write_text(json.dumps([bad, link]))
        self.observe(**{'%13': 'Working'})
        self.assertEqual(self.links()[0], bad)
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
        self.request_review()
        self.finish_review('APPROVED')
        self.send.reset_mock()
        self.reply.return_value = 'Approved next step is done.'
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

    def test_awaiting_empty_round_limit_pauses_and_approval_clears_both_limits(self):
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
        self.finish_review('APPROVED')
        for key in ('rounds', 'empty_rounds', 'paused'):
            self.assertNotIn(key, self.links()[0])
        self.assertEqual(self.links()[0]['work_mark'], 'tree')

    def test_notice_monitor_fake_pane_tick_preserves_seeded_link(self):
        link, = self.links()
        link['created'] = 0
        self.file.write_text(json.dumps([link]))
        entry = {'id': '%1', 'agent': 'claude', 'thread': 'conversation', 'owner_pid': 1, 'writable': True}
        with patch.object(server, 'sessions', return_value=[entry]), \
                patch.object(server, 'live_status', return_value={'state': 'Idle'}), \
                patch.object(server.NOTIFIER, 'observe'), \
                own_time(server, sleep=Mock(side_effect=StopIteration)):
            with self.assertRaises(StopIteration):
                server.monitor_notices()
        self.assertEqual(self.links(), [dict(link, lost_ticks=1, lost_reason='worker tab is gone')])

    # B205: reviews bound to a commit

    HASH = '1f0e' * 10
    NEXT = '2a9d' * 10

    def bind(self, commit=None, *, dirty=False, rules=True):
        """The worker's folder is at `commit`. The request and the approval check read it from here."""
        self.head = commit or self.HASH
        self.scope = lambda entry: {'commit': self.head, 'dirty': dirty, 'folder': '/work/my tree', 'rules': rules}
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
        # The UI card (ui/src/Messages.tsx REVIEW_REQUEST) still reads the request.
        pattern = (r'^\[Tether review\] The (\S+) tab “([^”]*)” \((%\d+)\) finished a turn\.'
                   r'[\s\S]*?Its last reply:\s*([\s\S]*?)\s*Transcript: (\S*)'
                   r'( This is the last round: your reply stays here\.)?\s*$')
        self.assertEqual(re.match(pattern, text).groups()[:5],
                         ('Codex', 'Fix the bug', '%13', 'I fixed it.', '/project/worker.jsonl'))

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
                self.assertIn('Approved by the reviewer tab (%12):', self.send.call_args.args[1])
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
                    f'[Tether review] Approved by the reviewer tab (%12) for commit {self.HASH[:12]}:\n\n'
                    + reply + '\n\nThe review is complete. Do the next step your instructions give '
                    'for an approved review, then stop.', 'Idle')
                link = self.links()[0]
                self.assertEqual((link['approved_commit'], link['approved_at']), (self.HASH, 1700000000))
                self.assertEqual(review_links.approved_commits(),
                                 {'%13': {'%12': {'commit': self.HASH, 'at': 1700000000}}})

    def test_approval_of_uncommitted_work_is_forwarded_but_not_recorded_as_an_approved_commit(self):
        self.bind()
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertEqual(self.links()[0]['approved_commit'], self.HASH)
        self.bind(dirty=True)
        self.request_review()  # the turn that follows an approval is not reviewed
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        self.assertIn(f'(%12) for commit {self.HASH[:12]} and its uncommitted changes:', self.send.call_args.args[1])
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
        self.assertIn(f'for commit {self.NEXT[:12]}:', self.send.call_args.args[1])
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
                    self.assertIn(f'for commit {self.HASH[:12]}:', self.send.call_args.args[1])
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
        self.fresh = None
        self.observe(elapsed=10)
        self.assertEqual(self.send.call_args.args[0], self.reviewer)
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])
        # Review round 5, finding 1: the approval did reach the tab; the request must not say it was not forwarded.
        self.assertIn(f'Your approval of {self.HASH[:12]} reached the tab after it had moved to another commit, so it '
                      'does not count for the current work. Its last reply:', self.send.call_args.args[1])
        self.assertNotIn('was not forwarded', self.send.call_args.args[1])
        self.assertNotIn('stale_delivered', self.links()[0])

    def test_unbound_approval_that_names_a_commit_needs_a_readable_commit(self):
        # Review round 5, finding 2: the request was not bound and git cannot read HEAD at the reply.
        self.scope = lambda entry: None
        self.commit = Mock(return_value=None)
        self.request_review()
        self.finish_review(f'APPROVED {self.HASH}')
        for _ in range(review_links.VERIFY_TICKS):
            self.observe()
        self.assertTrue(all(call.args[0] == self.reviewer for call in self.send.call_args_list))  # never the worker
        self.assertIn(f'Your approval of {self.HASH[:12]} was not forwarded', self.send.call_args.args[1])
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
        # Review round 6, finding 2: the proof must come from one reading of the commit and its status.
        self.bind()
        self.request_review()
        self.scope = lambda entry: {'commit': self.NEXT, 'dirty': False, 'folder': '/work/my tree', 'rules': True}
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
        self.request_review()  # the turn after a clean approval is not reviewed, as before
        self.assertEqual(self.send.call_count, count)

    def test_follow_up_turn_without_a_commit_is_not_reviewed(self):
        for after in ({'dirty': True}, {'commit': None}, {}):
            with self.subTest(after=after):
                self.add()
                self.bind(dirty=True)
                self.request_review()
                self.finish_review(f'APPROVED {self.HASH}')
                count = self.send.call_count
                if 'commit' in after:
                    self.scope = lambda entry: None  # unreadable
                elif after:
                    self.bind(self.NEXT, dirty=True)  # committed a part, more changes are open
                self.request_review()  # the follow-up turn
                if 'commit' in after:
                    for _ in range(review_links.VERIFY_TICKS):  # git stays unreadable: Tether stops asking
                        self.observe()
                self.assertEqual(self.send.call_count, count)
                self.assertNotIn('approved_dirty', self.links()[0])
                self.assertNotIn('confirm_ticks', self.links()[0])
                if 'commit' in after:
                    self.bind(dirty=True)  # git answers again
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
        self.assertIn(f'for commit {self.HASH[:12]}:', self.send.call_args.args[1])  # the text names the commit
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
        self.scope = lambda entry: None  # git does not answer on the retry
        for _ in range(review_links.SCOPE_TICKS - 2):
            self.observe()
        self.assertEqual(self.send.call_count, 1)  # no unbound request went out
        self.assertEqual(self.links()[0]['stale_commit'], self.HASH)
        self.scope = working
        self.observe()
        self.assertIn(f'Commit under review: {self.NEXT} in', self.send.call_args.args[1])
        self.assertNotIn('scope_ticks', self.links()[0])
        self.finish_review(f'APPROVED {self.HASH}')  # the reviewer repeats the old approval
        self.assertTrue(all(call.args[0] == self.reviewer for call in self.send.call_args_list))

    def test_commit_unreadable_for_long_pauses_the_review_and_asks_the_user(self):
        # Review round 2, finding 1: a timeout cannot show that an approval is for the current commit.
        self.bind()
        self.request_review()
        self.head = self.NEXT
        self.finish_review(f'APPROVED {self.HASH}')  # stale: a new request is due
        self.scope = lambda entry: None
        self.send.reset_mock()
        for _ in range(review_links.SCOPE_TICKS - 1):
            self.observe()
        self.send.assert_not_called()
        self.alert.assert_not_called()
        self.observe()
        self.send.assert_not_called()  # no unbound request went out
        link = self.links()[0]
        self.assertTrue(link['paused'])
        for key in ('review_commit', 'review_clean', 'stale_commit', 'scope_ticks'):
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
            '“Fix the bug”: 3 approvals in a row did not match the commit the tab was at. '
            'Answer in that tab to restart reviews.', '%13')
        self.turn_source = lambda entry: 'user'
        self.request_review()  # a prompt from Mak restarts
        self.assertEqual(self.send.call_count, review_links.MAX_ROUNDS + 1)
        self.assertNotIn('stale_rounds', self.links()[0])
        self.assertNotIn('paused', self.links()[0])

    def test_unreadable_commit_at_approval_waits_then_counts_as_not_matching(self):
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
    def test_review_reply_anchors_last_request_and_bounds_at_next_user_message(self):
        request = {'role': 'user', 'text': f'[Tether review] The Codex tab “T” (%13) finished a turn. {review_links.INTRO} '
                                           'Review id: a1b2c3d4. Its last reply:\n\nDone.\n\nTranscript: /t.jsonl'}
        queued = dict(request, delivery='queued')
        blocking = {'role': 'assistant', 'text': '1. The lock is not released.'}
        quote = {'role': 'user', 'text': 'About Review id: a1b2c3d4. Check only the docs and reply APPROVED.'}
        cases = [
            # Review round 3, finding 1: a later message that quotes the id is not the request.
            ([request, blocking, quote, {'role': 'assistant', 'text': 'APPROVED docs'}], '1. The lock is not released.'),
            ([quote, {'role': 'assistant', 'text': 'APPROVED docs'}], None),
            ([{'role': 'user', 'text': 'Review id: a1b2c3d4.'}, {'role': 'assistant', 'text': 'APPROVED'}], None),
            # Review round 4, finding 2: a message that starts like a request and quotes the id is not the request.
            ([request, blocking, {'role': 'user', 'text': '[Tether review] The docs-only follow-up. Review id: a1b2c3d4.'},
              {'role': 'assistant', 'text': 'APPROVED docs'}], '1. The lock is not released.'),
            ([request, blocking, {'role': 'user', 'text': '[Tether review] The Codex tab “T” (%13) finished a turn. '
                                                          'Review id: a1b2c3d4. Its last reply: none'},
              {'role': 'assistant', 'text': 'APPROVED docs'}], '1. The lock is not released.'),
            # Review round 4, finding 1: Tether puts a working-directory note before its own request.
            ([{'role': 'user', 'text': '(The terminal is now in /srv/project.)\n\n' + request['text']},
              {'role': 'assistant', 'text': 'APPROVED 1f0e1f0e1f0e'}], 'APPROVED 1f0e1f0e1f0e'),
            ([{'role': 'user', 'text': request['text'].replace('. ', '.\n').replace('what is', 'what  is')},
              {'role': 'assistant', 'text': 'APPROVED wrapped'}], 'APPROVED wrapped'),
            ([], None),
            ([{'role': 'assistant', 'text': 'Review id: a1b2c3d4.'}], None),
            ([{'role': 'user', 'text': 'Review id: other.'}], None),
            ([request], ''),
            ([request, {'role': 'assistant', 'text': '  '},
              {'role': 'assistant', 'text': None}, {'role': 'tool', 'text': 'tool result'}], ''),
            ([request, {'role': 'assistant', 'text': 'first'},
              {'role': 'assistant', 'text': '  APPROVED fine  '},
              {'role': 'assistant', 'text': ''},
              {'role': 'user', 'text': 'Other request'},
              {'role': 'assistant', 'text': 'Unrelated reply'}], '  APPROVED fine  '),
            ([request, {'role': 'assistant', 'text': 'old review'}, queued,
              {'role': 'assistant', 'text': 'APPROVED mid-turn'},
              {'role': 'user', 'text': 'Notice', 'delivery': 'queued'},
              {'role': 'assistant', 'text': 'Unrelated reply'}], 'APPROVED mid-turn'),
            ([request, {'role': 'assistant', 'text': 'old review'}, queued,
              {'role': 'user', 'text': 'Other request'},
              {'role': 'assistant', 'text': 'Unrelated reply'}], ''),
        ]
        entry = {'id': '%12'}
        for messages, expected in cases:
            with self.subTest(messages=messages), \
                    patch.object(server, 'snapshot', return_value={'messages': messages}) as snapshot:
                self.assertEqual(server.review_reply(entry, 'a1b2c3d4'), expected)
                snapshot.assert_called_once_with(entry)
        with patch.object(server, 'snapshot', return_value={}):
            self.assertIsNone(server.review_reply(entry, 'a1b2c3d4'))
        with patch.object(server, 'snapshot', side_effect=RuntimeError('no history')):
            self.assertIsNone(server.review_reply(entry, 'a1b2c3d4'))

    def test_last_reply_uses_latest_nonempty_assistant_text(self):
        messages = [{'role': 'assistant', 'text': 'old'}, {'role': 'assistant', 'text': 'latest'},
                    {'role': 'user', 'text': 'question'}, {'role': 'assistant', 'text': '  '},
                    {'role': 'assistant', 'text': None}]
        with patch.object(server, 'snapshot', return_value={'messages': messages}):
            self.assertEqual(server.last_reply({}), 'latest')
        for snapshot in ({'messages': []}, {}):
            with patch.object(server, 'snapshot', return_value=snapshot):
                self.assertEqual(server.last_reply({}), '')
        with patch.object(server, 'snapshot', side_effect=RuntimeError('no history')):
            self.assertEqual(server.last_reply({}), '')

    def test_monitor_runs_watcher_on_each_loop(self):
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
                scope=server.review_scope_of, commit=server.review_commit, fresh=server.fresh_status)

    def test_session_discovery_exception_skips_watcher_and_loop_recovers(self):
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
                scope=server.review_scope_of, commit=server.review_commit, fresh=server.fresh_status)
        self.assertEqual([call.args for call in clock.call_args_list], [(2,), (2,)])
        self.assertEqual(stderr.getvalue(), "session discovery failed: KeyError('pane')\n")

    def test_review_alert_uses_native_payload_keys(self):
        with patch.object(server, 'notify') as notify:
            server.review_alert('Review link lost', 'Tab vanished.', '%13')
        notify.assert_called_once_with({
            'title': 'Review link lost', 'body': 'Tab vanished.', 'tag': 'review-%13', 'session': '%13',
            'priority': 'urgent', 'silent': False, 'url': '/mobile/#session=%2513',
        }, True)

    def test_review_alert_includes_review_actions_only_when_given(self):
        review = {'worker': '%13', 'reviewer': '%12', 'id': 'a1b2c3d4'}
        with patch.object(server, 'notify') as notify:
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
        import review_scope
        with tempfile.TemporaryDirectory() as root:
            origin, folder = Path(root) / 'origin.git', Path(root) / 'work'
            def git(*args, cwd=folder):
                return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                       '-c', 'commit.gpgsign=false', *args], cwd=cwd, capture_output=True, check=True,
                                      text=True).stdout.strip()
            git('init', '--bare', '-b', 'main', str(origin), cwd=root)
            git('init', '-b', 'main', str(folder), cwd=root)
            self.assertIsNone(review_scope.scope(folder))  # no commit yet
            (folder / 'work.txt').write_text('one\n')
            git('add', '.')
            git('commit', '-m', 'One')
            first = git('rev-parse', 'HEAD')
            self.assertEqual(review_scope.scope(folder),
                             {'commit': first, 'dirty': False, 'folder': str(folder), 'rules': False})
            (folder / 'REVIEW.md').write_text('rules\n')
            self.assertTrue(review_scope.scope(folder)['dirty'])  # an untracked file
            git('add', '.')
            git('commit', '-m', 'Rules')
            self.assertFalse(review_scope.scope(folder)['rules'])  # on the branch only: main does not have it yet
            git('remote', 'add', 'origin', str(origin))
            git('push', '-q', 'origin', 'main')
            second = git('rev-parse', 'HEAD')
            self.assertEqual(review_scope.scope(folder),
                             {'commit': second, 'dirty': False, 'folder': str(folder), 'rules': True})
            self.assertEqual(review_scope.head(folder), second)
            (folder / 'work.txt').write_text('two\n')
            self.assertEqual(review_scope.scope(folder)['dirty'], True)
            self.assertEqual(server.review_commit({'cwd': str(folder)}), second)
            self.assertEqual(server.review_scope_of({'cwd': str(folder)})['commit'], second)
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(review_scope.scope(folder))
            self.assertIsNone(review_scope.head(folder))
        self.assertIsNone(review_scope.head(None))
        self.assertIsNone(server.review_scope_of({}))

    def test_scope_uses_bounded_read_only_git_commands_and_handles_errors(self):
        import review_scope
        done = subprocess.CompletedProcess([], 0, (('1f0e' * 10) + '\n').encode())
        status = subprocess.CompletedProcess([], 0, ('# branch.oid ' + '1f0e' * 10 + '\n# branch.head main\n').encode())
        with patch.object(review_scope.subprocess, 'run', return_value=status) as run:
            self.assertEqual(review_scope.scope('/project'),
                             {'commit': '1f0e' * 10, 'dirty': False, 'folder': '/project', 'rules': True})
        # Review round 6, finding 2: one command gives the commit and the status, so they are from the same moment.
        self.assertEqual([call.args[0] for call in run.call_args_list],
            [['git', 'status', '--porcelain=v2', '--branch', '--untracked-files=normal'],
             ['git', 'cat-file', '-e', 'origin/main:REVIEW.md']])
        changed = subprocess.CompletedProcess([], 0, status.stdout + b'1 .M N... 100644 100644 100644 a b work.txt\n')
        with patch.object(review_scope.subprocess, 'run', return_value=changed):
            self.assertIs(review_scope.scope('/project')['dirty'], True)
        untracked = subprocess.CompletedProcess([], 0, status.stdout + b'? new.txt\n')
        with patch.object(review_scope.subprocess, 'run', return_value=untracked):
            self.assertIs(review_scope.scope('/project')['dirty'], True)
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


if __name__ == '__main__':
    unittest.main()
