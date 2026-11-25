import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import advisor_log
import paths
import projects
import projects_api

PID = 'a1b2c3d4e5f6'
STACK = {'pid': 'b1b2c3d4e5f6', 'name': 'Docs'}


class Run:
    """A fake tmux: one session $3 named 'web', with or without @project."""

    def __init__(self, project=''):
        self.project = project
        self.calls = []

    def __call__(self, args, **_):
        self.calls.append(args)
        if args[1] == 'display-message':
            target = args[4]
            if target != '$3:':
                return SimpleNamespace(returncode=1, stdout='', stderr="can't find session")
            return SimpleNamespace(returncode=0, stdout=f'$3\tweb\t{self.project}\n', stderr='')
        if args[1] == 'set-option':
            self.project = args[-1]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(args)


class Handler:
    def __init__(self, path, body=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / 'home'
        self.home.mkdir()
        patch = mock.patch.object(paths, 'TETHER', Path(temp.name) / 'tether')
        patch.start()
        self.addCleanup(patch.stop)

    def test_session_gets_an_id_once(self):
        run = Run()
        pid, name = projects.session_project('$3', run)
        self.assertRegex(pid, projects.ID)
        self.assertEqual(name, 'web')
        self.assertEqual(projects.session_project('$3', run)[0], pid)
        self.assertEqual(sum(call[1] == 'set-option' for call in run.calls), 1)

    def test_bad_session_and_bad_id(self):
        with self.assertRaises(projects.ProjectError):
            projects.session_project('web; rm', Run())
        with self.assertRaises(projects.ProjectError) as gone:
            projects.session_project('$9', Run())
        self.assertEqual(gone.exception.status, 404)
        for bad in ('../x', 'A1B2C3D4E5F6', '', None, 'a1b2c3d4e5f6/..'):
            with self.assertRaises(projects.ProjectError):
                projects.bundle(bad)

    def test_empty_project_injects_agent_guidance_and_writes_no_folder(self):
        text = projects.render(PID)
        self.assertIn('## Notes', text)
        self.assertIn(str(projects.bundle(PID) / 'notes'), text)
        self.assertNotIn('## Instructions', text)
        self.assertNotIn('\n### Project notes\n', text)
        self.assertIn('@chat_icon', text)  # agents learn to set their tab icon
        # Count the notes and code paths at a typical install length, not the test or CI folder length.
        code = str(Path(projects.__file__).resolve().parent.parent)
        real = text.replace(str(paths.TETHER), '/srv/user/.tether')
        real = real.replace(code, '/srv/user/apps/tether')
        real = real.replace(sys.executable, '/usr/bin/python3')  # CI's interpreter path is about 36 characters longer
        self.assertLess(len(real), projects.BUDGET_MAX // 2)  # Keep the built-in guidance below half of the prompt cap.
        self.assertIn('use Monitor', text)
        self.assertLess(text.index('## Claims of absence'), text.index('## Watchers'))
        self.assertLess(text.index('## Watchers'), text.index('## New tabs and reviewers'))
        self.assertIn('not evidence of absence', text)
        self.assertLess(text.index('## Naming tabs'), text.index('## Claims of absence'))
        self.assertLess(text.index('## Claims of absence'), text.index('## New tabs and reviewers'))
        self.assertLess(text.index('## Tab icon'), text.index('## New tabs and reviewers'))
        self.assertLess(text.index('## Tab icon'), text.index('## Naming tabs'))
        self.assertLess(text.index('## Naming tabs'), text.index('## New tabs and reviewers'))
        self.assertLess(text.index('## Notes'), text.index('## Tab icon'))
        self.assertIn('Use your own subagents', text[text.index('## New tabs and reviewers'):text.index('## Backlog')])
        self.assertIn(str(Path(projects.__file__).with_name('agent_tab_cli.py')), text)
        for option in ('--review-it', '--reviews-me', '--agent claude|codex', '--name NAME',
                       '--cwd DIR', '--worktree BRANCH', '--context', '--owner self|%N', 'stop --worker %N [--reviewer %N]', '`list`',
                       '`escalate --pane %N` sends a helper tab\'s paused review to Mak.',
                       '`link --worker %N --reviewer %N` makes one open tab review another.', 'Start your review reply with APPROVED when nothing is wrong.',
                       'send --pane %N "text"', 'wait --pane %N [--timeout SECONDS]',
                       'max 600', '3 timeout (still working)', '4 gone', '5 needs_input (with prompt), 6 no_turn (none in 60 s; message may be lost).'):
            self.assertIn(option, text)
        self.assertIn(
            'not raw tmux. Options: `--review-it` '
            '(this tab reviews each turn of the new tab), '
            '`--reviews-me` (the new tab reviews this tab), ', text)
        self.assertIn("`--stack GOAL` (1–3 words; names the new tab. A helper opens in its owner's stack; `--stack Unstacked` = none)", text)
        self.assertIn(
            'Start your review reply with APPROVED when nothing is wrong. '
            '`send --pane %N "text"` (`-` = stdin) steers a tab you started. '
            '`wait --pane %N [--timeout SECONDS]` (max 600) prints its new reply as JSON. '
            'Exit: 0 finished, 3 timeout (still working), 4 gone, 5 needs_input (with prompt), 6 no_turn (none in 60 s; message may be lost). '
            'When a tab you started or review is done, save a note in notes/ (what it did, outcome, findings), '
            'then run `close --pane %N`. Busy tabs stay open.', text)
        self.assertIn('Asked to close this tab: run `close --self`', text)
        self.assertFalse(projects.has_context(PID, self.home))
        state = projects.state(PID, self.home)
        self.assertEqual((state['chars'], state['fixed_chars'], state['revision']), (0, len(text), 0))
        projects.set_name(PID, 'web')
        projects.mark_seen(PID)
        self.assertFalse(projects.bundle(PID).exists())

    def test_backlog_guidance_defers_to_autonomy(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                text = projects.render(PID, agent)
                backlog_section = text.split('\n## Backlog\n', 1)[1].split('\n## Tether tools\n', 1)[0]
                self.assertIn(
                    '- Work on an item when the user asks, or when your autonomy level tells you to work the backlog. '
                    'Run `start` first. When finished, run `done` with the commit, PR, or outcome.',
                    backlog_section.splitlines(),
                )
                handoff = next(line for line in backlog_section.splitlines()
                               if line.startswith('- An item that is not the task this tab was started for goes to a new tab'))
                for part in ('at every autonomy level, also mid-work', 'handoff note in the work-notes folder',
                             'what not to redo', '`agent_tab_cli.py start --owner self --name "B12 two to four words" --cwd ITEM_REPO "TASK"`',
                             'run `start B12` first', 'report the new tab name', 'Hand off one item, then stop.'):
                    self.assertIn(part, handoff)

    def test_tether_tools_guidance_is_separate_from_backlog(self):
        reminder_cli = Path(projects.__file__).resolve().with_name('agent_reminder_cli.py')
        command = shlex.join((sys.executable, str(reminder_cli)))
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                text = projects.render(PID, agent, home=self.home)
                backlog_text, heading, tools = text.partition('\n## Tether tools\n')
                self.assertTrue(heading)
                self.assertNotIn('agent_reminder_cli.py', backlog_text.split('\n## Backlog\n', 1)[1])
                self.assertIn(f'`{command} add "text" --at "YYYY-MM-DD HH:MM"`', tools)
                self.assertIn('`tether tools`', tools)

    def test_instructions_count_revisions_and_refuse_a_stale_save(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        self.assertEqual(projects.meta(PID)['revision'], 1)
        with self.assertRaises(projects.ProjectError) as stale:
            projects.set_instructions(PID, 'Use npm.', 0)
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(projects.instructions(PID), 'Use pnpm.\n')
        text = projects.render(PID)
        self.assertIn('## Instructions\nUse pnpm.', text)
        self.assertIn('Project guidance from Tether', text)

    def test_small_budget_cuts_instructions_and_keeps_all_builtin_guidance(self):
        projects.set_instructions(PID, 'Use pnpm.', 0)
        projects.set_name(PID, 'web')
        full = projects.render(PID)
        projects.set_budget(PID, 1000, 1)
        instructions = 'Use pnpm.\n' * 1000 + 'End of instructions.'
        projects.set_instructions(PID, instructions, 2)
        state = projects.state(PID, self.home)
        preview = state['preview']
        intro, rules = full.split('\n## Instructions\nUse pnpm.\n')
        self.assertTrue(preview.startswith(intro))
        self.assertTrue(preview.endswith(rules))
        self.assertIn('\n## Instructions\nUse pnpm.\n', preview)
        self.assertIn('Cut to fit', preview)
        self.assertNotIn('End of instructions.', preview)
        self.assertLessEqual(state['chars'], 1000)
        self.assertEqual(state['chars'] + state['fixed_chars'], len(preview))
        self.assertEqual(state['budget'], 1000)
        self.assertEqual(projects.instructions(PID), instructions + '\n')

    def test_full_prompt_stays_under_the_cap_with_the_largest_budget(self):
        projects.set_instructions(PID, 'Use pnpm.\n' * 1000, 0)
        projects.set_budget(PID, 10000, 1)
        state = projects.state(PID, self.home)
        self.assertLessEqual(len(state['preview']), 10000)
        self.assertGreater(len(state['preview']), 9900)
        self.assertIn('Cut to fit', state['preview'])
        self.assertIn('\n## Backlog\n', state['preview'])
        self.assertEqual(projects.meta(PID)['budget'], 10000)
        with self.assertRaises(projects.ProjectError):
            projects.set_budget(PID, 50000, 2)

    def test_default_budget_keeps_notes_and_instructions_with_long_paths(self):
        short_tether = paths.TETHER
        # Fixed paths of a typical install, so the real checkout and interpreter paths of the test run do not count (B291).
        short_checkout, short_python = Path('/srv/user/apps/tether'), Path('/usr/bin/python3')
        # About 150 more characters per path, 7 path copies in all. The guidance stays under BUDGET_MAX - BUDGET,
        # so the default budget must stay whole. Before 8127a1b the guidance used the budget and cut the notes.
        long_tether = short_tether / ('state-' + 'x' * 144)
        long_checkout = short_checkout / ('checkout-' + 'x' * 141)
        long_python = short_python.parent / ('python-' + 'x' * 143) / short_python.name
        stack = {'pid': 'b1b2c3d4e5f6', 'name': 'Docs'}
        instructions = 'Use pnpm.'
        project_note = '# Setup\nRun make.'
        stack_note = '# Chart\nUse the bar chart.'

        for tether in (short_tether, long_tether):
            with mock.patch.object(paths, 'TETHER', tether):
                projects.set_instructions(PID, instructions, 0)
                projects.write_note(PID, 'setup.md', project_note)
                projects.write_note(stack['pid'], 'chart.md', stack_note)

        for group in (None, stack):
            with self.subTest(group=group):
                rendered = {}
                for name, tether, checkout, python in (('short', short_tether, short_checkout, short_python),
                                                       ('long', long_tether, long_checkout, long_python)):
                    with (
                        mock.patch.object(projects, '__file__', str(checkout / 'server/projects.py')),
                        mock.patch.object(sys, 'executable', str(python)),
                        mock.patch.object(paths, 'TETHER', tether),
                    ):
                        rendered[name] = projects.render(PID, home=self.home, group=group)
                short, long = rendered['short'], rendered['long']

                self.assertNotIn('Cut to fit', long)
                self.assertNotIn('Cut to fit', short)
                self.assertIn(f'\n## Instructions\n{instructions}\n', short)
                self.assertIn(f'\n#### setup.md\n{project_note}\n', short)
                if group:
                    self.assertIn(f'\n#### chart.md\n{stack_note}\n', short)
                normalized = long.replace(str(long_tether), str(short_tether))
                normalized = normalized.replace(str(long_checkout), str(short_checkout))
                normalized = normalized.replace(str(long_python), str(short_python))
                self.assertEqual(normalized, short)

    def _content(self, group=None):
        """The rendered text and the part of it that uses the project budget."""
        prompt, fixed = projects._render(PID, projects.meta(PID), self.home, group)
        return prompt, len(prompt) - fixed

    def test_long_instructions_leave_room_for_the_stack_identity(self):
        opening = 'Use the shared test fixtures.\n'
        projects.set_instructions(PID, opening + ('x\n' * 10000)[:20000 - len(opening)], 0)
        projects.set_instructions(STACK['pid'], 'Stack rule.', 0)
        for budget in (1000, 4000):
            with self.subTest(budget=budget):
                projects.set_budget(PID, budget, projects.meta(PID)['revision'])
                text, used = self._content(STACK)
                self.assertLessEqual(used, budget)
                self.assertIn('## Instructions\n' + opening, text)
                self.assertIn('Cut to fit', text)
                self.assertIn('## Stack: Docs\n', text)
                self.assertIn(f'Stack folder: {projects.bundle(STACK["pid"])}\n', text)
                self.assertIn(f'{projects.bundle(PID)}/notes/', text)
                self.assertIn('## Backlog\n', text)  # Tether's rules stay whole

    def test_large_budget_keeps_the_heading_order(self):
        projects.set_budget(PID, 10000, 0)
        projects.set_instructions(PID, 'Project rule.', 1)
        projects.set_instructions(STACK['pid'], 'Stack rule.', 0)
        (self.home / 'spec.md').write_text('Spec')
        projects.pin(PID, '~/spec.md', 'link', 2, self.home)
        projects.pin(STACK['pid'], '~/spec.md', 'link', 1, self.home)
        text = projects.render(PID, home=self.home, group=STACK)
        self.assertNotIn('Cut to fit', text)
        self.assertEqual([line for line in text.splitlines() if line.startswith('#')], [
            '# Tether project', '## Notes', '## Instructions', '## Pinned files',
            '## Stack: Docs', '### Stack instructions', '### Stack pinned files',
            '## Tab icon', '## Naming tabs', '## Claims of absence', '## Watchers', '## New tabs and reviewers', '## Backlog',
            '## Tether tools',
        ])

    def test_state_reports_the_usable_maximum_without_rewriting_the_budget(self):
        for saved in (4000, 8000):
            with self.subTest(saved=saved):
                projects.set_budget(PID, saved, projects.meta(PID)['revision'])
                metadata = projects.bundle(PID) / 'project.json'
                before = metadata.read_bytes()
                state = projects.state(PID, self.home)
                self.assertEqual(state['usable_budget_max'], projects.BUDGET_MAX - state['fixed_chars'])
                self.assertEqual(state['budget'], saved)
                self.assertEqual(metadata.read_bytes(), before)

    def test_state_keeps_the_usable_maximum_at_least_the_minimum(self):
        fixed_chars = len(projects.render(PID, home=self.home))
        with mock.patch.object(projects, 'BUDGET_MAX', fixed_chars + 500):
            state = projects.state(PID, self.home)
        self.assertEqual(state['usable_budget_max'], projects.BUDGET_MIN)
        self.assertLessEqual(len(state['preview']), fixed_chars + 500)

    def test_pins_link_and_copy_and_refuse_secrets(self):
        (self.home / 'docs').mkdir()
        (self.home / 'docs/spec.md').write_text('spec')
        (self.home / '.env').write_text('TOKEN=1')
        projects.pin(PID, '~/docs/spec.md', 'link', 0, self.home)
        projects.pin(PID, '~/docs/spec.md', 'copy', 1, self.home)
        with self.assertRaises(projects.ProjectError):
            projects.pin(PID, '~/docs/spec.md', 'link', 2, self.home)
        with self.assertRaises(projects.ProjectError) as secret:
            projects.pin(PID, '~/.env', 'copy', 2, self.home)
        self.assertEqual(secret.exception.status, 403)
        with self.assertRaises(projects.ProjectError):
            projects.pin(PID, '/etc/passwd', 'link', 2, self.home)
        found = projects.pins(PID, self.home)
        self.assertEqual([(item['kind'], item['name']) for item in found], [('copy', 'spec 2.md'), ('link', 'spec.md')])
        self.assertIn('- spec.md: ~/docs/spec.md', projects.render(PID, home=self.home))
        (self.home / 'docs/spec.md').unlink()
        self.assertTrue(projects.pins(PID, self.home)[1]['missing'])
        projects.unpin(PID, 'spec.md', 2)
        projects.unpin(PID, 'spec 2.md', 3)
        self.assertEqual(projects.pins(PID, self.home), [])
        with self.assertRaises(projects.ProjectError):
            projects.unpin(PID, '../project.json', 4)

    def test_notes_list_new_badge_and_soft_delete(self):
        projects.write_note(PID, 'MEMORY.md', '- [Build](build.md)\n')
        projects.write_note(PID, 'build.md', '---\nname: build\ndescription: How to build\n---\nRun make.\n')
        names = {item['name']: item for item in projects.notes(PID)}
        self.assertEqual(names['build.md']['title'], 'build')
        self.assertTrue(names['build.md']['new'])
        projects.mark_seen(PID)
        self.assertFalse(any(item['new'] for item in projects.notes(PID)))
        claude, codex = projects.render(PID, 'claude'), projects.render(PID, 'codex')
        self.assertEqual(claude, codex)
        self.assertIn('\n#### MEMORY.md\n', claude)
        self.assertIn('Run make.', claude)
        self.assertIn('Keep your own memory as before.', claude)
        projects.delete_note(PID, 'build.md')
        self.assertEqual([item['name'] for item in projects.notes(PID)], ['MEMORY.md'])
        self.assertEqual(len(list((projects.bundle(PID) / 'notes/.trash').iterdir())), 1)
        for bad in ('../x.md', 'a/b.md', 'x.txt', '.hidden.md'):
            with self.assertRaises(projects.ProjectError):
                projects.write_note(PID, bad, 'x')

    def test_only_five_newest_notes_have_text(self):
        for at in range(7):
            projects.write_note(PID, f'n{at}.md', f'# Note {at}\nbody {at}\n')
            os.utime(projects.bundle(PID) / f'notes/n{at}.md', (time.time() + at, time.time() + at))
        text = projects.render(PID, 'codex')
        self.assertEqual(text.count('\n#### n'), 5)
        self.assertIn('- n0.md: Note 0', text)
        self.assertNotIn('body 0', text)

    def test_a_cut_with_little_room_keeps_the_heading_line(self):
        note = '\n[Cut to fit the project size limit. The full text is in the project folder.]\n'
        for heading in ('\n### Project notes\n', '\n### Stack notes\n', '\n## Instructions\n'):
            with self.subTest(heading=heading):
                part = heading + '\n#### a.md\n' + 'body\n' * 200
                for room in (len(heading) + len(note), 150, 200):
                    fitted = projects._fit([((1, 0), 'x' * 100), ((4, 0), part), ((5, 0), 'later')], 100 + room)
                    self.assertEqual(fitted, ['x' * 100, heading + note, ''])
        # No heading, or no room for it: the marker alone still says that text was cut.
        self.assertEqual(projects._fit([((4, 0), 'plain\n' + 'body\n' * 100)], 150), [note])
        self.assertEqual(projects._fit([((4, 0), '\n### Project notes\n' + 'body\n' * 100)], len(note) + 5), [note])

    def test_default_budget_fits_all_five_excerpts_before_the_older_notes_list(self):
        for at in range(30):
            projects.write_note(PID, f'n{at}.md', f'# Note {at}\n' + f'body {at} ' * 100)
            mtime = 1700000000 + at
            os.utime(projects.bundle(PID) / f'notes/n{at}.md', (mtime, mtime))
        items = [f'B{at} ' + 'Backlog item. ' * 7 for at in range(5)]
        with mock.patch('backlog.summary', return_value=(items, len(items))):
            text = projects.render(PID, home=self.home, backlog_project='tether')
        self.assertLess(text.index('## Backlog'), text.index('### Open backlog items (5)'))
        self.assertLess(text.index('### Open backlog items (5)'), text.index('## Tether tools'))
        for at in range(25, 30):
            self.assertIn(f'\n#### n{at}.md\n# Note {at}\nbody {at} ', text)
        self.assertIn('[Cut to fit the project size limit.', text)
        self.assertLessEqual(len(text) - projects.state(PID, self.home)['fixed_chars'], projects.BUDGET)

    def test_default_budget_fits_instructions_pin_five_excerpts_and_two_older_titles(self):
        instructions = 'Use the project notes. ' * 9
        projects.set_instructions(PID, instructions, 0)
        (self.home / 'guide.md').write_text('Guide')
        projects.pin(PID, '~/guide.md', 'link', 1, self.home)
        for at in range(7):
            projects.write_note(PID, f'n{at}.md', f'# Note {at}\n' + f'body {at} ' * 100)
            mtime = 1700000000 + at
            os.utime(projects.bundle(PID) / f'notes/n{at}.md', (mtime, mtime))
        text = projects.render(PID, home=self.home)
        self.assertIn(f'\n## Instructions\n{instructions.strip()}\n', text)
        self.assertIn('\n- guide.md: ~/guide.md\n', text)
        self.assertIn('\n### Project notes\n\n#### n6.md\n', text)
        for at in range(2, 7):
            self.assertIn(f'\n#### n{at}.md\n# Note {at}\nbody {at} ', text)
        self.assertTrue(text.endswith('\nOlder notes:\n- n1.md: Note 1\n- n0.md: Note 0\n'))

    def test_long_instructions_win_the_default_budget_over_note_excerpts(self):
        opening = 'Use the project guide.\n'
        instructions = opening + 'x' * (20000 - len(opening))
        projects.set_instructions(PID, instructions, 0)
        projects.write_note(PID, 'setup.md', '# Setup\nRun make.')
        text = projects.render(PID, home=self.home)
        self.assertIn(f'\n## Instructions\n{opening}[Cut to fit the project size limit.', text)
        self.assertNotIn(instructions, text)
        self.assertNotIn('\n#### setup.md\n', text)

    def test_archive_moves_the_bundle(self):
        projects.set_instructions(PID, 'x', 0)
        projects.archive(PID)
        self.assertFalse(projects.bundle(PID).exists())
        self.assertEqual(len(list((projects.root() / '.archive').iterdir())), 1)

    def test_marks_show_context_and_stale_panes(self):
        other = 'b1b2c3d4e5f6'
        projects.set_instructions(PID, 'Use pnpm.', 0)
        lines = (f'$3\t{PID}\t%1\t{PID}:1\n$3\t{PID}\t%2\t{PID}:0\n$3\t{PID}\t%3\t\n'
                 f'$3\t{PID}\t%4\t{other}:1\n$4\t{other}\t%5\t\n$5\t\t%6\t\n')
        run = lambda args, **_: SimpleNamespace(returncode=0, stdout=lines, stderr='')
        found = projects.marks(run, self.home)
        self.assertEqual(found['$3'], {'project': PID, 'revision': 1, 'stale': ['%2', '%4'], 'context': True})
        self.assertEqual(found['$4']['context'], False)
        self.assertNotIn('$5', found)
        self.assertFalse(projects.bundle(other).exists())
        self.assertEqual(projects.marks(lambda args, **_: SimpleNamespace(returncode=1, stdout='', stderr='')), {})

    def test_api_routes(self):
        run = Run()
        handler = Handler('/mobile/api/projects/project?session=%243')
        projects_api.get(handler, run, self.home)
        self.assertEqual(handler.status, 200, handler.body)
        pid = handler.body['id']
        save = Handler('/mobile/api/projects/instructions', {'project': pid, 'text': 'Hi', 'revision': 0, 'session_name': 'web'})
        projects_api.post(save, self.home)
        self.assertEqual((save.status, save.body['revision'], save.body['chars'] > 0), (200, 1, True))
        stale = Handler('/mobile/api/projects/instructions', {'project': pid, 'text': 'Bye', 'revision': 0})
        projects_api.post(stale, self.home)
        self.assertEqual(stale.status, 409)
        bad = Handler('/mobile/api/projects/instructions', {'project': '../../etc', 'text': 'x', 'revision': 1})
        projects_api.post(bad, self.home)
        self.assertEqual(bad.status, 400)
        missing = Handler('/mobile/api/projects/nope', {'project': pid})
        projects_api.post(missing, self.home)
        self.assertEqual(missing.status, 404)
        self.assertEqual(json.loads((projects.bundle(pid) / 'project.json').read_text())['name'], 'web')


    def test_advisor_log_routes_merge_older_lines_and_check_ids(self):
        folder = projects.bundle(PID)
        folder.mkdir(parents=True)
        entry = advisor_log.make('Which cache?\nSecond line', {'status': 'ask_user', 'answer': 'Ask the user.\n' + 'x' * 600,
                                                                 'evidence': 'docs/cache.md'}, 'claude', 'fable', now=time.time())
        older = ['- 2026-01-02 03:04 [decided] Q: Old question -> A: Old answer (codex gpt)',
                 '- 2026-01-02 03:04 [error] Q: Same minute -> A:  (claude fable)',
                 'not a log line', '- 2026-01-02 03:05 [weird] Q: Bad status -> A: x (claude fable)']
        (folder / 'advisor-log.md').write_text('\n'.join(older + [advisor_log.line(entry)]) + '\n')
        (folder / 'advisor-log.jsonl').write_text(json.dumps(entry) + '\n')

        listed = Handler(f'/mobile/api/projects/advisor-log?project={PID}')
        projects_api.get(listed, Run())
        self.assertEqual(listed.status, 200, listed.body)
        rows = listed.body['entries']
        self.assertEqual([(row['status'], row['full']) for row in rows], [('ask_user', True), ('error', False), ('decided', False)])
        self.assertEqual(rows[0]['question'], 'Which cache? Second line')
        self.assertEqual(len(rows[0]['answer']), advisor_log.PREVIEW_ANSWER)
        self.assertEqual((rows[2]['question'], rows[2]['answer'], rows[2]['agent'], rows[2]['model'], rows[2]['id']),
                         ('Old question', 'Old answer', 'codex', 'gpt', ''))
        self.assertEqual(listed.body['keep'], 200)

        one = Handler(f'/mobile/api/projects/advisor-entry?project={PID}&id={entry["id"]}')
        projects_api.get(one, Run())
        self.assertEqual((one.status, one.body), (200, dict(entry, limit=advisor_log.FULL)))
        del entry['cut']  # an entry written before the cut list
        (folder / 'advisor-log.jsonl').write_text(json.dumps(entry) + '\n')
        older_entry = Handler(f'/mobile/api/projects/advisor-entry?project={PID}&id={entry["id"]}')
        projects_api.get(older_entry, Run())
        self.assertEqual(older_entry.body['cut'], [])
        for path, status in ((f'advisor-entry?project={PID}&id=0123456789ab', 404), (f'advisor-entry?project={PID}&id=..%2Fx', 404),
                             ('advisor-log?project=..%2F..%2Fetc', 400), ('advisor-entry?id=0123456789ab', 400)):
            bad = Handler('/mobile/api/projects/' + path)
            projects_api.get(bad, Run())
            self.assertEqual(bad.status, status, path)

    def test_advisor_log_is_empty_without_files_and_ignores_links(self):
        empty = Handler(f'/mobile/api/projects/advisor-log?project={PID}')
        projects_api.get(empty, Run())
        self.assertEqual((empty.status, empty.body['entries']), (200, []))
        folder = projects.bundle(PID)
        folder.mkdir(parents=True)
        secret = Path(self.home) / 'secret.md'
        secret.write_text('- 2026-01-02 03:04 [decided] Q: private -> A: private (claude fable)\n')
        (folder / 'advisor-log.md').symlink_to(secret)
        linked = Handler(f'/mobile/api/projects/advisor-log?project={PID}')
        projects_api.get(linked, Run())
        self.assertEqual(linked.body['entries'], [])

    def test_advisor_log_route_answers_when_a_line_is_hostile(self):
        folder = projects.bundle(PID)
        folder.mkdir(parents=True)
        entry = advisor_log.make('Which cache?', {'status': 'decided', 'answer': 'File.'}, 'claude', 'fable', now=time.time())
        huge = dict(entry, id='aaaaaaaaaaaa', time=10 ** 100)
        (folder / 'advisor-log.jsonl').write_text('\n'.join([json.dumps(huge), '[' * 20000 + ']' * 20000, json.dumps(entry)]) + '\n')
        listed = Handler(f'/mobile/api/projects/advisor-log?project={PID}')
        projects_api.get(listed, Run())
        self.assertEqual((listed.status, [row['id'] for row in listed.body['entries']]), (200, [entry['id']]))

if __name__ == '__main__':
    unittest.main()
