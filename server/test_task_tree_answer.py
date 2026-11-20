"""B446 answers are bound to the helper and the current question."""
import unittest_isolated  # noqa: F401
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import attention
import helper_tabs
import server
import task_tree_answer as answers
import terminal_input


THREAD = '00000000-0000-0000-0000-000000000012'
HELPER = {'id': '%12', 'thread': THREAD, 'agent': 'claude', 'registry': 'test', 'writable': True}
REPLY = 'Next: Which format should I use?\nNeeds you: yes'
SCREEN = ('────────────────────\n ☐ Format\n\nWhich format should I use?\n\n'
          '❯ 1. Text\n  2. Image\n  3. Type something.\n'
          '────────────────────\n  4. Chat about this\n\n'
          'Enter to select · ↑/↓ to navigate · Esc to cancel\n')


def menu_row(screen, entry):
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
        self.transcript_records(self.tool_ask('tool-1'))
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

    def transcript_records(self, *records):
        replacement = self.transcript.with_suffix('.new')
        replacement.write_text(''.join(json.dumps(record) + '\n' for record in records))
        replacement.replace(self.transcript)

    def tool_ask(self, identifier, question='Which format should I use?'):
        return {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'AskUserQuestion', 'id': identifier, 'input': {'questions': [
                {'header': 'Format', 'question': question, 'multiSelect': False,
                 'options': [{'label': 'Text', 'description': ''}, {'label': 'Image', 'description': ''}]},
            ]}},
        ]}}

    def tool_result(self, identifier):
        return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': identifier,
                                                         'content': 'The user chose Text.'}]}}

    def request_menu(self, run, **body):
        payload = json.dumps(dict(session=HELPER['id'], thread=THREAD, choice=1, **body)).encode()
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/menu'
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(payload))}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        choose = terminal_input.menu_choose
        with patch.object(server, 'sessions', return_value=[self.entry]), patch.object(
                server, 'menu_choose', side_effect=lambda *args, **kwargs: choose(*args, **kwargs, run=run)):
            handler.do_POST()
        return handler.respond.call_args.args[0]

    def test_menu_route_refuses_answered_and_superseded_questions(self):
        first = self.tool_ask('old')
        second = self.tool_ask('new', 'Which layout should I use?')
        malformed = self.tool_ask('malformed')
        malformed['message']['content'][0]['input'] = 1
        second_frame = SCREEN.replace('Which format should I use?', 'Which layout should I use?')
        cases = (
            ('no recorded ask', (), SCREEN),
            ('answered', (first, self.tool_result('old')), SCREEN),
            ('older outstanding', (first, second, self.tool_result('new')), SCREEN),
            ('newest answered', (first, second, self.tool_result('new')), second_frame),
            ('earlier frame', (first, second), SCREEN),
            ('newer unreadable ask', (first, malformed), SCREEN),
        )
        for name, records, frame in cases:
            with self.subTest(name=name), patch.object(terminal_input, 'verify'):
                self.transcript_records(second if frame == second_frame else first)
                revision = menu_row(frame, self.entry)['attention']['menu_verified']
                self.assertTrue(revision)
                self.transcript_records(*records)
                run = FakeTerminal(frame)
                self.assertEqual(self.request_menu(run, revision=revision), 409)
                self.assertEqual(run.writes, [])

    def test_menu_route_accepts_the_newest_outstanding_question(self):
        self.transcript_records(self.tool_ask('old'), self.tool_ask('new'), self.tool_result('old'))
        revision = menu_row(SCREEN, self.entry)['attention']['menu_verified']
        with patch.object(terminal_input, 'verify'):
            run = FakeTerminal(SCREEN)
            self.assertEqual(self.request_menu(run, revision=revision), 200)
        self.assertEqual([command[-1] for command in run.writes], ['Enter'])

    def test_menu_route_refuses_when_the_question_is_answered_before_enter(self):
        revision = menu_row(SCREEN, self.entry)['attention']['menu_verified']
        def verify(*_):
            if checks:
                with self.transcript.open('a') as stream:
                    stream.write(json.dumps(self.tool_result('tool-1')) + '\n')
            checks.append(True)

        checks = []
        run = FakeTerminal(SCREEN)
        with patch.object(terminal_input, 'verify', side_effect=verify):
            self.assertEqual(self.request_menu(run, revision=revision), 409)
        self.assertEqual(len(checks), 2)
        self.assertEqual(run.writes, [])

    def test_menu_route_binds_the_click_to_the_transcript_call_even_for_identical_text(self):
        for number, question in enumerate(('Which format should I use?', 'Which layout should I use?')):
            with self.subTest(question=question), patch.object(terminal_input, 'verify'):
                old, new = f'old-{number}', f'new-{number}'
                frame = SCREEN.replace('Which format should I use?', question)
                self.transcript_records(self.tool_ask(old, question))
                revision = menu_row(frame, self.entry)['attention']['menu_verified']
                self.transcript_records(self.tool_ask(old, question), self.tool_result(old), self.tool_ask(new, question))
                run = FakeTerminal(frame)
                self.assertEqual(self.request_menu(run, revision=revision), 409)
                self.assertEqual(run.writes, [])
                self.assertEqual(self.request_menu(run), 409)
                self.assertEqual(run.writes, [])
                current = menu_row(frame, self.entry)['attention']['menu_verified']
                self.assertEqual(self.request_menu(run, revision=current), 200)
                self.assertEqual([command[-1] for command in run.writes], ['Enter'])

    def test_status_publishes_choices_only_for_verified_menu(self):
        for records, frame in (
            ((self.tool_ask('current'), self.tool_result('current')), SCREEN),
            ((self.tool_ask('current'),), SCREEN.replace('☐ Format', 'Format')),
        ):
            with self.subTest(records=records, frame=frame):
                self.transcript_records(*records)
                status = menu_row(frame, self.entry)['attention']
                self.assertFalse(status.get('options'))
                self.assertIs(status['menu_verified'], False)
                self.assertEqual(status['question'], 'Which format should I use?')
        status = menu_row(SCREEN, self.entry)['attention']
        self.assertTrue(status['menu_verified'])
        self.assertEqual([option['text'] for option in status['options'][:2]], ['Text', 'Image'])

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
                row = menu_row(screen, self.entry)
                digest = self.digest(row)
                run = FakeTerminal(screen)
                with self.assertRaises(helper_tabs.Stale):
                    self.answer(row, self.digest(menu_row(SCREEN, self.entry)), run, choice=1)
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

    def test_codex_170_reasoning_menus_are_verified(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'codex_0.170.0_menus'
        entry = dict(self.entry, agent='codex', agent_version='0.170.0', codex_binding='open')
        for name, question, labels, selected in (
            ('effort-120', 'Select Reasoning Level for GPT-6.2-Sol',
             ['Low (default)', 'Medium', 'High', 'Extra high', 'More reasoning… (current)'], 5),
            ('effort-44', 'Select Reasoning Level for GPT-6.2-Sol',
             ['Low (default)', 'Medium', 'High', 'Extra high', 'More reasoning… (current)'], 5),
            ('more-120', 'Advanced Reasoning', ['Max (current)', 'Ultra'], 1),
        ):
            with self.subTest(fixture=name), patch.object(terminal_input, 'verify'):
                screen = (fixtures / f'{name}.ansi').read_text()
                menu = menu_row(screen, entry)['attention']
                self.assertTrue(menu['menu_verified'])
                self.assertEqual(menu['question'], question)
                self.assertEqual([option['text'] for option in menu['options']], labels)
                self.assertEqual([option['id'] for option in menu['options'] if option['selected']], [selected])
                run = FakeTerminal(screen)
                terminal_input.menu_choose(entry, selected, label=labels[selected - 1], run=run,
                                           revision=menu['menu_verified'])
                self.assertEqual([command[-1] for command in run.writes], ['Enter'])

    def test_codex_170_effort_menu_stays_verified_when_focus_changes_the_footer(self):
        path = Path(__file__).parent / 'fixtures' / 'codex_0.170.0_menus' / 'effort-120.ansi'
        entry = dict(self.entry, agent='codex', agent_version='0.170.0')
        screen = terminal_input.prompt_actions.plain(path.read_text())
        revision = menu_row(screen, entry)['attention']['menu_verified']
        run = FakeTerminal(screen)

        def redraw(argv, **kwargs):
            result = run(argv, **kwargs)
            if argv[1] == 'send-keys' and argv[-1] == 'Up':
                run.screen = (screen.replace('  4.', '› 4.').replace('› 5.', '  5.')
                              .replace('enter select · esc back', 'enter default · s session · esc back'))
            return result

        with patch.object(terminal_input, 'verify'):
            terminal_input.menu_choose(entry, 4, label='Extra high', run=redraw, sleep=lambda _: None,
                                       revision=revision)
        self.assertEqual([command[-1] for command in run.writes], ['Up', 'Enter'])

    def test_codex_170_clipped_and_unfamiliar_menus_send_no_keys(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'codex_0.170.0_menus'
        effort = terminal_input.prompt_actions.plain((fixtures / 'effort-120.ansi').read_text())
        more = terminal_input.prompt_actions.plain((fixtures / 'more-120.ansi').read_text())
        entry = dict(self.entry, agent='codex', agent_version='0.170.0')
        for name, screen in (
            ('clipped models', (fixtures / 'model-wide.ansi').read_text()),
            ('extra text after ellipsis', effort.replace('More reasoning…', 'More reasoning… extra')),
            ('partial effort footer', effort.replace('enter select · esc back', 'enter select · esc')),
            ('wrong first effort', effort.replace('Low (default)', 'Minimal (default)')),
            ('extra effort', effort.replace('  enter select', '  6. Bonus effort\n  enter select')),
            ('missing warning', '\n'.join(line for line in more.splitlines() if '⚠' not in line)),
            ('third option', more.replace('  enter default', '  3. Extra high\n  enter default')),
            ('extra indented row', more.replace('  enter default', '     Unknown row\n  enter default')),
        ):
            with self.subTest(case=name), patch.object(terminal_input, 'verify'):
                self.assertIsNone(terminal_input.verified_numbered_menu(entry, screen))
                run = FakeTerminal(screen)
                with self.assertRaises(terminal_input.MenuChanged):
                    terminal_input.menu_choose(entry, 1, run=run)
                self.assertEqual(run.writes, [])

    def test_claude_329_captures_bind_menu_to_the_ask_across_widths(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'claude_2.1.329_ask'
        entry = dict(self.entry, agent_version='2.1.329')
        first, second = (self.tool_ask(identifier) for identifier in ('color-1', 'color-2'))
        for record in (first, second):
            record['message']['content'][0]['input']['questions'] = [
                {'header': 'Color', 'question': 'Which color do you pick?', 'multiSelect': False,
                 'options': [{'label': 'Red', 'description': 'Warm color'},
                             {'label': 'Blue', 'description': 'Cool color'}]},
            ]
        revision = {}
        for name, records in (('q1-22', (first,)),
                              ('q2-22', (first, self.tool_result('color-1'), second)),
                              ('q2-120', (first, self.tool_result('color-1'), second))):
            with self.subTest(fixture=name):
                self.transcript_records(*records)
                screen = (fixtures / f'{name}.ansi').read_text()
                prompt = menu_row(screen, entry)['attention']
                self.assertTrue(prompt['menu_verified'])
                revision[name] = prompt['menu_verified']
        self.assertNotEqual(revision['q1-22'], revision['q2-22'])
        self.assertEqual(revision['q2-22'], revision['q2-120'])
        run = FakeTerminal((fixtures / 'q2-22.ansi').read_text())
        with patch.object(terminal_input, 'verify'), self.assertRaises(terminal_input.MenuChanged):
            terminal_input.menu_choose(entry, 1, revision=revision['q1-22'], run=run)
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
                if agent == 'claude':
                    # The captures contain one active question. Provide its transcript plus the other tab headers.
                    current = terminal_input.choice_state.ask_current(screen)
                    record = self.tool_ask('captured')
                    template = record['message']['content'][0]['input']
                    template['questions'] = [
                        {'header': tab['label'], 'question': current['question'] if tab['active'] else 'Another question?',
                         'options': [{'label': option['label'], 'description': option['description']}
                                     for option in current['options'][:-2]]}
                        for tab in current['tabs']]
                    self.transcript_records(record)
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
        row = menu_row(SCREEN, self.entry)
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
        row = menu_row(SCREEN, self.entry)
        digest = self.digest(row)
        for question in ('Which image size?', 'Use the existing format?'):
            with self.subTest(question=question):
                run = FakeTerminal(SCREEN.replace('Which format should I use?', question))
                with patch.object(terminal_input, 'verify'), self.assertRaises(helper_tabs.Stale):
                    self.answer(row, digest, run, choice=1)
                self.assertEqual(run.writes, [])

    def test_same_menu_from_new_transcript_activity_refuses_old_digest(self):
        row = menu_row(SCREEN, self.entry)
        digest = self.digest(row)
        self.transcript.write_text(REPLY + '\nNew tool question')
        run = Mock()
        with self.assertRaises(helper_tabs.Stale):
            self.answer(row, digest, run, choice=1)
        run.assert_not_called()

    def test_conversation_change_after_discovery_is_stale(self):
        row = menu_row(SCREEN, self.entry)
        run = Mock()
        with patch.object(terminal_input, 'verify', side_effect=terminal_input.ConversationChanged('Different chat.')):
            with self.assertRaisesRegex(helper_tabs.Stale, 'Different chat'):
                self.answer(row, self.digest(row), run, choice=1)
        run.assert_not_called()

    def test_menu_option_uses_the_child_and_preserves_cursor_independent_digest(self):
        row = menu_row(SCREEN, self.entry)
        run = FakeTerminal(SCREEN.replace('❯ 1.', '  1.').replace('  2.', '❯ 2.'))
        self.assertEqual(self.digest(row), self.digest(menu_row(run.screen, self.entry)))
        with patch.object(terminal_input, 'verify'):
            self.assertEqual(self.answer(row, self.digest(row), run, choice=2), {'ok': True})
        self.assertEqual([command[1:] for command in run.writes], [['send-keys', '-t', '%12', 'Enter']])
        self.send.assert_not_called()

    def test_menu_draft_is_never_erased_or_submitted(self):
        row = menu_row(SCREEN, self.entry)
        run = FakeTerminal(SCREEN.replace('Type something.', 'Keep this draft'))
        with patch.object(terminal_input, 'verify'), self.assertRaises(terminal_input.DraftError):
            self.answer(row, self.digest(row), run, text='Use text')
        self.assertEqual(run.writes, [])

    def test_menu_text_reuses_guarded_navigation_and_paste(self):
        row = menu_row(SCREEN, self.entry)
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
        row = menu_row(SCREEN, self.entry)
        for detail in ({'askBridge': {'id': 'bridge'}}, {'ask_current': {'multi': True}}):
            self.assertIsNone(self.digest(dict(row, attention=dict(row['attention'], **detail))))
        self.assertIsNone(answers.prompt_digest(dict(self.entry, registry=None), row, reply_turn=self.reply_turn))

    def test_requires_one_valid_answer(self):
        row = menu_row(SCREEN, self.entry)
        for fields in ({}, {'choice': True}, {'text': '\x1b'}, {'text': 'Yes', 'choice': 1}, {'choice': 4}):
            run = Mock()
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.answer(row, self.digest(row), run, **fields)
            run.assert_not_called()
        self.send.assert_not_called()
