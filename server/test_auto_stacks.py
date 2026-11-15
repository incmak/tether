"""Auto stack names (rules 4 and 5) and the empty-stack timer. The stack rules against a real tmux are in test_groups.py."""
import unittest
from unittest import mock

import auto_stacks
import delegate


class StackNameTests(unittest.TestCase):  # rule 4
    def test_title_case_keeps_acronyms_and_small_words(self):
        self.assertEqual(auto_stacks.title_case('header swipe'), 'Header Swipe')
        self.assertEqual(auto_stacks.title_case('fix the BriskBot UI'), 'Fix the BriskBot UI')
        self.assertEqual(auto_stacks.title_case('of mice'), 'Of Mice')

    def test_goal_drops_a_ticket_and_refuses_a_bare_one(self):
        self.assertEqual(auto_stacks.goal('B45 advisor log'), 'Advisor Log')
        self.assertEqual(auto_stacks.goal('B45: advisor log'), 'Advisor Log')
        self.assertIsNone(auto_stacks.goal('B45'))
        self.assertEqual(auto_stacks.goal('Advisor: Session brief'), 'Session Brief')
        self.assertEqual(auto_stacks.goal('Header swipe · review'), 'Header Swipe')
        self.assertIsNone(auto_stacks.goal('Automation tether . Can we have…'))
        self.assertIsNone(auto_stacks.goal('codex'))
        self.assertIsNone(auto_stacks.goal('B76 review'), 'a ticket and a role word is no goal')

    def test_source_order(self):
        root = {'agent_name': 'Billing', 'pane_name': 'Docs'}
        self.assertEqual(auto_stacks.stack_name(root, 'Release', 'Task x'), 'Billing', '/rename comes first')
        root = {'pane_name': 'Docs', 'named_by_user': True}
        self.assertEqual(auto_stacks.stack_name(root, 'Release', 'Task x'), 'Docs', 'a rename in Tether comes first')
        root = {'pane_name': 'Docs'}
        self.assertEqual(auto_stacks.stack_name(root, 'Release', 'Task x'), 'Release', 'then the --stack name')
        self.assertEqual(auto_stacks.stack_name(root, None, 'Task x'), 'Docs', 'then the goal from the tab name')
        root = {'pane_name': 'lets add search sort and other…'}
        self.assertEqual(auto_stacks.stack_name(root, None, 'Search sort'), 'Search Sort', 'then the first task')
        self.assertEqual(auto_stacks.stack_name({}, None, None), 'Work')

    def test_never_a_branch_or_folder(self):
        root = {'pane_name': 'tether-wt', 'folder': '/srv/apps/tether-wt', 'worktree': 'feature/search-sort'}
        self.assertEqual(auto_stacks.stack_name(root, None, None), 'Work')
        self.assertEqual(auto_stacks.stack_name({'pane_name': 'b25-dm', 'cwd': '/x/wt/b25-dm'}, None, None), 'Work')
        root = {'pane_name': 'search-sort', 'worktree': 'search-sort'}
        self.assertEqual(auto_stacks.stack_name(root, None, 'Fix nav'), 'Fix Nav')
        self.assertEqual(auto_stacks.stack_name({'pane_name': 'X', 'folder': '/a/tether'}, 'Tether', None), 'Tether',
                         'the agent chose --stack Tether: that is not a guess from the folder')


class TabNameTests(unittest.TestCase):  # rule 5
    def name(self, **kwargs):
        return auto_stacks.helper_tab_name(**kwargs)

    def test_worker_is_its_task_in_title_case(self):
        self.assertEqual(self.name(name='header swipe', message='x'), 'Header Swipe')
        self.assertEqual(self.name(branch='feature/desktop-nav', message='Can you fix this, please…'), 'Desktop Nav')
        self.assertEqual(self.name(message='Fix login bug\nDetails…'), 'Fix Login Bug')
        self.assertEqual(self.name(message='Read and build the spec in /x/spec.md.', stack='Search'), 'Search')
        self.assertEqual(self.name(message='Read and build the spec in /x/spec.md.', stack_goal='Billing'), 'Billing')
        self.assertEqual(self.name(name='A long name the agent chose for it', message='x'),
                         'A long name the agent chose for it')
        self.assertEqual(self.name(name='codex', message='Please do it all…'), 'Task', 'never a bare agent name')

    def test_reviewer_and_advisor(self):
        caller = {'pane_name': 'B45 advisor log'}
        self.assertEqual(self.name(review=True, caller=caller), 'B45 Advisor Log · review')
        self.assertEqual(self.name(role='advisor', caller=caller), 'B45 Advisor Log · advisor')
        self.assertEqual(self.name(role='second-advisor', caller=caller), 'B45 Advisor Log · advisor 2')
        self.assertEqual(self.name(role='advisor', caller={'pane_name': 'can you check how…'}, stack_goal='Billing'),
                         'Billing · advisor')
        self.assertEqual(self.name(review=True, caller={'pane_name': 'Header swipe · review'}), 'Header Swipe · review')
        long = self.name(role='second-advisor', caller={'pane_name': 'Abcdefghij klmnopqrst uvwxyzabcd'})
        self.assertLessEqual(len(long), 40)
        self.assertEqual(long, 'Abcdefghij Klmnopqrst · advisor 2')

    def test_delegate_uses_the_given_name(self):
        run = mock.Mock()
        run.return_value = mock.Mock(stdout='%9\n', returncode=0)
        with mock.patch.object(delegate, 'new_window_after', return_value='%9'), \
                mock.patch.object(delegate, 'write_prompt', return_value='/tmp/p.md'):
            pane, name = delegate.start({'id': '%1', 'agent': 'claude', 'transcript': '/t'},
                                        {'agent': 'claude', 'message': 'do it'}, '/tmp', run=run, name='Header Swipe')
        self.assertEqual((pane, name), ('%9', 'Header Swipe'))
        self.assertIn('Header Swipe', run.call_args_list[0].args[0])


class RootTests(unittest.TestCase):  # rule 1, any depth
    def test_a_long_chain_reaches_the_main_session(self):
        records = {f'%{n}': {'owner_pane': f'%{n - 1}', 'sent_at': 1.0} for n in range(2, 20)}
        with mock.patch.object(auto_stacks.helper_tabs, 'load', lambda pane, run: (True, records.get(pane))):
            self.assertEqual(auto_stacks.root_of('%19', lambda pane: None, None), '%1')
        records['%1'] = {'owner_pane': '%19', 'sent_at': 1.0}  # a loop stops
        with mock.patch.object(auto_stacks.helper_tabs, 'load', lambda pane, run: (True, records.get(pane))):
            self.assertEqual(auto_stacks.root_of('%19', lambda pane: None, None), '%1')


class WatchTests(unittest.TestCase):  # rule 6, the timer
    def test_sweeps_unless_auto_resume_restores(self):
        calls, slept = [], []

        def sleep(_):
            slept.append(1)
            if len(slept) == 3:
                raise SystemExit
        states = iter([True, False, False])
        with mock.patch.object(auto_stacks.groups, 'drop_empty_auto', side_effect=lambda run: calls.append(1)):
            with self.assertRaises(SystemExit):
                auto_stacks.watch(run=None, sleep=sleep, restoring=lambda: next(states))
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
