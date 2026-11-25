import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
import attach
import pane_view


class AttachTarget(unittest.TestCase):
    def test_session_and_pane_ids_pass(self):
        self.assertEqual(attach.target(['$3']), '$3')
        self.assertEqual(attach.target(['%20']), '%20')
        self.assertEqual(attach.target(['w%20']), 'w%20')
        self.assertEqual(attach.target(['z%20']), 'z%20')

    def test_anything_else_is_refused(self):
        for bad in ([], ['$3', '%1'], ['research'], ['%1;kill-server'], ['-t'], ['%'], ['w$3'], ['w%'], ['z$3'], ['zw%1']):
            with self.assertRaises(ValueError):
                attach.target(bad)


class PaneView(unittest.TestCase):
    def test_typed_bytes_go_to_the_pane_as_hex_keys(self):
        self.assertEqual(pane_view.key_commands('%7', b'h\xc3\xa9\x1b[A\r'), [b'send-keys -t %7 -H 68 c3 a9 1b 5b 41 0d\n'])
        self.assertEqual(len(pane_view.key_commands('%7', b'x' * 600)), 3)

    def test_a_tall_pane_shows_its_bottom_rows_and_the_cursor(self):
        lines = ['row %d' % n for n in range(6)]
        drawn = pane_view.frame((2, 5, 1, 30, 6), lines, rows=4)
        self.assertIn('tether-pane 30x6', drawn)
        self.assertNotIn('row 1', drawn)
        self.assertIn('\x1b[1;1H\x1b[0mrow 2', drawn)
        self.assertIn('\x1b[4;3H\x1b[?25h', drawn)

    def test_a_hidden_cursor_stays_hidden(self):
        self.assertNotIn('?25h', pane_view.frame((0, 0, 0, 10, 2), ['a', 'b'], rows=5))


if __name__ == '__main__':
    unittest.main()

    def test_the_wheel_scrolls_and_mouse_reports_never_reach_the_pane(self):
        self.assertEqual(pane_view.split_mouse(b'\x1b[<64;5;3M\x1b[<64;5;3Mx'), (b'x', 6, b''))
        self.assertEqual(pane_view.split_mouse(b'\x1b[<65;1;1M\x1b[<0;2;2M\x1b[<0;2;2m'), (b'', -3, b''))
        self.assertEqual(pane_view.split_mouse(b'\x1b[<80;1;1M'), (b'', 3, b''))  # Ctrl+wheel is still the wheel
        self.assertEqual(pane_view.split_mouse(b'a\x1b[<64;1'), (b'a', 0, b'\x1b[<64;1'))  # finished on the next read
        self.assertEqual(pane_view.split_mouse(b'\x1b[A'), (b'\x1b[A', 0, b''))

    def test_scrolled_back_shows_a_tag_and_hides_the_cursor(self):
        drawn = pane_view.frame((0, 1, 1, 20, 2), ['a', 'b'], rows=2, back=6, history=40)
        self.assertIn('[6/40]', drawn)
        self.assertNotIn('?25h', drawn)
