import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import paths
import projects
import projects_api

PID = 'a1b2c3d4e5f6'


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
        self.assertIn('@chat_icon', text)  # agents learn to set their tab icon
        # Count the notes and code paths at a typical install length, not the test or CI folder length.
        code = str(Path(projects.__file__).resolve().parent.parent)
        real = text.replace(str(projects.bundle(PID)), f'/srv/user/.tether/projects/{PID}')
        real = real.replace(code, '/srv/user/apps/tether')
        self.assertLess(len(real), 3300)  # +150 subagents-first rule, +150 worker tab for a separate task (2026-11-14)
        self.assertIn('not evidence of absence', text)
        self.assertLess(text.index('## Naming tabs'), text.index('## Claims of absence'))
        self.assertLess(text.index('## Claims of absence'), text.index('## New tabs and reviewers'))
        self.assertLess(text.index('## Tab icon'), text.index('## New tabs and reviewers'))
        self.assertLess(text.index('## Tab icon'), text.index('## Naming tabs'))
        self.assertLess(text.index('## Naming tabs'), text.index('## New tabs and reviewers'))
        self.assertLess(text.index('## New tabs and reviewers'), text.index('## Notes'))
        self.assertIn('Use your own subagents', text[text.index('## New tabs and reviewers'):text.index('## Notes')])
        self.assertIn(str(Path(projects.__file__).with_name('agent_tab_cli.py')), text)
        for option in ('--review-it', '--reviews-me', '--agent claude|codex', '--name NAME',
                       '--cwd DIR', '--worktree BRANCH', '--context', 'stop --worker %N [--reviewer %N]', '`list`',
                       '`link --worker %N --reviewer %N` makes one open tab review another.', 'Start your review reply with APPROVED when nothing is wrong.',
                       'send --pane %N "text"', 'wait --pane %N [--timeout SECONDS]',
                       'max 600', '3 timeout (still working)', '4 gone', '5 needs_input (with prompt), 6 no_turn (none in 60 s; message may be lost).'):
            self.assertIn(option, text)
        self.assertIn(
            'It opens a tab Tether can track. Options: `--review-it` '
            '(this tab reviews each turn of the new tab), '
            '`--reviews-me` (the new tab reviews this tab), ', text)
        self.assertIn(
            'Start your review reply with APPROVED when nothing is wrong. '
            '`send --pane %N "text"` (`-` = stdin) steers a tab you started. '
            '`wait --pane %N [--timeout SECONDS]` (max 600) prints its new reply as JSON. '
            'Exit: 0 finished, 3 timeout (still working), 4 gone, 5 needs_input (with prompt), 6 no_turn (none in 60 s; message may be lost). '
            'When a tab you started or review is done, save a note in notes/ (what it did, outcome, findings), '
            'then run `close --pane %N`. Busy tabs stay open.', text)
        self.assertFalse(projects.has_context(PID, self.home))
        state = projects.state(PID, self.home)
        self.assertEqual((state['chars'] > 0, state['revision']), (True, 0))
        projects.set_name(PID, 'web')
        projects.mark_seen(PID)
        self.assertFalse(projects.bundle(PID).exists())

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

    def test_render_stays_inside_the_budget(self):
        projects.set_instructions(PID, 'x' * 20000, 0)
        self.assertLessEqual(len(projects.render(PID)), projects.BUDGET)
        projects.set_budget(PID, 10000, 1)
        text = projects.render(PID)
        self.assertLessEqual(len(text), 10000)
        self.assertIn('Cut to fit', text)
        with self.assertRaises(projects.ProjectError):
            projects.set_budget(PID, 50000, 2)

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
        self.assertIn('### MEMORY.md', claude)
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
        self.assertEqual(text.count('\n### '), 5)
        self.assertIn('- n0.md: Note 0', text)
        self.assertNotIn('body 0', text)

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


if __name__ == '__main__':
    unittest.main()
