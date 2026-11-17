"""Codex 0.169 writes thread_settings_applied at once on /model; turn_context follows only with the next turn."""
import unittest
import server


def settings(model, effort, ordinal, stamp='2026-11-17T18:01:43.678Z'):
    return {'timestamp': stamp, 'ordinal': ordinal, 'type': 'event_msg', 'payload': {
        'type': 'thread_settings_applied', 'thread_id': 't', 'thread_settings': {
            'model': model, 'model_provider_id': 'openai', 'reasoning_effort': effort, 'cwd': '/srv/user'}}}


def turn(model, effort):
    return {'type': 'turn_context', 'payload': {'turn_id': 'a', 'cwd': '/srv/user', 'model': model, 'effort': effort}}


class ModelChangeTests(unittest.TestCase):
    def run_records(self, rows):
        state = server.codex_state()
        for index, row in enumerate(rows):
            server.codex_consume(state, index, row)
        return state

    def test_model_change_without_a_new_turn_updates_the_label(self):
        state = self.run_records([settings('gpt-6.1-sol', 'medium', 1), turn('gpt-6.1-sol', 'medium'),
                                  settings('gpt-6.2-sol', 'high', 19), settings('gpt-6.2-sol', 'high', 20)])
        self.assertEqual((state['model'], state['effort']), ('gpt-6.2-sol', 'high'))
        notices = [m['text'] for m in state['messages'] if m.get('kind') == 'notice']
        self.assertEqual(notices, ['Model changed to gpt-6.2-sol high.'])

    def test_model_then_effort_records_show_one_notice(self):
        state = self.run_records([turn('gpt-6.1-sol', 'medium'), settings('gpt-6.2-sol', 'medium', 19), settings('gpt-6.2-sol', 'high', 20)])
        self.assertEqual([m['text'] for m in state['messages']], ['Model changed to gpt-6.2-sol high.'])

    def test_effort_only_change_is_shown(self):
        state = self.run_records([turn('gpt-6.2-sol', 'high'), settings('gpt-6.2-sol', 'low', 2)])
        self.assertEqual(state['effort'], 'low')
        self.assertEqual([m['text'] for m in state['messages']], ['Model changed to gpt-6.2-sol low.'])

    def test_settings_on_every_turn_add_no_notice(self):
        rows = []
        for _ in range(3):
            rows += [settings('gpt-6.2-sol', 'high', 1), turn('gpt-6.2-sol', 'high')]
        state = self.run_records(rows)
        self.assertEqual(state['messages'], [])

    def test_malformed_settings_are_ignored(self):
        state = self.run_records([turn('m', 'low'), {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_settings': 'x'}},
                                  {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_settings': {'model': 5}}}])
        self.assertEqual((state['model'], state['effort'], state['messages']), ('m', 'low', []))

    def test_two_choices_keep_two_notices(self):
        state = self.run_records([turn('gpt-6.1-sol', 'medium'), settings('gpt-6.2-sol', 'high', 5), {'type': 'event_msg', 'payload': {'type': 'token_count'}},
                                  settings('gpt-6-luna', 'low', 9)])
        self.assertEqual([m['text'] for m in state['messages']], ['Model changed to gpt-6.2-sol high.', 'Model changed to gpt-6-luna low.'])

    def test_adjacent_choices_minutes_apart_keep_two_notices(self):
        state = self.run_records([turn('gpt-6.1-sol', 'medium'), settings('gpt-6.2-sol', 'high', 5, '2026-11-17T18:01:43.678Z'),
                                  settings('gpt-6-luna', 'low', 6, '2026-11-17T18:03:44.100Z')])
        self.assertEqual([m['text'] for m in state['messages']], ['Model changed to gpt-6.2-sol high.', 'Model changed to gpt-6-luna low.'])

    def test_records_without_time_are_not_merged(self):
        state = self.run_records([turn('a', 'low'), settings('b', 'low', 1, None), settings('b', 'high', 2, None)])
        self.assertEqual([m['text'] for m in state['messages']], ['Model changed to b low.', 'Model changed to b high.'])


if __name__ == '__main__':
    unittest.main()
