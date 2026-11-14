"""Groups (groups.py, groups_api.py) and the places that carry @group. The store runs against a real tmux server in
its own socket folder (TMUX_TMPDIR), never the user's tmux. No test sends input to a terminal."""
import functools
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_tab_cli
import agent_tabs
import groups
import groups_api
import paths
import project_launch
import projects
import server
import terminal_input
import tmux_merge

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


class RenderTests(Bundles):
    GPID = 'b1b2b3b4b5b6'

    def test_group_part_comes_after_the_session_part_and_names_its_notes_folder(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        projects.write_note(PID, 'setup.md', '# Setup\nRun make.')
        projects.set_instructions(self.GPID, 'Only touch the dashboard.', 0)
        projects.write_note(self.GPID, 'chart.md', '# Chart\nUse the bar chart.')
        text = projects.render(PID, 'claude', self.home, group={'pid': self.GPID, 'name': 'Home dashboard'})
        self.assertLessEqual(len(text), projects.meta(PID)['budget'])
        self.assertTrue(text.startswith('# Tether project'))
        self.assertLess(text.index('Use pnpm.'), text.index('## Stack: Home dashboard'))
        self.assertLess(text.index('## Stack: Home dashboard'), text.index('Only touch the dashboard.'))
        self.assertIn(f'{projects.bundle(self.GPID)}/notes/', text)
        self.assertLess(text.index('### Project notes\n- setup.md: Setup'), text.index('### Stack notes\n- chart.md: Chart'))
        self.assertIn('#### chart.md\n# Chart\nUse the bar chart.', text)

    def test_group_text_is_cut_to_the_session_budget_and_keeps_the_session_part(self):
        projects.set_budget(PID, projects.BUDGET_MIN, 0)
        projects.set_instructions(PID, 'Session rule.', 1)
        projects.set_instructions(self.GPID, 'Group rule. ' * 1500, 0)
        for number in range(8):
            projects.write_note(self.GPID, f'n{number}.md', f'# Note {number}\n' + 'word ' * 300)
        text = projects.render(PID, 'codex', self.home, group={'pid': self.GPID, 'name': 'Big'})
        self.assertLessEqual(len(text), projects.BUDGET_MIN)
        self.assertIn('Session rule.', text)
        self.assertIn('[Cut to fit', text)

    def test_no_group_is_unchanged(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        projects.write_note(PID, 'setup.md', '# Setup\nRun make.')
        text = projects.render(PID, 'claude', self.home)
        self.assertEqual(projects.render(PID, 'claude', self.home, group=None), text)
        self.assertNotIn('Stack', text.replace('--stack NAME', ''))
        self.assertIn('\n## Notes\n', text)
        self.assertNotIn('### Project notes', text)
        self.assertIn('`--stack NAME` (made if missing)', text)

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
        self.assertEqual(args[:4], ['--add-dir', str(projects.bundle(PID)), str(projects.bundle(self.GPID)), '--settings'])
        self.tmux.assert_called_with('set-option', '-p', '-t', '%4', '@project_rev', f'{PID}:0+{self.GPID}:1')
        args = project_launch.codex_args(PID, [], self.group)
        self.assertIn('## Stack: Docs', args[1])
        self.assertIn('Docs rule.', args[1])
        project_launch.claude_args(PID, ['task'])
        self.tmux.assert_called_with('set-option', '-p', '-t', '%4', '@project_rev', f'{PID}:0')

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
        output = io.StringIO()
        with mock.patch.object(project_launch, 'current_group', return_value=self.group), \
                mock.patch('sys.stdin', io.StringIO('{}')), mock.patch('sys.stdout', output):
            project_launch.hook(PID)
        text = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn('## Stack: Docs', text)


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

    def test_agent_tab_group_is_found_or_made_in_the_callers_session(self):
        run = self.run_tmux
        caller = {'id': self.a1}
        self.assertIsNone(agent_tabs._start_group({}, caller, run))
        made = agent_tabs._start_group({'group': 'Docs'}, caller, run)
        self.assertEqual([(g['id'], g['name']) for g in groups.read(self.a, run)], [(made, 'Docs')])
        self.assertEqual(agent_tabs._start_group({'group': 'docs'}, caller, run), made)
        self.assertEqual(agent_tabs._start_group({'group': 'Main'}, caller, run), '')
        self.assertEqual(agent_tabs._start_group({'group': 'unstacked'}, caller, run), '')
        self.assertEqual(self.status(lambda: agent_tabs._start_group({'group': ''}, caller, run)), 400)

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
