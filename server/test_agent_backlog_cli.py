"""The agent command: no live tmux or agent. The fake tmux fails, as it does in Codex's sandbox, unless a test says otherwise."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_backlog_cli as cli
import audit
import backlog
import paths

ENV = {'TMUX': '/tmp/invented/default,123,7', 'TMUX_PANE': '%12', 'CLAUDE_CODE_SESSION_ID': 'claude-chat', 'CODEX_THREAD_ID': 'codex-thread'}


class BacklogCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'home'
        self.repo = self.home / 'code' / 'tether'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / 'server').mkdir()
        for patch in (mock.patch.object(paths, 'TETHER', self.root / 'tether'), mock.patch.object(paths, 'HOME', self.home),
                      mock.patch.object(paths, 'CONFIG', self.root / 'config'),
                      mock.patch.dict(os.environ, ENV, clear=True), mock.patch.object(cli.subprocess, 'run', side_effect=OSError('blocked'))):
            patch.start()
            self.addCleanup(patch.stop)
        here = os.getcwd()
        os.chdir(self.repo / 'server')
        self.addCleanup(os.chdir, here)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def text(self):
        return (backlog.folder() / 'BACKLOG.md').read_text()

    def test_each_agent_records_its_own_chat_under_the_repository(self):
        self.assertEqual(self.run_cli('add', '--agent', 'codex', 'One')[0], 0)
        self.assertEqual(self.run_cli('add', '--agent', 'claude', 'Two')[0], 0)
        self.assertEqual(self.text(), '## tether\n- [ ] One (codex · tmux $7 · pane %12 · chat codex-thread) #B1\n'
                                      '- [ ] Two (claude · tmux $7 · pane %12 · chat claude-chat) #B2\n')

    def test_direct_cli_audit_uses_the_calling_pane(self):
        self.enterContext(mock.patch.object(audit, '_TABS', {}))
        self.assertEqual(self.run_cli('add', 'Fix login')[0], 0)
        entry, = audit.query()['entries']
        self.assertEqual(entry['kind'], 'backlog.add')
        self.assertEqual(entry['actor'], {'source': 'agent-cli', 'pane': '%12'})
        self.assertEqual(entry['project'], 'tether')

    def test_list_detail_and_done(self):
        self.run_cli('add', '--agent', 'codex', 'Fix login')
        self.assertEqual(self.run_cli('detail', 'login', 'Happens on phones')[0], 0)
        code, out, _ = self.run_cli('list', '--details')
        self.assertEqual(code, 0)
        self.assertIn('tether:\n- [ ] Fix login', out)
        self.assertIn('  - Happens on phones', out)
        self.assertEqual(self.run_cli('done', 'login'), (0, 'Done: B1 Fix login\n', ''))
        self.assertEqual(self.run_cli('list')[1].strip(), 'No open items.')

    def test_list_is_one_line_for_each_item_and_show_has_the_rest(self):
        """B74: the detail lines and who added an item were most of `list`. They stay one command away."""
        self.assertEqual(self.run_cli('add', '--agent', 'codex', 'Fix login', '--detail', 'Happens on phones', '--detail', 'See auth.py')[1],
                         'Added: B1 Fix login\n')
        self.run_cli('add', '--agent', 'claude', 'Plain one')
        self.assertEqual(self.run_cli('list'), (0, 'tether:\n- B1 Fix login [2 detail lines]\n- B2 Plain one\n\n'
                                                   'The detail lines of an item: show B12.\n', ''))
        whole = ('- [ ] Fix login (codex · tmux $7 · pane %12 · chat codex-thread) #B1\n  - Happens on phones\n  - See auth.py\n')
        self.assertEqual(self.run_cli('show', 'B1'), (0, whole, ''))
        self.assertEqual(self.run_cli('show', 'login')[1], whole)
        # start hands the tab the details of its item; list then names who has it, without the chat or the tmux session
        self.assertEqual(self.run_cli('start', 'B1', '--agent', 'claude')[1], 'Started: B1 Fix login\n  - Happens on phones\n  - See auth.py\n')
        self.assertEqual(self.run_cli('start', 'B2', '--agent', 'claude')[1], 'Started: B2 Plain one\n')
        self.assertIn('- B1 Fix login [doing: claude · pane %12] [2 detail lines]\n- B2 Plain one [doing: claude · pane %12]\n', self.run_cli('list')[1])
        self.assertEqual(self.run_cli('reword', 'B2', 'Plainer')[1], 'Reworded: B2 Plainer\n')
        self.assertEqual(self.run_cli('move', 'B2', 'other')[1], 'Moved to other: B2 Plainer\n')
        # no detail lines anywhere: no hint. A finished item still shows, by ID; nothing matches: a clear error.
        self.run_cli('done', 'B1', 'fixed')
        self.run_cli('add', 'Third')
        self.assertEqual(self.run_cli('list')[1], 'tether:\n- B3 Third\n')
        self.assertIn('- [x] Fix login', self.run_cli('show', 'B1')[1])
        code, out, err = self.run_cli('show', 'B99')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('No item B99', err)

    def test_start_prints_the_details_of_an_item_that_has_no_id_yet(self):
        """An item typed into the file gets its ID when the file is next read. start must not need the ID."""
        backlog.folder().mkdir(parents=True)
        (backlog.folder() / 'BACKLOG.md').write_text('- [ ] Typed by hand\n  - Doing: codex · pane %1\n  - its why\n\n  - after a blank line\n- [ ] Other\n  - not this\n')
        for query in ('Typed by hand', 'typed'):
            code, out, _ = self.run_cli('start', query, '--agent', 'claude')
            self.assertEqual((code, out.split('\n', 1)[1]), (0, '  - its why\n  - after a blank line\n'))
        self.assertIn('- B1 Typed by hand [doing: claude · pane %12] [2 detail lines]\n- B2 Other [1 detail line]', self.run_cli('list')[1])

    def test_a_clear_error_for_no_match(self):
        code, _, err = self.run_cli('done', 'nothing')
        self.assertEqual(code, 1)
        self.assertIn('No open item matches', err)

    def test_archive_reports_a_dry_run_then_moves_the_finished_items(self):
        self.assertEqual(self.run_cli('add', 'Keep')[0], 0)
        self.assertEqual(self.run_cli('add', 'Finish')[0], 0)
        self.assertEqual(self.run_cli('done', 'B2', 'shipped')[0], 0)
        code, out, err = self.run_cli('archive', '--dry-run')
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(out.startswith('Would move 1 finished items to DONE.md.'), out)
        self.assertIn('Finish', backlog.read()['text'])
        code, out, _ = self.run_cli('archive')
        self.assertTrue(out.startswith('Moved 1 finished items'), out)
        self.assertNotIn('Finish', backlog.read()['text'])
        self.assertIn('Finish', (backlog.folder() / 'DONE.md').read_text())

    def test_project_name(self):
        self.assertEqual(cli.project_name(self.repo / 'server'), 'tether')
        self.assertEqual(cli.project_name(self.home), '')
        worktree = self.home / 'lanes' / 'feature'
        worktree.mkdir(parents=True)
        (worktree / '.git').write_text(f'gitdir: {self.repo}/.git/worktrees/feature\n')
        self.assertEqual(cli.project_name(worktree), 'tether')
        plain = self.home / 'notes dir!'
        plain.mkdir()
        self.assertEqual(cli.project_name(plain), 'notes dir')
        self.assertEqual(cli.project_name(self.home / 'gone' / 'missing'), 'missing')

    def test_tmux_answer_wins_over_the_variable_after_a_merge(self):
        with mock.patch.object(cli.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='$9\n')):
            self.assertIn('tmux $9 · pane %12', cli.source('codex'))

    def test_missing_or_odd_values_stay_out(self):
        with mock.patch.dict(os.environ, {'TMUX_PANE': '%12; rm', 'CODEX_THREAD_ID': 'x y)'}):
            self.assertEqual(cli.source('codex'), 'codex')
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(cli.source('claude'), 'claude')

    def test_empty_text_fails_and_writes_nothing(self):
        code, _, err = self.run_cli('add', '--agent', 'codex', '  ')
        self.assertEqual(code, 1)
        self.assertIn('Backlog error', err)
        self.assertFalse((backlog.folder() / 'BACKLOG.md').exists())


if __name__ == '__main__':
    unittest.main()
