"""B446 answers are bound to the helper and the current question."""
import unittest_isolated  # noqa: F401
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import attention
import helper_tabs
import task_tree_answer as answers
import terminal_input


THREAD = '00000000-0000-0000-0000-000000000012'
HELPER = {'id': '%12', 'thread': THREAD, 'agent': 'claude', 'registry': 'test', 'writable': True}
REPLY = 'Next: Which format should I use?\nNeeds you: yes'
SCREEN = ('────────────────────\n ☐ Format\n\nWhich format should I use?\n\n'
          '❯ 1. Text\n  2. Image\n  3. Type something.\n'
          '────────────────────\n  4. Chat about this\n\n'
          'Enter to select · ↑/↓ to navigate · Esc to cancel\n')


def menu_row(screen, entry=HELPER):
    styled = screen
    screen = terminal_input.prompt_actions.plain(screen)
    with patch.dict(terminal_input._attention_seen, clear=True):
        terminal_input.attention(entry, screen, False, now=0, styled_screen=styled)
        prompt = terminal_input.attention(entry, screen, False, now=4, styled_screen=styled)
    return {'state': 'Input needed', 'attention': prompt}


class FakeTerminal:
    def __init__(self, screen):
        self.screen = screen
        self.writes = []
        self.buffer = ''

    def __call__(self, argv, **kwargs):
        if argv[1] == 'capture-pane':
            return subprocess.CompletedProcess(argv, 0, self.screen)
        self.writes.append(argv)
        if argv[1] == 'load-buffer':
            self.buffer = kwargs['input'].decode()
        if argv[1] == 'paste-buffer':
            self.screen = self.screen.replace('Type something.', self.buffer)
        if argv[1] == 'send-keys' and argv[-1] == 'Down':
            cursor = self.screen.index('❯')
            rest = self.screen[cursor + 1:].replace('\n  ', '\n❯ ', 1)
            self.screen = self.screen[:cursor] + ' ' + rest
        return subprocess.CompletedProcess(argv, 0, '')


class HelperAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transcript = Path(self.temp.name) / 'chat.jsonl'
        self.transcript.write_text(REPLY)
        self.entry = dict(HELPER, transcript=str(self.transcript))
        self.store = attention.Attention(Path(self.temp.name) / 'asks.json')
        self.store.note_reply(HELPER['id'], THREAD, REPLY, 'reply-1')
        store_patch = patch.object(attention, 'STORE', self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.send = Mock()
        self.reply_turn = Mock(return_value=(REPLY, 'reply-1'))

    def digest(self, row):
        return answers.prompt_digest(self.entry, row, reply_turn=self.reply_turn)

    def answer(self, row, digest, run, **kwargs):
        body = {'pane': HELPER['id'], 'thread': THREAD, 'digest': digest, **kwargs}
        return answers.answer(body, self.entry, run, lambda _: row, self.send, reply_turn=self.reply_turn)

    def test_idle_reply_goes_to_the_child(self):
        row = {'state': 'Idle', 'ask': self.store.ask(HELPER['id'], THREAD)}
        run = Mock()
        self.assertEqual(self.answer(row, self.digest(row), run, text='Use text'), {'ok': True})
        self.send.assert_called_once_with(self.entry, 'Use text', 'Idle')
        run.assert_not_called()

    def test_new_turn_with_the_same_reply_cannot_use_old_ask(self):
        row = {'state': 'Idle', 'ask': self.store.ask(HELPER['id'], THREAD)}
        digest = self.digest(row)
        self.reply_turn.return_value = REPLY, 'reply-2'
        with self.assertRaises(helper_tabs.Stale):
            self.answer(row, digest, Mock(), text='Use text')
        self.send.assert_not_called()
        self.assertIsNone(self.digest(row))

    def test_transcript_activity_invalidates_the_seen_ask(self):
        row = {'state': 'Idle', 'ask': self.store.ask(HELPER['id'], THREAD)}
        digest = self.digest(row)
        self.transcript.write_text(REPLY + '\nNew input')
        with self.assertRaises(helper_tabs.Stale):
            self.answer(row, digest, Mock(), text='Use text')
        self.send.assert_not_called()

    def test_unverified_menu_has_no_digest_and_neither_answer_path_sends_keys(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'task_tree_menus'
        for path in sorted(fixtures.glob('*.txt')):
            with self.subTest(fixture=path.name), patch.object(terminal_input, 'verify'):
                screen = path.read_text()
                row = menu_row(screen)
                digest = self.digest(row)
                run = FakeTerminal(screen)
                with self.assertRaises(helper_tabs.Stale):
                    self.answer(row, digest or self.digest(menu_row(SCREEN)), run, choice=1)
                self.assertIsNone(digest)
                self.assertEqual(run.writes, [])
                with self.assertRaises(terminal_input.MenuChanged):
                    terminal_input.menu_choose(self.entry, 1, run=run)
                self.assertEqual(run.writes, [])

    def test_clipped_codex_model_picker_does_not_send_enter(self):
        path = Path(__file__).parent / 'fixtures' / 'codex_0.169.0_model_menu.ansi'
        screen = terminal_input.prompt_actions.plain(path.read_text())
        run = FakeTerminal(screen)
        with patch.object(terminal_input, 'verify'), self.assertRaises(terminal_input.MenuChanged):
            terminal_input.menu_choose(dict(self.entry, agent='codex'), 1, run=run)
        self.assertEqual(run.writes, [])

    def test_complete_captured_menus_are_actionable(self):
        here = Path(__file__).parent
        fixtures = (
            ('claude', 'fixtures/claude_question_narrow.txt', 1, 'Mask the real org'),
            ('claude', 'test_fixtures/native_ask/single.txt', 1, 'React'),
            ('codex', 'test_fixtures/pickers/codex_models.txt', 3, 'GPT-6-Sol (current)'),
            ('codex', 'test_fixtures/pickers/codex_effort.txt', 2, 'Medium (default) (current)'),
        )
        for agent, file, choice, label in fixtures:
            with self.subTest(file=file), patch.object(terminal_input, 'verify'):
                screen = (here / file).read_text()
                entry = dict(self.entry, agent=agent, codex_binding='open')
                row = menu_row(screen, entry)
                digest = answers.prompt_digest(entry, row, reply_turn=self.reply_turn)
                self.assertIsNotNone(digest)
                run = FakeTerminal(screen)
                terminal_input.menu_choose(entry, choice, label=label, run=run, sleep=lambda _: None)
                self.assertEqual([command[-1] for command in run.writes], ['Enter'])

    def test_transcript_question_must_match_frame_before_either_path_can_answer(self):
        read = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'AskUserQuestion', 'id': 'tool-1',
            'input': {'questions': [{'header': 'Format', 'question': 'Which format should I use?', 'multiSelect': False,
                                     'options': [{'label': 'Text', 'description': ''}, {'label': 'Image', 'description': ''}]}]}}]}}
        self.transcript.write_text(json.dumps(read) + '\n')
        original = self.digest(menu_row(SCREEN, self.entry))
        self.assertIsNotNone(original)
        question = read['message']['content'][0]['input']['questions'][0]
        for field, change in (('question', 'Which kind?'), ('options', [{'label': 'Text'}, {'label': 'Photo'}])):
            with self.subTest(field=field), patch.object(terminal_input, 'verify'):
                previous = question[field]
                question[field] = change
                with self.transcript.open('a') as stream:
                    stream.write(json.dumps(read) + '\n')
                question[field] = previous
                current = menu_row(SCREEN, self.entry)
                self.assertIsNone(self.digest(current))
                run = FakeTerminal(SCREEN)
                with self.assertRaisesRegex(helper_tabs.Stale, 'Open the helper'):
                    self.answer(current, original, run, choice=1)
                with self.assertRaisesRegex(terminal_input.MenuChanged, 'Claude Code 2.1.327'):
                    terminal_input.menu_choose(dict(self.entry, agent_version='2.1.327'), 1, run=run)
                self.assertEqual(run.writes, [])

    def test_loss_of_frame_during_navigation_stops_before_enter(self):
        row = menu_row(SCREEN)
        for broken in (lambda frame: frame.replace('Enter to select', 'Enter to sel'),
                       lambda frame: frame.replace('☐ Format', 'Format')):
            with self.subTest(broken=broken), patch.object(terminal_input, 'verify'):
                class Redraw(FakeTerminal):
                    def __call__(self, argv, **kwargs):
                        result = super().__call__(argv, **kwargs)
                        if argv[-1] == 'Down':
                            self.screen = broken(self.screen)
                        return result

                run = Redraw(SCREEN)
                with self.assertRaises(helper_tabs.Stale):
                    self.answer(row, self.digest(row), run, choice=2)
                self.assertEqual([command[-1] for command in run.writes], ['Down'])

    def test_changed_menu_with_same_options_refuses_before_any_write(self):
        row = menu_row(SCREEN)
        digest = self.digest(row)
        for question in ('Which image size?', 'Use the existing format?'):
            with self.subTest(question=question):
                run = FakeTerminal(SCREEN.replace('Which format should I use?', question))
                with patch.object(terminal_input, 'verify'), self.assertRaises(helper_tabs.Stale):
                    self.answer(row, digest, run, choice=1)
                self.assertEqual(run.writes, [])

    def test_same_menu_from_new_transcript_activity_refuses_old_digest(self):
        row = menu_row(SCREEN)
        digest = self.digest(row)
        self.transcript.write_text(REPLY + '\nNew tool question')
        run = Mock()
        with self.assertRaises(helper_tabs.Stale):
            self.answer(row, digest, run, choice=1)
        run.assert_not_called()

    def test_conversation_change_after_discovery_is_stale(self):
        row = menu_row(SCREEN)
        run = Mock()
        with patch.object(terminal_input, 'verify', side_effect=terminal_input.ConversationChanged('Different chat.')):
            with self.assertRaisesRegex(helper_tabs.Stale, 'Different chat'):
                self.answer(row, self.digest(row), run, choice=1)
        run.assert_not_called()

    def test_menu_option_uses_the_child_and_preserves_cursor_independent_digest(self):
        row = menu_row(SCREEN)
        run = FakeTerminal(SCREEN.replace('❯ 1.', '  1.').replace('  2.', '❯ 2.'))
        self.assertEqual(self.digest(row), self.digest(menu_row(run.screen)))
        with patch.object(terminal_input, 'verify'):
            self.assertEqual(self.answer(row, self.digest(row), run, choice=2), {'ok': True})
        self.assertEqual([command[1:] for command in run.writes], [['send-keys', '-t', '%12', 'Enter']])
        self.send.assert_not_called()

    def test_menu_draft_is_never_erased_or_submitted(self):
        row = menu_row(SCREEN)
        run = FakeTerminal(SCREEN.replace('Type something.', 'Keep this draft'))
        with patch.object(terminal_input, 'verify'), self.assertRaises(terminal_input.DraftError):
            self.answer(row, self.digest(row), run, text='Use text')
        self.assertEqual(run.writes, [])

    def test_menu_text_reuses_guarded_navigation_and_paste(self):
        row = menu_row(SCREEN)
        run = FakeTerminal(SCREEN)
        choose = terminal_input.menu_choose
        with patch.object(terminal_input, 'verify'), patch.object(
                terminal_input, 'menu_choose', side_effect=lambda *args, **kwargs: choose(
                    *args, **kwargs, sleep=lambda _: None)):
            self.assertEqual(self.answer(row, self.digest(row), run, text='Use custom text'), {'ok': True})
        self.assertEqual([command[-1] for command in run.writes if command[1] == 'send-keys'],
                         ['Down', 'Down', 'Enter'])
        self.assertEqual(run.buffer, 'Use custom text')

    def test_bridge_multi_menu_and_unbound_helper_need_another_path(self):
        row = menu_row(SCREEN)
        for detail in ({'askBridge': {'id': 'bridge'}}, {'ask_current': {'multi': True}}):
            self.assertIsNone(self.digest(dict(row, attention=dict(row['attention'], **detail))))
        self.assertIsNone(answers.prompt_digest(dict(self.entry, registry=None), row, reply_turn=self.reply_turn))

    def test_requires_one_valid_answer(self):
        row = menu_row(SCREEN)
        for fields in ({}, {'choice': True}, {'text': '\x1b'}, {'text': 'Yes', 'choice': 1}, {'choice': 4}):
            run = Mock()
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.answer(row, self.digest(row), run, **fields)
            run.assert_not_called()
        self.send.assert_not_called()
