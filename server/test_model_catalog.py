"""Catalog discovery uses fake executables, temporary caches and provider fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import codex_level
import delegate
import model_catalog as catalog
import paths


class Catalog(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        self.enterContext(patch.object(paths, 'CACHE', self.root / 'cache'))
        self.binary = self.root / 'claude'
        self.binary.write_bytes(b'claude-fable-5-2 claude-opus-5-5')
        self.which = self.enterContext(patch.object(catalog.shutil, 'which', return_value=str(self.binary)))
        self.clock = self.enterContext(patch.object(catalog.time, 'time', return_value=1_000_000))
        self.models = self.enterContext(patch.object(codex_level, 'models', return_value=[
            {'slug': 'gpt-new', 'name': 'GPT New', 'efforts': ['high', 'ultra'], 'fast': True}]))

    def test_scan_chunk_borders_dates_and_optional_minor(self):
        self.binary.write_bytes(b'x' * 60 + b' claude-fable-5-2 claude-opus-5 claude-haiku-4-5-20251001 '
                                b'claude-opus-4-20250514 claude-new-7-12 claude-sonnet-5-55 '
                                b'claude-new-7-20251114 claude-fable-5-1')
        with patch.object(catalog, 'CHUNK_SIZE', 64), patch.object(catalog, 'OVERLAP', 48):
            self.assertEqual(catalog.claude_ids(self.binary), {
                ('fable', (5, 2)), ('opus', (5, None)), ('new', (7, 12)),
                ('sonnet', (5, 55)), ('fable', (5, 1))})

    def test_newest_known_families_and_possible_new_top(self):
        self.binary.write_bytes(b'claude-fable-5-1 claude-fable-5-2 claude-opus-6 claude-sonnet-5-5 '
                                b'claude-haiku-4-5-20251001 claude-new-5-1 claude-old-4-9 '
                                b'claude-eval-9 claude-code-20250219 claude-desktop-3')
        result = catalog.claude()
        self.assertEqual(result['source'], 'claude-code')
        self.assertEqual(result['latest'], {'launch': 'fable', 'slug': 'claude-fable-5-2', 'name': 'Fable 5.2'})
        self.assertEqual([(row['slug'], row['name']) for row in result['models']], [
            ('claude-fable-5-2', 'Fable 5.2'), ('claude-opus-6', 'Opus 6'), ('claude-sonnet-5-5', 'Sonnet 5.5')])
        self.assertEqual(result['models'][0]['efforts'], list(delegate.EFFORTS))
        self.assertEqual(result['new_families'], ['new'])
        self.assertIsNone(result['changed'])

    def test_missing_unreadable_and_no_known_ids_use_builtin(self):
        expected = [(slug, name) for slug, name in delegate.CLAUDE_MODELS if slug.startswith('claude-')]
        for binary in (None, str(self.root / 'missing')):
            self.which.return_value = binary
            result = catalog.claude()
            self.assertEqual(result['source'], 'built-in')
            self.assertEqual(result['latest']['launch'], 'fable')
            self.assertEqual([(row['slug'], row['name']) for row in result['models']], expected)
        self.which.return_value = str(self.binary)
        with patch.object(catalog, 'claude_ids', side_effect=PermissionError):
            self.assertEqual(catalog.claude()['source'], 'built-in')
        self.binary.write_bytes(b'claude-future-9-1')
        self.assertEqual(catalog.claude()['source'], 'built-in')

    def test_top_is_first_present_family(self):
        self.binary.write_bytes(b'claude-opus-6 claude-haiku-7-2')
        self.assertEqual(catalog.claude()['latest'], {'launch': 'opus', 'slug': 'claude-opus-6', 'name': 'Opus 6'})

    def test_cache_uses_realpath_size_and_mtime_and_private_atomic_file(self):
        link = self.root / 'link'
        link.symlink_to(self.binary)
        self.which.return_value = str(link)
        with patch.object(catalog, 'claude_ids', wraps=catalog.claude_ids) as scan:
            result = catalog.claude()
            self.assertEqual(catalog.claude(), result)
            self.which.return_value = str(self.binary)
            catalog.claude()
            self.assertEqual(scan.call_count, 1)
            stat = self.binary.stat()
            os.utime(self.binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            catalog.claude()
            self.assertEqual(scan.call_count, 2)
            self.binary.write_bytes(b'claude-fable-6')
            catalog.claude()
            self.assertEqual(scan.call_count, 3)
        cache = paths.CACHE / 'model-catalog.json'
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(cache.read_text())['scan']['key'][0], str(self.binary.resolve()))
        self.assertEqual(list(paths.CACHE.iterdir()), [cache])

    def test_release_notices_are_per_provider_persist_and_expire(self):
        first = catalog.catalog()
        self.assertIsNone(first['claude']['changed'])
        self.assertIsNone(first['codex']['changed'])
        self.binary.write_bytes(b'claude-fable-6-1')
        self.models.return_value = [{'slug': 'gpt-newer', 'name': 'GPT Newer', 'efforts': ['ultra']}]
        result = catalog.catalog()
        self.assertEqual(result['claude']['changed'], {'from': 'Fable 5.2', 'to': 'Fable 6.1', 'at': 1_000_000})
        self.assertEqual(result['codex']['changed'], {'from': 'GPT New', 'to': 'GPT Newer', 'at': 1_000_000})
        self.clock.return_value += catalog.NOTICE_SECONDS - 1
        self.assertEqual(catalog.catalog(), result)
        self.clock.return_value += 1
        self.assertIsNone(catalog.claude()['changed'])
        self.assertIsNone(catalog.codex()['changed'])
        cache = json.loads((paths.CACHE / 'model-catalog.json').read_text())
        self.assertIsNone(cache['providers']['codex']['changed'])

    def test_codex_uses_provider_order_and_all_efforts(self):
        result = catalog.codex()
        self.assertEqual(result, {'latest': {'launch': 'gpt-new', 'slug': 'gpt-new', 'name': 'GPT New'},
                                 'models': [{'slug': 'gpt-new', 'name': 'GPT New', 'efforts': ['high', 'ultra']}],
                                 'new_families': [], 'source': 'codex', 'changed': None})
        self.models.return_value = []
        self.assertIsNone(catalog.codex()['latest'])
        self.assertEqual(catalog.codex()['models'], [])
        self.models.return_value = [{'slug': 'gpt-new', 'name': 'GPT New', 'efforts': []}]
        self.assertIsNone(catalog.codex()['changed'])  # Missing cache is not a release.

    def test_unwritable_cache_does_not_break_discovery(self):
        with patch.object(catalog.tempfile, 'mkstemp', side_effect=PermissionError):
            self.assertEqual(catalog.claude()['latest']['name'], 'Fable 5.2')
            self.assertEqual(catalog.codex()['latest']['name'], 'GPT New')

    def registry_binary(self):
        self.binary.write_bytes(
            b'x' * 63 + b'{id:"claude-mythos-5-2",family:"mythos",display_name:"Mythos 5.2",'
            b'provider_ids:{nested:{x:1}},advisor_rank:10}'
            b'{id:"claude-fable-5-1",family:"fable",display_name:"Old Fable",advisor_rank:99}'
            b'{id:"claude-fable-5-2",family:"fable",display_name:"Fable 5.2",advisor_rank:10}'
            b'{id:"claude-opus-5-5",family:"opus",display_name:"Opus 5.5",advisor_rank:7}'
            b'{id:"claude-future-6-1",family:"future",display_name:"Future 6.1",advisor_rank:11}'
            b'{id:"claude-3-5-haiku",family:"haiku",display_name:"Old Haiku",advisor_rank:1}')

    def test_registry_chunk_border_nested_rank_old_ids_and_order(self):
        self.registry_binary()
        with patch.object(catalog, 'CHUNK_SIZE', 64):
            rows = catalog.registry(self.binary)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], dict(id='claude-mythos-5-2', family='mythos', name='Mythos 5.2',
                                       rank=10, version=(5, 2)))
        with patch.object(catalog, '_start_probes'):
            result = catalog.claude()
        self.assertEqual([row['family'] for row in result['models']], ['future', 'mythos', 'fable', 'opus'])
        self.assertEqual(result['new_families'], ['future'])
        self.assertEqual(result['latest']['launch'], 'fable')
        self.assertEqual([r['available'] for r in result['models']], [None, None, True, True])
        self.assertEqual([r['launch'] for r in result['models']],
                         ['claude-future-6-1', 'claude-mythos-5-2', 'fable', 'opus'])
        self.assertEqual(result['models'][0]['access'], 'checking')

    def test_registry_lookahead_cutoff_missing_rank_and_denied_families(self):
        self.binary.write_bytes(
            b'x' * 4090 + b'{id:"claude-fable-5-2",family:"fable",display_name:"Provider name",'
            + b'x' * 3800 + b',provider_ids:{nested:{x:1}},advisor_rank:10}'
            b'{id:"claude-haiku-5-5",family:"haiku",display_name:"Haiku 5.5"}'
            b'{id:"claude-code-999",family:"code",display_name:"Not a model",advisor_rank:99}')
        with patch.object(catalog, 'CHUNK_SIZE', 4096):
            entries = catalog.registry(self.binary)
        self.assertEqual(entries[0]['rank'], 10)
        self.assertIsNone(entries[1]['rank'])
        with patch.object(catalog, '_start_probes'):
            result = catalog.claude()
        self.assertEqual([row['name'] for row in result['models']], ['Provider name', 'Haiku 5.5'])
        self.assertTrue(all(row['available'] is True for row in result['models']))
        self.binary.write_bytes(b'{id:"claude-opus-6",family:"opus",display_name:"Opus 6"}')
        self.assertEqual(catalog.claude()['latest']['launch'], 'opus')

    def test_old_scan_cache_is_refreshed_and_unchanged_cache_is_not_written(self):
        first = catalog.claude()
        cache = catalog._read()
        for row in cache['scan']['result']['models']:
            row.pop('family')
            row.pop('rank')
        catalog._write(cache)
        self.assertEqual(catalog.claude(), first)
        with patch.object(catalog, '_write') as write:
            catalog.claude()
        write.assert_not_called()

    def test_probe_outcomes_and_safe_command(self):
        import subprocess
        from types import SimpleNamespace
        for done, expected in (
                (SimpleNamespace(returncode=0, stdout='OK', stderr=''), 'available'),
                (SimpleNamespace(returncode=1, stdout='It may not exist or you may not have access to it.', stderr=''), 'unavailable'),
                (SimpleNamespace(returncode=1, stdout='', stderr='Not logged in'), 'error'),
                (subprocess.TimeoutExpired('claude', 90), 'error'), (OSError('bad binary'), 'error')):
            options = {'side_effect': done} if isinstance(done, Exception) else {'return_value': done}
            with patch.object(catalog, '_run', **options) as run:
                catalog._probe_all(self.binary, ['key'], ['claude-mythos-5-2'])
            state = catalog._read()['probes']['claude-mythos-5-2']
            self.assertEqual(state, dict(state=expected, at=1_000_000, key=['key']))
            self.assertEqual(run.call_args.args[0], [str(self.binary), '-p', '--setting-sources', 'local',
                             '--no-session-persistence', '--max-turns', '1', '--model', 'claude-mythos-5-2',
                             'Reply with OK only.'])
            self.assertEqual(run.call_args.kwargs, dict(cwd=paths.CACHE / 'model-probe', stdin=subprocess.DEVNULL,
                                                      capture_output=True, text=True, timeout=90))
        self.assertEqual(list((paths.CACHE / 'model-probe').iterdir()), [])
        self.assertEqual((paths.CACHE / 'model-probe').stat().st_mode & 0o777, 0o700)

    def test_probe_staleness(self):
        self.assertTrue(catalog._stale({}, ['key']))
        for state, days in (('available', 7), ('unavailable', 1), ('error', 1)):
            value = dict(state=state, key=['key'], at=1_000_000)
            self.assertFalse(catalog._stale(value, ['key'], 1_000_000 + days * 86400 - 1))
            self.assertTrue(catalog._stale(value, ['key'], 1_000_000 + days * 86400))
            self.assertTrue(catalog._stale(value, ['other']))

    def test_latest_follows_only_available_ranked_models_and_recheck(self):
        self.registry_binary()
        with patch.object(catalog, '_start_probes') as start:
            result = catalog.claude()
            key = catalog._read()['scan']['key']
            self.assertEqual(result['latest']['launch'], 'fable')
            for state, available, access, launch in (('unavailable', False, 'no-access', 'fable'),
                                                    ('error', None, 'error', 'fable'),
                                                    ('available', True, 'ok', 'claude-mythos-5-2')):
                cache = catalog._read()
                cache['probes'] = {'claude-mythos-5-2': dict(state=state, key=key, at=1_000_000)}
                catalog._write(cache)
                result = catalog.claude()
                self.assertEqual(result['latest']['launch'], launch)
                mythos = result['models'][1]
                self.assertIs(mythos['available'], available)
                self.assertEqual(mythos['access'], access)
            cache['probes']['claude-future-6-1'] = dict(state='available', key=key, at=1_000_000)
            catalog._write(cache)
            self.assertEqual(catalog.claude()['latest']['launch'], 'claude-future-6-1')
            result = catalog.recheck()
            self.assertNotIn('probes', catalog._read())
            self.assertEqual(result['claude']['latest']['launch'], 'fable')
            self.assertTrue(start.called)

    def test_only_one_daemon_worker_and_recheck_discards_inflight_results(self):
        import threading
        entered, finish = threading.Event(), threading.Event()
        self.registry_binary()
        self.enterContext(patch.object(catalog, '_PENDING', []))
        self.enterContext(patch.object(catalog, '_ACTIVE', None))
        self.enterContext(patch.object(catalog, '_PROBING', False))
        def probe(binary, slug):
            entered.set()
            self.assertTrue(finish.wait(5))
            return 'available'
        with patch.object(catalog, '_probe', side_effect=probe) as run:
            catalog.claude()
            self.assertTrue(entered.wait(2))
            for _ in range(5):
                catalog.claude()
            self.assertEqual(catalog._PENDING, [])
            with catalog._LOCK:
                catalog._GENERATION += 1
            finish.set()
            # Wait on the actual thread with a bound; never leave it behind after this test.
            threads = [t for t in threading.enumerate() if t is not threading.current_thread()
                       and t.name.endswith('(_probe_worker)')]
            for thread in threads:
                self.assertTrue(thread.daemon)
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(run.call_count, 2)
        self.assertFalse(catalog._PROBING)
        self.assertNotIn('probes', catalog._read())


if __name__ == '__main__':
    unittest.main()
