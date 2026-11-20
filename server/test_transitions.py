import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
import server


class TransitionTests(unittest.TestCase):
    def setUp(self):
        server.TRANSITIONS.clear()
        self.addCleanup(server.TRANSITIONS.clear)

    def test_finished_set_only_when_a_turn_ends(self):
        server.track('%1', 'Idle', 100)
        self.assertEqual(server.timeline('%1'), {'since': 100, 'finished': None})
        server.track('%1', 'Working', 110)
        server.track('%1', 'Working', 115)
        self.assertEqual(server.timeline('%1'), {'since': 110, 'finished': None})
        server.track('%1', 'Approval', 120)
        server.track('%1', 'Idle', 130)
        self.assertEqual(server.timeline('%1'), {'since': 130, 'finished': 130})

    def test_a_question_is_part_of_the_turn(self):
        server.track('%1', 'Working', 100)
        server.track('%1', 'Input needed', 110)
        self.assertEqual(server.timeline('%1'), {'since': 110, 'finished': None})
        server.track('%1', 'Working', 120)
        server.track('%1', 'Idle', 130)
        self.assertEqual(server.timeline('%1'), {'since': 130, 'finished': 130})

    def test_unknown_pane(self):
        self.assertEqual(server.timeline('%9'), {'since': None, 'finished': None})


if __name__ == '__main__':
    unittest.main()
