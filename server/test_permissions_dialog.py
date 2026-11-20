"""Saved Claude permissions dialogs parse without terminal access."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import unittest

import permissions_dialog


FIXTURES = Path(__file__).parent / 'fixtures'
TABS = ['Recently blocked', 'Allow', 'Ask', 'Deny', 'Auto mode', 'Workspace']
HEADER = '   Permissions  ' + '   '.join(TABS)
HINT = '←/→ to switch · ↓ to select · Esc to cancel'


class PermissionsDialogTests(unittest.TestCase):
    def test_saved_dialogs(self):
        for name, tab in [('ask', 'Ask'), ('recently_blocked', 'Recently blocked')]:
            with self.subTest(name=name):
                screen = (FIXTURES / f'claude_permissions_{name}.txt').read_text()
                styled = (FIXTURES / f'claude_permissions_{name}.ansi').read_text()
                found = permissions_dialog.parse(screen, styled)
                self.assertEqual(found['tabs'], TABS)
                self.assertEqual(found['tab'], tab)
                self.assertEqual(found['hint'], HINT)
                self.assertIsNone(found['selected'])
                self.assertEqual(found['empty'], tab == 'Recently blocked')
                if tab == 'Ask':
                    self.assertEqual(found['lines'], [
                        '', 'Claude Code will always ask for confirmation before using these tools.',
                        '╭' + '─' * 68 + '╮', '│ ⌕ Search…' + ' ' * 58 + '│',
                        '╰' + '─' * 68 + '╯', '', '  1. Add a new rule…'])
                else:
                    self.assertEqual(found['lines'], [
                        '', 'No recent blocks. Commands auto mode blocks will appear here.'])

    def test_cursor_row_and_command_details(self):
        for marker in ('❯', '›'):
            with self.subTest(marker=marker):
                screen = (HEADER + '\n\n   Bash(first)\n   ' + marker + ' Bash(second)\n'
                          "   This row can't be approved here\n\n   curl x\n\n"
                          '   r to retry · Enter to approve for later · Esc to close and apply\n')
                found = permissions_dialog.parse(screen, None)
                self.assertEqual(found['selected'], 2)
                self.assertEqual(found['lines'][found['selected']], marker + ' Bash(second)')
                self.assertIn("This row can't be approved here", found['lines'])
                self.assertEqual(found['lines'][-1], 'curl x')
                self.assertTrue(found['hint'].startswith('r to retry'))

    def test_background_tab_and_row_styles(self):
        for style in ('48;5;153', '44', '104', '1;38;5;16;48;5;153'):
            with self.subTest(style=style):
                screen = HEADER + '\n   First\n   Second\n   ' + HINT
                styled = (HEADER.replace('Recently blocked', '\x1b[' + style + 'm Recently blocked \x1b[0m')
                          + '\n   First\n   \x1b[' + style + 'mSecond\x1b[0m\n   ' + HINT)
                found = permissions_dialog.parse(screen, styled)
                self.assertEqual(found['tab'], 'Recently blocked')
                self.assertEqual(found['selected'], 1)

    def test_inverse_row_but_foreground_and_blank_background_are_not_selected(self):
        screen = HEADER + '\n   First\n   Second\n   ' + HINT
        styled = (HEADER + '\n\x1b[48;5;153m   \x1b[49m\x1b[38;5;104mFirst\x1b[39m'
                  '\n   \x1b[7mSecond\x1b[27m\n   ' + HINT)
        found = permissions_dialog.parse(screen, styled)
        self.assertEqual(found['selected'], 1)
        self.assertIsNone(found['tab'])

    def test_last_dialog_optional_tabs_and_unknown_active_tab(self):
        screen = HEADER + '\n   Old body\n   ' + HINT + '\n'
        screen += '   Permissions  Allow   Ask   Deny   Workspace\n   New body\n\n'
        for styled in (None, screen):
            with self.subTest(styled=styled is not None):
                found = permissions_dialog.parse(screen, styled)
                self.assertEqual(found['tabs'], ['Allow', 'Ask', 'Deny', 'Workspace'])
                self.assertIsNone(found['tab'])
                self.assertEqual(found['lines'], ['New body'])
                self.assertEqual(found['hint'], '')
                self.assertFalse(found['empty'])

    def test_plain_screen_does_not_guess_active_tab_or_empty(self):
        screen = (FIXTURES / 'claude_permissions_recently_blocked.txt').read_text()
        found = permissions_dialog.parse(screen, None)
        self.assertIsNone(found['tab'])
        self.assertFalse(found['empty'])

    def test_list_focus_shows_the_active_tab_in_inverse(self):
        # Real capture (2.1.318): after Down, the tab row shows `ESC[1;7m Ask ESC[0m` and the row has a cursor.
        name = 'claude_permissions_ask_list'
        dialog = permissions_dialog.parse((FIXTURES / f'{name}.txt').read_text(), (FIXTURES / f'{name}.ansi').read_text())
        self.assertEqual(dialog['tab'], 'Ask')
        self.assertEqual(dialog['lines'][dialog['selected']], '❯ 1. Add a new rule…')
        self.assertIn('Enter to select', dialog['hint'])

    def test_tab_background_survives_foreground_or_bold_changes(self):
        styled = HEADER.replace('Recently blocked',
                                '\x1b[48;5;153m Recently\x1b[1;38;5;16m blocked \x1b[0m')
        found = permissions_dialog.parse(HEADER + '\n   Body', styled + '\n   Body')
        self.assertEqual(found['tab'], 'Recently blocked')

    def test_prose_is_not_a_dialog(self):
        self.assertIsNone(permissions_dialog.parse('● Permissions let you choose Allow or Ask.\n❯ ', None))


if __name__ == '__main__':
    unittest.main()
