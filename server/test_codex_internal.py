import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
import server


def record(text, kinds=None, provenance='harness'):
    return {'type': 'response_item', 'timestamp': '2026-11-06T00:00:00Z', 'payload': {
        'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}],
        'internal_chat_message_metadata_passthrough': {'content_item_kinds': kinds or ['goal.internal_context'],
            'content_item_metadata': [{'provenance': {'type': provenance}}]}}}


class InternalMessageTests(unittest.TestCase):
    def parse(self, row):
        state = server.codex_state()
        server.codex_consume(state, 1, row)
        return state['messages']

    def test_goal_context_is_compact_activity_not_user_input(self):
        text = '<codex_internal_context source="goal">\nContinue working.\n<objective>' + 'objective '*2000 + '</objective>\n</codex_internal_context>'
        messages = self.parse(record(text))
        self.assertEqual(messages[0]['role'], 'assistant')
        self.assertEqual(messages[0]['kind'], 'notice')
        self.assertEqual(messages[0]['title'], 'Goal continuation')
        self.assertLess(len(messages[0]['text']), 200)

    def test_user_goal_update_is_activity(self):
        messages = self.parse(record('<codex_internal_context source="user_goal">User set goal status: "active".</codex_internal_context>', ['user.goal']))
        self.assertEqual(messages[0]['title'], 'Goal updated')

    def test_real_user_literal_is_preserved(self):
        text = '<codex_internal_context source="goal">Please explain this tag.</codex_internal_context>'
        for kinds in (['user.text'], ['goal.internal_context']):
            messages = self.parse(record(text, kinds, 'user'))
            self.assertEqual(messages[0]['role'], 'user')
            self.assertEqual(messages[0]['text'], text)

    def test_mixed_content_keeps_user_words_and_classifies_goal(self):
        row = record('Please fix this.', ['user.text', 'goal.internal_context'], 'user')
        row['payload']['content'].append({'type': 'input_text', 'text': '<codex_internal_context source="goal">Goal text</codex_internal_context>'})
        row['payload']['internal_chat_message_metadata_passthrough']['content_item_metadata'].append({'provenance': {'type': 'harness'}})
        messages = self.parse(row)
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant'])
        self.assertEqual(messages[0]['text'], 'Please fix this.')

    def test_unknown_metadata_is_visible(self):
        messages = self.parse(record('Future event details', ['new.future.kind']))
        self.assertTrue(messages)
        self.assertIn('Future event details', messages[0]['text'])

    def test_image_only_input_does_not_disappear(self):
        row = record('')
        row['payload']['content'] = [{'type': 'input_image', 'image_url': 'data:image/png;base64,private'}]
        self.assertEqual(self.parse(row)[0]['text'], '[Image attachment]')

    def image_record(self, count=1, extra=None):
        content = []
        for number in range(1, count + 1):
            content.extend([
                {'type': 'input_text', 'text': f' <image name=[Image #{number}] path="/uploads/image{number}.png"> '},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,private'},
                {'type': 'input_text', 'text': ' </image> '},
            ])
        content.append({'type': 'input_text', 'text': ' '.join(f'[Image #{n}]' for n in range(1, count + 1))})
        if extra is not None:
            content.append({'type': 'input_text', 'text': extra})
        row = record('', ['user.text'] * len(content), 'user')
        row['payload']['content'] = content
        row['payload']['internal_chat_message_metadata_passthrough']['content_item_metadata'] *= len(content)
        return row

    def test_pasted_image_uses_bare_path(self):
        messages = self.parse(self.image_record())
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['role'], 'user')
        self.assertEqual(messages[0]['text'], '/uploads/image1.png')

    def test_pasted_image_keeps_typed_text(self):
        self.assertEqual(self.parse(self.image_record(extra='fix this'))[0]['text'], '/uploads/image1.png\nfix this')

    def test_two_pasted_images_use_bare_paths(self):
        self.assertEqual(self.parse(self.image_record(2))[0]['text'], '/uploads/image1.png\n/uploads/image2.png')

    def test_pasted_image_removes_only_matching_inline_placeholders(self):
        self.assertEqual(self.parse(self.image_record(extra='fix [Image #1]this [Image #9]'))[0]['text'],
                         '/uploads/image1.png\nfix this [Image #9]')

    def test_image_placeholders_without_tags_stay_visible(self):
        self.assertEqual(self.parse(record('[Image #1]', ['user.text'], 'user'))[0]['text'], '[Image #1]')

    def test_agent_message_is_peer_and_encrypted_content_is_never_displayed(self):
        row = {'type': 'response_item', 'payload': {'type': 'agent_message', 'author': {'role': 'worker'},
            'content': [{'type': 'input_text', 'text': 'Review passed.'}, {'type': 'encrypted_content', 'encrypted_content': 'secret'}]}}
        messages = self.parse(row)
        self.assertEqual(messages[0]['kind'], 'peer')
        self.assertEqual(messages[0]['text'], 'Review passed.')
        self.assertNotIn('secret', str(messages))

    def test_unknown_and_malformed_metadata_do_not_hide_input(self):
        for meta in (None, {}, {'content_item_kinds': ['goal.internal_context'], 'content_item_metadata': [None]},
                     {'content_item_kinds': ['goal.internal_context'], 'content_item_metadata': [{'provenance': 'bad'}]}):
            row = record('Visible input')
            row['payload']['internal_chat_message_metadata_passthrough'] = meta
            self.assertEqual(self.parse(row)[0]['text'], 'Visible input')

    def test_literal_unregistered_handoff_envelope_is_preserved(self):
        text = '<tether_handoff_context id="' + '0'*32 + '">\nLiteral example\n</tether_handoff_context>\n\nExplain the example.'
        self.assertEqual(self.parse(record(text, ['user.text'], 'user'))[0]['text'], text)

    def test_notification_origin_distinguishes_auto_goal_from_user_goal(self):
        import codex_messages
        self.assertTrue(codex_messages.background(record('Auto continuation')['payload']))
        self.assertFalse(codex_messages.background(record('User set a goal', ['user.goal'])['payload']))
        self.assertFalse(codex_messages.background(record('Real user', ['user.text'], 'user')['payload']))
