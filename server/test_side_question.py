"""Checks for Claude Code's /btw side question card. Frames in fixtures/ come from Claude Code 2.1.325."""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import prompt_actions
import side_question
import terminal_input as terminal
import terminal_replies

FIXTURES = Path(__file__).parent / 'fixtures'
ENTRY = {'id': '%7', 'agent': 'claude', 'owner_pid': 11, 'process_start': '22', 'thread': 'chat-1', 'writable': True}
MARKDOWN = '| Command | Use |\n|---|---|\n| `ls -la` | List all files, including hidden ones, with permissions and sizes. |\n'


def frame(name):
    raw = (FIXTURES / name).read_text()
    return prompt_actions.plain(raw), raw if name.endswith('.ansi') else None


class DetectTests(unittest.TestCase):
    def test_finished_answer(self):
        found = side_question.detect(*frame('claude_btw_answer.txt'))
        self.assertTrue(found['done'])
        self.assertTrue(found['fork'])
        self.assertTrue(found['shown'].startswith('give a markdown table of 3 linux commands'))
        self.assertTrue(found['shown'].endswith('…'))
        self.assertIn('ls -la', found['text'])
        self.assertNotIn('Esc to close', found['text'])
        self.assertNotIn('/btw', found['text'])

    def test_answer_in_progress_has_no_body(self):
        found = side_question.detect(*frame('claude_btw_answering.txt'))
        self.assertEqual((found['done'], found['text'], found['fork']), (False, '', False))

    def test_narrow_pane_wraps_hints_and_lists_earlier_questions(self):
        found = side_question.detect(*frame('claude_btw_narrow.ansi'))
        self.assertEqual(found['shown'], 'say hello in two short sentences…')
        self.assertTrue(found['done'] and found['fork'])
        self.assertTrue(found['text'].startswith('Hello there.'))
        self.assertNotIn('to browse', found['text'])

    def test_bold_row_is_the_question_when_browsing(self):
        found = side_question.detect(*frame('claude_btw_narrow_earlier.ansi'))
        self.assertEqual(found['shown'], 'give a markdown table of 3 linux…')
        self.assertFalse(found['fork'])  # Claude offers the fork key only for the newest answer

    def test_normal_screens_are_not_overlays(self):
        for name in ('claude_hint_above_box.txt', 'claude_numbered_draft.txt', 'claude_permissions_ask.txt'):
            self.assertIsNone(side_question.detect((FIXTURES / name).read_text()), name)
        chat = '● Use /btw to ask a side question.\n\n❯ /btw what is this\n' + '─' * 40 + '\n❯ \n' + '─' * 40 + '\n  Esc to close'
        self.assertIsNone(side_question.detect(chat))

    def test_matches_survives_wrapping_and_table_redraw(self):
        self.assertTrue(side_question.matches(MARKDOWN, side_question.detect(*frame('claude_btw_answer.txt'))['text']))
        self.assertTrue(side_question.matches(MARKDOWN, side_question.detect(*frame('claude_btw_narrow_earlier.ansi'))['text']))
        self.assertFalse(side_question.matches('def unrelated(): return other_things', 'Hello there. Good to see you.'))
        # Half the words on screen is not proof: another pane's copy can share common words.
        self.assertFalse(side_question.matches('Hello there. Totally different content follows here today', 'Hello there. Good to see you.'))
        self.assertTrue(side_question.matches('42', 'The answer: 42'))


class QuestionTests(unittest.TestCase):
    def test_full_question_comes_from_this_chat_history(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / 'history.jsonl'
            history.write_text('\n'.join(json.dumps(row) for row in (
                {'display': '/btw say hello in two short sentences from another chat', 'sessionId': 'other'},
                {'display': '/btw say hello in two short sentences then give one bullet', 'sessionId': 'chat-1'},
                {'display': 'plain message', 'sessionId': 'chat-1'})) + '\n')
            self.assertEqual(side_question.full_question(ENTRY, 'say hello in two short sentences…', history),
                             'say hello in two short sentences then give one bullet')
            self.assertEqual(side_question.full_question(ENTRY, 'not in the file…', history), 'not in the file')
        self.assertEqual(side_question.full_question(ENTRY, 'short', Path(folder) / 'missing'), 'short')


class CopyTests(unittest.TestCase):
    def tmux(self, buffers_after, texts):
        calls = []
        state = {'sent': False}

        def run(command, **_):
            calls.append(command[1:])
            if command[1] == 'send-keys':
                state['sent'] = True
            if command[1] == 'list-buffers':
                rows = ['mine\t1'] + (buffers_after if state['sent'] else [])
                return subprocess.CompletedProcess(command, 0, stdout='\n'.join(rows) + '\n')
            if command[1] == 'show-buffer':
                return subprocess.CompletedProcess(command, 0, stdout=texts[command[3]])
            return subprocess.CompletedProcess(command, 0, stdout='')
        return run, calls

    def copy(self, run, name='claude_btw_answer.txt', shown=None):
        plain, styled = frame(name)
        shown = shown or side_question.detect(plain, styled)['shown']
        return side_question.copy_answer(ENTRY, shown, run=run, sleep=lambda _: None,
                                         capture=lambda *_, **__: styled or plain, verify=lambda *_: None)

    def test_copy_key_reads_new_buffers_and_removes_only_them(self):
        run, calls = self.tmux(['buffer0\t5', 'buffer1\t5'], {'buffer0': MARKDOWN, 'buffer1': MARKDOWN})
        self.assertEqual(self.copy(run), MARKDOWN.strip('\n'))
        self.assertIn(['send-keys', '-t', '%7', 'c'], calls)
        self.assertEqual([call[2] for call in calls if call[0] == 'delete-buffer'], ['buffer0', 'buffer1'])

    def test_a_buffer_from_another_pane_is_left_alone(self):
        run, calls = self.tmux(['buffer0\t5'], {'buffer0': 'def unrelated(): return other_things'})
        self.assertIsNone(self.copy(run))
        self.assertFalse([call for call in calls if call[0] == 'delete-buffer'])

    def test_no_key_when_the_screen_changed(self):
        run, calls = self.tmux([], {})
        self.assertIsNone(self.copy(run, shown='another question'))
        self.assertIsNone(self.copy(run, name='claude_btw_answering.txt'))
        self.assertFalse([call for call in calls if call[0] == 'send-keys'])


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(side_question, '_seen', {}))
        self.enterContext(patch.object(side_question, 'full_question', lambda entry, shown: shown.rstrip('…') + ' in full'))
        self.copies, self.saved = [], []

    def observe(self, name, now, answer=MARKDOWN, entry=ENTRY):
        def copy(entry, shown):
            self.copies.append(shown)
            return answer
        plain, styled = frame(name)
        return side_question.observe(entry, plain, styled, now=now, copy=copy,
                                     save=lambda *args: self.saved.append(args[1:]), start=lambda work: work())

    def test_answer_is_fetched_once_and_kept(self):
        self.assertTrue(self.observe('claude_btw_answering.txt', 0)['pending'])
        self.assertEqual(self.copies, [])
        first = self.observe('claude_btw_answer.txt', 1)
        # The Markdown is on its way: no flash of the terminal's drawing of a table.
        self.assertEqual((first['reason'], first['requiresResponse'], first['pending'], first['text']), ('side', False, True, ''))
        second = self.observe('claude_btw_answer.txt', 2)
        self.assertEqual((second['markdown'], second['text'], second['pending'], second['fork']), (True, MARKDOWN, False, True))
        self.assertTrue(second['question'].endswith('in full'))
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(len(self.copies), 1)
        self.assertEqual(self.saved, [(second['question'], MARKDOWN)])

    def test_failed_fetch_retries_a_few_times_then_keeps_the_drawn_text(self):
        for now in range(0, 60, 2):
            card = self.observe('claude_btw_answer.txt', now, answer=None)
        self.assertEqual(len(self.copies), side_question.ATTEMPTS)
        self.assertEqual((card['markdown'], card['pending']), (False, False))
        self.assertIn('ls -la', card['text'])
        self.assertEqual(self.saved, [])

    def test_read_only_pane_and_other_agents_send_no_key(self):
        self.assertIn('ls -la', self.observe('claude_btw_answer.txt', 0, entry=dict(ENTRY, writable=False))['text'])
        self.assertIsNone(self.observe('claude_btw_answer.txt', 0, entry=dict(ENTRY, agent='codex')))
        self.assertEqual(self.copies, [])

    def test_browsing_to_an_earlier_question_fetches_its_answer(self):
        self.observe('claude_btw_narrow.ansi', 0)
        self.observe('claude_btw_narrow_earlier.ansi', 1)
        self.assertEqual(self.copies, ['say hello in two short sentences…', 'give a markdown table of 3 linux…'])

    def test_same_question_asked_again_gets_its_new_answer(self):
        self.observe('claude_btw_answer.txt', 0)
        self.assertEqual(self.observe('claude_btw_answer.txt', 1)['text'], MARKDOWN)
        self.assertTrue(self.observe('claude_btw_answering.txt', 2)['pending'])  # Claude answers the same words again
        self.observe('claude_btw_answer.txt', 3, answer='A new answer.')
        self.assertEqual(self.observe('claude_btw_answer.txt', 4)['text'], 'A new answer.')
        self.assertIsNone(side_question.observe(ENTRY, '❯ \n', now=5))  # closed, then opened again with a fast answer
        self.observe('claude_btw_answer.txt', 7, answer='A third answer.')
        self.assertEqual(self.observe('claude_btw_answer.txt', 8)['text'], 'A third answer.')
        self.assertEqual([answer for _, answer in self.saved], [MARKDOWN, 'A new answer.', 'A third answer.'])

    def test_closed_overlay_is_forgotten_after_the_grace_time(self):
        self.observe('claude_btw_answer.txt', 0)
        self.assertIsNone(side_question.observe(ENTRY, '❯ \n', now=1))
        self.assertIn('%7', side_question._seen)
        self.assertIsNone(side_question.observe(ENTRY, '❯ \n', now=side_question.GRACE + 1))
        self.assertNotIn('%7', side_question._seen)


class StatusTests(unittest.TestCase):
    def test_live_status_shows_the_card_not_a_raw_screen(self):
        plain, _ = frame('claude_btw_answer.txt')
        with patch.object(side_question, '_seen', {}), patch.object(side_question, 'full_question', lambda entry, shown: shown), \
                patch.object(threading.Thread, 'start', lambda self: None), patch.object(terminal, 'capture_quota_notices'):
            status = terminal._live_status(dict(ENTRY), None, plain)
        self.assertEqual(status['state'], 'Idle')
        self.assertEqual(status['attention']['reason'], 'side')
        self.assertIsNone(status['approval'])


class SavedTests(unittest.TestCase):
    def test_side_answer_is_saved_once_as_a_chat_message(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(terminal_replies, 'CACHE', Path(folder)):
            first = terminal_replies.add_side(ENTRY, 'why?', MARKDOWN)
            self.assertIsNone(terminal_replies.add_side(ENTRY, 'why?', MARKDOWN))
            merged = terminal_replies.merge(ENTRY, [])
        self.assertEqual((first['kind'], first['title'], first['text']), ('side', 'why?', MARKDOWN))
        self.assertEqual([item['id'] for item in merged], [first['id']])

    def test_side_answer_follows_the_conversation_not_the_process(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(terminal_replies, 'CACHE', Path(folder)):
            first = terminal_replies.add_side(ENTRY, 'why?', MARKDOWN)
            resumed = terminal_replies.merge(dict(ENTRY, owner_pid=99, process_start='77'), [])
            other_chat = terminal_replies.merge(dict(ENTRY, thread='chat-2'), [])
            codex = terminal_replies.merge(dict(ENTRY, agent='codex'), [])
        self.assertEqual([item['id'] for item in resumed], [first['id']])
        self.assertEqual((other_chat, codex), ([], []))


if __name__ == '__main__':
    unittest.main()
