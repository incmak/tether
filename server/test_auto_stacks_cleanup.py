"""The one-time cleanup plan (rule 8). plan() is pure: these tests give it a snapshot of one session."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auto_stacks_cleanup as cleanup

A, B, C, D = 'gaaaaaaaa', 'gbbbbbbbb', 'gcccccccc', 'gdddddddd'


def snap(groups, panes, roots, entries=None):
    return {'session': '$0', 'groups': [dict({'id': g, 'name': n, 'project': ''}, **extra) for g, n, extra in groups],
            'panes': [{'id': p, 'group': g, 'by_user': by, 'named_by_user': False} for p, g, by in panes],
            'roots': roots, 'entries': entries or {}}


class PlanTests(unittest.TestCase):
    def test_a_family_in_two_stacks_goes_to_one_and_the_empty_one_goes(self):
        s = snap([(A, 'B45 advisor log', {}), (B, 'B82 Codex trust', {})],
                 [('%1', '', False), ('%2', A, False), ('%3', B, False)],
                 {'%1': '%1', '%2': '%1', '%3': '%1'},
                 {'%1': {'id': '%1', 'pane_name': 'Vscode-claude_codex', 'cwd': '/x/proj'}})
        out = cleanup.plan(s, {A, B})
        self.assertEqual(out['moves'], [{'pane': '%1', 'from': '', 'to': A}, {'pane': '%3', 'from': B, 'to': A}])
        self.assertEqual((out['drops'], out['notes']), ([B], [{'from': B, 'to': A}]))
        self.assertEqual(out['renames'], [{'group': A, 'old': 'B45 advisor log', 'new': 'Advisor Log'}])
        self.assertEqual(out['mark_auto'], [A, B], 'proven stacks are marked, the dropped one too (drop_if_empty)')
        self.assertEqual(out['roots'], {A: '%1'})
        self.assertEqual(out['unproven'], [])

    def test_a_stack_without_proof_is_not_touched(self):
        s = snap([(A, 'tether bugs', {}), (B, 'helpers', {})],
                 [('%1', '', False), ('%2', A, False), ('%3', B, False)], {'%1': '%1', '%2': '%1', '%3': '%1'})
        out = cleanup.plan(s, {B})
        self.assertNotIn('%2', [m['pane'] for m in out['moves']])
        self.assertNotIn(A, [r['group'] for r in out['renames']] + out['drops'] + out['mark_auto'])
        self.assertEqual(out['unproven'], [{'group': A, 'name': 'tether bugs', 'by': 'unknown'}])

    def test_no_tab_joins_a_stack_without_proof(self):
        s = snap([(A, 'mine', {}), (B, 'helper', {})], [('%1', A, False), ('%2', B, False)], {'%1': '%1', '%2': '%1'})
        out = cleanup.plan(s, {B})
        self.assertEqual(out['moves'], [])
        self.assertEqual(out['kept'][0]['pane'], '%2')

    def test_user_stacks_and_user_placed_tabs_stay(self):
        s = snap([(A, 'Mine', {'by': 'user'}), (B, 'Auto', {'auto': True})],
                 [('%1', '', True), ('%2', B, False), ('%3', A, False)], {'%1': '%1', '%2': '%1', '%3': '%1'})
        out = cleanup.plan(s, {A})  # proof never overrides the user's mark
        self.assertEqual(out['moves'], [])
        self.assertEqual({k['pane'] for k in out['kept']}, {'%1', '%3'})
        self.assertEqual(out['unproven'], [{'group': A, 'name': 'Mine', 'by': 'user'}])

    def test_a_tab_whose_root_closed_stays_and_is_listed(self):
        s = snap([(A, 'search', {})], [('%2', A, False)], {'%2': '%9'})
        out = cleanup.plan(s, {A})
        self.assertEqual(out['orphans'], [{'pane': '%2', 'root': '%9', 'group': 'search'}])
        self.assertEqual(out['moves'], [])
        self.assertEqual(out['renames'], [{'group': A, 'old': 'search', 'new': 'Search'}], 'the name is tidied only')

    def test_two_families_in_one_stack_get_one_stack_each(self):
        s = snap([(A, 'shared', {})], [('%1', '', False), ('%2', A, False), ('%3', '', False), ('%4', A, False)],
                 {'%1': '%1', '%2': '%1', '%3': '%3', '%4': '%3'},
                 {'%3': {'id': '%3', 'agent_name': 'Billing'}})
        out = cleanup.plan(s, {A})
        self.assertEqual([m['key'] for m in out['made']], ['new:%3'])
        self.assertIn({'pane': '%4', 'from': A, 'to': 'new:%3'}, out['moves'])
        self.assertIn({'group': 'new:%3', 'old': None, 'new': 'Billing'}, out['renames'])

    def test_a_lone_main_session_keeps_its_stack_with_a_rule_4_name(self):
        s = snap([(A, 'B76 review', {})], [('%1', A, False)], {'%1': '%1'},
                 {'%1': {'id': '%1', 'pane_name': 'lets add search sort and other…'}})
        out = cleanup.plan(s, {A})
        self.assertEqual((out['moves'], out['drops']), ([], []))
        self.assertEqual(out['renames'], [{'group': A, 'old': 'B76 review', 'new': 'Work'}])

    def test_report_lists_every_unproven_stack(self):
        s = snap([(A, 'tether bugs', {}), (B, 'x', {})], [('%1', A, False)], {'%1': '%1'})
        text, after = cleanup.report(cleanup.plan(s, {B}), 2, {B: 'agent command'})
        self.assertIn('NOT PROVEN, not touched: tether bugs', text)
        self.assertEqual(after, 1)


class CopyNotesTests(unittest.TestCase):  # review finding 5
    def test_a_taken_prefixed_name_gets_a_number_and_a_copy_is_made_once(self):
        with tempfile.TemporaryDirectory() as temp:
            folders = {pid: Path(temp, pid) for pid in ('src', 'dst')}
            (folders['src'] / 'notes').mkdir(parents=True)
            (folders['dst'] / 'notes').mkdir(parents=True)
            (folders['src'] / 'notes' / 'a.md').write_text('new')
            (folders['dst'] / 'notes' / 'a.md').write_text('theirs')
            (folders['dst'] / 'notes' / 'old-a.md').write_text('earlier copy of another stack')
            with mock.patch.object(cleanup.projects, 'bundle', folders.get):
                cleanup._copy_notes('src', 'dst', 'old')
                cleanup._copy_notes('src', 'dst', 'old')
            notes = {p.name: p.read_text() for p in (folders['dst'] / 'notes').iterdir()}
        self.assertEqual(notes, {'a.md': 'theirs', 'old-a.md': 'earlier copy of another stack', 'old-2-a.md': 'new'})


if __name__ == '__main__':
    unittest.main()
