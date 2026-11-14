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


if __name__ == '__main__':
    unittest.main()
