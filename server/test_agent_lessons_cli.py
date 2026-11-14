"""The agent CLI uses fake pane metadata and isolated lesson files."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent_lessons_cli as cli
import lessons
import paths

PID = 'abcdef012345'


class Cli(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('CONFIG', 'TETHER'):
            self.enterContext(patch.object(paths, name, root / name.lower()))
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, TMUX_PANE='%7'))
        self.values = {'#{@project}': PID, '#{@tether_helper}': '', '#{pane_current_command}': 'codex'}
        self.run = self.enterContext(patch.object(cli.subprocess, 'run', side_effect=self.process))

    def process(self, command, **options):
        self.assertEqual(command[:5], [paths.TMUX, 'display-message', '-p', '-t', '%7'])
        self.assertEqual(options, dict(capture_output=True, text=True, timeout=5))
        return SimpleNamespace(returncode=0, stdout=self.values[command[-1]])

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(args)
        return code, json.loads(output.getvalue())

    def test_add_similar_new_reinforce_and_propose(self):
        args = ['add', '--avoid', 'When checks fail, read the exact error.']
        code, result = self.invoke(args)
        self.assertEqual((code, result['status'], result['lesson']['agent']), (0, 'added', 'codex'))
        identifier = result['lesson']['id']
        self.assertEqual(self.invoke(args)[0], 9)
        self.assertEqual(self.invoke(args[:2] + ['--new'] + args[2:])[0], 0)
        self.assertEqual(self.invoke(['reinforce', identifier])[1]['lesson']['hits'], 2)
        self.assertTrue(self.invoke(['propose-global', identifier])[1]['lesson']['proposed_global'])
        self.assertEqual(len(self.invoke(['list'])[1]['lessons']), 2)
        self.values['#{pane_current_command}'] = 'claude'
        code, result = self.invoke(['add', '--keep', '--source', 'reviewer', 'When checks pass, keep the output.'])
        self.assertEqual((code, result['lesson']['agent'], result['lesson']['source']), (0, 'claude', 'reviewer'))
        self.values['#{pane_current_command}'] = 'python'
        with patch.object(cli.sys, 'stdin', io.StringIO('When widths shrink, wrap labels.')):
            self.assertEqual(self.invoke(['add', '--keep', '-'])[1]['lesson']['agent'], '')

    def test_helper_off_no_project_and_bad_pane(self):
        self.values['#{@tether_helper}'] = '{}'
        self.assertEqual(self.invoke(['list'])[0], 2)
        self.assertFalse(lessons.path(PID).exists())
        self.values['#{@tether_helper}'] = ''
        lessons.set_enabled(False)
        self.assertEqual(self.invoke(['list']), (8, {'status': 'off'}))
        lessons.set_enabled(True)
        for pid in ('', '../x', '000000000000'):
            self.values['#{@project}'] = pid
            self.assertEqual(self.invoke(['list'])[0], 2)
        with patch.dict(os.environ, TMUX_PANE=''):
            self.assertEqual(self.invoke(['list'])[0], 2)

    def test_invalid_arguments_ids_and_tmux_errors(self):
        for args in (['remove', 'abcd1234'], ['edit', 'abcd1234'], ['add', '--avoid', 'short'],
                     ['add', '--avoid', '--source', 'agent', 'When tests fail, inspect output.'],
                     ['reinforce', 'bad'], ['reinforce', 'abcd1234'], ['add']):
            self.assertEqual(self.invoke(args)[0], 2)
        for error in (OSError('missing'), subprocess.TimeoutExpired('tmux', 5)):
            self.run.side_effect = error
            self.assertEqual(self.invoke(['list'])[0], 2)
