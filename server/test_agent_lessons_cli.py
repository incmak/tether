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
        self.values = {'#{@project}': PID, '#{@tether_helper}': '', '#{pane_current_command}': 'codex', '#{@chat_name}': 'Review'}
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

    def test_quote_tab_pane_dispute_and_independent_reinforcement(self):
        args = ['add', '--avoid', '--quote', '-', 'When tests fail, inspect the exact error.']
        code, result = self.invoke(args)
        row = result['lesson']
        self.assertEqual((code, row['quote'], row['tab'], row['first_pane']), (0, '-', 'Review', '%7'))
        identifier = row['id']
        self.assertEqual(self.invoke(['reinforce', identifier])[1]['lesson']['status'], 'trial')
        self.assertEqual(self.invoke(['dispute', identifier, 'Tests show a different result.'])[1]['status'], 'disputed')
        self.assertEqual(self.invoke(['dispute', identifier, 'Another reason.']),
                         (2, {'error': 'This tab already disputed this lesson.'}))
        with patch.object(cli, 'context', return_value=(PID, 'codex', '%8', 'Other tab')):
            self.assertEqual(self.invoke(['reinforce', identifier])[1]['lesson']['status'], 'confirmed')
        self.assertTrue(lessons.entries(PID)[0]['disputed'])

    def test_helper_refuses_every_write(self):
        self.values['#{@tether_helper}'] = 'review'
        for args in (['add', '--keep', 'When tests pass, keep the output.'], ['reinforce', 'abcd1234'],
                     ['propose-global', 'abcd1234'], ['dispute', 'abcd1234', 'Evidence differs.']):
            self.assertEqual(self.invoke(args), (2, {'error': 'Helper tabs cannot record lessons.'}))
        self.assertFalse(lessons.path(PID).exists())

    def test_missing_tab_name_falls_back_to_empty(self):
        def unnamed(command, **options):
            if command[-1] == '#{@chat_name}':
                return SimpleNamespace(returncode=1, stdout='')
            return self.process(command, **options)
        self.run.side_effect = unnamed
        row = self.invoke(['add', '--keep', 'When checks pass, keep the output.'])[1]['lesson']
        self.assertEqual(row['tab'], '')
