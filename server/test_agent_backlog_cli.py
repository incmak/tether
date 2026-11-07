"""The agent command: no live tmux or agent. The fake tmux fails, as it does in Codex's sandbox, unless a test says otherwise."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_backlog_cli as cli
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
        self.assertEqual(self.text(), '## tether\n- [ ] One (codex · tmux $7 · pane %12 · chat codex-thread)\n'
                                      '- [ ] Two (claude · tmux $7 · pane %12 · chat claude-chat)\n')

    def test_list_detail_and_done(self):
        self.run_cli('add', '--agent', 'codex', 'Fix login')
        self.assertEqual(self.run_cli('detail', 'login', 'Happens on phones')[0], 0)
        code, out, _ = self.run_cli('list')
        self.assertEqual(code, 0)
        self.assertIn('tether:\n- [ ] Fix login', out)
        self.assertIn('  - Happens on phones', out)
        self.assertEqual(self.run_cli('done', 'login')[0], 0)
        self.assertEqual(self.run_cli('list')[1].strip(), 'No open items.')

    def test_a_clear_error_for_no_match(self):
        code, _, err = self.run_cli('done', 'nothing')
        self.assertEqual(code, 1)
        self.assertIn('No open item matches', err)

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
        self.assertEqual(cli.project_name(self.root / 'tether' / 'missing'), 'missing')

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
