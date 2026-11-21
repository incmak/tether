"""B446 answers are bound to the helper and the current question."""
import unittest_isolated  # noqa: F401
import io
import json
from pathlib import Path
import re
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


def seen(frame, entry):
    """The ticked rows the Dock shows for this frame: Done sends them as its labels."""
    menu = terminal_input.verified_numbered_menu(entry, frame)
    return [terminal_input.picker_row_key(item) for item in menu['options'] if item['kind'] == 'option' and item.get('checked')]


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


def focus_multi(frame, target):
    """Move the cursor marker among the rows in a captured question."""
    plain = terminal_input.prompt_actions.plain
    rows = frame.splitlines(keepends=True)
    start = next(i for i, line in enumerate(rows) if '✔ Submit' in plain(line))
    for i in range(start + 1, len(rows)):
        line = rows[i]
        match = re.match(r'^(?:❯ |  )(?:(\d+)\. |   (Submit|Next)$)', plain(line).rstrip())
        if not match:
            continue
        line = line.replace('❯', ' ', 1)
        if target in match.groups():
            marker = re.match(r'^(?:\x1b\[[0-9;]*m)*', line).end()
            line = line[:marker] + '❯' + line[marker + 1:]
        rows[i] = line
    return ''.join(rows)


class ScriptedTerminal(FakeTerminal):
    def __init__(self, screen, steps=()):
        super().__init__(screen)
        self.steps = iter(steps)

    def __call__(self, argv, **kwargs):
        if argv[1] == 'capture-pane':
            return super().__call__(argv, **kwargs)
        assert server.SEND_LOCK.locked()
        key, frame = next(self.steps, (None, None))
        assert argv[1] == 'send-keys' and argv[-1] == key, argv
        self.writes.append(key)
        self.screen = frame
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
        return self.request_choice('menu', run, choice=1, **body)[0]

    def request_choice(self, endpoint, run, **body):
        payload = json.dumps(dict(session=HELPER['id'], thread=THREAD, **body)).encode()
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/' + endpoint
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(payload))}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        name = 'menu_choose' if endpoint == 'menu' else 'picker_choose'
        choose = getattr(terminal_input, name)
        with patch.object(server, 'sessions', return_value=[self.entry]), patch.object(
                server, name, side_effect=lambda *args, **kwargs: choose(*args, **kwargs, run=run, sleep=lambda _: None)):
            handler.do_POST()
        return handler.respond.call_args.args

    def multi_ask(self, identifier):
        ask = self.tool_ask(identifier)
        ask['message']['content'][0]['input']['questions'] = [
            dict(header=header, question=question, multiSelect=multi,
                 options=[dict(label=label, description=detail) for label, detail in rows])
            for header, question, multi, rows in (
                ('Library', 'Which library?', False, [('React', ''), ('Vue', '')]),
                ('Features', 'Which features?', True, [('Search', 'Find'), ('Export', 'Save'), ('Sync', 'Share')]),
                ('Name', 'Which name?', False, [('A', 'First'), ('B', 'Second')]),
            )
        ]
        return ask

    def multi_pick(self, run, **body):
        return self.request_choice('pick', run, heading='Which features?',
                                   labels=['Search\nFind', 'Export\nSave', 'Sync\nShare',
                                           'Type something', 'Chat about this'], **body)

    def multi_frame(self):
        self.transcript_records(self.multi_ask('features'))
        return (Path(__file__).parent / 'test_fixtures/native_ask/multi.txt').read_text()

    def test_verified_multi_tick_keeps_the_same_revision(self):
        screen = self.multi_frame()
        run = FakeTerminal(screen)
        original = run.__call__

        def tick(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[1] == 'send-keys' and argv[-1] == 'Space':
                run.screen = run.screen.replace('[✔]', '[ ]')
            return result

        status = menu_row(screen, self.entry)['attention']
        revision = status['menu_verified']
        self.assertTrue(revision)
        self.assertTrue(status['ask_current']['options'][1]['checked'])
        with patch.object(terminal_input, 'verify'):
            self.assertEqual(self.multi_pick(tick, row=1, then='Space', revision=revision, ticked=True)[0], 200)
            after = menu_row(run.screen, self.entry)['attention']
            self.assertFalse(after['ask_current']['options'][1]['checked'])
            self.assertEqual(after['menu_verified'], revision)
            self.assertEqual(self.multi_pick(run, row=1, then='Enter', revision=revision)[0], 409)
            self.assertEqual(self.request_menu(run, revision=revision), 409)
        self.assertEqual([command[-1] for command in run.writes], ['Space'])

    def test_multi_pick_sends_no_keys_on_free_or_chat_rows(self):
        screen = self.multi_frame()
        revision = menu_row(screen, self.entry)['attention']['menu_verified']
        for row, marker in ((3, '  \x1b[38;5;246m4.'), (4, '  5. Chat about this')):
            focused = screen.replace('❯', ' ').replace(marker, '❯' + marker[1:])
            with self.subTest(row=row), patch.object(terminal_input, 'verify'):
                run = FakeTerminal(focused)
                self.assertTrue(terminal_input.verified_numbered_menu(self.entry, focused)['options'][row]['selected'])
                for key in ('Space', 'Enter'):
                    self.assertEqual(self.multi_pick(run, row=row, then=key, revision=revision),
                                     (409, {'error': terminal_input.menu_refusal(self.entry)}))
                self.assertEqual(run.writes, [])

    def submit_fixture(self, name):
        return (Path(__file__).parent / 'fixtures/claude_2.1.330_ask_multi' / f'multi-{name}.ansi').read_text()

    def submit_ask(self):
        ask = self.multi_ask('features')
        question = ask['message']['content'][0]['input']['questions'][1]
        question['question'] = 'Which features do you want?'
        for option in question['options']:
            option['description'] += ' things'
        ask['message']['content'][0]['input']['questions'] = [question]
        self.transcript_records(ask)

    def done(self, run, revision, labels=None):
        labels = seen(run.screen, self.entry) if labels is None else labels
        with patch.object(terminal_input, 'verify'):
            return self.request_choice('pick', run, then='Enter', revision=revision, labels=labels,
                                       heading='Which features do you want?')

    def test_done_reaches_submit_and_confirms_at_each_width(self):
        self.submit_ask()
        question = self.submit_fixture('q1-120')
        revision = menu_row(question, self.entry)['attention']['menu_verified']
        self.assertTrue(revision)
        for ticked, submit, review in (
            ('ticked', 'on-submit', 'after-submit'),
            (None, 'on-submit-44', 'review-44'),
            ('back-22', 'on-submit-22', 'review-22'),
        ):
            with self.subTest(submit=submit):
                focused = self.submit_fixture(submit)
                start = self.submit_fixture(ticked) if ticked else focus_multi(focused, '1')
                steps = [('Down', focus_multi(focused, row)) for row in ('2', '3', '4')]
                steps += [('Down', focused), ('Enter', self.submit_fixture(review)), ('Enter', '')]
                run = ScriptedTerminal(start, steps)
                self.assertEqual(self.done(run, revision), (200, {'accepted': True}))
                self.assertEqual(run.writes, ['Down'] * 4 + ['Enter', 'Enter'])
                for frame in (start, focused):
                    self.assertEqual(menu_row(frame, self.entry)['attention']['menu_verified'], revision)

    def test_done_moves_from_free_and_chat_or_starts_on_submit(self):
        self.submit_ask()
        submit = self.submit_fixture('on-submit')
        review = self.submit_fixture('after-submit')
        revision = menu_row(self.submit_fixture('ticked'), self.entry)['attention']['menu_verified']
        for row, move in (('4', ['Down']), ('5', ['Up']), ('Submit', [])):
            with self.subTest(row=row):
                start = focus_multi(submit, row) if move else submit
                run = ScriptedTerminal(start, [(key, submit) for key in move] + [('Enter', review), ('Enter', '')])
                self.assertEqual(self.done(run, revision)[0], 200)
                self.assertEqual(run.writes, move + ['Enter', 'Enter'])

    def test_done_sends_nothing_more_after_a_review_that_does_not_verify(self):
        self.submit_ask()
        submit = self.submit_fixture('on-submit-22')
        revision = menu_row(self.submit_fixture('ticked'), self.entry)['attention']['menu_verified']
        review = self.submit_fixture('review-22').replace('→ Search', '→ Export')
        wide = self.submit_fixture('after-submit')
        review_question = wide.replace('● Which features do you want?', '● Which fonts do you want?')
        unreadable = wide.replace('→ Search', '→')
        self.assertNotIn(review_question, (wide,))
        self.assertNotEqual(unreadable, wide)
        for name, start, frame in (('answer', submit, review), ('question', self.submit_fixture('on-submit'), review_question),
                                   ('unreadable', self.submit_fixture('on-submit'), unreadable)):
            with self.subTest(name=name):
                run = ScriptedTerminal(start, [('Enter', frame)])
                status, response = self.done(run, revision)
                self.assertEqual(status, 409)
                self.assertIn('Open the terminal to check', response['error'])
                self.assertEqual(run.writes, ['Enter'])

    def test_done_refuses_a_missing_or_unfamiliar_review(self):
        self.submit_ask()
        self.entry['agent_version'] = '2.1.331'
        submit = self.submit_fixture('on-submit')
        revision = menu_row(self.submit_fixture('ticked'), self.entry)['attention']['menu_verified']
        for name, frame in (('missing', ''), ('renumbered', self.submit_fixture('after-submit').replace('1. ', '3. '))):
            with self.subTest(name=name):
                run = ScriptedTerminal(submit, [('Enter', frame)])
                status, response = self.done(run, revision)
                self.assertEqual(status, 409)
                self.assertIn('Claude Code 2.1.331', response['error'])
                self.assertEqual(run.writes, ['Enter'])

    def test_done_matches_multiple_ticked_labels_in_the_review(self):
        self.submit_ask()
        submit = self.submit_fixture('on-submit').replace('[ ] Sync', '[✔] Sync')
        review = self.submit_fixture('after-submit')
        revision = menu_row(self.submit_fixture('ticked'), self.entry)['attention']['menu_verified']
        for answer, status, keys in (('Sync, Search', 200, ['Enter', 'Enter']), ('Sync, Search, Search', 409, ['Enter'])):
            with self.subTest(answer=answer):
                run = ScriptedTerminal(submit, [('Enter', review.replace('→ Search', '→ ' + answer)), ('Enter', '')])
                self.assertEqual(self.done(run, revision)[0], status)
                self.assertEqual(run.writes, keys)

    def test_done_refuses_when_the_ticks_changed_after_the_user_looked(self):
        self.submit_ask()
        submit = self.submit_fixture('on-submit')
        revision = menu_row(self.submit_fixture('ticked'), self.entry)['attention']['menu_verified']
        tick = '\x1b[38;5;114m[✔]\x1b[39m'
        moved = submit.replace(tick + ' Search', '[ ] Search').replace('[ ] Export', tick + ' Export')
        self.assertEqual(seen(submit, self.entry), ['Search\nFind things'])
        self.assertEqual(seen(moved, self.entry), ['Export\nSave things'])
        self.assertEqual(menu_row(moved, self.entry)['attention']['menu_verified'], revision)
        # The labels the screen shows pass the gate: the refusals below come from the ticks, not from the frame.
        run = ScriptedTerminal(moved, [('Enter', self.submit_fixture('after-submit').replace('→ Search', '→ Export')), ('Enter', '')])
        self.assertEqual(self.done(run, revision, labels=['Export\nSave things'])[0], 200)
        self.assertEqual(run.writes, ['Enter', 'Enter'])
        for labels in (['Search\nFind things'], ['Search\nFind things', 'Export\nSave things'], [], None):
            with self.subTest(labels=labels):
                run = ScriptedTerminal(moved)
                with patch.object(terminal_input, 'verify'):
                    body = {} if labels is None else {'labels': labels}
                    status, response = self.request_choice('pick', run, then='Enter', revision=revision,
                                                           heading='Which features do you want?', **body)
                self.assertEqual(status, 409)
                self.assertEqual(run.writes, [])

    def test_done_refuses_missing_control_and_space_refuses_control(self):
        self.submit_ask()
        submit = self.submit_fixture('on-submit')
        question = self.submit_fixture('ticked')
        revision = menu_row(question, self.entry)['attention']['menu_verified']
        frame = ''.join(line for line in question.splitlines(keepends=True)
                        if terminal_input.prompt_actions.plain(line).strip() != 'Submit')
        self.assertEqual(menu_row(frame, self.entry)['attention']['menu_verified'], revision)
        run = ScriptedTerminal(frame)
        self.assertEqual(self.done(run, revision)[0], 409)
        self.assertEqual(run.writes, [])
        run = ScriptedTerminal(submit)
        with patch.object(terminal_input, 'verify'):
            for row in (4, 5):
                self.assertEqual(self.request_choice('pick', run, row=row, then='Space', revision=revision, ticked=False,
                                                     labels=['Search\nFind things', 'Export\nSave things', 'Sync\nShare things',
                                                             'Type something', 'Submit', 'Chat about this'],
                                                     heading='Which features do you want?')[0], 409)
        self.assertEqual(run.writes, [])

    def test_done_at_next_only_accepts_the_next_question_of_the_same_ask(self):
        screen = self.multi_frame()
        revision = menu_row(screen, self.entry)['attention']['menu_verified']
        self.assertTrue(revision)
        following = (Path(__file__).parent / 'test_fixtures/native_ask/last.txt').read_text()
        for start, directions in (('2', [('Down', '3'), ('Down', '4'), ('Down', 'Next')]), ('5', [('Up', 'Next')])):
            for frame, status in ((following, 200), (focus_multi(screen, 'Next'), 409)):
                with self.subTest(start=start, status=status), patch.object(terminal_input, 'verify'):
                    steps = [(key, focus_multi(screen, row)) for key, row in directions] + [('Enter', frame)]
                    run = ScriptedTerminal(focus_multi(screen, start), steps)
                    code, response = self.request_choice('pick', run, then='Enter', revision=revision, heading='Which features?',
                                                         labels=seen(screen, self.entry))
                    self.assertEqual(code, status)
                    if code == 409:
                        self.assertIn('Open the terminal to check', response['error'])
                    self.assertEqual(run.writes, [key for key, _ in steps])

    def live2(self, name):
        return (Path(__file__).parent / 'fixtures/claude_2.1.330_ask_multi2' / f'm2-{name}.ansi').read_text()

    def two_question_ask(self):
        ask = self.tool_ask('two')
        ask['message']['content'][0]['input']['questions'] = [
            dict(header=header, question=question, multiSelect=True,
                 options=[dict(label=label, description=detail) for label, detail in rows])
            for header, question, rows in (
                ('Features', 'Which features do you want?', [('Search', 'Find things'), ('Export', 'Save things'),
                                                             ('Sync', 'Share things')]),
                ('Formats', 'Which formats do you want?', [('PNG', 'Raster'), ('SVG', 'Vector'), ('PDF', 'Print')]),
            )
        ]
        self.transcript_records(ask)

    def live_done(self, prefix, start, last, after, heading):
        """Replay a live Done: Down from frame START to frame LAST, Enter, then AFTER (key, frame) steps."""
        frames = [self.live2(f'{prefix}-{n:02d}') for n in range(start, last + 1)]
        revision = menu_row(frames[0], self.entry)['attention']['menu_verified']
        self.assertTrue(revision)
        steps = [('Down', frame) for frame in frames[1:]] + after
        run = ScriptedTerminal(frames[0], steps)
        with patch.object(terminal_input, 'verify'):
            result = self.request_choice('pick', run, then='Enter', revision=revision, heading=heading,
                                         labels=seen(frames[0], self.entry))
        return result, run.writes

    def test_live_two_question_done_goes_next_then_submits_at_120_and_44(self):
        """Claude 2.1.330, live: Next on question 1, Submit and the two-question review on question 2."""
        self.two_question_ask()
        q1, q2 = 'Which features do you want?', 'Which formats do you want?'
        for prefix, start, last, after, heading, keys in (
            ('120-q1', 0, 4, [('Enter', self.live2('120-q1-05'))], q1, ['Down'] * 4 + ['Enter']),
            ('44-q1', 3, 6, [('Enter', self.live2('44-q1-07'))], q1, ['Down'] * 3 + ['Enter']),
            ('120-q2', 4, 6, [('Enter', self.live2('120-q2-07')), ('Enter', '')], q2, ['Down'] * 2 + ['Enter'] * 2),
            ('44-q2', 2, 5, [('Enter', self.live2('44-q2-06')), ('Enter', '')], q2, ['Down'] * 3 + ['Enter'] * 2),
        ):
            with self.subTest(prefix=prefix):
                (status, _), writes = self.live_done(prefix, start, last, after, heading)
                self.assertEqual(status, 200)
                self.assertEqual(writes, keys)

    def test_live_stale_ticks_on_next_and_submit_rows_send_no_keys(self):
        """Claude 2.1.330, live two-question ask: Done with ticks other than the screen's is refused before any key."""
        self.two_question_ask()
        for name, heading in (('120-q1-00', 'Which features do you want?'), ('44-q1-03', 'Which features do you want?'),
                              ('44-q2-02', 'Which formats do you want?')):
            frame = self.live2(name)
            shown = seen(frame, self.entry)
            self.assertTrue(shown, name)
            revision = menu_row(frame, self.entry)['attention']['menu_verified']
            self.assertTrue(revision, name)
            for labels in ([], shown[:-1], shown + ['Extra\nrow']):
                with self.subTest(name=name, labels=labels):
                    run = ScriptedTerminal(frame)
                    with patch.object(terminal_input, 'verify'):
                        status, _ = self.request_choice('pick', run, then='Enter', revision=revision, heading=heading,
                                                        labels=labels)
                    self.assertEqual(status, 409)
                    self.assertEqual(run.writes, [])

    def test_done_with_nothing_ticked_sends_no_keys(self):
        """Claude 2.1.330, live: with no ticks, Next goes on and Submit shows a review that lacks this question."""
        self.two_question_ask()
        for name, heading in (('44-q2-none-00', 'Which formats do you want?'), ('44-q1-00', 'Which features do you want?')):
            with self.subTest(name=name):
                frame = self.live2(name)
                revision = menu_row(frame, self.entry)['attention']['menu_verified']
                run = ScriptedTerminal(frame)
                with patch.object(terminal_input, 'verify'):
                    status, response = self.request_choice('pick', run, then='Enter', revision=revision, heading=heading,
                                                           labels=[])
                self.assertEqual((status, response), (409, {'error': terminal_input.NO_TICKS}))
                self.assertEqual(run.writes, [])

    def test_live_unanswered_review_sends_nothing_more(self):
        """Ticks on this question but a review that warns "Some questions are unanswered": no further key."""
        self.two_question_ask()
        question = self.live2('44-q2-05')
        revision = menu_row(question, self.entry)['attention']['menu_verified']
        run = ScriptedTerminal(question, [('Enter', self.live2('44-q2-none-05'))])
        with patch.object(terminal_input, 'verify'):
            status, response = self.request_choice('pick', run, then='Enter', revision=revision, labels=['SVG\nVector'],
                                                   heading='Which formats do you want?')
        self.assertEqual(status, 409)
        self.assertIn('Open the terminal to check', response['error'])
        self.assertEqual(run.writes, ['Enter'])

    def test_live_truncated_question_tabs_give_no_revision(self):
        """At 22 columns Claude 2.1.330 shortens the tabs to '☐ … ☐ F…': the question is not verified."""
        self.two_question_ask()
        self.assertFalse(menu_row(self.live2('22-q1-00'), self.entry)['attention'].get('menu_verified'))
        self.assertTrue(menu_row(self.live2('44-q1-00'), self.entry)['attention']['menu_verified'])

    def test_a_wrapped_key_hint_row_keeps_a_single_choice_question_verified(self):
        """A narrow pane wraps the key hint to a row that starts with '· '. It is not a chat line."""
        wrapped = SCREEN.replace('to navigate · Esc to cancel', 'to navigate\n· Esc to cancel')
        self.assertNotEqual(wrapped, SCREEN)
        revision = menu_row(wrapped, self.entry)['attention']['menu_verified']
        self.assertEqual(revision, menu_row(SCREEN, self.entry)['attention']['menu_verified'])
        with patch.object(terminal_input, 'verify'):
            run = FakeTerminal(wrapped)
            self.assertEqual(self.request_choice('menu', run, choice=2, revision=revision), (200, {'accepted': True}))
        self.assertEqual([command[-1] for command in run.writes], ['Down', 'Enter'])

    def test_multi_pick_refuses_missing_stale_answered_or_mismatched_menu(self):
        screen = self.multi_frame()
        revision = menu_row(screen, self.entry)['attention']['menu_verified']
        old = self.multi_ask('features')
        mismatched_type = self.multi_ask('features')
        mismatched_type['message']['content'][0]['input']['questions'][1]['multiSelect'] = False
        for records, frame, body in (
            ((old,), screen, {}),
            ((old, self.tool_result('features'), self.multi_ask('new')), screen, {'revision': revision}),
            ((old, self.tool_result('features')), screen, {'revision': revision}),
            ((mismatched_type,), screen, {'revision': revision}),
            ((old,), screen.replace('Library', 'Libraries'), {'revision': revision}),
            ((old,), screen.replace('Which features?', 'Which features now?'), {'revision': revision}),
            ((old,), screen.replace('Export', 'Download'), {'revision': revision}),
            ((old,), screen.replace('Save', 'Archive'), {'revision': revision}),
        ):
            with self.subTest(records=records, frame=frame, body=body), patch.object(terminal_input, 'verify'):
                self.transcript_records(*records)
                run = FakeTerminal(frame)
                for key in ('Space', 'Enter'):
                    self.assertEqual(self.multi_pick(run, row=1, then=key, **body),
                                     (409, {'error': terminal_input.menu_refusal(self.entry)}))
                self.assertEqual(run.writes, [])

    def test_multi_pick_rechecks_the_transcript_after_cursor_moves(self):
        screen = self.multi_frame()
        revision = menu_row(screen, self.entry)['attention']['menu_verified']
        self.assertTrue(revision)
        run = FakeTerminal(screen)

        def move(argv, **kwargs):
            if argv[1] != 'send-keys' or argv[-1] != 'Down':
                return run(argv, **kwargs)
            run.writes.append(argv)
            run.screen = screen.replace('❯', ' ').replace('  \x1b[38;5;246m3.', '❯ \x1b[38;5;246m3.')
            self.transcript_records(self.multi_ask('new'))
            return subprocess.CompletedProcess(argv, 0, '')

        with patch.object(terminal_input, 'verify'):
            self.assertEqual(self.multi_pick(move, row=2, then='Space', revision=revision, ticked=False)[0], 409)
        self.assertEqual([command[-1] for command in run.writes], ['Down'])

    def test_space_refuses_when_the_box_changed_after_the_user_looked(self):
        """The revision names the question only: Space must also match the box state the user saw, before any key."""
        screen = self.multi_frame()
        revision = menu_row(screen, self.entry)['attention']['menu_verified']
        self.assertTrue(menu_row(screen, self.entry)['attention']['ask_current']['options'][1]['checked'])
        for row, ticked in ((1, False), (0, True), (1, None), (1, 'yes')):
            with self.subTest(row=row, ticked=ticked):
                run = FakeTerminal(screen)
                body = {} if ticked is None else {'ticked': ticked}
                with patch.object(terminal_input, 'verify'):
                    self.assertEqual(self.multi_pick(run, row=row, then='Space', revision=revision, **body)[0], 409)
                self.assertEqual(run.writes, [])
        # The state the screen shows passes: the refusals above come from the box, not from the frame. The reply waits
        # until the box flips, so the Dock's next read shows the new tick; a box that never flips asks for a check.
        for flips, status in ((True, 200), (False, 409)):
            with self.subTest(flips=flips):
                run = FakeTerminal(screen)
                original = run.__call__

                def space(argv, **kwargs):
                    result = original(argv, **kwargs)
                    if flips and argv[1] == 'send-keys' and argv[-1] == 'Space':
                        run.screen = run.screen.replace('[✔]', '[ ]')
                    return result

                with patch.object(terminal_input, 'verify'):
                    code, response = self.multi_pick(space, row=1, then='Space', revision=revision, ticked=True)
                self.assertEqual(code, status)
                if not flips:
                    self.assertIn('Open the terminal to check', response['error'])
                self.assertFalse(menu_row(run.screen, self.entry)['attention']['ask_current']['options'][1]['checked'] and flips)
                self.assertEqual([command[-1] for command in run.writes], ['Space'])

    def test_codex_numbered_checkboxes_are_refused_with_or_without_revision(self):
        screen = ('Select Model and Effort\n\n  1. [x] First\n› 2. [ ] Second\n\n'
                  'enter select · esc back\n')
        self.entry['agent'] = 'codex'
        self.assertFalse(menu_row(screen, self.entry)['attention']['menu_verified'])
        run = FakeTerminal(screen)
        with patch.object(terminal_input, 'verify'):
            for body in ({}, {'revision': 'a' * 64, 'ticked': False}):
                self.assertEqual(self.request_choice('pick', run, row=1, then='Space',
                                                     heading='Select Model and Effort',
                                                     labels=['[x] First', '[ ] Second'], **body)[0], 409)
        self.assertEqual(run.writes, [])

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
