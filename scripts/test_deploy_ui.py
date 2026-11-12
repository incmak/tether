"""Regression checks for preserving open-tab assets during an internal UI deploy."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deploy_ui', Path(__file__).with_name('deploy-ui.py'))
deploy_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_ui)


class DeployUiTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.build = Path(self.folder.name) / 'build'
        self.target = Path(self.folder.name) / 'dist'
        for root in (self.build, self.target):
            (root / 'assets').mkdir(parents=True)
        (self.build / 'assets/new.js').write_text('new')
        (self.build / 'index.html').write_text('<script src="/chat/assets/new.js"></script>')
        (self.target / 'assets/old.js').write_text('old')
        (self.target / 'index.html').write_text('old entry')

    def test_preserves_old_chunks_and_publishes_entry_last(self):
        replace = deploy_ui.os.replace
        installed = []
        def record(source, target):
            installed.append(Path(target).name)
            replace(source, target)
        with patch.object(deploy_ui.os, 'replace', side_effect=record):
            self.assertEqual(deploy_ui.deploy(self.build, self.target), 2)
        self.assertEqual(installed[-1], 'index.html')
        self.assertEqual((self.target / 'assets/old.js').read_text(), 'old')
        self.assertEqual((self.target / 'assets/new.js').read_text(), 'new')
        self.assertEqual((self.target / '.live-ui').read_bytes(), b'')

    def test_rejects_missing_assets_before_install(self):
        (self.build / 'assets/new.js').unlink()
        with self.assertRaises(ValueError):
            deploy_ui.deploy(self.build, self.target)
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')

    def test_failed_asset_install_keeps_old_entry(self):
        with patch.object(deploy_ui.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                deploy_ui.deploy(self.build, self.target)
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')


if __name__ == '__main__':
    unittest.main()
