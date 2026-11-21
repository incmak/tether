"""Narrow screen guards: styled input, clipped controls, and private tmux only."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import agents
import footer
import model_picker
import terminal_input as terminal
from own_time import own_time
from test_tmux_isolation import private_tmux


class Screen:
    def __init__(self, styled, width=80, height=39):
        self.styled, self.width, self.height = styled, width, height
        self.commands = []

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[1] == 'capture-pane':
            output = self.styled if '-e' in argv else terminal.ANSI_SGR.sub('', self.styled)
        elif argv[1] == 'display-message':
            output = f'{self.width}\t{self.height}\t80\t40\ttest\tpane\tmanual\n'
        else:
            raise AssertionError(f'Unexpected write: {argv}')
        return subprocess.CompletedProcess(argv, 0, output, '')


class StyledInputTests(unittest.TestCase):
    def test_reported_codex_input_is_empty(self):
        run = Screen('\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do any\x1b[0m\n', 22)
        terminal.empty_prompt({'id': '%test', 'agent': 'codex'}, run)
        self.assertTrue(all('-e' in argv for argv in run.commands if argv[1] == 'capture-pane'))

    def test_cut_rotating_placeholders_use_style_for_every_agent(self):
        for agent, marker in (('claude', '❯'), ('codex', '›'), ('antigravity', '>')):
            for width in (20, 22, 30, 40, 80):
                for hint in ('Ask about the current project and its tests', 'Another hint with no known wording'):
                    text = hint[:width - 2]
                    with self.subTest(agent=agent, width=width, text=text):
                        run = Screen(f'\x1b[1m{marker}\x1b[0m \x1b[2m{text}\x1b[0m\n────\n', width)
                        self.assertFalse(any(terminal.prompt_draft({'id': '%test', 'agent': agent}, run)))

    def test_typed_placeholder_words_are_still_a_draft(self):
        for agent, marker, hint in (
                ('claude', '❯', 'Press up to edit queued messages'),
                ('codex', '›', 'Ask Codex to do anything'),
                ('antigravity', '>', 'Auto mode: edits & safe commands')):
            with self.subTest(agent=agent):
                run = Screen(f'\x1b[1m{marker}\x1b[0m {hint}\n────\n')
                self.assertEqual(terminal.prompt_draft({'id': '%test', 'agent': agent}, run), [hint])

    def test_antigravity_cut_hint_colour_needs_no_mode_words_or_ellipsis(self):
        run = Screen('\x1b[38;5;111m>\x1b[39m \x1b[38;5;109mAuto mo\x1b[39m\n────\n', 20)
        self.assertEqual(terminal.prompt_draft({'id': '%test', 'agent': 'antigravity'}, run), [])

    def test_wrapped_dim_input_and_mixed_typed_continuation(self):
        for agent, marker in (('claude', '❯'), ('codex', '›'), ('antigravity', '>')):
            entry = {'id': '%test', 'agent': agent}
            with self.subTest(agent=agent):
                run = Screen(f'\x1b[2m{marker} a wrapped hint\n  with more words\x1b[0m\n────\n', 20)
                self.assertFalse(any(terminal.prompt_draft(entry, run)))
                run = Screen(f'{marker} \x1b[2ma hint\x1b[22m\n  typed continuation\n────\n', 20)
                self.assertTrue(any(terminal.prompt_draft(entry, run)))

    def test_colour_parameters_do_not_change_faint_attribute(self):
        for codes in ('38;2;2;0;2', '38;5;2', '48;2;0;2;0'):
            with self.subTest(codes=codes):
                styled = f'❯ \x1b[{codes}mtyped text\x1b[0m\n────\n'
                self.assertEqual(terminal.dim_prompt_text(styled), '')
                self.assertEqual(terminal.prompt_draft({'id': '%test', 'agent': 'claude'}, Screen(styled)), ['typed text'])


class NarrowControlTests(unittest.TestCase):
    def test_codex_cwd_capture_joins_physical_wraps(self):
        def run(argv, **kwargs):
            output = 'model · /srv/example/project · low\n' if '-J' in argv else 'model · /srv/example/\nproject · low\n'
            return subprocess.CompletedProcess(argv, 0, output, '')
        self.assertEqual(agents.footer_cwd(run, '%test'), '/srv/example/project')

    def test_antigravity_panel_hint_in_history_never_escapes_a_draft(self):
        screen = 'Earlier instructions: esc Close\n────\n> keep this draft\n────\n'
        run = Screen(screen, 22)
        with self.assertRaises(terminal.DraftError):
            terminal.empty_prompt({'id': '%test', 'agent': 'antigravity'}, run)
        self.assertTrue(all(argv[1] == 'capture-pane' for argv in run.commands))

    def test_typed_queue_hint_never_pulls_history_or_clears_the_draft(self):
        run = Screen('\x1b[39m❯ \x1b[0mPress up to edit queued messages\n────\n', 80)
        with patch.object(terminal, 'verify'):
            with self.assertRaises(terminal.DraftError):
                terminal.unqueue({'id': '%test', 'agent': 'claude'}, run=run, sleep=lambda _: None)
        self.assertTrue(all(argv[1] in ('capture-pane', 'display-message') for argv in run.commands))

    def test_wrapped_login_code_instruction_stays_detected(self):
        self.assertTrue(terminal.login_link(['https://claude.com/cai/oauth/authorize', '', 'Paste', 'code here'])['code'])

    def test_old_approval_above_current_input_is_not_actionable(self):
        screen = 'Run this command?\n❯ 1. Yes\n  2. No\n\nEsc to cancel\n\n● Done\n────\n❯ \n────\n'
        self.assertIsNone(terminal.approval(screen))

    def test_narrow_controls_reject_changed_content_before_any_write(self):
        for agent in ('claude', 'codex', 'antigravity'):
            entry = {'id': '%test', 'agent': agent}
            screen = 'Run this command?\n❯ 1. Yes\n  2. No\n'
            actions = [lambda run: terminal.respond_approval(entry, 'stale-approval-id', choice=1, run=run),
                       lambda run: terminal.menu_choose(entry, 1, label='Yes, allow once', run=run, sleep=lambda _: None)]
            if agent == 'claude':
                actions.extend((lambda run: terminal.unqueue(entry, run=run),
                                lambda run: terminal.card_key(entry, '1', run=run)))
            for action in actions:
                with self.subTest(agent=agent, action=action), patch.object(terminal, 'verify'):
                    run = Screen(screen, 22)
                    with self.assertRaises(RuntimeError) as caught:
                        action(run)
                    self.assertNotIsInstance(caught.exception, terminal.PaneTooSmall)
                    self.assertTrue(all(argv[1] in ('capture-pane', 'display-message') for argv in run.commands))

    def test_22_column_codex_usage_menu_sends_down_and_enter(self):
        screen = 'Usage\nAccount usage and\nresets.\n\n› 1. View analytics\n  2. Redeem reset\n\nenter open · esc\nback'
        menu = terminal.menu_question(screen.splitlines())
        self.assertEqual(menu['question'], 'Usage Account usage and resets.')
        self.assertEqual([(row['id'], row['text'], row['selected']) for row in menu['options']],
                         [(1, 'View analytics', True), (2, 'Redeem reset', False)])
        for choice, label, expected in ((1, 'View analytics', ['Enter']),
                                         (2, 'Redeem reset', ['Down', 'Enter'])):
            with self.subTest(choice=choice):
                state = Screen(screen, 22)
                keys = []

                def run(argv, **kwargs):
                    if argv[1] != 'send-keys':
                        return state(argv, **kwargs)
                    keys.append(argv[-1])
                    if argv[-1] == 'Down':
                        state.styled = screen.replace('› 1.', '  1.').replace('  2.', '› 2.')
                    return subprocess.CompletedProcess(argv, 0, '', '')

                with patch.object(terminal, 'verify'):
                    terminal.menu_choose({'id': '%test', 'agent': 'codex'}, choice, label=label,
                                         run=run, sleep=lambda _: None)
                self.assertEqual(keys, expected)
                self.assertTrue(all(argv[1] == 'capture-pane' for argv in state.commands))

    def test_cut_menu_label_after_move_never_sends_enter(self):
        screen = 'Usage\n› 1. View analytics\n  2. Redeem reset\nenter open · esc back'
        state = Screen(screen, 22)
        keys = []

        def run(argv, **kwargs):
            if argv[1] != 'send-keys':
                return state(argv, **kwargs)
            keys.append(argv[-1])
            state.styled = 'Usage\n  1. View analytics\n› 2. Redeem re\nenter open · esc back'
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch.object(terminal, 'verify'), self.assertRaisesRegex(RuntimeError, 'question changed'):
            terminal.menu_choose({'id': '%test', 'agent': 'codex'}, 2, label='Redeem reset',
                                 run=run, sleep=lambda _: None)
        self.assertEqual(keys, ['Down'])

    def test_narrow_model_open_rejects_a_draft_before_any_write(self):
        for agent, marker in (('claude', '❯'), ('codex', '›'), ('antigravity', '>')):
            with self.subTest(agent=agent), patch.object(terminal, 'verify'), \
                    patch.object(terminal, 'live_status', return_value={'state': 'Idle'}):
                run = Screen(f'{marker} keep this draft\n────\n', 22)
                with self.assertRaises(terminal.DraftError):
                    model_picker.open_picker({'id': '%test', 'agent': agent}, run=run, sleep=lambda _: None)
                self.assertTrue(all(argv[1] == 'capture-pane' for argv in run.commands))

    def test_wrapped_persistent_approval_is_not_a_once_choice(self):
        screen = 'Run this command?\n❯ 1. Yes, and don\'t\n     ask again\n  2. No\n'
        pending = terminal.approval(screen)
        self.assertIsNone(pending['_once'])
        self.assertIn("don't ask again", pending['options'][0]['text'])

    def test_clipped_or_missing_approval_options_are_not_guessed(self):
        for screen in ('Run this command?\n❯ 1. Yes, and don\n  2. No\n',
                       'Run this command?\n❯ 1. Yes\n  3. No\n'):
            with self.subTest(screen=screen):
                pending = terminal.approval(screen)
                if '3. No' in screen:
                    self.assertFalse(pending['actionable'])
                else:
                    self.assertIsNone(pending['_once'])
                run = Screen(screen, 22)
                with patch.object(terminal, 'verify'), self.assertRaises(RuntimeError) as caught:
                    terminal.respond_approval({'id': '%test', 'agent': 'claude'}, pending['id'],
                                              action='approve', run=run)
                self.assertNotIsInstance(caught.exception, terminal.PaneTooSmall)
                self.assertTrue(all(argv[1] == 'capture-pane' for argv in run.commands))

    def test_approval_last_option_continuation_is_bound_to_request(self):
        screen = 'Run this command?\n❯ 1. Yes\n  2. No, explain\n     the reason\n\nEsc to cancel\n'
        pending = terminal.approval(screen)
        self.assertEqual(pending['options'][-1]['text'], 'No, explain the reason')
        self.assertNotEqual(pending['id'], terminal.approval(screen.replace('the reason', 'a new reason'))['id'])

    def test_menu_scroll_rechecks_the_target_id_before_enter(self):
        frames = iter(['Choose\n❯ 1. One\n  3. Three\nEnter to select · Esc to cancel',
                       'Choose\n  1. One\n❯ 2. Two\n  3. Three\nEnter to select · Esc to cancel',
                       'Choose\n  1. One\n  2. Two\n❯ 3. Three\nEnter to select · Esc to cancel'])
        run = Screen('', 80)
        keys = []

        def commands(argv, **kwargs):
            if argv[1] == 'send-keys':
                keys.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, '', '')
            return run(argv, **kwargs)

        with patch.object(terminal, 'verify'), patch.object(terminal, 'capture', side_effect=lambda *args, **kwargs: next(frames)):
            terminal.menu_choose({'id': '%test', 'agent': 'claude'}, 3, label='Three', run=commands, sleep=lambda _: None)
        self.assertEqual(keys, ['Down', 'Down', 'Enter'])

    def test_missing_prompt_in_narrow_or_short_pane_tries_tether_sizing_once(self):
        # Only window_size.apply (Tether's own window sizes) may run; it never touches a split.
        for width, height in ((20, 39), (22, 39), (30, 39), (80, 5)):
            with self.subTest(width=width, height=height):
                run = Screen('A clipped panel\nNo input marker\n', width, height)
                with patch.object(terminal.window_size, 'apply') as apply, \
                        self.assertRaisesRegex(terminal.PaneTooSmall, 'pane|Pane'):
                    terminal.empty_prompt({'id': '%test', 'agent': 'claude'}, run)
                apply.assert_called_once_with(run)
                self.assertTrue(all(argv[1] in ('capture-pane', 'display-message') for argv in run.commands))

    def test_wrapped_approval_is_not_a_draft_or_a_send_target(self):
        for marker in ('❯', '›', '>'):
            screen = f'Command\ntouch example.txt\n\nDo you want to\nproceed?\n{marker} 1. Yes\n  2. No\n\nEsc to cancel\n'
            with self.subTest(marker=marker):
                pending = terminal.approval(screen)
                self.assertIsNotNone(pending)
                run = Screen(screen, 20)
                with self.assertRaisesRegex(RuntimeError, 'approval'):
                    terminal.empty_prompt({'id': '%test', 'agent': {'❯': 'claude', '›': 'codex', '>': 'antigravity'}[marker]}, run)

    def test_clipped_unnumbered_picker_is_never_closed_as_information(self):
        screen = 'Rewind\n\n  Previous point\n❯ (current)\n\nEnter to continue ·\nEsc to cancel\n'
        run = Screen(screen, 20)
        self.assertFalse(terminal.close_info_panel({'id': '%test', 'agent': 'claude'}, run))

    def test_wrapped_panel_close_hint_is_recognised(self):
        run = Screen('Usage\nCurrent session\nEsc to\ncancel\n', 20)
        with own_time(terminal, sleep=Mock()):
            with patch.object(terminal, 'capture', return_value=run.styled), patch.object(terminal.subprocess, 'run') as send:
                self.assertTrue(terminal.close_info_panel({'id': '%test', 'agent': 'claude'}, run=send))
                self.assertEqual(send.call_args.args[0][-1], 'Escape')

    def test_wrapped_model_headers_and_hints(self):
        screen = 'Select Model and\nEffort\n\n› 1. Model A\n  2. Model B\n\nenter select · esc\nback\n'
        self.assertTrue(model_picker.picker_open('codex', screen))
        self.assertEqual(model_picker.parse({'agent': 'codex'}, screen)['rows'][0]['label'], 'Model A')

    def test_model_cursor_uses_live_rows_instead_of_old_scrollback(self):
        history = 'Select Model and Effort\n› 1. Model A\n  2. Model B\n\n› 2. Model B\nenter select · esc back\n'
        visible = '  1. Model A\n› 2. Model B\nenter select · esc back\n'
        parsed = model_picker.parse({'agent': 'codex'}, history, visible)
        self.assertEqual([row['id'] for row in parsed['rows'] if row['selected']], ['2'])

    def test_multiple_or_duplicate_model_rows_never_choose_a_model(self):
        entry = {'id': '%test', 'agent': 'codex'}
        for rows in ([{'id': '1', 'selected': True}, {'id': '2', 'selected': True}],
                     [{'id': 'Model with a cut name', 'selected': True}, {'id': 'Model with a cut name', 'selected': False}]):
            with self.subTest(rows=rows), patch.object(model_picker, 'press') as press:
                with self.assertRaises(model_picker.PickerError):
                    model_picker.move_to(entry, rows[0]['id'], fetch=lambda: {'rows': rows})
                press.assert_not_called()

    def test_hidden_effort_cursor_is_unknown(self):
        self.assertIsNone(model_picker.slider_level(['  Effort  ◂     ●━━━━━━━━●━━━━', '               low    medium']))
        self.assertIsNone(model_picker.slider_level(['  Effort                               ◉', '                                    medi']))
        self.assertIsNone(model_picker.slider_level(['  Effort                               ◉', '               low    medium']))
        self.assertEqual(model_picker.claude_effort('Extra high\neffort (default)\n←/→ to adjust'), 'extra high')

    def test_missing_or_cut_mode_never_causes_a_key(self):
        for result in ({'observed': False, 'mode': ''}, {'observed': True, 'mode': '⏵⏵ au'}):
            with self.subTest(result=result), patch.object(footer, 'time'):
                presses = []
                with self.assertRaises(RuntimeError):
                    footer.set_mode(lambda: result, lambda: presses.append(True), 'auto', sleep=lambda _: None)
                self.assertEqual(presses, [])


class CapturedScreenTests(unittest.TestCase):
    fixtures = json.loads((Path(__file__).parent / 'test_fixtures/narrow/screens.json').read_text())

    def test_styled_captures_at_20_22_30_40_and_80_columns(self):
        for fixture in self.fixtures:
            kind = fixture['kind']
            if kind not in ('empty', 'typed', 'typed_hint', 'wrapped_hint', 'wrapped_draft'):
                continue
            with self.subTest(agent=fixture['agent'], width=fixture['width'], kind=kind, source=fixture['source']):
                run = Screen(fixture['screen'], fixture['width'])
                draft = terminal.prompt_draft({'id': '%test', 'agent': fixture['agent']}, run)
                self.assertEqual(any(draft), kind in ('typed', 'typed_hint', 'wrapped_draft'))
                if kind == 'typed':
                    self.assertEqual(draft, ['keep this draft'])

    def test_captured_menus_and_approvals_never_get_message_input(self):
        for fixture in self.fixtures:
            if fixture['kind'] not in ('menu', 'approval', 'clipped_menu', 'clipped_approval'):
                continue
            with self.subTest(agent=fixture['agent'], width=fixture['width'], kind=fixture['kind']):
                run = Screen(fixture['screen'], fixture['width'])
                entry = {'id': '%test', 'agent': fixture['agent']}
                with self.assertRaises(RuntimeError) as caught:
                    terminal.empty_prompt(entry, run)
                self.assertNotIsInstance(caught.exception, terminal.DraftError)
                self.assertTrue(all(argv[1] in ('capture-pane', 'display-message') for argv in run.commands))

    def test_narrow_slash_commands_never_press_enter_twice_on_a_menu(self):
        for fixture in self.fixtures:
            if fixture['kind'] not in ('menu', 'clipped_menu'):
                continue
            with self.subTest(agent=fixture['agent'], width=fixture['width'], kind=fixture['kind']):
                agent = fixture['agent']
                initial = next(item for item in self.fixtures if item['agent'] == agent
                               and item['width'] == fixture['width'] and item['kind'] == 'empty')
                state = Screen(initial['screen'], fixture['width'])
                enters = []

                def run(argv, **kwargs):
                    if argv[1] == 'send-keys':
                        self.assertEqual(argv[-1], 'Enter')
                        enters.append(True)
                        state.styled = fixture['screen']
                    elif argv[1] not in ('load-buffer', 'paste-buffer', 'delete-buffer'):
                        return state(argv, **kwargs)
                    return subprocess.CompletedProcess(argv, 0, '', '')

                with patch.object(terminal, 'verify'), patch.object(terminal, 'leave_scrollback'), own_time(terminal, sleep=Mock()):
                    terminal.submit({'id': '%test', 'agent': agent}, '/model', run=run)
                self.assertEqual(enters, [True])

    @unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
    def test_real_tmux_22_column_styled_input_on_private_socket(self):
        fixture = next(item for item in self.fixtures if item['source'].startswith('native Codex'))
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix='narrow-pane-'))
        env = dict(HOME=folder, CLAUDE_CONFIG_DIR=folder + '/.claude', CODEX_HOME=folder + '/.codex')
        tmux = private_tmux(self, 'tnarrow-', env).tmux
        screen = fixture['screen'].rstrip('\n') + '\n\nmodel · /srv/example/project · low'
        code = 'import time; print(' + repr(screen) + ',flush=True); time.sleep(10)'
        command = 'timeout 12 ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code) + ' </dev/null'
        created = tmux('new-session', '-d', '-s', 'input', '-x', '22', '-y', '39', '-P', '-F', '#{pane_id}', command)
        created.check_returncode()
        pane = created.stdout.strip()
        deadline = time.monotonic() + 3
        while '›' not in tmux('capture-pane', '-e', '-p', '-t', pane).stdout:
            if time.monotonic() >= deadline:
                self.fail('The private pane did not draw its input.')
            time.sleep(.02)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return tmux(*argv[1:])

        terminal.empty_prompt({'id': pane, 'agent': 'codex'}, run)
        self.assertEqual(tmux('display-message', '-p', '-t', pane, '#{pane_width}x#{pane_height}').stdout.strip(), '22x39')
        self.assertTrue(all(argv[1] == 'capture-pane' and '-e' in argv for argv in calls))
        self.assertEqual(agents.footer_cwd(run, pane), '/srv/example/project')


if __name__ == '__main__':
    unittest.main()
