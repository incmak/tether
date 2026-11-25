import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import footer
import server


def settings(tier, second, model='gpt-6.2-sol', effort='max'):
    return {'timestamp': f'2026-11-18T12:00:{second:02d}Z', 'type': 'event_msg', 'payload': {
        'type': 'thread_settings_applied', 'thread_settings': {'model': model, 'reasoning_effort': effort, **tier}}}


class ServiceTierTests(unittest.TestCase):
    def test_tier_state_and_notices(self):
        state = server.codex_state()
        self.assertEqual(state['tier'], '')
        for index, (service_tier, tier, notices) in enumerate([
            ('default', '', []),
            ('priority', 'fast', ['Fast mode on.']),
            ('default', '', ['Fast mode on.', 'Fast mode off.']),
            ('ultrafast', 'ultrafast', ['Fast mode on.', 'Fast mode off.', 'Ultrafast mode on.']),
        ]):
            with self.subTest(service_tier=service_tier, index=index):
                server.codex_consume(state, index, settings({'service_tier': service_tier}, index * 3))
                self.assertEqual(state['tier'], tier)
                self.assertEqual([message['text'] for message in state['messages']], notices)

    def test_first_settings_are_silent_even_after_turn_context(self):
        state = server.codex_state()
        server.codex_consume(state, 0, {'type': 'turn_context', 'payload': {'model': 'gpt-6.2-sol', 'effort': 'max'}})
        server.codex_consume(state, 1, settings({'service_tier': 'fast'}, 1))
        self.assertEqual(state['tier'], 'fast')
        self.assertEqual(state['messages'], [])

    def test_absent_and_unknown_tiers_clear_the_badge(self):
        for value in ({}, {'service_tier': None}, {'service_tier': 'unknown'}):
            with self.subTest(value=value):
                state = server.codex_state()
                server.codex_consume(state, 0, settings({'service_tier': 'priority'}, 0))
                server.codex_consume(state, 1, settings(value, 3))
                self.assertEqual(state['tier'], '')
                self.assertEqual([message['text'] for message in state['messages']], ['Fast mode off.'])

    def test_adjacent_records_merge_by_setting_and_keep_distinct_notice_ids(self):
        state = server.codex_state()
        rows = [settings({'service_tier': 'default'}, 0),
                settings({'service_tier': 'priority'}, 5, model='gpt-5'),
                settings({'service_tier': 'ultrafast'}, 5, model='gpt-5', effort='high'),
                settings({'service_tier': 'ultrafast'}, 5, model='gpt-5', effort='high')]
        for index, row in enumerate(rows):
            server.codex_consume(state, index, row)
        self.assertEqual(state['tier'], 'ultrafast')
        self.assertEqual([message['text'] for message in state['messages']], ['Model changed to gpt-5 high.', 'Ultrafast mode on.'])
        self.assertEqual(len({message['id'] for message in state['messages']}), 2)


class StatusLineTests(unittest.TestCase):
    fixtures = Path(__file__).parent / 'fixtures' / 'codex_0.170.0_status'

    def test_captured_status_lines(self):
        for name, tier in [('fast', 'fast'), ('ultrafast', 'ultrafast'), ('none', None)]:
            with self.subTest(name=name):
                result = footer.codex_screen((self.fixtures / f'{name}.txt').read_text())
                self.assertEqual((result['model'], result['effort'], result.get('tier')), ('GPT-6.2-Sol', 'max', tier))

    def test_tier_without_effort(self):
        result = footer.codex_screen('gpt-5 ultrafast · ~/x · 100% context left')
        self.assertEqual((result['model'], result.get('effort'), result['tier']), ('gpt-5', None, 'ultrafast'))

    def test_rollout_precedes_screen_and_footer_passes_through(self):
        screen = (self.fixtures / 'fast.txt').read_text()
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'RATES_FILE', Path(directory) / 'rates.json'), \
                patch.object(server, 'recent_rates', return_value=([], None)), patch.object(server, 'capture', return_value=screen):
            for rollout, expected in [('ultrafast', 'ultrafast'), ('', 'fast')]:
                with self.subTest(rollout=rollout):
                    report = server.codex_metrics({}, None, tier=rollout)
                    self.assertEqual(report['tier'], expected)
                    self.assertEqual(footer.codex(report)['tier'], expected)
