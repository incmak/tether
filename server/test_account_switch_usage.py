"""Account switches invalidate usage from the previous account."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import agents
import claude_probe
import live_usage
import paths
import saved_accounts
import server
from own_time import own_time


class AccountSwitchUsage(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.metrics = self.root / 'metrics'
        self.metrics.mkdir()
        self.rates = self.root / 'rates.json'
        for target, name, value in (
            (paths, 'CACHE', self.root / 'cache'),
            (agents, 'METRICS', self.metrics),
            (server, 'RATES_FILE', self.rates),
            (saved_accounts, '_snapshot', None),
            (saved_accounts, 'binary', lambda: '/fake/ccswap'),
            (claude_probe, '_usage', {'at': 90, 'report': {'old': True}, 'error': 'old'}),
            (live_usage, '_cache', {'at': 90, 'report': {'old': True}, 'error': 'old'}),
        ):
            self.enterContext(patch.object(target, name, value))
        self.enterContext(patch.object(server, 'recent_rates', return_value=([], None)))
        self.enterContext(patch.object(server.accounts, 'summary', return_value={}))

    def switch(self, agent='claude', code=0):
        def run(command, **kwargs):
            listing = command[-2:] == ['list', '--json']
            data = {agent: {'accounts': [{'number': 1, 'active': True, 'usage': {
                'fiveHour': {'pct': 0, 'resetsAt': '2030-01-01T00:00:00Z'},
                'sevenDay': {'pct': 0}}}]}}
            return subprocess.CompletedProcess(command, 0 if listing else code, json.dumps(data), 'failed')
        clock = Mock(return_value=100)
        with own_time(saved_accounts, time=clock), own_time(agents, time=clock), own_time(claude_probe, time=clock):
            return saved_accounts.switch(agent, 1, run=run)

    def metric(self, name, reported, percent):
        path = self.metrics / name
        path.write_text(json.dumps({'reported_at': reported, 'rate_limits': {
            'seven_day': {'used_percentage': percent, 'resets_at': 300}}}))
        os.utime(path, (reported, reported))

    def test_switch_ignores_old_claude_report_and_uses_new_report(self):
        self.metric('old.json', 90, 93)
        self.switch()
        self.assertEqual(server.usage_summary(now=110)['claude']['windows'][0]['percent'], 0)
        self.metric('new.json', 101, 12)
        os.utime(self.metrics / 'old.json', (200, 200))
        report = server.usage_summary(now=110)['claude']
        self.assertEqual(report['reported_at'], 101)
        self.assertEqual(report['windows'][0]['percent'], 12)

    def test_switch_uses_active_account_windows_as_fallback(self):
        self.metric('old.json', 90, 93)
        self.switch()
        saved_accounts._snapshot['claude'].insert(0, {
            'active': False, 'windows': [{'label': '7d', 'percent': 93, 'resets_at': None}]})
        self.assertEqual(server.usage_summary(now=110)['claude'], {
            'windows': [{'label': '5h', 'percent': 0.0, 'resets_at': 1893456000.0},
                        {'label': '7d', 'percent': 0.0, 'resets_at': None}],
            'reported_at': 100, 'source': 'account'})
        self.assertEqual(saved_accounts.snapshot()['claude'][1]['windows'][0]['resets_at'], '2030-01-01T00:00:00Z')

    def test_switch_resets_claude_cache_and_records_private_timestamp(self):
        self.switch()
        self.assertEqual(claude_probe._usage, {'at': 0, 'report': None, 'error': None})
        self.assertEqual(saved_accounts.switched_at('claude'), 100)
        self.assertEqual((paths.CACHE / 'account_switch.json').stat().st_mode & 0o777, 0o600)

    def test_switch_resets_codex_cache_and_preserves_claude_timestamp(self):
        self.switch()
        self.switch('codex')
        self.assertEqual(live_usage._cache, {})
        self.assertEqual(json.loads((paths.CACHE / 'account_switch.json').read_text()), {'claude': 100, 'codex': 100})

    def test_no_switch_file_keeps_old_reports(self):
        self.metric('old.json', 90, 93)
        self.rates.write_text(json.dumps({'windows': [{'label': '168h', 'percent': 80}], 'reported_at': None}))
        report = server.usage_summary(now=110)
        self.assertEqual(report['claude']['windows'][0]['percent'], 93)
        self.assertEqual(report['codex']['windows'][0]['percent'], 80)

    def test_codex_switch_drops_old_saved_and_recent_reports(self):
        self.switch('codex')
        windows = [{'label': '168h', 'percent': 80}]
        for reported in (None, '1970-01-01T00:01:30Z', 90):
            with self.subTest(reported=reported):
                self.rates.write_text(json.dumps({'windows': windows, 'reported_at': reported}))
                self.assertIsNone(server.usage_summary(now=110)['codex'])
                self.rates.unlink()
                with patch.object(server, 'recent_rates', return_value=(windows, reported)):
                    self.assertIsNone(server.usage_summary(now=110)['codex'])
        self.rates.write_text(json.dumps({'windows': windows, 'reported_at': 101}))
        self.assertEqual(server.usage_summary(now=110)['codex']['reported_at'], 101)

    def test_live_reports_before_switch_are_ignored(self):
        self.switch('codex')
        self.switch()
        for reported in (90, 101):
            with self.subTest(reported=reported):
                live = {'report': {'windows': [{'label': '7d', 'percent': 12}], 'reported_at': reported, 'source': 'live'}, 'error': 'check failed'}
                with patch.object(claude_probe, 'check_usage', return_value=live), patch.object(live_usage, 'check', return_value=live):
                    report = server.usage_summary(now=110, live_age=30)
                self.assertEqual(report['claude']['source'], 'account' if reported == 90 else 'live')
                self.assertEqual(report['codex'] is None, reported == 90)
                self.assertEqual(report['live_errors'], {'claude': 'check failed', 'codex': 'check failed'})

    def test_failed_switch_does_not_reset_cache_or_record_timestamp(self):
        with self.assertRaisesRegex(ValueError, 'failed'):
            self.switch(code=1)
        self.assertFalse((paths.CACHE / 'account_switch.json').exists())
        self.assertEqual(claude_probe._usage['at'], 90)

    def test_unreadable_switch_store_returns_zero(self):
        paths.CACHE.mkdir()
        store = paths.CACHE / 'account_switch.json'
        for content in ('invalid', '[]', '{"claude": null}'):
            with self.subTest(content=content):
                store.write_text(content)
                self.assertEqual(saved_accounts.switched_at('claude'), 0)


if __name__ == '__main__':
    unittest.main()
