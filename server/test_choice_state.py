"""Source-derived ANSI frames, not authenticated runtime captures.

Codex rust-v0.166.0: bottom_pane/{list_selection_view,multi_select_picker,
request_user_input/render}. Claude 2.1.318: question tabs at byte 318751843,
question renderer at 318764000, checkbox glyph table at 289538072.
Claude resume renderer at 324513007; qE filter at 301747028 (⌕ prefix).
Fixtures use public dummy labels. Every width is a physical terminal layout.
"""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

import choice_state
import terminal_input as terminal
from own_time import own_time


def styled(text, code='1;7'):
    return f'\x1b[{code}m{text}\x1b[0m'


def frame(width, rows, hint='enter select · esc back', header=None):
    header = header or [styled('Choices', '1'), '']
    footer = [styled(line, '2') for line in textwrap.wrap(hint, width - 2)]
    result = '\n'.join(header + rows + [''] + footer) + '\n'
    assert all(len(terminal.ANSI_SGR.sub('', row)) <= width for row in result.splitlines())
    return result


class ChoiceStateTests(unittest.TestCase):
    def setUp(self):
        terminal._attention_seen.clear()

    def attention(self, screen, agent):
        entry = {'id': '%choices', 'agent': agent}
        plain = terminal.ANSI_SGR.sub('', screen)
        terminal.attention(entry, plain, False, now=0, styled_screen=screen)
        return terminal.attention(entry, plain, False, now=4, styled_screen=screen)

    def test_codex_single_picker_and_dispatch_at_each_width(self):
        for width in (23, 40, 80):
            with self.subTest(width=width):
                terminal._attention_seen.clear()
                screen = frame(width, ['  Alpha', styled('› Beta'), '  Gamma'])
                result = choice_state.parse(screen, 'codex')
                self.assertEqual(result['selection'], 'single')
                self.assertEqual(result['focus'], 1)
                self.assertEqual([i['text'] for i in result['items']], ['Alpha', 'Beta', 'Gamma'])
                self.assertEqual(self.attention(screen, 'codex')['choice'], result)
                self.assertEqual(self.attention(screen, 'codex')['reason'], 'picker')

    def test_codex_multiselect_has_two_checks_independent_of_focus(self):
        for width in (23, 40, 80):
            with self.subTest(width=width):
                rows = ['  [x] Alpha', '  [ ] Beta', styled('› [ ] Gamma'), '  [x] Delta', '  [ ] Echo']
                screen = frame(width, rows, 'space toggle · ←/→ reorder · enter save · esc cancel')
                result = choice_state.parse(screen, 'codex')
                self.assertEqual(result['selection'], 'multi')
                self.assertEqual(result['focus'], 2)
                self.assertEqual([i['checked'] for i in result['items']], [True, False, False, True, False])
                self.assertIn('←/→ reorder', result['hints'])
                self.assertEqual(self.attention(screen, 'codex')['reason'], 'picker')

    def test_filter_tabs_and_counts_at_each_width(self):
        for width in (23, 40, 80):
            for agent, cursor in (('claude', '❯'), ('codex', '›')):
                with self.subTest(width=width, agent=agent):
                    terminal._attention_seen.clear()
                    header = [styled('Select session', '1'), '', styled('All', '4') + '  Local',
                              'Search: release', '']
                    screen = frame(width, ['↑ 12 more', '  Alpha', styled(cursor + ' Beta'), '↓ 7 more'],
                                   'Enter to select · Esc to cancel', header)
                    result = choice_state.parse(screen, agent)
                    self.assertEqual(result['filter'], 'release')
                    self.assertEqual(result['tabs'], [{'label': 'All', 'active': True}, {'label': 'Local', 'active': False}])
                    self.assertEqual((result['more_above'], result['more_below']), (12, 7))
                    self.assertEqual([i['text'] for i in result['items']], ['Alpha', 'Beta'])
                    self.assertEqual(self.attention(screen, agent)['choice'], result)

    def test_claude_three_question_tabs_and_multi_select_at_each_width(self):
        for width in (23, 40, 80):
            with self.subTest(width=width):
                terminal._attention_seen.clear()
                header = ['──────────', '☒ A  ' + styled('☐ B', '44') + '  ☐ C', '', 'Choose features?', '']
                screen = frame(width, [styled('❯ 1. ☒ Alpha', '1;38;5;75'), '  2. ☐ Beta',
                                       '  3. ☒ Gamma'],
                               'Space to toggle · Enter to confirm · Esc to cancel', header)
                result = choice_state.parse(screen, 'claude')
                self.assertEqual(result['selection'], 'multi')
                self.assertEqual([i['checked'] for i in result['items']], [True, False, True])
                self.assertEqual(result['tabs'], [{'label': 'A', 'active': False},
                                                 {'label': 'B', 'active': True},
                                                 {'label': 'C', 'active': False}])
                self.assertEqual(result['focus'], 0)
                found = self.attention(screen, 'claude')
                self.assertEqual(found['reason'], 'menu')
                self.assertEqual([i['id'] for i in found['options']], [1, 2, 3])
                self.assertEqual(found['choice'], result)

    def test_claude_resume_uses_the_source_filter_and_footer(self):
        for width in (23, 40, 80):
            with self.subTest(width=width):
                terminal._attention_seen.clear()
                header = [styled('Resume session', '1;36'), '╭' + '─' * (width - 2) + '╮',
                          '│ ⌕ release' + ' ' * (width - 12) + '│', '╰' + '─' * (width - 2) + '╯', '']
                screen = frame(width, [styled('❯ 1. Alpha', '1;36'), '  2. Beta'],
                               'ctrl+a show all projects · Type to search · Esc to cancel', header)
                result = choice_state.parse(screen, 'claude')
                self.assertEqual(result['filter'], 'release')
                self.assertEqual(result['focus'], 0)
                self.assertEqual(len(result['items']), 2)
                self.assertEqual(self.attention(screen, 'claude')['choice'], result)

    def test_numbered_menu_over_nine_keeps_ids_and_existing_fields(self):
        screen = frame(80, [styled('› 1. One')] + [f'  {i}. Option {i}' for i in range(2, 13)],
                       'Enter to select · Esc to cancel')
        found = self.attention(screen, 'codex')
        self.assertEqual([i['id'] for i in found['options']], list(range(1, 13)))
        self.assertEqual([i['id'] for i in found['choice']['items']], list(range(1, 13)))
        self.assertEqual(found['options'][0]['text'], 'One')
        self.assertEqual(found['id'], self.attention(terminal.ANSI_SGR.sub('', screen), 'codex')['id'])

    def test_codex_request_user_input_with_wrapped_details(self):
        for width in (23, 40, 80):
            with self.subTest(width=width):
                screen = frame(width, [styled('› 1. Alpha'), '     First choice.', '  2. Beta', '     Second choice.'],
                               'tab to add notes | enter to submit answer | ←/→ to navigate questions | esc to interrupt',
                               ['Question 1/3', 'Choose an option.', ''])
                result = choice_state.parse(screen, 'codex')
                self.assertEqual(result['items'][0]['detail'], 'First choice.')
                self.assertEqual(len(result['items']), 2)
                self.assertIn('←/→ to navigate questions', result['hints'])

    def test_style_focus_reverse_colour_and_rgb(self):
        for code in ('7', '1;36', '1;38;5;75', '1;38;2;90;130;255', '48;2;50;50;50'):
            with self.subTest(code=code):
                result = choice_state.parse(frame(40, ['  Alpha', styled('  Beta', code), '  Gamma']), 'codex')
                self.assertEqual(result['focus'], 1)

    def test_checkbox_glyphs_are_not_disabled_flags(self):
        for yes, no in (('[x]', '[ ]'), ('☑', '☐'), ('☒', '☐'), ('✔', '☐'), ('◉', '◯'), ('[×]', '[ ]')):
            with self.subTest(glyph=yes):
                result = choice_state.parse(frame(40, [styled('› ' + yes + ' Alpha'), '  ' + no + ' Beta'],
                                                 'space toggle · enter save · esc cancel'), 'claude')
                self.assertEqual([i['checked'] for i in result['items']], [True, False])
                self.assertNotIn('disabled', result['items'][1])
                self.assertEqual(result['selection'], 'single' if yes == '◉' else 'multi')

    def test_checkbox_before_number_uses_the_visible_number(self):
        screen = frame(40, [styled('❯ ☒ 1. Alpha'), '  ☐ 2. Beta'],
                       'space toggle · enter save · esc cancel')
        result = choice_state.parse(screen, 'claude')
        self.assertEqual([i['id'] for i in result['items']], [1, 2])
        self.assertEqual([i['text'] for i in result['items']], ['Alpha', 'Beta'])

    def test_existing_captured_claude_frames_keep_every_visible_item(self):
        root = Path(__file__).parent
        for name, count in (('fixtures/claude_question_narrow.txt', 5),
                            ('test_fixtures/pickers/claude_rewind_150x39.txt', 3)):
            with self.subTest(name=name):
                screen = (root / name).read_text()
                result = choice_state.parse(screen, 'claude')
                self.assertEqual(len(result['items']), count)
        self.assertEqual(result['more_above'], 26)
        self.assertEqual(result['focus'], 2)

    def test_captured_native_codex_model_frame(self):
        screen = (Path(__file__).parent / 'test_fixtures/choices/codex_model_40.ansi').read_text()
        result = choice_state.parse(screen, 'codex')
        self.assertEqual([i['id'] for i in result['items']], list(range(1, 9)))
        self.assertEqual(result['focus'], 0)
        self.assertEqual(result['more_below'], -1)
        self.assertIsNone(result['filter'])

    def test_codex_question_option_counter_supplies_hidden_counts(self):
        screen = frame(40, ['  2. Tests', '  3. Review', styled('› 4. Refactor')],
                       'option 4/5 | tab to add notes | enter to submit answer | esc to interrupt')
        result = choice_state.parse(screen, 'codex')
        self.assertEqual((result['more_above'], result['more_below']), (1, 1))

    def test_picker_after_chat_uses_the_styled_heading_boundary(self):
        screen = '› 1. earlier question\n● earlier reply\n' + frame(40, [styled('› Alpha'), '  Beta'])
        self.assertEqual(choice_state.parse(screen, 'codex')['items'][0]['text'], 'Alpha')
        picker = choice_state.cursor_picker(screen, 'codex')
        self.assertNotIn('earlier', picker['text'])
        self.assertEqual(picker['items'][0]['text'], 'Alpha')

    def test_title_does_not_become_a_filter(self):
        screen = frame(40, [styled('› 1. Alpha'), '  2. Beta'],
                       header=['Older status facts', '', styled('Select Model', '1'), ''])
        self.assertIsNone(choice_state.parse(screen, 'codex')['filter'])

    def test_dim_description_does_not_disable_an_item(self):
        screen = frame(40, [styled('› Alpha'), '  Beta', styled('    A detail', '2'), '  Gamma (disabled)'])
        result = choice_state.parse(screen, 'codex')
        self.assertNotIn('disabled', result['items'][1])
        self.assertEqual(result['items'][1]['detail'], 'A detail')
        self.assertTrue(result['items'][2]['disabled'])

    def test_osc_links_and_attributes_across_rows(self):
        screen = frame(40, ['  Alpha', '\x1b[7m› \x1b]8;;https://example.com\x1b\\Beta\x1b]8;;\x1b\\',
                               '\x1b[27m  Gamma'])
        result = choice_state.parse(screen, 'codex')
        self.assertEqual(result['focus'], 1)
        self.assertEqual(result['items'][1]['text'], 'Beta')

    def test_overflow_arrow_is_explicitly_unknown(self):
        screen = frame(40, ['↑', styled('› Alpha'), '  Beta', '↓'])
        result = choice_state.parse(screen, 'codex')
        self.assertEqual((result['more_above'], result['more_below']), (-1, -1))

    def test_codex_immediate_filter_source_layout(self):
        screen = frame(40, [styled('› Alpha'), '  Beta'], header=[styled('Fields', '1'), '', 'release', ''])
        self.assertEqual(choice_state.parse(screen, 'codex')['filter'], 'release')

    def test_multi_preview_is_not_an_extra_item(self):
        screen = frame(40, [styled('› [x] Alpha'), '  [ ] Beta', '  [x] Gamma', '', '  Alpha · Gamma'],
                       'space toggle · enter save · esc cancel')
        self.assertEqual(len(choice_state.parse(screen, 'codex')['items']), 3)
        self.assertIsNone(choice_state.parse(screen.replace('Alpha · Gamma', 'Unfamiliar footer'), 'codex'))
        self.assertIsNone(choice_state.parse(frame(40, [styled('› [?] Alpha'), '  [?] Beta'],
                                                  'space toggle · enter save · esc cancel'), 'codex'))

    def test_unknown_ambiguous_old_and_clipped_frames_fail_closed(self):
        valid = frame(40, ['  Alpha', styled('› Beta'), '  Gamma'])
        frames = [valid.replace('esc back', 'esc ba'), valid.replace('Alpha', 'Al…'),
                  valid.replace('  Alpha', '› Alpha'), valid + '\n● Reply\n› \n',
                  valid.replace('Choices', '› conversation'),
                  '  Alpha\n' + styled('› Beta') + '\nenter select · esc back\n',
                  'Just prose.\nenter select · esc back\n',
                  frame(40, ['  Alpha', '  Beta']),
                  frame(40, [styled('› Alpha'), styled('  Beta')]),
                  valid.replace('  Gamma', '  Gamma\x1b[2J')]
        for screen in frames:
            with self.subTest(screen=repr(screen)):
                self.assertIsNone(choice_state.parse(screen, 'codex'))

    def test_clipped_frames_at_each_width(self):
        for width in (23, 40, 80):
            screen = frame(width, [styled('› Alpha'), '  Beta'])
            rows = screen.splitlines()
            for clipped in ('\n'.join(rows[:-1]), '\n'.join(rows[2:]), screen.replace('Alpha', 'Al…')):
                with self.subTest(width=width, screen=repr(clipped)):
                    self.assertIsNone(choice_state.parse(clipped, 'codex'))

    def test_live_status_uses_one_styled_frame(self):
        screen = frame(40, ['  Alpha', styled('› Beta'), '  Gamma'])
        entry = {'id': '%test', 'agent': 'codex', 'writable': True, 'owner_pid': 123}
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, screen, '')
        with own_time(terminal, monotonic=Mock(return_value=0)):
            terminal.live_status(entry, run)
        calls.clear()
        with own_time(terminal, monotonic=Mock(return_value=4)):
            found = terminal.live_status(entry, run)
        self.assertEqual(found['attention']['choice']['focus'], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('-e', calls[0])

    def test_antigravity_unmatched_confirmation_stays_read_only(self):
        for width in (23, 40, 80):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as folder:
                Path(folder, 'thread.json').write_text(json.dumps({'tool_confirmation_pending': True}))
                entry = {'id': '%agy', 'agent': 'antigravity', 'thread': 'thread'}
                screen = frame(width, [styled('> Alpha'), '  Beta'])
                with patch.object(terminal, 'AGY_CACHE', Path(folder)), patch.object(terminal, 'capture_notices'):
                    result = terminal._live_status(entry, None, terminal.ANSI_SGR.sub('', screen), styled_screen=screen)
                self.assertEqual(result['state'], 'Approval')
                self.assertEqual(result['approval']['actionable'], False)
                self.assertNotIn('attention', result)


if __name__ == '__main__':
    unittest.main()
