"""Permissions attention and guarded retry keys use saved screens and mocked writes."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import paths
import server
import terminal_input as terminal


FIXTURES = Path(__file__).parent / 'fixtures'


def frame(name, suffix='txt'):
    return (FIXTURES / f'claude_permissions_{name}.{suffix}').read_text()


class NumberedDraftTests(unittest.TestCase):
    entry = {'id': '%1', 'agent': 'claude'}

    def setUp(self):
        self.enterContext(patch.dict(terminal._attention_seen, clear=True))
        self.screen = (FIXTURES / 'claude_numbered_draft.txt').read_text()

    def test_numbered_draft_does_not_show_a_menu_after_delay(self):
        terminal.attention(self.entry, self.screen, False, now=0)
        found = terminal.attention(self.entry, self.screen, False, now=100)
        self.assertNotEqual((found or {}).get('reason'), 'menu')

    def test_numbered_draft_returns_both_lines(self):
        with patch.object(terminal, 'capture', return_value=self.screen):
            self.assertEqual(terminal.prompt_draft(self.entry),
                             ['1. waiiting on me inside the chat i guess', '2. yes'])

    def test_chat_about_this_under_rule_stays_a_menu(self):
        rule = '─' * 80
        for closing in ('', rule):
            with self.subTest(closing=closing):
                terminal._attention_seen.clear()
                screen = '\n'.join([rule, 'Pick one?', '  1. Red', '  2. Blue',
                                    '  3. Type something.', rule, '❯ 4. Chat about this',
                                    closing, 'Enter to select · ↑/↓ to navigate · Esc to cancel'])
                terminal.attention(self.entry, screen, False, now=0)
                self.assertEqual(terminal.attention(self.entry, screen, False, now=100)['reason'], 'menu')


class HeadsUpAttentionTests(unittest.TestCase):
    """A ✦ Heads up card is a side agent's note. Words in it ("before you press Enter") are not a terminal prompt."""
    entry = {'id': '%1', 'agent': 'claude'}

    def setUp(self):
        self.enterContext(patch.dict(terminal._attention_seen, clear=True))
        self.screen = (FIXTURES / 'claude_2.1.325_heads_up' / 'offer_press_enter.txt').read_text()

    def attention(self, screen):
        terminal.attention(self.entry, screen, False, now=0)
        return terminal.attention(self.entry, screen, False, now=100)

    def test_card_text_does_not_open_a_panel(self):
        self.assertIsNone(self.attention(self.screen))

    def test_reply_text_above_the_input_box_does_not_open_a_panel(self):
        for words in ('press Enter', 'press any key', 'Esc to cancel', '(y/n)'):
            with self.subTest(words=words):
                terminal._attention_seen.clear()
                screen = self.screen.replace('✦ Heads up · ', 'Note · ').replace('before you press Enter', f'then {words}')
                self.assertIsNone(self.attention(screen))

    def test_press_enter_without_an_input_box_still_opens_a_panel(self):
        screen = '\n'.join(self.screen.splitlines()[:-6] + ['', '  Press Enter to continue…'])
        self.assertEqual((self.attention(screen) or {}).get('reason'), 'prompt')


class PermissionsAttentionTests(unittest.TestCase):
    entry = {'id': '%permissions', 'agent': 'claude'}

    def setUp(self):
        self.enterContext(patch.dict(terminal._attention_seen, clear=True))

    def observe(self, name, now):
        return terminal.attention(self.entry, frame(name), False, now=now, styled_screen=frame(name, 'ansi'))

    def test_dialog_is_a_panel_without_chat_above_it(self):
        self.assertIsNone(self.observe('ask', 0))
        found = self.observe('ask', 4)
        self.assertEqual(found['reason'], 'permissions')
        self.assertFalse(found['requiresResponse'])
        self.assertTrue(found['text'].lstrip().startswith('Permissions  Recently blocked'))
        self.assertNotIn('Status:', found['text'])
        self.assertEqual(found['permissions']['tab'], 'Ask')

    def test_tab_switch_does_not_restart_initial_delay_or_change_shown_id(self):
        self.assertIsNone(self.observe('ask', 0))
        self.assertIsNone(self.observe('recently_blocked', 2))
        found = self.observe('recently_blocked', 3)
        self.assertIsNotNone(found)
        changed = self.observe('ask', 3.1)
        self.assertEqual(changed['id'], found['id'])
        self.assertEqual(changed['permissions']['tab'], 'Ask')

    def test_cursor_move_and_transient_redraw_keep_shown_id(self):
        self.observe('recently_blocked', 0)
        found = self.observe('recently_blocked', 4)
        header = '   Permissions  Recently blocked   Allow   Ask   Deny'
        screen = header + '\n   First\n   ❯ Second\n   Esc to cancel'
        moved = terminal.attention(self.entry, screen, False, now=4.1)
        self.assertEqual(moved['id'], found['id'])
        self.assertEqual(moved['permissions']['selected'], 1)
        self.assertIsNone(terminal.attention(self.entry, '❯ ', False, now=4.2))
        self.assertEqual(self.observe('ask', 4.3)['id'], found['id'])

    def test_other_agents_do_not_publish_permissions(self):
        for agent in ('codex', 'antigravity'):
            with self.subTest(agent=agent):
                entry = {'id': '%' + agent, 'agent': agent}
                terminal.attention(entry, frame('ask'), False, now=0)
                found = terminal.attention(entry, frame('ask'), False, now=4)
                self.assertNotEqual((found or {}).get('reason'), 'permissions')
                self.assertNotIn('permissions', found or {})


class PermissionsKeyRouteTests(unittest.TestCase):
    def setUp(self):
        self.entry = {'id': '%12', 'thread': 'chat', 'agent': 'claude', 'writable': True}
        self.enterContext(patch.object(server, 'sessions', return_value=[self.entry]))
        self.verify = self.enterContext(patch.object(server, 'verify'))
        self.capture = self.enterContext(patch.object(server, 'capture'))
        self.run = self.enterContext(patch.object(server.subprocess, 'run'))

    def request(self, **extra):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/key'
        body = {'session': '%12', 'thread': 'chat', 'key': 'r', **extra}
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        handler.do_POST()
        return handler.respond.call_args.args

    def test_retry_refused_without_live_recently_blocked_tab(self):
        for screen in ('❯ ', frame('ask', 'ansi'), frame('recently_blocked')):
            with self.subTest(screen=screen[-100:]):
                self.capture.return_value = screen
                self.assertEqual(self.request(), (400, {'error': 'Open Recently blocked first.'}))
                self.run.assert_not_called()

    def test_other_agents_cannot_retry_even_with_dialog_text(self):
        self.capture.return_value = frame('recently_blocked', 'ansi')
        for agent in ('codex', 'antigravity', 'shell'):
            with self.subTest(agent=agent):
                self.entry['agent'] = agent
                self.assertEqual(self.request(), (400, {'error': 'Open Recently blocked first.'}))
        self.capture.assert_not_called()
        self.run.assert_not_called()

    def test_retry_uses_verified_live_styled_screen_under_lock(self):
        def capture(config, styled=False):
            self.assertIs(config, self.entry)
            self.assertTrue(styled)
            self.assertTrue(server.SEND_LOCK.locked())
            self.verify.assert_called_once_with(self.entry)
            return frame('recently_blocked', 'ansi')

        self.capture.side_effect = capture
        self.assertEqual(self.request(), (200, {'accepted': True}))
        self.capture.assert_called_once_with(self.entry, styled=True)
        self.run.assert_called_once_with([paths.TMUX, 'send-keys', '-t', '%12', 'r'],
                                         check=True, capture_output=True, timeout=5)

    def test_stale_conversation_refuses_retry_before_capture_or_write(self):
        self.assertEqual(self.request(thread='old')[0], 409)
        self.capture.assert_not_called()
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
