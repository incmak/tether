import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pane_icons


class CleanTests(unittest.TestCase):
    def test_keeps_shapes_and_drops_everything_that_can_run_or_load(self):
        svg = pane_icons.clean('Here:\n<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)" viewBox="0 0 24 24">'
                               '<script>alert(2)</script><path d="M4 4h16" onclick="x()" stroke="currentColor"/>'
                               '<circle cx="12" cy="12" r="3" fill="url(#g)"/><image href="http://x/y.png"/>'
                               '<foreignObject><div>hi</div></foreignObject><text>hi</text></svg> done')
        self.assertIn('<path d="M4 4h16" stroke="black"', svg)
        self.assertIn('<circle cx="12" cy="12" r="3"', svg)
        for bad in ('script', 'onload', 'onclick', 'url', 'image', 'href', 'foreignObject', 'text', 'alert'):
            self.assertNotIn(bad, svg)

    def test_rejects_entities_empty_and_oversized_svg(self):
        self.assertIsNone(pane_icons.clean('<!DOCTYPE svg [<!ENTITY a "x">]><svg><path d="M1 1"/></svg>'))
        self.assertIsNone(pane_icons.clean('<svg><script/></svg>'))
        self.assertIsNone(pane_icons.clean('<svg>' + '<path d="M1 1"/>' * 1000 + '</svg>'))
        self.assertIsNone(pane_icons.clean('no svg here'))


class DrawTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patcher = patch.object(pane_icons, 'folder', lambda: self.home / 'icons')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_draw_saves_a_clean_icon_and_read_serves_it(self):
        run = Mock(return_value=Mock(returncode=0, stdout='<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18"/></svg>'))
        name = pane_icons.draw('api server', 'a plug', run=run)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['claude', '-p'])
        self.assertIn('api server', command[2])
        self.assertIn('a plug', command[2])
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertIn('<rect', pane_icons.read(name))
        self.assertIsNone(pane_icons.read('../etc/passwd'))

    def test_draw_fails_when_claude_returns_no_svg(self):
        with self.assertRaises(RuntimeError):
            pane_icons.draw('x', run=Mock(return_value=Mock(returncode=0, stdout='Sorry, I cannot.')))
        with self.assertRaises(RuntimeError):
            pane_icons.draw('x', run=Mock(return_value=Mock(returncode=1, stdout='')))

    def test_agent_written_files_are_cleaned_when_read(self):
        (self.home / 'icons').mkdir()
        (self.home / 'icons' / 'mine.svg').write_text('<svg><path d="M2 2L22 22" onmouseover="x()"/><script>y()</script></svg>')
        svg = pane_icons.read('mine')
        self.assertIn('M2 2L22 22', svg)
        self.assertNotIn('onmouseover', svg)
        self.assertNotIn('script', svg)


if __name__ == '__main__':
    unittest.main()
