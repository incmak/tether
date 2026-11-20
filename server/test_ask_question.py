"""A question taller than the pane: the card gets the whole question from the transcript, not only the screen's bottom rows."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents
import history
import server

ASK = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'q1', 'name': 'AskUserQuestion', 'input': {
    'questions': [{'question': 'How should Tether size the pane?', 'header': 'Pane size', 'multiSelect': False,
                   'options': [{'label': 'Zoom button', 'description': 'Off by default.'}, {'label': 'Zoom automatically', 'description': 'Always.'}]}]}}]}}
BOTTOM = {'id': 'a', 'reason': 'prompt', 'text': '  4. Type something.\n  5. Chat about this\nEnter to select'}


class AskQuestionTests(unittest.TestCase):
    def setUp(self):
        history.forget()

    def test_open_question_fills_a_card_without_options_and_goes_when_answered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'chat.jsonl'
            path.write_text(json.dumps(ASK) + '\n')
            config = {'id': 'fake', 'thread': 'fake', 'agent': 'claude', 'transcript': str(path)}
            with patch.object(server, 'snapshot', side_effect=lambda config: agents.claude_snapshot(config, lambda text: text)), \
                    patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages), patch.object(server, 'live_status') as status:
                status.return_value = {'state': 'Input needed', 'approval': None, 'attention': dict(BOTTOM)}
                payload = server.chat_payload(config)
                self.assertNotIn('ask', payload)
                ask = payload['attention']['ask']['questions'][0]
                self.assertEqual((ask['header'], [option['label'] for option in ask['options']]), ('Pane size', ['Zoom button', 'Zoom automatically']))
                # A menu the screen parsed in full keeps its own option buttons.
                status.return_value = {'state': 'Input needed', 'approval': None, 'attention': dict(BOTTOM, reason='menu', options=[{'id': 1, 'text': 'x'}, {'id': 2, 'text': 'y'}])}
                self.assertNotIn('ask', server.chat_payload(config)['attention'])
                with path.open('a') as stream:
                    stream.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'q1', 'content': 'ok'}]}}) + '\n')
                status.return_value = {'state': 'Input needed', 'approval': None, 'attention': dict(BOTTOM)}
                self.assertNotIn('ask', server.chat_payload(config)['attention'])


if __name__ == '__main__':
    unittest.main()
