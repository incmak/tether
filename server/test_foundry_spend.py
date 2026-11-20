"""Foundry spend: token totals from Codex logs, Retail Prices parsing, and the Azure bill (fake network)."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

import foundry_spend

NOW = dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc)


def log(path, provider, totals, last_input=1000, models=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{'type': 'session_meta', 'payload': {'model_provider': provider}}]
    for index, (stamp, (inp, cached, write, out)) in enumerate(totals):
        if models and models[index] is not None:
            lines.append({'type': 'turn_context', 'payload': {'model': models[index]}})
        usage = {'input_tokens': inp, 'cached_input_tokens': cached, 'cache_write_input_tokens': write, 'output_tokens': out}
        lines.append({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'total_token_usage': usage, 'last_token_usage': {'input_tokens': last_input}}}})
    path.write_text('\n'.join(json.dumps(line) for line in lines) + '\n')


PRICES = {'Items': [{'skuName': f'6.1-sol {tier} {meter} Std Gl', 'unitOfMeasure': '1M', 'retailPrice': price}
                    for tier, base in (('ShortCo', 1), ('LongCo', 2))
                    for meter, price in (('Inp', 2.0 * base), ('Cd Inp', 0.1 * base), ('Cd Wr', 2.5 * base), ('Opt', 10.0 * base))]
          + [{'skuName': '6.1-sol ShortCo Inp Std DZ', 'unitOfMeasure': '1M', 'retailPrice': 99.0}]}

TABLE = {'short': {'input': 2.0, 'cached': 0.1, 'cache_write': 2.5, 'output': 10.0},
         'long': {'input': 4.0, 'cached': 0.2, 'cache_write': 5.0, 'output': 20.0}}
ASTRA_TABLE = {tier: {kind: rate * 5 for kind, rate in rates.items()} for tier, rates in TABLE.items()}


class Spend(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        foundry_spend._files.clear()
        foundry_spend._bill.clear()
        patcher = mock.patch.multiple(foundry_spend, PRICES_FILE=self.root / 'prices.json', BILL_CONFIG=self.root / 'azure-cost.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_counts_only_foundry_growth_this_month(self):
        log(self.root / '2026/11/02/a.jsonl', 'azure', [('2026-11-01T10:00:00Z', (1000, 0, 900, 10)), ('2026-11-01T10:00:01Z', (1000, 0, 900, 10)),
                                                       ('2026-11-02T09:00:00Z', (3000, 800, 900, 30))])
        log(self.root / '2026/11/02/b.jsonl', 'openai', [('2026-11-02T09:00:00Z', (5000, 0, 0, 50))])
        out = foundry_spend.usage(NOW, self.root)
        self.assertEqual(out['month']['short'], {'input': 1300, 'cached': 800, 'cache_write': 900, 'output': 30, 'requests': 2})
        self.assertEqual(out['today']['short']['requests'], 1)
        self.assertEqual(out['sessions'], 1)
        self.assertEqual(out['by_model'][None], {'month': out['month'], 'today': out['today']})

    def test_long_requests_and_last_month_file(self):
        old = self.root / '2026/10/31/c.jsonl'
        log(old, 'azure', [('2026-10-31T23:00:00Z', (100, 0, 0, 1)), ('2026-11-01T01:00:00Z', (400_100, 0, 0, 11))], last_input=400_000)
        os.utime(old, (NOW.timestamp(), NOW.timestamp()))
        out = foundry_spend.usage(NOW, self.root)
        self.assertEqual(out['month']['long'], {'input': 400_000, 'cached': 0, 'cache_write': 0, 'output': 10, 'requests': 1})

    def test_prices_use_global_standard_and_cost_adds_up(self):
        table = foundry_spend.prices('gpt-6.1-sol', fetch=lambda url: PRICES, now=0)
        self.assertEqual(table['short'], {'input': 2.0, 'cached': 0.1, 'cache_write': 2.5, 'output': 10.0})
        tokens = {'short': {'input': 1e6, 'cached': 1e6, 'cache_write': 0, 'output': 1e6}, 'long': {'input': 0, 'cached': 0, 'cache_write': 0, 'output': 1e6}}
        self.assertAlmostEqual(foundry_spend.cost(tokens, table), 2.0 + 0.1 + 10.0 + 20.0)
        # Cached for a day: a failing fetch still returns the saved table.
        self.assertEqual(foundry_spend.prices('gpt-6.1-sol', fetch=mock.Mock(side_effect=OSError), now=10), table)

    def test_models_follow_turn_context_and_report_uses_each_price(self):
        path = self.root / '2026/11/02/models.jsonl'
        log(path, 'azure', [('2026-11-01T09:00:00Z', (1_000_000, 0, 0, 0)),
                            ('2026-11-02T09:00:00Z', (3_000_000, 0, 0, 0)),
                            ('2026-11-02T10:00:00Z', (6_000_000, 0, 0, 0))],
            models=['gpt-6.1-sol', 'gpt-6-astra', None])
        self.assertEqual([event[-1] for event in foundry_spend._events(path)],
                         ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-astra'])
        tokens = foundry_spend.usage(NOW, self.root)
        self.assertEqual(tokens['month']['short']['input'], 6_000_000)
        self.assertEqual(tokens['today']['short']['input'], 5_000_000)
        self.assertEqual(tokens['by_model']['gpt-6-astra']['month']['short']['input'], 5_000_000)
        self.assertEqual(tokens['by_model']['gpt-6.1-sol']['today']['short']['input'], 0)
        with mock.patch.object(foundry_spend, 'usage', return_value=tokens), \
                mock.patch.object(foundry_spend, 'prices', side_effect={'gpt-6.1-sol': TABLE, 'gpt-6-astra': ASTRA_TABLE}.get), \
                mock.patch.object(foundry_spend, 'bill', return_value={'state': 'not-set-up'}):
            estimate = foundry_spend.report('gpt-6.1-sol')['estimate']
        self.assertEqual(estimate['usd'], 52.0)
        self.assertEqual(estimate['today_usd'], 50.0)
        self.assertEqual(estimate['prices'], TABLE)
        self.assertEqual(estimate['tokens'], tokens['month'])
        self.assertEqual(estimate['today_tokens'], tokens['today'])
        self.assertEqual(estimate['sessions'], 1)
        self.assertEqual(estimate['models'], [
            {'model': 'gpt-6-astra', 'usd': 50.0, 'tokens': tokens['by_model']['gpt-6-astra']['month']},
            {'model': 'gpt-6.1-sol', 'usd': 2.0, 'tokens': tokens['by_model']['gpt-6.1-sol']['month']}])

    def test_events_without_context_use_selected_model(self):
        path = self.root / '2026/11/02/fallback.jsonl'
        log(path, 'azure', [('2026-11-02T09:00:00Z', (1_000_000, 0, 0, 0)),
                            ('2026-11-02T10:00:00Z', (2_000_000, 0, 0, 0))],
            models=[None, 'gpt-6.1-sol'])
        self.assertIsNone(foundry_spend._events(path)[0][-1])
        tokens = foundry_spend.usage(NOW, self.root)
        with mock.patch.object(foundry_spend, 'usage', return_value=tokens), \
                mock.patch.object(foundry_spend, 'prices', return_value=TABLE) as prices, \
                mock.patch.object(foundry_spend, 'bill', return_value={'state': 'not-set-up'}):
            estimate = foundry_spend.report('gpt-6.1-sol')['estimate']
        self.assertEqual(estimate['usd'], 4.0)
        self.assertEqual(estimate['today_usd'], 4.0)
        self.assertEqual(estimate['models'], [{'model': 'gpt-6.1-sol', 'usd': 4.0, 'tokens': tokens['month']}])
        prices.assert_called_once_with('gpt-6.1-sol')

    def test_unknown_price_for_used_model_makes_totals_unknown(self):
        log(self.root / '2026/11/02/unknown.jsonl', 'azure',
            [('2026-11-01T09:00:00Z', (1_000_000, 0, 0, 0)),
             ('2026-11-02T09:00:00Z', (2_000_000, 0, 0, 0))],
            models=['unknown', 'gpt-6.1-sol'])
        with mock.patch.object(foundry_spend, 'usage', return_value=foundry_spend.usage(NOW, self.root)), \
                mock.patch.object(foundry_spend, 'prices', side_effect=lambda model: TABLE if model == 'gpt-6.1-sol' else None), \
                mock.patch.object(foundry_spend, 'bill', return_value={'state': 'not-set-up'}):
            estimate = foundry_spend.report('gpt-6.1-sol')['estimate']
        self.assertIsNone(estimate['usd'])
        self.assertIsNone(estimate['today_usd'])
        self.assertEqual([(row['model'], row['usd']) for row in estimate['models']],
                         [('gpt-6.1-sol', 2.0), ('unknown', None)])

    def test_prices_cache_keeps_each_model_and_stale_fallback(self):
        astra = {'Items': [{**item, 'skuName': item['skuName'].replace('6.1-sol', '6-astra'),
                            'retailPrice': item['retailPrice'] * 5} for item in PRICES['Items']]}
        self.assertEqual(foundry_spend.prices('gpt-6.1-sol', fetch=lambda url: PRICES, now=1000), TABLE)
        self.assertEqual(foundry_spend.prices('gpt-6-astra', fetch=lambda url: astra, now=1000), ASTRA_TABLE)
        saved = json.loads(foundry_spend.PRICES_FILE.read_text())
        self.assertEqual(set(saved['models']), {'gpt-6.1-sol', 'gpt-6-astra'})
        fetch = mock.Mock(side_effect=OSError)
        for model, table in (('gpt-6.1-sol', TABLE), ('gpt-6-astra', ASTRA_TABLE)):
            self.assertEqual(foundry_spend.prices(model, fetch=fetch, now=1100), table)
        fetch.assert_not_called()
        for model, table in (('gpt-6.1-sol', TABLE), ('gpt-6-astra', ASTRA_TABLE)):
            self.assertEqual(foundry_spend.prices(model, fetch=fetch, now=100_000), table)
        self.assertEqual(fetch.call_count, 2)
        self.assertIsNone(foundry_spend.prices('unknown', fetch=fetch, now=100_000))
        self.assertEqual(json.loads(foundry_spend.PRICES_FILE.read_text()), saved)

    def test_old_price_cache_migrates_without_using_wrong_model(self):
        foundry_spend.PRICES_FILE.write_text(json.dumps({'model': '6.1-sol', 'at': 1000, 'prices': TABLE}))
        fetch = mock.Mock(side_effect=OSError)
        self.assertEqual(foundry_spend.prices('gpt-6.1-sol', fetch=fetch, now=100_000), TABLE)
        self.assertIsNone(foundry_spend.prices('gpt-6-astra', fetch=fetch, now=100_000))
        astra = {'Items': [{**item, 'skuName': item['skuName'].replace('6.1-sol', '6-astra'),
                            'retailPrice': item['retailPrice'] * 5} for item in PRICES['Items']]}
        self.assertEqual(foundry_spend.prices('gpt-6-astra', fetch=lambda url: astra, now=100_000), ASTRA_TABLE)
        saved = json.loads(foundry_spend.PRICES_FILE.read_text())
        self.assertEqual(saved['models']['gpt-6.1-sol']['prices'], TABLE)
        self.assertEqual(saved['models']['gpt-6-astra']['prices'], ASTRA_TABLE)

    def test_bill_states(self):
        self.assertEqual(foundry_spend.bill()['state'], 'not-set-up')
        config = foundry_spend.BILL_CONFIG
        config.write_text(json.dumps({'tenant_id': 't', 'client_id': 'c', 'client_secret': 's', 'scope': '/subscriptions/x/resourceGroups/rg'}))
        os.chmod(config, 0o644)
        self.assertIn('chmod 600', foundry_spend.bill()['hint'])
        os.chmod(config, 0o600)
        calls = []

        def fetch(url, data=None, headers=None):
            calls.append(url)
            if 'oauth2' in url:
                return {'access_token': 'tok'}
            self.assertEqual(headers['Authorization'], 'Bearer tok')
            self.assertIn('/subscriptions/x/resourceGroups/rg/providers/Microsoft.CostManagement/query', url)
            self.assertEqual(json.loads(data)['dataset']['filter'], {
                'dimensions': {'name': 'ServiceName', 'operator': 'In', 'values': ['Foundry Models']}})
            return {'properties': {'columns': [{'name': 'Cost'}, {'name': 'Currency'}], 'rows': [[12.345, 'USD']]}}
        self.assertEqual(foundry_spend.bill(fetch, now=100)['usd'], 12.35)
        foundry_spend.bill(fetch, now=200)
        self.assertEqual(len(calls), 2, 'second call within the hour uses the cache')

    def az_config(self, **changes):
        foundry_spend.BILL_CONFIG.write_text(json.dumps({'auth': 'az', 'scope': 'subscriptions/x', **changes}))
        foundry_spend.BILL_CONFIG.chmod(0o600)

    @mock.patch.object(foundry_spend.shutil, 'which', return_value='/mock/az')
    def test_bill_az(self, which):
        self.az_config()
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"accessToken": "t"}'))

        def fetch(url, data=None, headers=None):
            self.assertIn('/subscriptions/x/providers/Microsoft.CostManagement/query', url)
            self.assertEqual(headers['Authorization'], 'Bearer t')
            self.assertEqual(json.loads(data)['dataset']['filter'], {
                'dimensions': {'name': 'ServiceName', 'operator': 'In', 'values': ['Foundry Models']}})
            return {'properties': {'columns': [{'name': 'Cost'}, {'name': 'Currency'}], 'rows': [[12.345, 'USD']]}}

        fetch = mock.Mock(side_effect=fetch)
        answer = foundry_spend.bill(fetch, now=100, run=run)
        self.assertEqual(answer, {'state': 'ok', 'usd': 12.35, 'currency': 'USD', 'checked': 100})
        which.assert_called_once_with('az')
        run.assert_called_once_with(
            ['/mock/az', 'account', 'get-access-token', '--resource', 'https://management.azure.com/', '--output', 'json'],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(foundry_spend.bill(fetch, now=200, run=run), answer)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(fetch.call_count, 1)
        foundry_spend.bill(fetch, now=3700, run=run)
        self.assertEqual(run.call_count, 2)

    @mock.patch.object(foundry_spend.shutil, 'which', return_value=None)
    def test_bill_az_binary_fallback_and_missing(self, which):
        self.az_config()
        run, fetch = mock.Mock(), mock.Mock()
        with mock.patch.object(foundry_spend.paths, 'HOME', self.root):
            self.assertEqual(foundry_spend.bill(fetch, now=100, run=run), {
                'state': 'error', 'hint': 'Install the Azure CLI on this server, or use a service principal.'})
            self.assertEqual(foundry_spend.bill(fetch, now=200, run=run)['state'], 'error')
            self.assertEqual(which.call_count, 1)  # the error is cached
            run.assert_not_called()
            az = self.root / '.local/bin/az'
            az.parent.mkdir(parents=True)
            az.touch()
            run.return_value = subprocess.CompletedProcess([], 1, '', 'private stderr')
            self.assertIn('sign-in has expired', foundry_spend.bill(fetch, now=401, run=run)['hint'])
            self.assertEqual(run.call_args.args[0][0], str(az))
        fetch.assert_not_called()

    @mock.patch.object(foundry_spend.shutil, 'which', return_value='/mock/az')
    def test_bill_az_token_failures(self, which):
        self.az_config()
        fetch = mock.Mock()
        failures = [subprocess.CompletedProcess([], 1, '', 'private stderr'),
                    subprocess.CompletedProcess([], 0, '{}'),
                    subprocess.CompletedProcess([], 0, 'invalid JSON'),
                    subprocess.TimeoutExpired('az', 60), OSError('private detail')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                run = mock.Mock(side_effect=failure) if isinstance(failure, Exception) else mock.Mock(return_value=failure)
                self.assertEqual(foundry_spend.bill(fetch, run=run), {
                    'state': 'error', 'hint': 'The Azure CLI sign-in has expired. Run az login on this server.'})
        fetch.assert_not_called()

    def test_bill_az_config_validation(self):
        run, fetch = mock.Mock(), mock.Mock()
        for scope in (None, '', 123):
            self.az_config(scope=scope)
            self.assertEqual(foundry_spend.bill(fetch, run=run), {
                'state': 'error', 'hint': 'azure-cost.json needs scope.'})
        self.az_config()
        foundry_spend.BILL_CONFIG.write_text('{"auth": "az"}')
        self.assertIn('needs scope.', foundry_spend.bill(fetch, run=run)['hint'])
        self.az_config()
        foundry_spend.BILL_CONFIG.chmod(0o644)
        self.assertIn('chmod 600', foundry_spend.bill(fetch, run=run)['hint'])
        foundry_spend.BILL_CONFIG.chmod(0o600)
        foundry_spend.BILL_CONFIG.write_text('{"scope": "subscriptions/x"}')
        self.assertEqual(foundry_spend.bill(fetch, run=run)['hint'],
                         'azure-cost.json needs tenant_id, client_id, client_secret and scope.')
        run.assert_not_called()
        fetch.assert_not_called()

    @mock.patch.object(foundry_spend.shutil, 'which', return_value='/mock/az')
    def test_bill_cache_refreshes_on_auth_or_scope_change(self, which):
        sp = {'tenant_id': 't', 'client_id': 'c', 'client_secret': 's', 'scope': 'subscriptions/x'}
        result = {'properties': {'columns': [{'name': 'Cost'}], 'rows': [[1]]}}
        fetch = mock.Mock(side_effect=lambda url, **kwargs: {'access_token': 'sp'} if 'oauth2' in url else result)
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"accessToken": "t"}'))
        self.az_config()
        foundry_spend.BILL_CONFIG.write_text(json.dumps(sp))
        self.assertEqual(foundry_spend.bill(fetch, now=100, run=run)['checked'], 100)
        self.az_config()
        self.assertEqual(foundry_spend.bill(fetch, now=200, run=run)['checked'], 200)
        self.az_config(scope='subscriptions/y')
        self.assertEqual(foundry_spend.bill(fetch, now=300, run=run)['checked'], 300)
        foundry_spend.BILL_CONFIG.write_text(json.dumps(sp))
        self.assertEqual(foundry_spend.bill(fetch, now=400, run=run)['checked'], 400)
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(run.call_count, 2)

    @mock.patch.object(foundry_spend.shutil, 'which', return_value='/mock/az')
    def test_bill_http_error_hint_matches_auth(self, which):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"accessToken": "t"}'))
        failure = urllib.error.HTTPError('https://example.invalid', 403, 'private detail', {}, None)
        fetch = mock.Mock(side_effect=failure)
        self.az_config()
        self.assertEqual(foundry_spend.bill(fetch, now=100, run=run)['hint'],
                         'Azure answered 403. Check the signed-in account can read costs on the scope.')
        foundry_spend.BILL_CONFIG.write_text(json.dumps({
            'tenant_id': 't', 'client_id': 'c', 'client_secret': 's', 'scope': 'subscriptions/x'}))
        self.assertEqual(foundry_spend.bill(fetch, now=200, run=run)['hint'],
                         'Azure answered 403. Check the service principal has Cost Management Reader on the scope.')


if __name__ == '__main__':
    unittest.main()
