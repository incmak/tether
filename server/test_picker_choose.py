"""A tap on a row of a picker moves the cursor there, checks each step on screen, then presses one key."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import os
import re
import unittest
from unittest.mock import patch

import choice_state
import terminal_input as terminal

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
DIM = '\x1b[38;5;246m'


class FakePane:
    """A Claude picker that redraws its cursor after Up and Down.
    drift: (row, text) renames a row after the first move. step: lines per cursor move (Rewind entries are two lines).
    lag: captures that still show the old cursor after each key. dim: rows drawn in the key hint's dim color."""
    def __init__(self, rows, cursor=0, drift=None, stuck=False, step=1, lag=0, dim=(), hint='Enter to select · Esc to cancel'):
        self.hint = hint
        self.rows, self.cursor, self.drift, self.stuck, self.step, self.lag, self.dim = list(rows), cursor, drift, stuck, step, lag, set(dim)
        self.keys, self.shown, self.wait = [], cursor, 0

    def screen(self):
        def row(index, text):
            mark = '❯ ' if index == self.shown else '  '
            return mark + (f'{DIM}{text}\x1b[39m' if index in self.dim else text)
        body = '\n'.join(row(index, text) for index, text in enumerate(self.rows))
        return f'Pick one\nWhich file\n\n{body}\n\n{DIM}{self.hint}\x1b[0m\n'

    def __call__(self, args, **_):
        class Done:
            stdout = ''
        if 'capture-pane' in args:
            if self.wait:
                self.wait -= 1
            else:
                self.shown = self.cursor
            Done.stdout = self.screen() if '-e' in args else re.sub(r'\x1b\[[0-9;]*m', '', self.screen())
        elif 'send-keys' in args:
            key = args[-1]
            self.keys.append(key)
            if not self.stuck and key in ('Up', 'Down'):
                self.cursor = max(0, min(len(self.rows) - 1, self.cursor + (self.step if key == 'Down' else -self.step)))
                self.wait = self.lag
            if self.drift and key in ('Up', 'Down'):
                self.rows[self.drift[0]], self.drift = self.drift[1], None
        return Done()


class PickerChooseTests(unittest.TestCase):
    entry = {'id': '%picker', 'agent': 'claude'}

    def choose(self, pane, row, labels, then='Enter', heading=None):
        # The heading the user saw: a picker shows its title, a numbered menu its question.
        if heading is None:
            heading = 'Pick one Which file' if any(re.match(r'\d+\. ', text) for text in pane.rows) else 'Pick one'
        with patch.object(terminal, 'verify'):
            terminal.picker_choose(self.entry, row, labels, then=then, heading=heading, run=pane, sleep=lambda _: None)

    def test_tap_moves_down_then_chooses(self):
        pane = FakePane(['First', 'Second', 'Third'])
        self.choose(pane, 2, ['First', 'Second', 'Third'])
        self.assertEqual(pane.keys, ['Down', 'Down', 'Enter'])

    def test_tap_moves_up(self):
        pane = FakePane(['First', 'Second', 'Third'], cursor=2)
        self.choose(pane, 0, ['First', 'Second', 'Third'])
        self.assertEqual(pane.keys, ['Up', 'Up', 'Enter'])

    def test_tap_on_the_marked_row_only_presses_the_key(self):
        pane = FakePane(['First', 'Second'], cursor=1)
        self.choose(pane, 1, ['First', 'Second'])
        self.assertEqual(pane.keys, ['Enter'])

    def test_more_below_moves_to_the_last_row_then_presses_down(self):
        pane = FakePane(['First', 'Second'])
        self.choose(pane, 1, ['First', 'Second'], then='Down')
        self.assertEqual(pane.keys, ['Down', 'Down'])

    def test_a_slow_redraw_never_queues_a_second_press(self):
        # The first capture after each key still shows the old cursor. One Down per row, never two.
        pane = FakePane(['First', 'Second', 'Third'], lag=3)
        self.choose(pane, 1, ['First', 'Second', 'Third'])
        self.assertEqual(pane.keys, ['Down', 'Enter'])
        self.assertEqual(pane.cursor, 1)

    def test_a_row_that_changed_is_never_chosen(self):
        pane = FakePane(['First', 'Second', 'Third'], drift=(2, 'Other'))
        with self.assertRaisesRegex(RuntimeError, 'list changed'):
            self.choose(pane, 2, ['First', 'Second', 'Third'])
        self.assertNotIn('Enter', pane.keys)

    def test_a_look_alike_entry_is_not_chosen(self):
        # The chosen "Fix" entry went away and another "Fix" took a new place: the list differs, so no key at all.
        pane = FakePane(['Fix', 'Add tests', 'Fix', '(current)'], cursor=3)
        with self.assertRaisesRegex(RuntimeError, 'list changed'):
            self.choose(pane, 0, ['Fix', 'Add tests', '(current)'])
        self.assertEqual(pane.keys, [])

    def test_a_cursor_that_does_not_move_stops_without_enter(self):
        pane = FakePane(['First', 'Second', 'Third'], stuck=True)
        with self.assertRaisesRegex(RuntimeError, 'did not move'):
            self.choose(pane, 2, ['First', 'Second', 'Third'])
        self.assertEqual(pane.keys, ['Down'])

    def test_bad_input_is_refused_before_any_key(self):
        pane = FakePane(['First', 'Second'])
        for row, labels, then in ((True, ['First', 'Second'], 'Enter'), (0, None, 'Enter'), (0, 'First', 'Enter'),
                                  (2, ['First', 'Second'], 'Enter'), (0, [], 'Enter'), (0, ['First', 'Second'], 'Escape')):
            with self.subTest(row=row, labels=labels, then=then), self.assertRaises(ValueError):
                self.choose(pane, row, labels, then)
        self.assertEqual(pane.keys, [])

    def test_a_closed_picker_is_refused(self):
        pane = FakePane(['First', 'Second'])
        pane.screen = lambda: '❯ \n'
        with self.assertRaisesRegex(RuntimeError, 'no longer open'):
            self.choose(pane, 1, ['First', 'Second'])
        self.assertEqual(pane.keys, [])

    def test_a_ruled_draft_is_not_a_menu(self):
        # A Claude draft that holds numbered lines sits between two rules, with no key hint: never press a key.
        draft = '● Done.\n\n' + '─' * 40 + '\n❯ 1. first point\n  2. second point\n' + '─' * 40 + '\n  ⏵⏵ auto mode on\n'
        pane = FakePane(['x'])
        pane.screen = lambda: draft
        with self.assertRaisesRegex(RuntimeError, 'no longer open'):
            self.choose(pane, 1, ['first point', 'second point'], then='Up')
        self.assertEqual(pane.keys, [])

    def test_unfolded_detail_line_is_refused_at_once(self):
        # Without the dim color, "No code changes" shows as a row; the cursor jumps over it, so stop after one key.
        rows = ['one', 'No code changes', 'two', 'No code changes', '(current)']
        pane = FakePane(rows, cursor=4, step=2)
        with self.assertRaisesRegex(RuntimeError, 'part of the entry above'):
            self.choose(pane, 3, rows)
        self.assertEqual(pane.keys, ['Up'])

    def test_numbered_multi_select_ticks_a_row_with_space(self):
        rows = ['1. ☐ Alpha', '2. ☐ Beta', '3. ☐ Gamma']
        pane = FakePane(rows, hint='Space to toggle · Enter to confirm · Esc to cancel')
        self.choose(pane, 2, ['☐ Alpha', '☐ Beta', '☐ Gamma'], then='Space')
        self.assertEqual(pane.keys, ['Down', 'Down', 'Space'])

    def test_numbered_multi_select_done_presses_enter_on_the_marked_row(self):
        pane = FakePane(['1. ☑ Alpha', '2. ☐ Beta'], cursor=1, hint='Space to toggle · Enter to confirm · Esc to cancel')
        self.choose(pane, 1, ['☑ Alpha', '☐ Beta'], then='Enter')
        self.assertEqual(pane.keys, ['Enter'])

    def test_done_after_the_list_closed_presses_nothing(self):
        pane = FakePane(['1. ☑ Alpha', '2. ☐ Beta'], hint='Space to toggle · Enter to confirm · Esc to cancel')
        pane.screen = lambda: '● Done.\n\n❯ \n'
        with self.assertRaisesRegex(RuntimeError, 'no longer open'):
            self.choose(pane, 0, ['☑ Alpha', '☐ Beta'], then='Enter')
        self.assertEqual(pane.keys, [])

    def test_a_list_with_another_heading_is_refused(self):
        pane = FakePane(['First', 'Second'])
        with self.assertRaisesRegex(RuntimeError, 'list changed'):
            self.choose(pane, 1, ['First', 'Second'], heading='Rewind')
        self.assertEqual(pane.keys, [])

    def test_missing_heading_is_refused(self):
        pane = FakePane(['First', 'Second'])
        with patch.object(terminal, 'verify'), self.assertRaises(ValueError):
            terminal.picker_choose(self.entry, 1, ['First', 'Second'], run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, [])

    def test_numbered_single_select_refuses_space_and_enter(self):
        rows = ['1. Alpha', '2. Beta', '3. Gamma']
        for then in ('Space', 'Enter'):
            with self.subTest(then=then):
                pane = FakePane(rows)
                with self.assertRaisesRegex(ValueError, 'own button'):
                    self.choose(pane, 2, ['Alpha', 'Beta', 'Gamma'], then=then)
                self.assertEqual(pane.keys, [])


class CodexPickerDetailTests(unittest.TestCase):
    """A Codex picker shows a dim description under a row. The click checks it, as the UI shows it."""
    def screen(self, detail):
        from test_choice_state import frame, styled
        return frame(40, [styled('› Alpha'), '  Beta', styled(f'    {detail}', '2'), '  Gamma'])

    def choose(self, screen, labels):
        keys = []
        def run(args, **_):
            class Done:
                stdout = screen if '-e' in args else re.sub(r'\x1b\[[0-9;]*m', '', screen)
            if 'send-keys' in args:
                keys.append(args[-1])
            return Done()
        picker = choice_state.cursor_picker(screen, 'codex')
        with patch.object(terminal, 'verify'):
            terminal.picker_choose({'id': '%c', 'agent': 'codex'}, 0, labels, heading=terminal.picker_heading(picker), run=run, sleep=lambda _: None)
        return keys

    def test_the_detail_is_part_of_the_row(self):
        picker = choice_state.cursor_picker(self.screen('Fast model'), 'codex')
        self.assertEqual([terminal.picker_row_key(item) for item in picker['items']], ['Alpha', 'Beta\nFast model', 'Gamma'])

    def test_a_changed_detail_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'list changed'):
            self.choose(self.screen('Slow model'), ['Alpha', 'Beta\nFast model', 'Gamma'])

    def test_the_same_detail_chooses(self):
        self.assertEqual(self.choose(self.screen('Fast model'), ['Alpha', 'Beta\nFast model', 'Gamma']), ['Enter'])


class MenuChooseRedrawTests(unittest.TestCase):
    """menu_choose waits for each move to show before the next key, as picker_choose does."""
    def menu(self, rows, **kwargs):
        self.enterContext(patch('agents.open_claude_ask', return_value={
            'id': 'tool-ask', 'questions': [{'header': 'File', 'question': 'Which file', 'multi': False,
                'options': [{'label': row.split('. ', 1)[1], 'description': ''} for row in rows]}]}))
        pane = FakePane([*rows, f'{len(rows) + 1}. Type something.', f'{len(rows) + 2}. Chat about this'],
                        hint='Enter to select · ↑/↓ to navigate · Esc to cancel', **kwargs)
        original = pane.screen
        pane.screen = lambda: (
            original().replace('Pick one\nWhich file', '────────────────────\n ☐ File\n\nWhich file')
            .replace(f'  {len(rows) + 2}. Chat about this', f'────────────────────\n  {len(rows) + 2}. Chat about this')
        )
        return pane

    def test_a_slow_redraw_never_queues_a_second_press(self):
        pane = self.menu(['1. Alpha', '2. Beta', '3. Gamma'], lag=3)
        with patch.object(terminal, 'verify'):
            terminal.menu_choose({'id': '%m', 'agent': 'claude'}, 2, label='Beta', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, ['Down', 'Enter'])

    def test_a_stuck_cursor_stops_without_enter(self):
        pane = self.menu(['1. Alpha', '2. Beta', '3. Gamma'], stuck=True)
        with patch.object(terminal, 'verify'), self.assertRaisesRegex(RuntimeError, 'did not move'):
            terminal.menu_choose({'id': '%m', 'agent': 'claude'}, 3, label='Gamma', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, ['Down'])

    def test_a_ruled_draft_is_never_answered(self):
        # A Claude draft with numbered lines sits between two rules. Enter there would send the draft.
        draft = '● Done.\n\n' + '─' * 40 + '\n❯ 1. first point\n  2. second point\n' + '─' * 40 + '\n  ⏵⏵ auto mode on\n'
        pane = FakePane(['x'])
        pane.screen = lambda: draft
        with patch.object(terminal, 'verify'), self.assertRaisesRegex(RuntimeError, 'no longer open'):
            terminal.menu_choose({'id': '%m', 'agent': 'claude'}, 1, label='first point', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, [])

    def test_a_draft_after_the_first_move_stops_without_enter(self):
        draft = '● Done.\n\n' + '─' * 40 + '\n❯ 1. Alpha\n  2. Beta\n' + '─' * 40 + '\n  ⏵⏵ auto mode on\n'
        pane = self.menu(['1. Alpha', '2. Beta', '3. Gamma'])
        menu = pane.screen
        pane.screen = lambda: draft if pane.keys else menu()
        with patch.object(terminal, 'verify'), self.assertRaisesRegex(RuntimeError, 'no longer open'):
            terminal.menu_choose({'id': '%m', 'agent': 'claude'}, 3, label='Gamma', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, ['Down'])

    def test_option_ten_is_reached_with_arrows(self):
        rows = [f'{n}. Option {n}' for n in range(1, 12)]
        pane = self.menu(rows)
        with patch.object(terminal, 'verify'):
            terminal.menu_choose({'id': '%m', 'agent': 'claude'}, 10, label='Option 10', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, ['Down'] * 9 + ['Enter'])


class DetailLineTests(unittest.TestCase):
    """Claude's Rewind draws a dim detail line under each point. It folds into the point, so it is never a row."""
    rows = ['one', 'No code changes', 'two', 'No code changes', '(current)']

    def test_dim_lines_fold_into_the_entry_above(self):
        pane = FakePane(self.rows, cursor=4, dim=(1, 3))
        styled = pane.screen()
        picker = terminal.claude_picker(re.sub(r'\x1b\[[0-9;]*m', '', styled), styled)
        self.assertEqual([(item['text'], item.get('detail', '')) for item in picker['items']],
                         [('one', 'No code changes'), ('two', 'No code changes'), ('(current)', '')])

    def test_folded_entry_two_lines_up_is_reached(self):
        pane = FakePane(self.rows, cursor=4, step=2, dim=(1, 3))
        with patch.object(terminal, 'verify'):
            terminal.picker_choose({'id': '%p', 'agent': 'claude'}, 0, ['one\nNo code changes', 'two\nNo code changes', '(current)'],
                                   heading='Pick one', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, ['Up', 'Up', 'Enter'])

    def test_swapped_details_are_refused(self):
        # Two Rewind points with the same text: the detail tells them apart. A swap between screens is a new list.
        rows = ['Fix', 'Edited a.py', 'Fix', 'No code changes', '(current)']
        pane = FakePane(rows, cursor=4, step=2, dim=(1, 3))
        with patch.object(terminal, 'verify'), self.assertRaisesRegex(RuntimeError, 'list changed'):
            terminal.picker_choose({'id': '%p', 'agent': 'claude'}, 0, ['Fix\nNo code changes', 'Fix\nEdited a.py', '(current)'],
                                   heading='Pick one', run=pane, sleep=lambda _: None)
        self.assertEqual(pane.keys, [])

    def test_no_styled_frame_folds_nothing(self):
        pane = FakePane(self.rows, cursor=4, dim=(1, 3))
        self.assertEqual(len(terminal.claude_picker(re.sub(r'\x1b\[[0-9;]*m', '', pane.screen()))['items']), 5)


class CodexModelMenuFixtureTests(unittest.TestCase):
    """Codex 0.169.0 /model: a numbered list in columns, with a lone "↓" when more models sit below."""
    def test_names_details_and_more_below(self):
        with open(os.path.join(FIXTURES, 'codex_0.169.0_model_menu.ansi')) as file:
            styled = file.read()
        plain = re.sub(r'\x1b\[[0-9;]*m', '', styled)
        menu = terminal.menu_question(terminal.menu_lines(plain.rstrip().splitlines()), 'codex')
        self.assertEqual(menu['question'], 'Select Model and Effort')
        self.assertEqual(len(menu['options']), 8)
        self.assertEqual(menu['options'][0], {'id': 1, 'text': 'GPT-6.2-Sol (current)', 'detail': 'Latest workhorse model for coding and everyday work.', 'selected': True})
        self.assertFalse(any('↓' in option['detail'] for option in menu['options']))
        self.assertEqual(choice_state.parse(styled, 'codex')['more_below'], -1)


if __name__ == '__main__':
    unittest.main()
