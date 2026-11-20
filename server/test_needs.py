"""The one-line "Needs you" summary in the session list."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
from unittest.mock import patch

import needs
import server
import terminal_input as terminal

BASH = '''● Bash(rm -r build)

────────────────────────────────────────────────────────
 Bash command

   rm -r build
   Remove the old build folder

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /work
   3. No, and tell Claude what to do differently (esc)
'''

PLAN = '''Here is Claude's plan:
# Add the inbox panel

Would you like to proceed?
❯ 1. Yes, and auto-accept edits
  2. Yes, manually approve edits
  3. No, keep planning
  4. Tell Claude what to change
'''


class SummaryTests(unittest.TestCase):
    def test_permission_names_the_tool_and_command(self):
        pending = terminal.approval(BASH)
        result = needs.summary({'state': 'Approval', 'approval': {k: v for k, v in pending.items() if not k.startswith('_')}})
        self.assertEqual(result, {'kind': 'approval', 'text': 'Bash command · rm -r build'})

    def test_plan_chooser_shows_the_plan_heading(self):
        pending = terminal.approval(PLAN)
        result = needs.summary({'state': 'Approval', 'approval': pending})
        self.assertEqual(result, {'kind': 'plan', 'text': 'Add the inbox panel'})

    def test_read_only_approval_still_has_a_line(self):
        result = needs.summary({'state': 'Approval', 'approval': {'id': None, 'text': '', 'actionable': False}})
        self.assertEqual(result, {'kind': 'approval', 'text': 'Waiting for your permission'})

    def test_menu_uses_its_question(self):
        attention = {'id': 'a', 'reason': 'menu', 'text': '...', 'question': 'Which  database?',
                     'options': [{'id': 1, 'text': 'Postgres'}, {'id': 2, 'text': 'SQLite'}]}
        self.assertEqual(needs.summary({'state': 'Input needed', 'attention': attention}), {'kind': 'question', 'text': 'Which database?'})

    def test_prompt_without_question_uses_the_last_question_line(self):
        attention = {'id': 'b', 'reason': 'prompt', 'text': '│ Some output\n│ Enter the code?\n│ 1. not an option line\n╰──────╯'}
        self.assertEqual(needs.summary({'state': 'Input needed', 'attention': attention}), {'kind': 'question', 'text': 'Enter the code?'})

    def test_long_text_is_cut_to_one_line(self):
        result = needs.summary({'state': 'Input needed', 'attention': {'id': 'c', 'reason': 'menu', 'text': '', 'question': 'x' * 400}})
        self.assertEqual(len(result['text']), needs.LIMIT)
        self.assertTrue(result['text'].endswith('…'))

    def test_calm_states_and_panels_need_nothing(self):
        self.assertIsNone(needs.summary({'state': 'Idle', 'approval': None}))
        self.assertIsNone(needs.summary({'state': 'Working', 'approval': None, 'attention': {'id': 'p', 'reason': 'panel', 'text': 'Usage'}}))
        self.assertIsNone(needs.summary(None))


class SessionRowTests(unittest.TestCase):
    def test_row_carries_needs_without_another_status_read(self):
        entry = {'id': '%9', 'tmux_session': '$1', 'label': 'work:0.0', 'agent': 'claude', 'thread': 't', 'writable': True}
        status = {'state': 'Input needed', 'approval': None, 'attention': {'id': 'q', 'reason': 'menu', 'text': '', 'question': 'Ship it?'}}
        with patch.object(server, 'shared_status', return_value=status) as read, \
                patch.object(server, 'tab_folder', return_value='/w'), patch.object(server, 'home_path', return_value='~/w'):
            row = server.session_row(entry)
        read.assert_called_once()
        self.assertEqual(row['needs'], {'kind': 'question', 'text': 'Ship it?'})
        self.assertEqual(row['state'], 'Input needed')
        self.assertEqual(row['dir'], '~/w')
        self.assertIn('finished', row)


if __name__ == '__main__':
    unittest.main()
