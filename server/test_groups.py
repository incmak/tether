"""Groups (groups.py, groups_api.py) and the places that carry @group. The store runs against a real tmux server in
its own socket folder (TMUX_TMPDIR), never the user's tmux. No test sends input to a terminal."""
import functools
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_tab_cli
import agent_tabs
import auto_stacks
import groups
import groups_api
import helper_close
import paths
import project_launch
import projects
import server
import terminal_input
import tmux_merge
from own_time import own_time

PID = 'a1b2c3d4e5f6'


class Handler:
    def __init__(self, path, body=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class Bundles(unittest.TestCase):
    """A private projects root for every test."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / 'home'
        self.home.mkdir()
        for name, value in (('TETHER', Path(temp.name) / 'tether'), ('CACHE', Path(temp.name) / 'cache')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)


class NameAndParseTests(unittest.TestCase):
    def test_name_rules(self):
        self.assertEqual(groups.clean_name('  Home dashboard  '), 'Home dashboard')
        self.assertEqual(groups.clean_name('x' * 40), 'x' * 40)
        for bad in ('', '   ', 'x' * 41, 'a\tb', 'a\x7fb', 'a‎b', None, 5):
            with self.assertRaises(groups.GroupError) as caught:
                groups.clean_name(bad)
            self.assertEqual(caught.exception.status, 400)

    def test_parse_drops_bad_entries_and_keeps_order(self):
        raw = json.dumps([{'id': 'gaaaaaaaa', 'name': 'B', 'project': ''}, {'id': 'bad', 'name': 'X'},
                          {'id': 'gbbbbbbbb', 'name': ' A ', 'project': PID}, {'id': 'gaaaaaaaa', 'name': 'Again'},
                          {'id': 'gcccccccc', 'name': ''}, {'id': 'gdddddddd', 'name': 'C', 'project': '../x'}])
        self.assertEqual(groups.parse(raw), [{'id': 'gaaaaaaaa', 'name': 'B', 'project': ''},
                                             {'id': 'gbbbbbbbb', 'name': 'A', 'project': PID},
                                             {'id': 'gdddddddd', 'name': 'C', 'project': ''}])
        self.assertEqual(groups.parse('not json'), [])
        # The maker: "auto" (Tether) or "by": "user". Neither: made before makers were recorded.
        makers = groups.parse(json.dumps([{'id': 'gaaaaaaaa', 'name': 'T', 'auto': True, 'by': 'user'},
                                          {'id': 'gbbbbbbbb', 'name': 'U', 'by': 'user'},
                                          {'id': 'gcccccccc', 'name': 'V', 'auto': 'yes', 'by': 'tether'}]))
        self.assertEqual([{k: g.get(k) for k in ('auto', 'by') if k in g} for g in makers],
                         [{'auto': True}, {'by': 'user'}, {}])
        self.assertEqual(groups.parse('{"id": "gaaaaaaaa"}'), [])
        self.assertEqual(groups.parse(''), [])

    def test_merged_numbers_clashing_names_and_renews_clashing_ids(self):
        target = [{'id': 'gaaaaaaaa', 'name': 'Docs', 'project': ''}, {'id': 'gbbbbbbbb', 'name': 'Docs 2', 'project': ''}]
        source = [{'id': 'gaaaaaaaa', 'name': 'docs', 'project': PID}, {'id': 'gcccccccc', 'name': 'API', 'project': ''},
                  {'id': 'gdddddddd', 'name': 'x' * 40, 'project': ''}]
        out, remap = groups.merged(target + [{'id': 'geeeeeeee', 'name': 'X' * 40, 'project': ''}], source)
        self.assertEqual([g['name'] for g in out], ['Docs', 'Docs 2', 'X' * 40, 'docs 3', 'API', 'x' * 38 + ' 2'])
        self.assertNotEqual(remap['gaaaaaaaa'], 'gaaaaaaaa')
        self.assertEqual(out[3], {'id': remap['gaaaaaaaa'], 'name': 'docs 3', 'project': PID})
        self.assertEqual(remap['gcccccccc'], 'gcccccccc')
        self.assertEqual(len({g['id'] for g in out}), len(out))


class AutoNameTests(unittest.TestCase):
    def test_short_names_pass_and_prompt_snippets_do_not(self):
        cases = {'Desktop back nav': 'Desktop back nav', 'desktop-nav': 'Desktop nav', 'feature/tab_icons': 'Tab icons',
                 'Advisor: Triage': 'Triage', 'Review: B81 fix': 'B81 fix', 'tether-bugs': 'Tether bugs',
                 'Automation tether . Can we have…': None, 'you know all my work and…': None,
                 'There are 2 issues. I will paste': None, 'can you check this?': None,
                 'Root - Edgar submission live integration': None, '': None, None: None, 'a\x07b': None,
                 'Release.': 'Release', 'x' * 33: None, 'Can you fix this': None, 'Fix this for you': None,
                 'please add search': None, 'Fix login bug': 'Fix login bug', 'Desktop back nav fix': None,
                 'feature/fix-tab-icon-order': None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(auto_stacks.short_name(text), expected)


class RenderTests(Bundles):
    GPID = 'b1b2b3b4b5b6'

    def test_group_part_comes_after_the_session_part_and_names_its_notes_folder(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        projects.write_note(PID, 'setup.md', '# Setup\nRun make.')
        projects.set_instructions(self.GPID, 'Only touch the dashboard.', 0)
        projects.write_note(self.GPID, 'chart.md', '# Chart\nUse the bar chart.')
        with mock.patch('backlog.summary', return_value=(['B1 Check the dashboard'], 1)):
            text = projects.render(PID, 'claude', self.home, group={'pid': self.GPID, 'name': 'Home dashboard'},
                                   backlog_project='tether')
        self.assertLessEqual(len(text), projects.BUDGET_MAX)
        self.assertTrue(text.startswith('# Tether project'))
        self.assertLess(text.index('Use pnpm.'), text.index('## Stack: Home dashboard'))
        self.assertLess(text.index('## Stack: Home dashboard'), text.index('Only touch the dashboard.'))
        self.assertIn(f'{projects.bundle(self.GPID)}/notes/', text)
        self.assertLess(text.index('Only touch the dashboard.'), text.index('## Tab icon'))
        self.assertLess(text.index('## Backlog'), text.index('### Open backlog items (1)'))
        self.assertLess(text.index('### Open backlog items (1)'), text.index('### Project notes'))
        self.assertLess(text.index('### Project notes\n- setup.md: Setup'), text.index('### Stack notes\n- chart.md: Chart'))
        self.assertLess(text.index('### Stack notes'), text.index('### setup.md'))
        self.assertIn('#### chart.md\n# Chart\nUse the bar chart.', text)

    def test_group_text_is_cut_to_the_session_budget_and_keeps_the_session_part(self):
        projects.set_budget(PID, projects.BUDGET_MIN, 0)
        projects.set_instructions(PID, 'Session rule.', 1)
        projects.set_instructions(self.GPID, 'Group rule. ' * 1500, 0)
        for number in range(8):
            projects.write_note(self.GPID, f'n{number}.md', f'# Note {number}\n' + 'word ' * 300)
        text = projects.render(PID, 'codex', self.home, group={'pid': self.GPID, 'name': 'Big'})
        fixed_chars = projects.state(PID, self.home)['fixed_chars']
        self.assertLessEqual(len(text) - fixed_chars, projects.BUDGET_MIN)
        self.assertIn('Session rule.', text)
        self.assertIn('## Stack: Big', text)
        self.assertNotIn('Group rule. ' * 1500, text)  # the stack guidance is cut, its identity is not
        self.assertIn('[Cut to fit', text)

    def test_no_group_is_unchanged(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        projects.write_note(PID, 'setup.md', '# Setup\nRun make.')
        text = projects.render(PID, 'claude', self.home)
        self.assertEqual(projects.render(PID, 'claude', self.home, group=None), text)
        self.assertNotIn('Stack', text.replace('--stack GOAL', ''))
        self.assertIn('\n## Notes\n', text)
        self.assertIn('\n### Project notes\n- setup.md: Setup\n', text)  # the list has its own heading: Notes comes first now
        self.assertIn("`--stack GOAL` (1–3 words; names the new tab. It opens in this tab's stack, or in none; `--stack Unstacked` = none)", text)

    def test_marks_cover_the_group_revision(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        projects.set_instructions(self.GPID, 'Group rule.', 0)
        raw = json.dumps([{'id': 'gaaaaaaaa', 'name': 'Docs', 'project': self.GPID}, {'id': 'gbbbbbbbb', 'name': 'New', 'project': ''}])
        lines = '\n'.join([f'$3\t{PID}\t%1\t{PID}:1+{self.GPID}:1\tgaaaaaaaa\t{raw}',
                           f'$3\t{PID}\t%2\t{PID}:1\tgaaaaaaaa\t{raw}',
                           f'$3\t{PID}\t%3\t{PID}:1+{self.GPID}:0\tgaaaaaaaa\t{raw}',
                           f'$3\t{PID}\t%4\t{PID}:1\tgbbbbbbbb\t{raw}',
                           f'$3\t{PID}\t%5\t{PID}:1\tgffffffff\t{raw}',
                           f'$3\t{PID}\t%6\t{PID}:1\t\t{raw}',
                           f'$3\t{PID}\t%7\t{PID}:1+{self.GPID}:1\t\t{raw}'])
        run = lambda args, **_: SimpleNamespace(returncode=0, stdout=lines, stderr='')
        self.assertEqual(projects.marks(run, self.home)['$3']['stale'], ['%2', '%3', '%7'])


class LaunchTests(Bundles):
    GPID = 'b1b2b3b4b5b6'

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {'TMUX': '/tmp/fake,1,0', 'TMUX_PANE': '%4', 'CODEX_HOME': str(self.home),
                                             'TETHER_CDP_URL': '', 'TETHER_LAUNCH': ''})
        patch.start()
        self.addCleanup(patch.stop)
        self.tmux = self.enterContext(mock.patch.object(project_launch, '_tmux'))
        self.group = {'pid': self.GPID, 'name': 'Docs'}

    def test_claude_and_codex_get_the_group(self):
        projects.set_instructions(self.GPID, 'Docs rule.', 0)
        args = project_launch.claude_args(PID, ['task'], self.group)
        self.assertEqual(args[0], '--settings')
        self.assertEqual(json.loads(args[1])['permissions']['additionalDirectories'],
                         [str(projects.bundle(PID)), str(projects.bundle(self.GPID))])
        self.tmux.assert_called_with('set-option', '-p', '-t', '%4', '@project_rev', f'{PID}:0+{self.GPID}:1')
        project_launch.claude_args(PID, ['task'])
        self.tmux.assert_called_with('set-option', '-p', '-t', '%4', '@project_rev', f'{PID}:0')
        for budget in (projects.BUDGET, projects.BUDGET_MIN):
            with self.subTest(budget=budget):
                projects.set_budget(PID, budget, projects.meta(PID)['revision'])
                args = project_launch.codex_args(PID, [], self.group)
                text = tomllib.loads(args[1])['developer_instructions']
                project, _, standing = text.rpartition('\n\n')
                self.assertTrue(standing.startswith('Autonomy level '))
                self.assertIn('## Stack: Docs\n', project)
                self.assertIn(f'Stack folder: {projects.bundle(self.GPID)}\n', project)
                self.assertIn('Docs rule.', project)
                self.assertLessEqual(len(project) - projects.state(PID, self.home)['fixed_chars'], budget)

    def test_current_group_reads_the_pane_and_makes_the_bundle(self):
        with mock.patch.object(groups, 'group_of_pane', return_value=('$1', {'id': 'gaaaaaaaa', 'name': 'Docs', 'project': ''})), \
                mock.patch.object(groups, 'context', return_value=self.GPID) as context:
            self.assertEqual(project_launch.current_group(), self.group)
            self.assertEqual(context.call_args.args[:2], ('$1', 'gaaaaaaaa'))
        with mock.patch.object(groups, 'group_of_pane', return_value=('$1', None)):
            self.assertIsNone(project_launch.current_group())
        with mock.patch.object(groups, 'group_of_pane', side_effect=groups.GroupError(404, groups.GONE_TAB)):
            self.assertIsNone(project_launch.current_group())

    def test_hook_reads_the_group_at_hook_time(self):
        for budget in (projects.BUDGET, projects.BUDGET_MIN):
            with self.subTest(budget=budget):
                projects.set_budget(PID, budget, projects.meta(PID)['revision'])
                output = io.StringIO()
                with mock.patch.object(project_launch, 'current_group', return_value=self.group), \
                        mock.patch('sys.stdin', io.StringIO('{}')), mock.patch('sys.stdout', output):
                    project_launch.hook(PID)
                text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                self.assertIn('## Stack: Docs\n', text)
                self.assertIn(f'Stack folder: {projects.bundle(self.GPID)}\n', text)
                self.assertLessEqual(len(text) - projects.state(PID, self.home)['fixed_chars'], budget)


@unittest.skipUnless(shutil.which(paths.TMUX) or os.path.exists(paths.TMUX), 'tmux is not installed')
class TmuxTests(Bundles):
    def setUp(self):
        super().setUp()
        self.socket_dir = tempfile.mkdtemp(prefix='tgr', dir='/tmp')  # short: long socket paths fail
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        env['TMUX_TMPDIR'] = self.socket_dir
        self.run_tmux = functools.partial(subprocess.run, env=env)
        scope = mock.patch.object(paths, 'SCOPE_TMUX', False)
        scope.start()
        self.addCleanup(scope.stop)
        self.a = self.new('alpha')
        self.tmux('new-window', '-t', '=alpha:')
        self.b = self.new('beta')
        self.a1, self.a2 = self.panes(self.a)
        self.b1, = self.panes(self.b)

    def tearDown(self):
        self.tmux('kill-server')
        shutil.rmtree(self.socket_dir, ignore_errors=True)

    def tmux(self, *args):
        return self.run_tmux([paths.TMUX, *args], capture_output=True, text=True, timeout=5)

    def new(self, name):
        self.tmux('new-session', '-d', '-s', name, '-x', '120', '-y', '40')
        return self.tmux('display-message', '-p', '-t', f'={name}:', '#{session_id}').stdout.strip()

    def panes(self, session):
        return self.tmux('list-panes', '-s', '-t', session + ':', '-F', '#{pane_id}').stdout.split()

    def label(self, pane):
        return self.tmux('display-message', '-p', '-t', pane, '#{@group}').stdout.strip()

    def status(self, work):
        with self.assertRaises(groups.GroupError) as caught:
            work()
        return caught.exception.status

    def test_create_keeps_order_and_empty_groups(self):
        run = self.run_tmux
        self.assertEqual(groups.read(self.a, run), [])
        first = groups.create(self.a, ' Docs ', run)
        second = groups.create(self.a, 'API', run, pane=self.a2)
        self.assertRegex(first['id'], r'^g[0-9a-f]{8}$')
        self.assertEqual([g['name'] for g in groups.read(self.a, run)], ['Docs', 'API'])
        self.assertEqual((self.label(self.a1), self.label(self.a2)), ('', second['id']))
        self.assertEqual(groups.all_groups(run), {self.a: groups.read(self.a, run)})
        stored = self.tmux('show-options', '-v', '-t', self.a, '@groups').stdout.strip()
        self.assertEqual(json.loads(stored)[0], {'id': first['id'], 'name': 'Docs', 'project': first['project']})
        self.assertTrue(projects.ID.fullmatch(first['project']), 'create makes the bundle id')

    def test_names_are_unique_and_main_is_reserved(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        api = groups.create(self.a, 'API', run)
        self.assertEqual(self.status(lambda: groups.create(self.a, 'DOCS', run)), 409)
        self.assertEqual(self.status(lambda: groups.create(self.a, 'main', run)), 400)
        self.assertEqual(self.status(lambda: groups.create(self.a, 'Unstacked', run)), 400)
        self.assertEqual(self.status(lambda: groups.rename(self.a, api['id'], 'docs', run)), 409)
        self.assertEqual(self.status(lambda: groups.rename(self.a, api['id'], ' Main ', run)), 400)
        self.assertEqual(groups.rename(self.a, docs['id'], 'docs', run)['name'], 'docs')  # its own name, new case
        groups.create(self.b, 'Docs', run)  # another session may use the name

    def test_arrange_reuses_names_makes_stacks_and_skips_closed_tabs(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        made = groups.arrange(self.a, [{'name': 'docs', 'panes': [self.a1, '%999']}, {'name': 'Release', 'panes': [self.a2, self.a1]},
                                       {'name': 'Gone', 'panes': ['%998']}], run)
        self.assertEqual([g['name'] for g in made], ['Docs', 'Release'])
        self.assertEqual(made[0]['id'], docs['id'])
        self.assertEqual([g['name'] for g in groups.read(self.a, run)], ['Docs', 'Release'])
        self.assertEqual((self.label(self.a1), self.label(self.a2)), (docs['id'], made[1]['id']))
        self.assertTrue(groups.placed_by_user(self.a1, run), 'auto stacks must leave arranged tabs alone')
        self.assertEqual(self.status(lambda: groups.arrange(self.a, [{'name': 'Main', 'panes': [self.a1]}], run)), 400)
        self.assertEqual(self.status(lambda: groups.arrange(self.a, [], run)), 400)
        self.assertEqual(self.status(lambda: groups.arrange(self.a, [{'name': 'X', 'panes': ['a1']}], run)), 400)
        self.assertEqual(self.status(lambda: groups.arrange('$999', [{'name': 'X', 'panes': [self.a1]}], run)), 404)

    def test_arrange_moves_helpers_with_owner_and_removes_emptied_stacks(self):
        run = self.run_tmux
        self.tmux('new-window', '-t', self.a + ':')
        a3 = [pane for pane in self.panes(self.a) if pane not in (self.a1, self.a2)][0]
        self.tmux('set-option', '-p', '-t', a3, '@tether_helper', json.dumps({'owner_pane': self.a1, 'sent_at': 1}))
        old = groups.create(self.a, 'Old', run, pane=self.a2, auto=True)
        keep = groups.create(self.a, 'Keep', run, by_user=True)  # empty before the apply: it stays
        made = groups.arrange(self.a, [{'name': 'Work', 'panes': [self.a1, self.a2]}, {'name': 'Other', 'panes': [a3]}], run)
        self.assertEqual([g['name'] for g in made], ['Work'])
        self.assertEqual(made[0].get('by'), 'user')
        self.assertEqual({self.label(pane) for pane in (self.a1, self.a2, a3)}, {made[0]['id']})
        names = [g['name'] for g in groups.read(self.a, run)]
        self.assertNotIn(old['name'], names)
        self.assertIn(keep['name'], names)
        # A helper whose owner is not in the plan stays where it is.
        groups.move(self.a, a3, '', run)
        groups.arrange(self.a, [{'name': 'Solo', 'panes': [a3, self.a2]}], run)
        self.assertEqual(self.label(a3), '')
        # unstack: the tab (and its helpers) go to no stack; the stack it emptied goes.
        self.tmux('set-option', '-p', '-t', a3, '@tether_helper', json.dumps({'owner_pane': self.a2, 'sent_at': 1}))
        self.tmux('set-option', '-p', '-t', a3, '@group', groups.read(self.a, run)[-1]['id'])
        self.assertEqual(groups.arrange(self.a, [], run, unstack=[self.a2]), [])
        self.assertEqual((self.label(self.a2), self.label(a3)), ('', ''))
        self.assertNotIn('Solo', [g['name'] for g in groups.read(self.a, run)])
        self.assertTrue(groups.placed_by_user(self.a2, run))
        self.assertEqual(self.status(lambda: groups.arrange(self.a, [], run, unstack=['a1'])), 400)

    def test_arrange_keeps_an_emptied_stack_while_a_helper_start_is_in_flight(self):
        run = self.run_tmux
        old = groups.create(self.a, 'Old', run, pane=self.a2, auto=True)
        token = groups.start_begin(self.a, old['id'], run)
        groups.arrange(self.a, [{'name': 'Work', 'panes': [self.a2]}], run)
        self.assertIn(old['id'], [g['id'] for g in groups.read(self.a, run)], 'a helper launch is about to fill it')
        self.assertFalse(groups.drop_if_empty(self.a, old['id'], run))
        groups.start_end(self.a, token, run)
        self.assertTrue(groups.drop_if_empty(self.a, old['id'], run), 'the start ended with no tab there: it goes')

    def test_arrange_helper_follows_owner_only_while_the_owner_holds_the_same_conversation(self):
        run = self.run_tmux
        one, two = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
        self.tmux('new-window', '-t', self.a + ':')
        a3 = [pane for pane in self.panes(self.a) if pane not in (self.a1, self.a2)][0]
        self.tmux('set-option', '-p', '-t', a3, '@tether_helper', json.dumps(
            {'owner_pane': self.a1, 'owner_thread': one, 'sent_at': 1}))
        made = groups.arrange(self.a, [{'name': 'Work', 'panes': [self.a1]}], run, thread_of={self.a1: two}.get)
        self.assertEqual(self.label(a3), '', 'the owner holds another conversation: the helper stays')
        groups.arrange(self.a, [{'name': 'Work', 'panes': [self.a1]}], run, thread_of={self.a1: one}.get)
        self.assertEqual(self.label(a3), made[0]['id'], 'same conversation: the helper follows')
        groups.move(self.a, a3, '', run)
        groups.arrange(self.a, [{'name': 'Work', 'panes': [self.a1]}], run)
        self.assertEqual(self.label(a3), made[0]['id'], 'conversation unknown: the link holds')

    def test_reorder_puts_stacks_in_the_given_order(self):
        run = self.run_tmux
        one, two, three = (groups.create(self.a, name, run, by_user=True) for name in ('One', 'Two', 'Three'))
        self.assertEqual([g['name'] for g in groups.reorder(self.a, [three['id'], one['id']], run)], ['Three', 'One', 'Two'])
        self.assertEqual([g['name'] for g in groups.read(self.a, run)], ['Three', 'One', 'Two'], 'a stack left out keeps its place after')
        groups.reorder(self.a, ['gffffffff', two['id'], two['id']], run)
        self.assertEqual([g['name'] for g in groups.read(self.a, run)], ['Two', 'Three', 'One'], 'gone and repeated ids are skipped')
        self.assertEqual(groups.read(self.a, run)[0].get('by'), 'user', 'the maker stays')
        self.assertEqual(self.status(lambda: groups.reorder(self.a, ['gffffffff'], run)), 404)
        for bad in ([], 'g1', ['%1'], [one['id']] * 51):
            self.assertEqual(self.status(lambda: groups.reorder(self.a, bad, run)), 400)

    def test_pane_session_and_check_group(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        self.assertEqual(groups.pane_session(self.a2, run), self.a)
        groups.check_group(self.a, docs['id'], run)
        groups.check_group(self.a, '', run)
        self.assertEqual(self.status(lambda: groups.check_group(self.b, docs['id'], run)), 404)
        self.assertEqual(self.status(lambda: groups.pane_session('%999', run)), 404)

    def test_stale_ids_give_404(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        self.assertEqual(self.status(lambda: groups.read('$999', run)), 404)
        self.assertEqual(self.status(lambda: groups.move(self.a, '%999', docs['id'], run)), 404)
        self.assertEqual(self.status(lambda: groups.move(self.a, self.a1, 'gffffffff', run)), 404)
        self.assertEqual(self.status(lambda: groups.move(self.a, self.b1, docs['id'], run)), 404)
        self.assertEqual(self.status(lambda: groups.create(self.a, 'API', run, pane=self.b1)), 404)
        self.assertEqual(self.status(lambda: groups.rename(self.a, 'gffffffff', 'X', run)), 404)
        self.assertEqual(self.status(lambda: groups.remove(self.a, 'gffffffff', run)), 404)
        self.assertEqual(self.status(lambda: groups.move(self.a, self.a1, 'nope', run)), 400)
        self.assertEqual([g['name'] for g in groups.read(self.a, run)], ['Docs'])
        self.assertEqual(self.label(self.b1), '')

    def test_move_remove_and_ungroup(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        groups.move(self.a, self.a1, docs['id'], run)
        groups.move(self.a, self.a2, docs['id'], run)
        self.assertEqual(groups.group_of_pane(self.a1, run), (self.a, docs))
        self.assertEqual(self.status(lambda: groups.remove(self.a, docs['id'], run)), 409)
        groups.move(self.a, self.a2, None, run)
        self.assertEqual(groups.group_of_pane(self.a2, run), (self.a, None))
        pid = groups.context(self.a, docs['id'], run)
        projects.set_instructions(pid, 'Docs rule.', 0)
        groups.ungroup(self.a, docs['id'], run)
        self.assertEqual((groups.read(self.a, run), self.label(self.a1)), ([], ''))
        self.assertFalse(projects.bundle(pid).exists())
        archived = list((projects.root() / '.archive').glob(pid + '-*'))
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / 'INSTRUCTIONS.md').read_text().strip(), 'Docs rule.')
        empty = groups.create(self.a, 'Empty', run)
        groups.remove(self.a, empty['id'], run)
        self.assertEqual(self.tmux('show-options', '-v', '-t', self.a, '@groups').stdout.strip(), '')

    def test_context_is_made_once_and_named(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        pid = groups.context(self.a, docs['id'], run)
        self.assertTrue(projects.ID.fullmatch(pid))
        self.assertTrue((projects.bundle(pid) / 'notes').is_dir())
        self.assertEqual(projects.meta(pid)['name'], 'Docs')
        self.assertEqual(groups.context(self.a, docs['id'], run), pid)
        self.assertEqual(groups.read(self.a, run)[0]['project'], pid)
        groups.rename(self.a, docs['id'], 'Guides', run)
        self.assertEqual(projects.meta(pid)['name'], 'Guides')

    def test_context_of_a_new_group_only_reads(self):
        # An agent launcher calls context() in its own process; it must not write @groups back (spec 6 review, defect 1).
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        with mock.patch.object(groups, 'write', side_effect=AssertionError('context wrote @groups')):
            self.assertEqual(groups.context(self.a, docs['id'], run), docs['project'])
        old = {'id': 'gaaaaaaaa', 'name': 'Old', 'project': ''}  # saved before create made ids
        groups.write(self.a, groups.read(self.a, run) + [old], run)
        pid = groups.context(self.a, old['id'], run)
        self.assertEqual([g['project'] for g in groups.read(self.a, run)], [docs['project'], pid])

    def test_writers_in_other_processes_lose_no_group(self):
        # The server and agent launchers are separate processes. Each read-modify-write holds a file lock.
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        env.update(TMUX_TMPDIR=self.socket_dir, MOBILE_TMUX_CACHE=str(paths.CACHE), TETHER_HOME=str(paths.TETHER))
        code = ('import sys, groups\n'
                'for i in range(15):\n'
                '    groups.create(sys.argv[1], sys.argv[2] + str(i))\n')
        workers = [subprocess.Popen([os.sys.executable, '-c', code, self.a, prefix], cwd=Path(__file__).parent, env=env,
                                    stderr=subprocess.PIPE, text=True) for prefix in ('A', 'B', 'C')]
        for worker in workers:
            self.assertEqual(worker.wait(timeout=60), 0, worker.stderr.read())
            worker.stderr.close()
        names = sorted(g['name'] for g in groups.read(self.a, self.run_tmux))
        self.assertEqual(names, sorted(f'{p}{i}' for p in 'ABC' for i in range(15)))

    def test_find_or_create(self):
        run = self.run_tmux
        made = groups.find_or_create(self.a, 'Docs', run)
        self.assertEqual(groups.find_or_create(self.a, ' docs ', run), made)
        self.assertIsNone(groups.find_or_create(self.a, 'MAIN', run))
        self.assertEqual(len(groups.read(self.a, run)), 1)

    def test_new_tabs_inherit_the_group_unless_told(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run, pane=self.a1)
        api = groups.create(self.a, 'API', run)
        inherited = terminal_input.new_window_after(self.a1, run)
        main = terminal_input.new_window_after(self.a1, run, group='')
        chosen = terminal_input.new_window_after(self.a2, run, group=api['id'])
        plain = terminal_input.new_window_after(self.a2, run)
        self.assertEqual([self.label(p) for p in (inherited, main, chosen, plain)], [docs['id'], '', api['id'], ''])

    def start_group(self, body, caller, entries=(), **kwargs):
        with agent_tabs._start_group(body, caller, self.run_tmux, entries, **kwargs) as (group, root, name):
            return group

    def names(self, session):
        return [g['name'] for g in groups.read(session, self.run_tmux)]

    def helper(self, pane, owner, **extra):
        """Mark pane as a helper that owner started (helper_tabs.py record)."""
        record = dict({'owner_pane': owner, 'owner_thread': None, 'helper_thread': None, 'sent_at': 1.0, 'before': None},
                      **extra)
        self.tmux('set-option', '-p', '-t', pane, '@tether_helper', json.dumps(record))

    def test_explicit_unstacked_and_bad_stack_names(self):
        caller = {'id': self.a1, 'pane_name': 'Home dashboard'}
        self.assertEqual(self.start_group({'group': 'Main'}, caller), '')
        self.assertEqual(self.start_group({'group': 'unstacked'}, caller), '')
        self.assertEqual(self.status(lambda: self.start_group({'group': ''}, caller)), 400)
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))

    def test_a_helper_opens_in_its_callers_place_and_makes_no_stack(self):  # notes/auto-stacks.md, rule 1
        run = self.run_tmux
        caller = {'id': self.a1, 'pane_name': 'desktop-nav'}
        self.assertEqual(self.start_group({}, caller), '')
        self.assertEqual(self.start_group({'group': 'Release notes', 'message': 'x'}, caller, branch='feature/nav'), '')
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''), '--stack and --worktree make no stack')
        docs = groups.create(self.a, 'Docs', run, pane=self.a2, by_user=True)
        self.assertEqual(self.start_group({'group': 'Other goal'}, {'id': self.a2, 'pane_name': 'Worker'}), docs['id'])
        self.assertEqual(self.start_group({'group': 'Unstacked'}, {'id': self.a2}), '')
        self.assertEqual((self.names(self.a), self.label(self.a1), self.label(self.a2)), (['Docs'], '', docs['id']))

    def test_helper_of_a_helper_finds_the_root_through_the_chain(self):  # the owner record keeps the root
        run = self.run_tmux
        third = terminal_input.new_window_after(self.a2, run)
        self.helper(self.a2, self.a1)
        self.helper(third, self.a2)
        with agent_tabs._start_group({}, {'id': third, 'pane_name': 'Deep'}, run,
                                     [{'id': self.a1, 'pane_name': 'Billing fix'}]) as (group, root, name):
            pass
        self.assertEqual((group, root), ('', self.a1))
        self.assertEqual(([self.label(p) for p in (self.a1, self.a2, third)], self.names(self.a)), (['', '', ''], []),
                         'nothing moves')
        one = '11111111-1111-4111-8111-111111111111'
        self.helper(third, '%999', root_pane=self.a1, root_thread=one)
        self.assertEqual(agent_tabs.auto_stacks.root_of(third, {self.a1: one}.get, run), self.a1)
        self.helper(third, '%999', root_pane=self.a1)
        self.assertEqual(agent_tabs.auto_stacks.root_of(third, {self.a1: one}.get, run), '%999', 'no root_thread: no cache')

    def test_a_reused_owner_pane_breaks_the_chain(self):
        self.helper(self.a2, self.a1, owner_thread='11111111-1111-4111-8111-111111111111')
        threads = {self.a1: '22222222-2222-4222-8222-222222222222'}
        self.assertEqual(agent_tabs.auto_stacks.root_of(self.a2, threads.get, self.run_tmux), self.a2)

    def test_lone_main_session_stays_unstacked_until_a_helper_starts(self):  # rule 2
        self.assertEqual((self.names(self.a), self.label(self.a1), self.label(self.a2)), ([], '', ''))

    def test_root_in_a_user_stack_shares_it(self):
        docs = groups.create(self.a, 'Docs', self.run_tmux, pane=self.a1, by_user=True)
        self.assertEqual(self.start_group({}, {'id': self.a1, 'pane_name': 'Other goal'}), docs['id'])
        self.assertEqual(self.names(self.a), ['Docs'])

    def test_user_placed_tabs_are_never_auto_moved(self):  # rule 7 (old rule 5)
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        groups.move(self.a, self.a1, docs['id'], run, by_user=True)
        groups.move(self.a, self.a1, None, run, by_user=True)
        self.assertTrue(groups.placed_by_user(self.a1, run))
        self.assertFalse(groups.placed_by_user(self.a2, run))
        self.assertEqual(self.start_group({}, {'id': self.a1, 'pane_name': 'Docs work'}), '')
        self.assertEqual(self.start_group({'group': 'Docs'}, {'id': self.a1}), '', '--stack does not move it either')
        self.assertEqual((self.label(self.a1), self.names(self.a)), ('', ['Docs']))
        api = groups.create(self.a, 'API', run, pane=self.a2, by_user=True)
        self.assertTrue(groups.placed_by_user(self.a2, run))
        groups.ungroup(self.a, api['id'], run, by_user=True)
        self.assertTrue(groups.placed_by_user(self.a2, run))
        self.assertFalse(groups.auto_move(self.a, self.a2, docs['id'], run))
        self.assertEqual(self.label(self.a2), '')

    def test_empty_auto_stacks_go_and_user_stacks_stay(self):  # rule 6
        run = self.run_tmux
        made = self.auto_stack(self.a, 'Billing', self.a1)  # the "Fixes" stack of work_fix.py is still auto
        user = groups.create(self.a, 'Mine', run, by_user=True)  # the Stacks sheet (groups_api.py)
        self.assertEqual(user['by'], 'user')
        self.tmux('set-option', '-p', '-t', self.a2, '@group', made)
        groups.move(self.a, self.a1, None, run, by_user=True)
        self.assertEqual(self.names(self.a), ['Billing', 'Mine'], 'one tab is still in it')
        groups.move(self.a, self.a2, user['id'], run, by_user=True)
        self.assertEqual(self.names(self.a), ['Mine'], 'the last tab moved out: the auto stack went')
        # A tab that closes: the sweep removes the empty auto stack and keeps the user stack.
        again = self.auto_stack(self.b, 'Beta', self.b1)
        self.tmux('set-option', '-p', '-u', '-t', self.b1, '@group')
        self.assertEqual(groups.drop_lone_auto(run), [(self.b, 'Beta')])
        self.assertEqual((self.names(self.b), self.names(self.a)), ([], ['Mine']))
        self.assertNotEqual(again, '')

    def test_a_user_rename_makes_an_auto_stack_the_users(self):  # rule 7
        run = self.run_tmux
        made = self.auto_stack(self.a, 'Billing', self.a1)
        groups.rename(self.a, made, 'Tether rename', run, by_user=False)  # Tether may rename its own stack
        groups.rename(self.a, made, 'Payments', run)  # the Stacks sheet: by the user
        self.assertNotIn('auto', groups.read(self.a, run)[0])
        self.assertEqual(groups.read(self.a, run)[0]['by'], 'user', 'the maker is recorded')
        self.assertEqual(self.status(lambda: groups.rename(self.a, made, 'Again', run, by_user=False)), 409)
        groups.move(self.a, self.a1, None, run, by_user=True)
        self.assertEqual(groups.drop_lone_auto(run), [])
        self.assertEqual(self.names(self.a), ['Payments'], 'a user stack is never removed')

    # Dissolve: a Tether-made stack with only its main session left goes; the root goes to Unstacked (spec stack-dissolve)
    def auto_stack(self, session, name, root, *helpers):
        """An auto stack with its root and helpers, as the old auto stacks (and work_fix.py) make one. Returns its id."""
        made = groups.create(session, name, self.run_tmux, pane=root, auto=True, root=root)['id']
        for pane in helpers:
            self.helper(pane, root)
            self.assertTrue(groups.auto_move(session, pane, made, self.run_tmux))
        return made

    def family(self):
        """An auto stack [a1 (root), a2 (helper)] from before helpers stopped making stacks. Returns the group id."""
        made = self.auto_stack(self.a, 'Billing', self.a1, self.a2)
        self.assertEqual((self.label(self.a1), self.label(self.a2)), (made, made))
        return made

    def test_last_helper_moving_out_dissolves_the_root_only_stack(self):  # rules 1 and 3: move out
        run = self.run_tmux
        made = self.family()
        pid = groups.read(self.a, run)[0]['project']
        groups.context(self.a, made, run)
        groups.move(self.a, self.a2, None, run, by_user=True)
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''), 'the root is back in Unstacked')
        self.assertFalse(groups.placed_by_user(self.a1, run), 'Tether moved the root, not the user')
        self.assertEqual(len(list((projects.root() / '.archive').glob(pid + '-*'))), 1, 'its context is archived')

    def test_last_helper_closing_dissolves_at_once_and_in_the_sweep(self):  # rules 1 and 3: close
        run = self.run_tmux
        made = self.family()
        with mock.patch.object(helper_close.auto_resume, 'forget_pane'):
            helper_close.close_pane(self.a2, run)
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''), 'the close dissolves it at once')
        # A tab that ends by itself: the 30 s sweep.
        b2 = terminal_input.new_window_after(self.b1, run)
        again = self.auto_stack(self.b, 'Beta', self.b1, b2)
        self.assertNotEqual(again, '')
        self.tmux('kill-pane', '-t', b2)
        self.assertEqual(groups.drop_lone_auto(run), [(self.b, 'Beta')])
        self.assertEqual((self.names(self.b), self.label(self.b1)), ([], ''))
        self.assertNotEqual(made, again)

    def test_dissolve_waits_while_auto_resume_restores(self):
        run = self.run_tmux
        self.family()
        self.tmux('kill-pane', '-t', self.a2)
        with mock.patch.object(groups, 'HOLD', lambda: True):
            self.assertEqual(groups.drop_lone_auto(run), [])
        self.assertEqual(self.names(self.a), ['Billing'], 'helpers may still come back')
        self.assertEqual(groups.drop_lone_auto(run), [(self.a, 'Billing')])

    def test_user_stacks_old_stacks_and_user_placed_roots_stay(self):  # rule 2
        run = self.run_tmux
        made = self.family()
        groups.move(self.a, self.a1, made, run, by_user=True)  # the user put the root there by hand
        groups.move(self.a, self.a2, None, run)
        self.assertEqual((self.names(self.a), self.label(self.a1)), (['Billing'], made), 'a root the user placed stays')
        # A user stack, an old stack (no maker) and an auto stack without a recorded root hold one tab each.
        self.tmux('set-option', '-p', '-u', '-t', self.a1, '@group_by')
        groups.write(self.a, [{'id': made, 'name': 'Billing', 'project': '', 'by': 'user'}], run)
        self.assertFalse(groups.drop_if_alone(self.a, made, run))
        groups.write(self.a, [{'id': made, 'name': 'Billing', 'project': ''}], run)
        self.assertFalse(groups.drop_if_alone(self.a, made, run))
        groups.write(self.a, [{'id': made, 'name': 'Billing', 'project': '', 'auto': True}], run)
        self.assertEqual(groups.drop_lone_auto(run), [])
        self.assertEqual((self.names(self.a), self.label(self.a1)), (['Billing'], made))
        # One tab that is not the root (the root closed or the user moved it out): not this rule.
        groups.write(self.a, [{'id': made, 'name': 'Billing', 'project': '', 'auto': True, 'root': self.a2}], run)
        self.assertEqual(groups.drop_lone_auto(run), [])
        self.assertEqual(self.label(self.a1), made)

    def test_a_start_in_flight_keeps_the_stack(self):  # rule 4: between create and the new tab's @group
        run = self.run_tmux
        caller = {'id': self.a1, 'pane_name': 'Billing'}
        made = self.auto_stack(self.a, 'Billing', self.a1)  # root-only: its last helper closed
        with agent_tabs._start_group({}, caller, run) as (group, root, name):
            self.assertEqual(group, made, "the helper opens in its caller's stack")
            self.assertEqual(groups.panes_in(self.a, group, run), [self.a1], 'root-only while the tab launches')
            self.assertEqual(groups.drop_lone_auto(run), [])
            self.assertFalse(groups.drop_if_empty(self.a, group, run))
            new = terminal_input.new_window_after(self.a1, run, group=group)
        self.assertEqual(self.names(self.a), ['Billing'])
        self.assertEqual(self.label(new), group)
        self.assertEqual(self.tmux('show-options', '-qv', '-t', self.a, groups.STARTS).stdout.strip(), '',
                         'the marker goes when the start ends')
        # A later helper of the same root, while the first helper closes: the stack stays for the new one.
        self.helper(new, self.a1)
        with agent_tabs._start_group({}, caller, run) as (again, root, name):
            self.assertEqual(again, group)
            self.tmux('kill-pane', '-t', new)
            self.assertEqual(groups.drop_lone_auto(run), [])
            later = terminal_input.new_window_after(self.a1, run, group=again)
        self.assertEqual((self.names(self.a), self.label(later)), (['Billing'], group))

    def test_another_process_sees_a_start_in_flight(self):  # rule 4 across processes, and the expiry
        run = self.run_tmux
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        env.update(TMUX_TMPDIR=self.socket_dir, MOBILE_TMUX_CACHE=str(paths.CACHE), TETHER_HOME=str(paths.TETHER))
        sweep = ['import groups', 'print(groups.drop_lone_auto())']
        self.auto_stack(self.a, 'Billing', self.a1)
        with agent_tabs._start_group({}, {'id': self.a1, 'pane_name': 'Billing'}, run) as (group, root, name):
            out = subprocess.run([os.sys.executable, '-c', '\n'.join(sweep)], cwd=Path(__file__).parent, env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, '[]'), out.stderr)
            with own_time(groups, time=mock.Mock(return_value=10 ** 12)):  # a marker its process never ended
                self.assertFalse(groups.starting(self.a, group, run))
        self.assertEqual(groups.drop_lone_auto(run), [(self.a, 'Billing')], 'no helper came: it dissolves after')

    def test_a_failed_start_on_a_root_only_stack_dissolves_it(self):
        run = self.run_tmux
        made = self.family()
        self.tmux('kill-pane', '-t', self.a2)
        with self.assertRaises(RuntimeError):
            with agent_tabs._start_group({}, {'id': self.a1, 'pane_name': 'Billing'}, run) as (group, root, name):
                self.assertEqual(group, made)
                raise RuntimeError('launch failed')
        self.assertEqual(groups.drop_lone_auto(run), [(self.a, 'Billing')])
        self.assertEqual(groups._starts(self.a, run), {})

    def test_dissolve_and_start_at_once_give_one_stack_and_keep_the_helper(self):  # rule 3 race
        import threading
        run = self.run_tmux
        for _ in range(5):
            self.family()
            self.tmux('kill-pane', '-t', self.a2)  # the last helper closed: root-only
            self.a2 = terminal_input.new_window_after(self.a1, run, group='')
            barrier, new = threading.Barrier(2), []

            def start():
                barrier.wait()
                with agent_tabs._start_group({}, {'id': self.a1, 'pane_name': 'Billing'}, run) as (group, root, name):
                    new.append(terminal_input.new_window_after(self.a1, run, group=group))

            def sweep():
                barrier.wait()
                groups.drop_lone_auto(run)
            workers = [threading.Thread(target=start), threading.Thread(target=sweep)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
            stacks = groups.read(self.a, run)
            self.assertLessEqual(len(stacks), 1, 'never a second stack')
            place = stacks[0]['id'] if stacks else ''  # the sweep may win first: then both are Unstacked
            self.assertEqual((self.label(self.a1), self.label(new[0])), (place, place),
                             'the new helper is where its root is')
            groups.drop_lone_auto(run)
            self.tmux('kill-pane', '-t', new[0])
            groups.drop_lone_auto(run)
            self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))

    def test_cleanup_moves_keep_a_root_only_stack_that_a_later_move_needs(self):  # review 1, finding 1
        import auto_stacks_cleanup
        run = self.run_tmux
        x, y = (terminal_input.new_window_after(self.a1, run, group='') for _ in range(2))
        self.helper(x, self.a1)
        self.helper(y, self.a2)
        one = groups.create(self.a, 'One', run, pane=self.a1, auto=True, root=self.a1)
        two = groups.create(self.a, 'Two', run, pane=self.a2, auto=True, root=self.a2)
        groups.move(self.a, x, two['id'], run)
        result = {'session': self.a, 'root_threads': {self.a1: None, self.a2: None}, 'mark_auto': [], 'roots': {},
                  'made': [], 'notes': [], 'drops': [], 'renames': [],
                  'moves': [{'pane': x, 'from': two['id'], 'to': one['id'], 'root': self.a1},
                            {'pane': y, 'from': '', 'to': two['id'], 'root': self.a2}]}
        self.assertEqual(auto_stacks_cleanup.apply(result, run), [])
        self.assertEqual([self.label(p) for p in (self.a1, x, self.a2, y)], [one['id']] * 2 + [two['id']] * 2)

    def test_unknown_start_markers_keep_the_stack(self):  # review 1, finding 2
        run = self.run_tmux
        made = self.family()
        self.tmux('kill-pane', '-t', self.a2)
        self.tmux('set-option', '-t', self.a, groups.STARTS, 'not json')
        self.assertTrue(groups.starting(self.a, made, run))
        self.assertEqual(groups.drop_lone_auto(run), [])
        failing = lambda args, **kw: (subprocess.CompletedProcess(args, 1, '', 'tmux failed') if 'show-options' in args
                                      else run(args, **kw))
        self.tmux('set-option', '-u', '-t', self.a, groups.STARTS)
        self.assertFalse(groups.drop_if_alone(self.a, made, failing), 'a failed marker read refuses')
        self.assertEqual(self.status(lambda: groups.start_begin(self.a, made, failing)), 500)
        self.assertEqual(groups.drop_lone_auto(run), [(self.a, 'Billing')], 'a readable, empty marker list frees it')

    def test_restore_keeps_even_an_empty_auto_stack(self):  # review 1, finding 3
        run = self.run_tmux
        made = self.family()
        with mock.patch.object(groups, 'HOLD', lambda: True):
            groups.move(self.a, self.a2, None, run, by_user=True)
            groups.move(self.a, self.a1, None, run, by_user=True)
            self.assertFalse(groups.drop_if_alone(self.a, made, run))
        self.assertEqual(self.names(self.a), ['Billing'])
        self.assertTrue(groups.drop_if_alone(self.a, made, run))

    def test_failed_start_undoes_the_auto_stack(self):
        caller = {'id': self.a1, 'pane_name': 'Docs'}
        with self.assertRaises(RuntimeError):
            with agent_tabs._start_group({}, caller, self.run_tmux):
                raise RuntimeError('launch failed')
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))
        with self.assertRaises(RuntimeError):
            with agent_tabs._start_group({'group': 'Docs'}, caller, self.run_tmux):
                raise RuntimeError('launch failed')
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))

    def test_rollback_keeps_a_move_the_user_made_during_launch(self):
        caller = {'id': self.a1, 'pane_name': 'Docs'}
        mine = groups.create(self.a, 'User choice', self.run_tmux)
        with self.assertRaises(RuntimeError):
            with agent_tabs._start_group({}, caller, self.run_tmux):
                groups.move(self.a, self.a1, mine['id'], self.run_tmux, by_user=True)
                raise RuntimeError('launch failed')
        self.assertEqual((self.names(self.a), self.label(self.a1)), (['User choice'], mine['id']))

    def test_a_stale_cached_root_is_not_used(self):  # review finding 3
        run = self.run_tmux
        one, two = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
        self.helper(self.a2, self.a1, owner_thread=one, root_pane=self.b1)
        self.assertEqual(auto_stacks.root_of(self.a2, {self.a1: two}.get, run), self.a2, 'the owner link is broken')
        self.helper(self.a2, self.a1, root_pane=self.b1, root_thread=one)
        self.assertEqual(auto_stacks.root_of(self.a2, {self.b1: one}.get, run), self.a1, 'a live chain wins over the cache')
        self.helper(self.a2, '%999', root_pane=self.b1, root_thread=one)
        self.assertEqual(auto_stacks.root_of(self.a2, {self.b1: one}.get, run), self.b1, 'the owner closed: the cache')
        self.assertEqual(auto_stacks.root_of(self.a2, {self.b1: two}.get, run), '%999', 'the root holds another chat')
        # R -> A -> B -> C, and A changes conversations: B and C both stop below A, though C caches R.
        third, fourth = (terminal_input.new_window_after(self.a2, run) for _ in range(2))
        self.helper(self.a2, self.a1, root_pane=self.a1, root_thread=one)  # A, under R = a1 (thread one)
        self.helper(third, self.a2, owner_thread=two, root_pane=self.a1, root_thread=one)  # B, under A (thread two)
        self.helper(fourth, third, root_pane=self.a1, root_thread=one)  # C, under B
        three = '33333333-3333-4333-8333-333333333333'
        changed = {self.a1: one, self.a2: three}.get  # A now holds another chat
        self.assertEqual({auto_stacks.root_of(p, changed, run) for p in (third, fourth)}, {third})
        self.assertEqual(auto_stacks.root_of(fourth, {self.a1: one, self.a2: two}.get, run), self.a1)
        self.assertEqual(agent_tabs._owner({'id': self.a2, 'thread': one}, self.a1, [{'id': self.a1, 'thread': two}])
                         ['root_thread'], two)

    def cleanup_snapshot(self, entries=()):
        import auto_stacks_cleanup
        return auto_stacks_cleanup, auto_stacks_cleanup.snapshot(self.a, list(entries), self.run_tmux)

    def test_cleanup_apply_skips_a_stack_the_user_renamed_after_the_plan(self):  # review finding 1
        run = self.run_tmux
        old = {'id': 'gaaaaaaaa', 'name': 'B45 advisor log', 'project': ''}
        groups.write(self.a, [old], run)
        self.tmux('set-option', '-p', '-t', self.a2, '@group', old['id'])
        self.helper(self.a2, self.a1)
        cleanup, snap = self.cleanup_snapshot([{'id': self.a1, 'pane_name': 'Billing'}])
        result = cleanup.plan(snap, {old['id']})
        self.assertEqual((len(result['moves']), result['mark_auto'], len(result['renames'])), (1, [old['id']], 1))
        groups.rename(self.a, old['id'], 'Mine', run)  # the user, between the dry run and the run
        skipped = cleanup.apply(result, run)
        self.assertTrue(skipped)
        self.assertEqual(groups.read(self.a, run), [dict(old, name='Mine', by='user')])
        self.assertEqual((self.label(self.a1), self.label(self.a2)), ('', old['id']), 'nothing moved')

    def test_cleanup_apply_removes_a_proven_stack_it_empties(self):  # review finding 6
        run = self.run_tmux
        third = terminal_input.new_window_after(self.a2, run)
        keep, drop = {'id': 'gaaaaaaaa', 'name': 'Helpers', 'project': ''}, {'id': 'gbbbbbbbb', 'name': 'Other', 'project': ''}
        groups.write(self.a, [keep, drop], run)
        self.tmux('set-option', '-p', '-t', self.a2, '@group', keep['id'])
        self.tmux('set-option', '-p', '-t', third, '@group', drop['id'])
        self.helper(self.a2, self.a1)
        self.helper(third, self.a1)
        cleanup, snap = self.cleanup_snapshot([{'id': self.a1, 'pane_name': 'Billing'}])
        self.assertEqual(cleanup.apply(cleanup.plan(snap, {keep['id'], drop['id']}), run), [])
        found = groups.read(self.a, run)
        self.assertEqual([(g['name'], g.get('auto'), g.get('root')) for g in found], [('Helpers', True, self.a1)],
                         'the old name is the --stack name, which comes before the tab name (rule 4)')
        self.assertEqual({self.label(p) for p in (self.a1, self.a2, third)}, {keep['id']})

    def test_cleanup_apply_leaves_a_family_whose_root_closed(self):  # review round 2, finding 2
        run = self.run_tmux
        third = terminal_input.new_window_after(self.a2, run)
        keep, drop = {'id': 'gaaaaaaaa', 'name': 'Helpers', 'project': ''}, {'id': 'gbbbbbbbb', 'name': 'Other', 'project': ''}
        groups.write(self.a, [keep, drop], run)
        self.tmux('set-option', '-p', '-t', self.a2, '@group', keep['id'])
        self.tmux('set-option', '-p', '-t', third, '@group', drop['id'])
        self.helper(self.a2, self.a1)
        self.helper(third, self.a1)
        cleanup, snap = self.cleanup_snapshot([{'id': self.a1, 'pane_name': 'Billing',
                                                'thread': '11111111-1111-4111-8111-111111111111'}])
        result = cleanup.plan(snap, {keep['id'], drop['id']})
        self.assertTrue(result['moves'])
        for now in ({self.a1: '22222222-2222-4222-8222-222222222222'}.get, lambda pane: None):  # another chat; a shell
            self.assertTrue(cleanup.apply(result, run, now))
            self.assertEqual([self.label(p) for p in (self.a1, self.a2, third)], ['', keep['id'], drop['id']])
            found = groups.read(self.a, run)
            self.assertEqual([g['name'] for g in found], ['Helpers', 'Other'], 'no rename, no drop')
            self.assertFalse([g for g in found if 'root' in g], 'no root marker for a family left in place')

    def test_cleanup_apply_copies_no_notes_from_a_stack_the_user_took(self):  # review round 2, finding 3
        run = self.run_tmux
        third = terminal_input.new_window_after(self.a2, run)
        keep = {'id': 'gaaaaaaaa', 'name': 'Helpers', 'project': ''}
        drop = groups.create(self.a, 'Other', run)
        groups.write(self.a, [keep] + groups.read(self.a, run), run)
        source = groups.context(self.a, drop['id'], run)
        (projects.bundle(source) / 'notes').mkdir(parents=True, exist_ok=True)
        (projects.bundle(source) / 'notes' / 'mine.md').write_text('# Mine')
        self.tmux('set-option', '-p', '-t', self.a2, '@group', keep['id'])
        self.tmux('set-option', '-p', '-t', third, '@group', drop['id'])
        self.helper(self.a2, self.a1)
        self.helper(third, self.a1)
        cleanup, snap = self.cleanup_snapshot([{'id': self.a1, 'pane_name': 'Billing'}])
        result = cleanup.plan(snap, {keep['id'], drop['id']})
        self.assertEqual(result['notes'], [{'from': drop['id'], 'to': keep['id']}])
        groups.rename(self.a, drop['id'], 'Mine', run)  # the user, after the plan
        cleanup.apply(result, run)
        target = groups.context(self.a, keep['id'], run)
        self.assertFalse((projects.bundle(target) / 'notes' / 'mine.md').exists())
        self.assertEqual(self.label(third), drop['id'])

    def test_cleanup_renames_in_a_safe_order(self):  # review finding 8
        import auto_stacks_cleanup
        run = self.run_tmux
        made = [dict({'id': f'g0000000{n}', 'name': name, 'project': ''}, auto=True)
                for n, name in enumerate(('One', 'Two', 'X', 'Y'), 1)]
        groups.write(self.a, made, run)
        renames = [{'group': 'g00000001', 'old': 'One', 'new': 'Two'}, {'group': 'g00000002', 'old': 'Two', 'new': 'Three'},
                   {'group': 'g00000003', 'old': 'X', 'new': 'Y'}, {'group': 'g00000004', 'old': 'Y', 'new': 'X'}]
        self.assertEqual(auto_stacks_cleanup._rename_all(self.a, renames, run), [])
        self.assertEqual(self.names(self.a), ['Two', 'Three', 'Y', 'X'])

    def test_free_name_numbers_taken_and_reserved_names(self):
        taken = [{'id': 'g00000001', 'name': 'Docs', 'project': ''}, {'id': 'g00000002', 'name': 'docs 2', 'project': ''}]
        self.assertEqual(groups.free_name(taken, 'DOCS'), 'DOCS 3')
        self.assertEqual(groups.free_name(taken, 'Main'), 'Main 2')
        self.assertEqual(groups.free_name([], 'x' * 40), 'x' * 40)
        self.assertEqual(groups.free_name([{'id': 'g00000001', 'name': 'x' * 40, 'project': ''}], 'x' * 40), 'x' * 38 + ' 2')

    def test_merge_appends_numbered_groups_and_relabels_clashing_ids(self):
        run = self.run_tmux
        groups.create(self.b, 'Docs', run)
        same = {'id': 'gaaaaaaaa', 'name': 'Shared', 'project': ''}
        groups.write(self.b, groups.read(self.b, run) + [same], run)
        groups.write(self.a, [same, {'id': 'gbbbbbbbb', 'name': 'docs', 'project': ''}], run)
        groups.move(self.a, self.a1, 'gaaaaaaaa', run)
        groups.move(self.a, self.a2, 'gbbbbbbbb', run)
        self.tmux('set-option', '-p', '-t', self.a2, '@group', 'gcccccccc')  # a label with no group
        tmux_merge.merge('session', self.a, self.b, run=run)
        found = groups.read(self.b, run)
        self.assertEqual([g['name'] for g in found], ['Docs', 'Shared', 'Shared 2', 'docs 2'])
        renewed = found[2]['id']
        self.assertNotEqual(renewed, 'gaaaaaaaa')
        self.assertEqual((self.label(self.a1), self.label(self.a2)), (renewed, ''))

    def test_a_pane_that_leaves_its_session_goes_to_main(self):
        run = self.run_tmux
        docs = groups.create(self.a, 'Docs', run)
        groups.move(self.a, self.a1, docs['id'], run)
        groups.move(self.a, self.a2, docs['id'], run)
        tmux_merge.merge('pane', self.a1, self.b, run=run)
        self.assertEqual(self.label(self.a1), '')
        self.tmux('new-window', '-t', '=alpha:')
        tmux_merge.break_out(self.a2, run=run)
        self.assertEqual(self.label(self.a2), '')

    def test_http_routes(self):
        run = self.run_tmux

        def post(name, body):
            handler = Handler(groups_api.PREFIX + name, body)
            groups_api.post(handler, run)
            return handler.status, handler.body
        status, body = post('create', {'session': self.a, 'name': 'Docs', 'pane': self.a1})
        self.assertEqual(status, 200)
        docs = body['group']
        self.assertEqual(self.label(self.a1), docs['id'])
        self.assertEqual(post('create', {'session': self.a, 'name': 'docs'})[0], 409)
        self.assertEqual(post('rename', {'session': self.a, 'group': docs['id'], 'name': 'Guides'}),
                         (200, {'group': dict(docs, name='Guides')}))
        self.assertEqual(post('move', {'session': self.a, 'pane': self.a1, 'group': None}), (200, {'ok': True}))
        self.assertEqual(post('move', {'session': self.a, 'pane': self.a2, 'group': docs['id']}), (200, {'ok': True}))
        self.assertEqual(post('remove', {'session': self.a, 'group': docs['id']})[0], 409)
        self.assertEqual(post('ungroup', {'session': self.a, 'group': docs['id']}), (200, {'ok': True}))
        self.assertEqual(post('move', {'session': self.a, 'pane': self.a2, 'group': docs['id']})[0], 404)
        for name, body in (('create', {'session': 'alpha', 'name': 'X'}), ('create', {'session': self.a, 'name': 'X', 'pane': '1'}),
                           ('move', {'session': self.a, 'pane': '%1;x', 'group': ''}), ('rename', {'session': self.a, 'group': 'g1', 'name': 'X'}),
                           ('remove', {'session': self.a, 'group': None}), ('create', ['x'])):
            self.assertEqual(post(name, body)[0], 400, (name, body))
        self.assertEqual(post('nope', {'session': self.a})[0], 404)
        api = post('create', {'session': self.a, 'name': 'API'})[1]['group']
        handler = Handler(f'{groups_api.PREFIX}context?session={self.a.replace("$", "%24")}&group={api["id"]}')
        groups_api.get(handler, run, self.home)
        self.assertEqual(handler.status, 200, handler.body)
        self.assertEqual((handler.body['group_name'], handler.body['session_name'], handler.body['name']), ('API', 'alpha', 'API'))
        self.assertTrue(Path(handler.body['folder'], 'notes').is_dir())
        bad = Handler(f'{groups_api.PREFIX}context?session={self.a.replace("$", "%24")}&group=gffffffff')
        groups_api.get(bad, run, self.home)
        self.assertEqual(bad.status, 404)
        # arrange reads each tab's conversation from sessions(): a helper whose owner changed conversations stays.
        self.tmux('set-option', '-p', '-t', self.a2, '@tether_helper', json.dumps(
            {'owner_pane': self.a1, 'owner_thread': '11111111-1111-4111-8111-111111111111', 'sent_at': 1}))
        listing = [{'id': self.a1, 'thread': '22222222-2222-4222-8222-222222222222'}, {'id': self.a2, 'thread': 'bad'}]
        handler = Handler(groups_api.PREFIX + 'arrange', {'session': self.a, 'stacks': [{'name': 'Work', 'panes': [self.a1]}]})
        groups_api.post(handler, run, sessions=lambda: listing)
        self.assertEqual(handler.status, 200, handler.body)
        self.assertEqual((self.label(self.a1), self.label(self.a2)), (handler.body['groups'][0]['id'], ''))


class CliAndServerTests(unittest.TestCase):
    def test_cli_puts_group_in_the_body(self):
        body = agent_tab_cli.arguments(['start', '--group', 'Docs', '--worktree', 'feature/x', 'do it'])
        self.assertEqual((body['group'], body['worktree']), ('Docs', 'feature/x'))
        self.assertNotIn('group', agent_tab_cli.arguments(['start', 'do it']))
        self.assertEqual(agent_tab_cli.arguments(['start', '--stack', 'Docs', 'do it'])['group'], 'Docs')

    def request(self, route, body, headers=None):
        handler = object.__new__(server.Handler)
        handler.path = route
        handler.command = 'POST'
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        handler.headers.update(headers or {})
        handler.respond = mock.Mock()
        handler.deny = mock.Mock(return_value=False)
        handler.do_POST()
        return handler.respond.call_args.args

    def test_writes_need_the_write_headers_and_reset_the_session_cache(self):
        with mock.patch.object(groups_api.groups, 'create', return_value={'id': 'gaaaaaaaa', 'name': 'Docs', 'project': ''}) as create:
            self.assertEqual(self.request('/mobile/api/groups/create', {'session': '$1', 'name': 'Docs'}, {'X-Codex-Mobile': ''})[0], 403)
            self.assertEqual(self.request('/mobile/api/groups/create', {'session': '$1', 'name': 'Docs'}, {'Origin': 'http://evil.test'})[0], 403)
            create.assert_not_called()
            self.assertEqual(self.request('/mobile/api/groups/create', {'session': '$1', 'name': 'Docs'})[0], 200)
            create.assert_called_once()
        self.assertTrue(groups_api.WRITES <= server.TMUX_WRITES)
        self.assertIn('/mobile/api/groups/move', server.TMUX_WRITES)

    def test_split_checks_the_group_and_passes_it_on(self):
        with mock.patch.object(server, 'choose'), \
                mock.patch.object(server, 'new_window_after', return_value='%9') as opened, \
                mock.patch.object(server.groups, 'find_in_session_of', side_effect=[{}, groups.GroupError(404, groups.GONE_GROUP)]):
            self.assertEqual(self.request('/mobile/api/split', {'session': '%1', 'direction': 'right', 'group': 'gaaaaaaaa'}), (201, {'pane': '%9'}))
            self.assertEqual(opened.call_args.kwargs, {'group': 'gaaaaaaaa'})
            self.assertEqual(self.request('/mobile/api/split', {'session': '%1', 'direction': 'right', 'group': 'gbbbbbbbb'})[0], 404)
            self.assertEqual(self.request('/mobile/api/split', {'session': '%1', 'direction': 'right', 'group': ''}), (201, {'pane': '%9'}))
            self.assertEqual(opened.call_args.kwargs, {'group': ''})
            self.assertEqual(self.request('/mobile/api/split', {'session': '%1', 'direction': 'right'}), (201, {'pane': '%9'}))
            self.assertEqual(opened.call_args.kwargs, {'group': None})

    def test_sessions_response_has_groups_and_the_pane_group(self):
        found = {'$1': [{'id': 'gaaaaaaaa', 'name': 'Docs', 'project': ''}]}
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/sessions'
        handler.command = 'GET'
        handler.headers = {'Host': 'phone.test'}
        handler.respond = mock.Mock()
        handler.deny = mock.Mock(return_value=False)
        rows = [{'id': '%1', 'group': 'gaaaaaaaa'}]
        with mock.patch.object(server, 'sessions', return_value=[{'id': '%1'}]), \
                mock.patch.object(server, 'session_row', side_effect=lambda entry: dict(rows[0])), \
                mock.patch.object(server.review_links, 'reviewed_by', return_value={}), \
                mock.patch.object(server.review_links, 'paused', return_value={}), \
                mock.patch.object(server.auto_resume, 'notice', return_value={'failed': []}), \
                mock.patch.object(server.groups, 'all_groups', return_value=found):
            server.LISTING_CACHE.update(generation=None)  # a new list on each call, not the one-second shared one
            handler.do_GET()
            listing = json.loads(handler.respond.call_args.args[1])
            self.assertEqual((listing['groups'], listing['sessions'][0]['group']), (found, 'gaaaaaaaa'))
            with mock.patch.object(server.groups, 'all_groups', side_effect=OSError):
                server.LISTING_CACHE.update(generation=None)
                handler.do_GET()
            self.assertNotIn('groups', json.loads(handler.respond.call_args.args[1]))
            self.assertEqual(handler.respond.call_args.args[0], 200)

    def test_session_row_copies_group(self):
        with mock.patch.object(server, 'shared_status', return_value={}), \
                mock.patch.object(server, 'tab_folder', return_value='/Users/test/project'), \
                mock.patch.object(server, 'timeline', return_value={}), \
                mock.patch.object(server.needs, 'summary', return_value=''):
            self.assertEqual(server.session_row({'id': '%1', 'group': 'gaaaaaaaa'})['group'], 'gaaaaaaaa')
            self.assertEqual(server.session_row({'id': '%2', 'role': 'advisor'})['role'], 'advisor')
            self.assertIsNone(server.session_row({'id': '%3'})['role'])


if __name__ == '__main__':
    unittest.main()
