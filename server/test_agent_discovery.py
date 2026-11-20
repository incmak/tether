"""Pane identity follows the outer agent in a mocked process tree."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import agent_tabs
import agents
import helper_tabs
import terminal_input
from test_helper_tabs import HELPER, OTHER, OWNER, Tmux


class AgentDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.processes = {100: (1, 'zsh'), 200: (100, 'claude'), 201: (200, 'bash'), 2: (201, 'claude')}
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0, '%13\t$1\twork\t0.0\t100\t/work\n'))
        self.enterContext(patch.object(agents, 'HOME', self.home))
        self.enterContext(patch.object(agents.pane_view, 'zoom_repair_all'))
        self.enterContext(patch.object(agents.procinfo, 'table', return_value=self.processes))
        self.enterContext(patch.object(agents, 'process_start', side_effect=str))
        self.enterContext(patch.object(agents, 'codex_database', side_effect=self.codex_database))
        self.enterContext(patch.object(agents.codex_provider, 'pane_profile', return_value=None))
        self.enterContext(patch.object(agents, 'codex_names', return_value={}))
        self.enterContext(patch.object(agents.procinfo, 'fd_targets',
                                       side_effect=lambda pid: [('7', '/r/rollout-' + str(pid) + '.jsonl')]))

    def codex_database(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE threads (id, rollout_path, archived, source)')
        db.executemany('INSERT INTO threads VALUES (?, ?, 0, ?)', [
            (HELPER, '/r/rollout-200.jsonl', 'cli'), (OTHER, '/r/rollout-2.jsonl', 'cli')])
        return db

    def registry(self, pid, thread, entrypoint):
        folder = self.home / '.claude/sessions'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{pid}.json'
        path.write_text(json.dumps(dict(pid=pid, sessionId=thread, tmux='/fake/tmux.%13', cwd='/work',
                                        kind='interactive', entrypoint=entrypoint)))
        transcript = self.home / '.claude/projects/-work' / f'{thread}.jsonl'
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()
        return path

    def discover(self, *registries):
        with patch.object(Path, 'glob', return_value=registries):
            entry, = agents.discover({}, self.run)
        return entry

    def test_dead_pane_is_skipped_before_reading_its_pid(self):
        self.run.return_value.stdout = (
            '%14\t$1\twork\t0.1\t101\t/work\t\t\t\t\t\t\t\t1\n'
            '%13\t$1\twork\t0.0\t100\t/work\t123\tFix\tbug\tfeature/fix\tgroup\t\tadvisor\t0\n')
        entry = self.discover()
        self.assertEqual((entry['id'], entry['worktree'], entry['group'], entry['role']),
                         ('%13', 'feature/fix', 'group', 'advisor'))
        self.assertEqual(agents.process_start.call_args_list, [call(100), call(200)])

    def test_missing_pane_pid_does_not_hide_live_pane(self):
        self.run.return_value.stdout = ('%14\t$1\twork\t0.1\t101\t/work\t\t\t\t\t\t\t\t0\n'
                                        + self.run.return_value.stdout)
        self.processes[101] = (1, 'zsh')
        for error in (FileNotFoundError('/proc/101/stat'), ProcessLookupError(101)):
            with self.subTest(error=type(error).__name__):
                agents.process_start.side_effect = [error, '100', '200']
                self.assertEqual(self.discover()['id'], '%13')

    def test_missing_agent_owner_does_not_hide_live_pane(self):
        self.run.return_value.stdout += '%14\t$1\twork\t0.1\t101\t/work\n'
        self.processes[101] = (1, 'zsh')
        registry = self.registry(200, HELPER, 'cli')
        for name, registries in (('claude', (registry,)), ('claude', ()), ('codex', ())):
            with self.subTest(agent=name, registered=bool(registries)):
                self.processes[200] = (100, name)
                agents.process_start.side_effect = ['100', ProcessLookupError(200), '101']
                self.assertEqual(self.discover(*registries)['id'], '%14')

    def test_process_start_permission_error_propagates(self):
        agents.process_start.side_effect = PermissionError('/proc/100/stat')
        with self.assertRaises(PermissionError):
            agents.discover({}, self.run)

    def test_registered_outer_claude_with_another_process_name_keeps_its_identity(self):
        # A Claude process that is not named "claude" (node, a wrapper) still owns the pane through its registry.
        self.processes[200] = (100, 'node')
        outer = self.registry(200, HELPER, 'cli')
        inner = self.registry(2, OTHER, 'sdk-cli')
        for order in ((outer, inner), (inner, outer)):
            with self.subTest(order=[p.name for p in order]):
                entry = self.discover(*order)
                self.assertEqual((entry['agent'], entry['thread'], entry['owner_pid']), ('claude', HELPER, 200))
                self.assertFalse(entry.get('pending_conversation'))

    def test_nested_print_registry_never_owns_the_pane_when_the_outer_registry_is_missing(self):
        # Outer Claude named node, its registry not readable this moment, a nested `claude -p` registry present.
        self.processes[200] = (100, 'node')
        inner = self.registry(2, OTHER, 'sdk-cli')
        entry = self.discover(inner)
        self.assertNotEqual(entry.get('thread'), OTHER)
        self.assertIsNone(helper_tabs.verified_thread(entry))
        owner = dict(id='%12', agent='claude', thread=OWNER, registry='/fake/12.json')
        record = dict(owner_pane='%12', owner_thread=OWNER, helper_thread=HELPER, sent_at=1000)
        with self.assertRaisesRegex(RuntimeError, 'cannot confirm that helper'):
            helper_tabs.check('%12', owner, entry, record, Tmux(), pane='%13')

    def test_nested_claude_registry_cannot_change_wait_send_or_close_identity(self):
        outer = self.registry(200, HELPER, 'cli')
        inner = self.registry(2, OTHER, 'sdk-cli')
        for order in ((outer, inner), (inner, outer)):
            with self.subTest(order=[p.name for p in order]):
                helper = self.discover(*order)
                self.assertEqual((helper['thread'], helper['owner_pid'], helper['registry']), (HELPER, 200, str(outer)))
                owner = dict(id='%12', agent='claude', thread=OWNER, registry='/fake/12.json')
                entries = [owner, helper]
                run = Tmux()
                run.options['%13'] = json.dumps(dict(owner_pane='%12', owner_thread=OWNER, helper_thread=HELPER,
                                                     sent_at=1000))
                body = {'pane': '%13', 'message': 'Follow up'}
                status = Mock(return_value={'state': 'Idle'})
                reply = Mock(return_value='Report')
                send = Mock(return_value='queued')
                result = helper_tabs.poll(body, owner, entries, run, status, reply, lambda pane: {})
                self.assertEqual(result['reply'], 'Report')
                self.assertEqual(helper_tabs.send_message(body, owner, entries, run, status, send, reply,
                                                          helper_tabs.SEND_LOCK)[0], 200)
                send.assert_called_once_with(helper, 'Follow up', 'Idle')
                with patch.object(agent_tabs.helper_close, 'close_pane') as close, \
                        patch.object(agent_tabs.undo, 'capture_safe', return_value=None), \
                        patch.object(agent_tabs.undo, 'record_tab_close'), patch.object(agent_tabs.audit, 'record'):
                    self.assertEqual(agent_tabs.close_tab(body, owner, entries, run, status), {'closed': '%13'})
                close.assert_called_once_with('%13', run)

    def test_pending_outer_claude_ignores_the_nested_registry(self):
        helper = self.discover(self.registry(2, OTHER, 'sdk-cli'))
        self.assertEqual((helper['agent'], helper['owner_pid'], helper['pending_conversation']), ('claude', 200, True))

    def test_pending_claude_uses_depth_then_lowest_pid(self):
        self.processes[199] = (100, 'claude')
        for table in (self.processes, dict(reversed(list(self.processes.items())))):
            with self.subTest(table=table):
                with patch.object(agents.procinfo, 'table', return_value=table):
                    helper = self.discover()
                self.assertEqual((helper['owner_pid'], helper['pending_conversation']), (199, True))

    def test_codex_pane_ignores_nested_claude_registry(self):
        self.processes[200] = (100, 'codex')
        helper = self.discover(self.registry(2, OTHER, 'sdk-cli'))
        self.assertEqual((helper['agent'], helper['owner_pid'], helper['thread'], helper['codex_binding']),
                         ('codex', 200, HELPER, 'open'))

    def test_codex_discovery_uses_outer_process_when_a_child_runs_codex(self):
        self.processes.update({200: (100, 'codex'), 2: (201, 'codex')})
        helper = self.discover()
        self.assertEqual((helper['owner_pid'], helper['thread']), (200, HELPER))

    def test_codex_send_recheck_uses_outer_process_with_nested_claude(self):
        self.processes[200] = (100, 'codex')
        self.run.return_value = subprocess.CompletedProcess([], 0, '%13\t100\twork\t\t/work\n')
        self.registry(2, OTHER, 'sdk-cli')
        entry = dict(id='%13', agent='codex', owner_pid=200, process_start='200', thread=HELPER)
        terminal_input.verify_codex(entry, self.run)

    def test_codex_recheck_skips_another_panes_missing_owner(self):
        self.processes.update({200: (100, 'codex'), 300: (1, 'codex')})
        self.run.return_value.stdout = '%14\t300\twork\t\t/work\n%13\t100\twork\t\t/work\n'
        agents.process_start.side_effect = [FileNotFoundError('/proc/300/stat'), '200']
        entry = dict(id='%13', agent='codex', owner_pid=200, process_start='200', thread=HELPER)
        self.assertEqual(agents.codex_recheck(entry, self.run)['thread'], HELPER)

    def test_antigravity_pane_ignores_nested_claude_registry(self):
        self.processes[200] = (100, 'agy')
        with patch.object(agents, 'antigravity_entry', return_value={'owner_pid': 200}) as live:
            helper = self.discover(self.registry(2, OTHER, 'sdk-cli'))
        self.assertEqual((helper['agent'], helper['owner_pid']), ('antigravity', 200))
        live.assert_called_once_with([200], cwd='/work')
