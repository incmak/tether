from collections import deque
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import paths
import teams_replies
import urgent_ping


class TeamsRepliesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        tools = self.root / 'tools'
        tools.mkdir()
        (tools / 'post-update.py').write_text('')
        (tools / 'chat-replies.py').write_text('')
        self.post = str(tools / 'post-update.py')
        self.config = {'preset': 'briskbot', 'fields': {'space': 'dm'}, 'enabled': True,
                       'command': ['/usr/bin/python3', self.post, '{title}', 'Needs you', '--space', 'dm',
                                   '--line', 'Question|{question}', '--button', 'Open the tab|{url}']}
        for patcher in (mock.patch.object(paths, 'CACHE', self.root / 'cache'),
                        mock.patch.object(paths, 'CONFIG', self.root / 'config')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = 1_000_000.0
        self.pages = {}
        self.calls = []
        self.delivered = []

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if Path(argv[1]).name == 'chat-replies.py':
            after = int(argv[argv.index('--after') + 1])
            page = self.pages.get(after, {'replies': [], 'last': after})
            return SimpleNamespace(returncode=0, stdout=json.dumps(page), stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def deliver(self, text, item):
        self.delivered.append((text, item))
        pane = item['pane'] if isinstance(item, dict) else item
        return {'pane': pane, 'name': 'B126 work', 'result': 'sent'}

    def poll(self, config=None):
        return teams_replies.poll_once(config or self.config, self.deliver, clock=lambda: self.now, runner=self.runner)

    def confirms(self):
        return [argv for argv in self.calls if Path(argv[1]).name == 'post-update.py']

    def test_reader_only_for_the_briskbot_dm(self):
        self.assertEqual(teams_replies.reader(self.config), ['/usr/bin/python3', str(Path(self.post).with_name('chat-replies.py'))])
        self.assertIsNone(teams_replies.reader({**self.config, 'fields': {'space': 'team'}}))
        self.assertIsNone(teams_replies.reader({**self.config, 'preset': 'custom'}))
        (Path(self.post).with_name('chat-replies.py')).unlink()
        self.assertIsNone(teams_replies.reader(self.config))

    def test_answer_goes_to_its_tab_and_thread_once(self):
        key = teams_replies.open_key('%7', 'thread-a', clock=lambda: self.now)
        other = teams_replies.open_key('%9', 'thread-b', clock=lambda: self.now)
        state = paths.CACHE / 'teams-replies.json'
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o600)
        self.pages[0] = {'replies': [{'sequence': 3, 'key': key, 'text': 'Use plan B\r\nthanks\x1b[2J', 'createTime': ''},
                                     {'sequence': 4, 'key': other, 'text': 'Yes', 'createTime': ''},
                                     {'sequence': 5, 'key': 'unknown-key-unknown-key-0', 'text': 'lost', 'createTime': ''}],
                         'last': 5}
        self.assertTrue(self.poll())
        self.assertEqual(self.delivered, [('[Answer from Teams] Use plan B\nthanks[2J', {'pane': '%7', 'thread': 'thread-a'}),
                                          ('[Answer from Teams] Yes', {'pane': '%9', 'thread': 'thread-b'})])
        confirm = self.confirms()
        self.assertEqual(len(confirm), 2)
        self.assertIn('The tab B126 work has your answer.', confirm[0])
        self.assertIn('--plain', confirm[0])
        self.assertNotIn('--reply-key', confirm[0])
        # The cursor moved: the same page is not read or typed again.
        self.assertEqual(json.loads(state.read_text())['after'], 5)
        self.poll()
        self.assertEqual(len(self.delivered), 2)

    def test_no_key_without_a_known_conversation(self):
        self.assertIsNone(teams_replies.open_key('%7', None, clock=lambda: self.now))
        self.assertIsNone(teams_replies.open_key('%7', '', clock=lambda: self.now))
        self.assertFalse((paths.CACHE / 'teams-replies.json').exists())

    def test_a_failed_delivery_does_not_drop_later_answers(self):
        key = teams_replies.open_key('%7', 'thread-a', clock=lambda: self.now)
        self.pages[0] = {'replies': [{'sequence': 1, 'key': key, 'text': 'one', 'createTime': ''},
                                     {'sequence': 2, 'key': key, 'text': 'two', 'createTime': ''}], 'last': 2}
        tried = []
        def deliver(text, item):
            tried.append(text)
            if text.endswith('one'):
                raise OSError('tmux gone')
            return {'pane': '%7', 'name': 'B126 work', 'result': 'sent'}
        teams_replies.poll_once(self.config, deliver, clock=lambda: self.now, runner=self.runner)
        self.assertEqual(tried, ['[Answer from Teams] one', '[Answer from Teams] two'])
        confirms = self.confirms()
        self.assertIn('Not sent to %7: Tether could not send it. Answer in Tether.', confirms[0])
        self.assertIn('The tab B126 work has your answer.', confirms[1])
        self.assertEqual(json.loads((paths.CACHE / 'teams-replies.json').read_text())['after'], 2)

    def test_reads_slow_down_an_hour_after_the_newest_ping(self):
        teams_replies.open_key('%7', 't', clock=lambda: self.now)
        self.assertEqual(self.poll(), teams_replies.POLL)
        self.now += teams_replies.FAST + 1
        self.assertEqual(self.poll(), teams_replies.SLOW)
        self.assertEqual(teams_replies.KEY_TTL, 2 * 86400 + 600)

    def test_an_answer_from_the_last_card_minute_is_delivered_late(self):
        # The card takes answers for one day and BriskBot keeps them one more day; Tether may read late.
        key = teams_replies.open_key('%7', 'thread-a', clock=lambda: self.now)
        self.now += teams_replies.CARD_TTL + teams_replies.ANSWER_TTL - 1
        self.pages[0] = {'replies': [{'sequence': 1, 'key': key, 'text': 'late but kept', 'createTime': ''}], 'last': 1}
        self.poll()
        self.assertEqual([text for text, _ in self.delivered], ['[Answer from Teams] late but kept'])

    def test_skipped_tab_is_reported_in_teams(self):
        key = teams_replies.open_key('%7', 'thread-a', clock=lambda: self.now)
        self.pages[0] = {'replies': [{'sequence': 1, 'key': key, 'text': 'Go', 'createTime': ''}], 'last': 1}
        self.deliver = lambda text, item: {'pane': '%7', 'name': 'B126 work', 'result': 'skipped',
                                           'reason': 'The pane changed conversations. Reload and choose again.'}
        self.poll()
        self.assertIn('Not sent to B126 work: The pane changed conversations. Reload and choose again. Answer in Tether.',
                      self.confirms()[0])

    def test_expired_key_and_no_keys(self):
        self.assertFalse(self.poll())  # no key open: no read at all
        self.assertEqual(self.calls, [])
        key = teams_replies.open_key('%7', 't', clock=lambda: self.now)
        self.now += teams_replies.KEY_TTL + 1
        self.pages[0] = {'replies': [{'sequence': 1, 'key': key, 'text': 'late', 'createTime': ''}], 'last': 1}
        self.assertFalse(self.poll())
        self.assertEqual(self.delivered, [])

    def test_pages_follow_the_cursor_and_read_failure_keeps_it(self):
        key = teams_replies.open_key('%7', 't', clock=lambda: self.now)
        self.pages[0] = {'replies': [], 'last': 200}  # other clients' rows only
        self.pages[200] = {'replies': [{'sequence': 201, 'key': key, 'text': 'second page', 'createTime': ''}], 'last': 201}
        self.poll()
        self.assertEqual([text for text, _ in self.delivered], ['[Answer from Teams] second page'])
        failing = lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout='', stderr='chat-replies failed: 401')
        self.assertTrue(teams_replies.poll_once(self.config, self.deliver, clock=lambda: self.now, runner=failing))
        self.assertEqual(json.loads((paths.CACHE / 'teams-replies.json').read_text())['after'], 201)

    def test_urgent_ping_adds_a_reply_key_for_the_dm_only(self):
        paths.CONFIG.mkdir()
        (paths.CONFIG / 'urgent-ping.json').write_text(json.dumps(self.config))
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stderr=b''))
        with mock.patch.object(urgent_ping, '_PANES', {}), mock.patch.object(urgent_ping, '_SENT', deque()), \
                mock.patch.object(urgent_ping, '_name', lambda pane: ''), \
                mock.patch.object(urgent_ping.threading, 'Thread') as thread:
            self.assertTrue(urgent_ping.send({'pane': '%7', 'thread': 'thread-a', 'next': 'Pick one'}, runner=runner))
            argv = thread.call_args.kwargs['args'][0]
            key = argv[argv.index('--reply-key') + 1]
            self.assertTrue(teams_replies.reader(self.config))
            saved = json.loads((paths.CACHE / 'teams-replies.json').read_text())['keys'][key]
            self.assertEqual((saved['pane'], saved['thread']), ('%7', 'thread-a'))
            (paths.CONFIG / 'urgent-ping.json').write_text(json.dumps({**self.config, 'fields': {'space': 'team'}}))
            self.assertTrue(urgent_ping.send({'pane': '%8', 'thread': 't', 'next': 'Pick'}, runner=runner))
            self.assertNotIn('--reply-key', thread.call_args.kwargs['args'][0])
            # No known conversation: the card goes out without a reply box.
            (paths.CONFIG / 'urgent-ping.json').write_text(json.dumps(self.config))
            self.assertTrue(urgent_ping.send({'pane': '%9', 'next': 'Pick'}, runner=runner))
            self.assertNotIn('--reply-key', thread.call_args.kwargs['args'][0])


if __name__ == '__main__':
    unittest.main()
