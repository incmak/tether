"""Native Claude 2.1.318 captures with synthetic questions; no real terminal writes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import choice_state
import paths
import server
import terminal_input as terminal

FIXTURES = Path(__file__).parent / 'test_fixtures/native_ask'


class NativeAskTests(unittest.TestCase):
    def frame(self, name):
        return (FIXTURES / (name + '.txt')).read_text()

    def test_active_question_is_screen_state_not_all_transcript_questions(self):
        found = choice_state.ask_current(self.frame('single'))
        self.assertEqual((found['index'], found['total'], found['header'], found['question']),
                         (0, 3, 'Library', 'Which library?'))
        self.assertEqual([option['label'] for option in found['options']],
                         ['React', 'Vue', 'Svelte', 'Type something.', 'Chat about this'])
        self.assertFalse(found['multi'])
        self.assertEqual([o['id'] for o in found['options'] if o['selected']], [1])
        self.assertNotIn('checked', found['options'][0])
        self.assertIn('Tab', found['keys'])
        self.assertNotIn('Space', found['keys'])  # Not in this native footer.

    def test_multiselect_distinguishes_cursor_checks_and_answered_tabs(self):
        found = choice_state.ask_current(self.frame('multi'))
        self.assertEqual((found['index'], found['header'], found['multi']), (1, 'Features', True))
        self.assertEqual([o['label'] for o in found['options'][:3]], ['Search', 'Export', 'Sync'])
        self.assertEqual([o['checked'] for o in found['options'][:3]], [False, True, False])
        self.assertEqual([o['id'] for o in found['options'] if o['selected']], [2])
        self.assertEqual(found['options'][1]['text'], '[✔] Export')
        self.assertEqual([tab['answered'] for tab in found['tabs']], [True, True, False])
        self.assertEqual(found['options'][-2]['kind'], 'free')
        self.assertEqual(found['options'][-1]['kind'], 'chat')

    def test_final_question_does_not_count_submit_as_a_question(self):
        found = choice_state.ask_current(self.frame('last'))
        self.assertEqual((found['index'], found['total'], found['header']), (2, 3, 'Name'))

    def test_unknown_or_clipped_state_never_borrows_from_transcript(self):
        single = self.frame('single')
        for index, frame in enumerate((self.frame('clipped'), terminal.ANSI_SGR.sub('', single),
                      single.replace('Esc to cancel', 'Esc to'),
                      single + '\n● Answered\n──────────\n❯ \n──────────\n',
                      single.replace('Library', 'Lib…'),
                      single.replace('❯', ' '),
                      single.replace('2.', '8.'),
                      single.replace('3.', '❯ 3.'),
                      single.replace('Which library?', ''))):
            with self.subTest(case=index):
                self.assertIsNone(choice_state.ask_current(frame))

    def test_attention_publishes_current_state_only_for_claude(self):
        styled = self.frame('multi')
        plain = terminal.ANSI_SGR.sub('', styled)
        for agent in ('claude', 'codex', 'antigravity'):
            with self.subTest(agent=agent), patch.dict(terminal._attention_seen, clear=True):
                entry = {'id': '%native', 'agent': agent}
                terminal.attention(entry, plain, False, now=0, styled_screen=styled)
                found = terminal.attention(entry, plain, False, now=4, styled_screen=styled)
                self.assertEqual('ask_current' in (found or {}), agent == 'claude')

    def test_payload_keeps_transcript_as_text_without_inventing_clipped_state(self):
        config = {'id': '%native', 'agent': 'claude'}
        ask = {'id': 'tool-ask', 'questions': [{'question': 'Transcript question'}]}
        with patch.object(server, 'snapshot', side_effect=lambda _: {'ask': ask}), \
                patch.object(server, 'live_status') as status:
            for name in ('single', 'clipped'):
                with self.subTest(frame=name), patch.dict(terminal._attention_seen, clear=True):
                    styled = self.frame(name)
                    plain = terminal.ANSI_SGR.sub('', styled)
                    terminal.attention(config, plain, False, now=0, styled_screen=styled)
                    attention = terminal.attention(config, plain, False, now=4, styled_screen=styled)
                    status.return_value = {'state': 'Input needed', 'approval': None, 'attention': attention}
                    payload = server.chat_payload(config)['attention']
                    self.assertEqual(payload['ask'], ask)
                    if name == 'clipped':
                        self.assertNotIn('ask_current', payload)
                    else:
                        self.assertEqual(payload['ask_current']['index'], 0)


class NativeAskKeyRouteTests(unittest.TestCase):
    def request(self, body, entries):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/key'
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(payload))}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        with patch.object(server, 'sessions', return_value=entries):
            handler.do_POST()
        return handler.respond.call_args.args

    def test_phone_digit_reaches_selected_pane_once_without_card_focus_chord(self):
        entries = [{'id': '%11', 'thread': 'other', 'agent': 'claude', 'writable': True},
                   {'id': '%12', 'thread': 'ask', 'agent': 'claude', 'writable': True}]
        with patch.object(server, 'verify') as verify, patch.object(server, 'card_key') as card, \
                patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(self.request({'session': '%12', 'thread': 'ask', 'key': '1'}, entries),
                             (200, {'accepted': True}))
            verify.assert_called_once_with(entries[1])
            card.assert_not_called()
            run.assert_called_once_with([paths.TMUX, 'send-keys', '-t', '%12', '1'],
                                        check=True, capture_output=True, timeout=5)
            run.reset_mock()
            self.assertEqual(self.request({'session': '%12', 'thread': 'old', 'key': '1'}, entries)[0], 409)
            run.assert_not_called()

    def test_owner_refusal_does_not_report_success_or_send_a_key(self):
        entry = {'id': '%12', 'thread': 'ask', 'agent': 'claude', 'writable': True}
        with patch.object(server, 'verify', side_effect=RuntimeError('The agent restarted.')), \
                patch.object(server.subprocess, 'run') as run, patch('builtins.print'):
            self.assertEqual(self.request({'session': '%12', 'thread': 'ask', 'key': '1'}, [entry])[0], 503)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class GutterAskTests(unittest.TestCase):
    """Claude 2.1.330 draws a dim "│" gutter left of a question that wraps (live captures, 120 columns)."""
    DIR = Path(__file__).parent / 'fixtures/claude_2.1.330_ask_gutter'

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.asks = json.loads((self.DIR / 'asks.json').read_text())

    def entry(self, name):
        transcript = Path(self.temp.name) / f'{name}.jsonl'
        record = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': f'tool-{name}', 'name': 'AskUserQuestion', 'input': self.asks[name]}]}}
        transcript.write_text(json.dumps(record) + '\n')
        return {'id': '%gutter', 'agent': 'claude', 'transcript': str(transcript)}

    def test_wrapped_question_verifies_without_the_gutter(self):
        for name in ('long', 'two', 'short'):
            with self.subTest(name=name):
                frame = (self.DIR / f'{name}-120.ansi').read_text()
                expected = self.asks[name]['questions'][0]
                self.assertEqual(choice_state.ask_current(frame)['question'], ' '.join(expected['question'].split()))
                menu = terminal.verified_numbered_menu(self.entry(name), frame)
                self.assertIsNotNone(menu)
                self.assertEqual(menu['question'], expected['question'])
                self.assertEqual([option['text'] for option in menu['options'][:2]],
                                 [option['label'] for option in expected['options']])
                self.assertTrue(terminal.menu_digest(menu))

    def test_a_gutter_on_only_some_rows_is_refused(self):
        frame = (self.DIR / 'long-120.ansi').read_text()
        lines = frame.split('\n')
        at = next(i for i, line in enumerate(lines) if '│' in line and 'exist on dev today' in line)
        lines[at] = terminal.ANSI_SGR.sub('', lines[at]).replace('│', ' ', 1)
        self.assertIsNone(terminal.verified_numbered_menu(self.entry('long'), '\n'.join(lines)))
