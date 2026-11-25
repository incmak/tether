"""Plan review pairs only unresolved transcript plans with the native chooser."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import agents
import history
import server
import terminal_input as terminal

SCREEN = '''Here is Claude's plan:
# A plan

Would you like to proceed?
❯ 1. Yes, and auto-accept edits
  2. Yes, manually approve edits
  3. No, keep planning
  4. Tell Claude what to change
'''


class PlanReviewTests(unittest.TestCase):
    def setUp(self):
        history.forget()

    def test_detection_and_labels(self):
        for question in ('Would you like to proceed?', 'Ready to code?',
                         'Claude has written up a plan and is ready to execute. Would you like to proceed?'):
            with self.subTest(question=question):
                pending = terminal.approval(SCREEN.replace('Would you like to proceed?', question))
                self.assertTrue(terminal.plan_menu(pending))
                self.assertEqual(pending['options'][1]['text'], 'Yes, manually approve edits')
        self.assertFalse(terminal.plan_menu(terminal.approval('Run this command?\n❯ 1. Yes\n  2. No')))
        self.assertFalse(terminal.plan_menu(None))

    def test_pairing_latest_pending_main_plan_and_resolution(self):
        def tool(identifier, text, **extra):
            return dict(type='assistant', message={'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': identifier, 'name': 'ExitPlanMode', 'input': {'plan': text}}]}, **extra)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'chat.jsonl'
            config = {'id': 'fake', 'thread': 'fake', 'agent': 'claude', 'transcript': str(path)}
            records = [tool('old', '# Old'), tool('new', '# New\n\n- Item'), tool('side', '# Side', isSidechain=True)]
            path.write_text(''.join(json.dumps(record) + '\n' for record in records))
            def snapshot(config):
                return agents.claude_snapshot(config, lambda text: text)
            with patch.object(server, 'snapshot', side_effect=snapshot), patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages), patch.object(server, 'live_status') as status:
                for pending in ('approval', 'attention'):
                    status.return_value = {'state': 'Approval', pending: terminal.approval(SCREEN)}
                    self.assertEqual(server.chat_payload(config)['plan'], {'id': 'new', 'text': '# New\n\n- Item'})
                status.return_value = {'state': 'Idle', 'approval': None}
                self.assertNotIn('plan', server.chat_payload(config))
                status.return_value = {'state': 'Approval', 'approval': terminal.approval(SCREEN)}
                with path.open('a') as stream:
                    stream.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'new', 'content': 'Rejected'}]}}) + '\n')
                self.assertIsNone(server.chat_payload(config)['plan'])
                with path.open('a') as stream:
                    stream.write(json.dumps(tool('empty', '')) + '\n')
                self.assertIsNone(server.chat_payload(config)['plan'])

    def test_feedback_checks_choice_and_waits_for_empty_prompt(self):
        cursor = [1]
        done, keys = [], []
        entry = {'id': 'fake', 'agent': 'claude'}
        def screen():
            if done:
                return '❯ \n────────────────'
            return SCREEN.replace('❯ 1.', '  1.').replace(f'  {cursor[0]}.', f'❯ {cursor[0]}.')
        def run(argv, **kwargs):
            if 'capture-pane' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=screen())
            if 'send-keys' in argv:
                keys.append(argv[-1])
                if argv[-1] == 'Down':
                    cursor[0] += 1
                if argv[-1] == 'Enter':
                    done.append(True)
            return subprocess.CompletedProcess(argv, 0, stdout='')
        identifier = terminal.approval(screen())['id']
        with patch.object(terminal, 'verify'), patch.object(terminal, 'submit') as submit:
            for choice, feedback, error in ((1, 'Change it', ValueError), (3, '\x1b', ValueError), (3, 42, ValueError)):
                with self.assertRaises(error):
                    terminal.respond_approval(entry, identifier, choice=choice, feedback=feedback, run=run)
            with self.assertRaises(RuntimeError):
                terminal.respond_approval(entry, 'stale', choice=3, feedback='Change it', run=run)
            self.assertEqual(keys, [])
            terminal.respond_approval(entry, identifier, choice=3, feedback='Change it', run=run)
            self.assertEqual(keys, ['Down', 'Down', 'Enter'])
            submit.assert_called_once_with(entry, 'Change it', run=run)

    def test_feedback_does_not_overwrite_a_draft(self):
        identifier = terminal.approval(SCREEN)['id']
        with patch.object(terminal, 'verify'), patch.object(terminal, 'capture', return_value=SCREEN), patch.object(terminal, 'empty_prompt', side_effect=terminal.DraftError('draft')), patch.object(terminal, 'submit') as submit:
            # A draft after rejection blocks feedback; the rejection itself is explicit.
            selected = terminal.approval(SCREEN.replace('❯ 1.', '  1.').replace('  3.', '❯ 3.'))
            with patch.object(terminal, 'approval', side_effect=[selected, selected, None]), patch.object(terminal, 'menu_question', return_value=None):
                with self.assertRaises(terminal.DraftError):
                    terminal.respond_approval({'id': 'fake', 'agent': 'claude'}, identifier, choice=3, feedback='Change', run=lambda *args, **kwargs: None)
            submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
