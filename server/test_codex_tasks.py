"""Background terminal checks against captured and injected process trees."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import agents
import codex_tasks
import procinfo


FIXTURES = Path(__file__).with_name('fixtures') / 'codex_0.170.0_background'
THREAD = '01a22294-1111-7222-8333-123456789abc'


class CodexTasksTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(procinfo, 'pids', side_effect=AssertionError('live scan')))
        self.stats = self.enterContext(patch.object(procinfo, 'stat_fields', side_effect=AssertionError('live stat')))
        self.enterContext(patch.object(procinfo, 'comm', side_effect=lambda pid: self.rows[pid]['comm']))
        self.enterContext(patch.object(procinfo, 'argv', side_effect=lambda pid: self.rows[pid].get('argv', [b'codex'])))
        self.env = self.enterContext(patch.object(procinfo, 'environ', side_effect=self.environ))
        self.rollout = '/sessions/rollout-test.jsonl'
        self.fds = self.enterContext(patch.object(procinfo, 'fd_targets', return_value=[('7', self.rollout)]))
        db = agents.codex_database()
        self.addCleanup(db.close)
        db.execute('INSERT INTO threads (id, rollout_path, archived, source) VALUES (?, ?, 0, ?)',
                   (THREAD, self.rollout, 'cli'))
        db.execute('CREATE TABLE IF NOT EXISTS thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT)')
        db.executemany('INSERT INTO thread_spawn_edges VALUES (?, ?, ?)',
                       [(THREAD, 'child', 'closed'), ('child', 'grandchild', 'open'), ('other', 'unrelated', 'open')])
        self.db = db
        self.enterContext(patch.object(agents, 'codex_database', return_value=db))

    def environ(self, pid):
        row = self.rows[pid]
        if 'error' in row:
            raise row['error']
        return [f'{key}={value}'.encode() for key, value in row['tags'].items()]

    def check(self, fixture='background_terminal', *, edits=None):
        capture = json.loads((FIXTURES / (fixture + '.json')).read_text())
        self.rows = {row['pid']: row for row in capture['processes']}
        owner = capture['owner_pid']
        if edits:
            edits(owner)
        table = {pid: (row['ppid'], row['state']) for pid, row in self.rows.items()}
        return codex_tasks.background_terminals({'owner_pid': owner, 'thread': THREAD}, table=table)

    def test_embedded_terminal_counts_once_even_with_a_different_thread_id(self):
        self.assertEqual(self.check(), [{'pid': 3472384, 'command': 'codex-linux-san'}])

    def test_after_stop_with_mcp_and_code_mode_processes_left(self):
        self.assertEqual(self.check('after_stop'), [])

    def test_zombie_and_vanished_descendants_are_skipped(self):
        def edits(owner):
            self.rows[101] = dict(ppid=owner, state='Z', tags={'CODEX_TOOL_CALL_ID': 'exec-zombie'},
                                  error=AssertionError('read zombie'))
            self.rows[102] = dict(ppid=owner, state='S', error=FileNotFoundError())
            self.rows[103] = dict(ppid=owner, state='S', error=ProcessLookupError())

        self.assertEqual(self.check('after_stop', edits=edits), [])

    def test_permission_error_on_any_live_descendant_is_unchecked(self):
        def edits(owner):
            self.rows[3472421]['error'] = PermissionError()  # Nested under a terminal already found.

        with self.assertRaises(codex_tasks.Unchecked) as raised:
            self.check(edits=edits)
        self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_owner_read_errors_are_unchecked(self):
        def edits(owner):
            self.rows[owner]['error'] = ProcessLookupError()

        with self.assertRaises(codex_tasks.Unchecked):
            self.check('after_stop', edits=edits)

    def test_unreadable_owner_rollout_is_unchecked(self):
        self.fds.side_effect = PermissionError
        with self.assertRaises(codex_tasks.Unchecked):
            self.check('after_stop')

    def test_reparented_terminal_is_outside_the_scan(self):
        def edits(owner):
            self.rows[3472384]['ppid'] = 1

        self.assertEqual(self.check(edits=edits), [])
        self.assertFalse({3472384, 3472412, 3472413, 3472421} & {call.args[0] for call in self.env.call_args_list})

    def daemon_rows(self, owner):
        self.rows[101] = dict(ppid=1, state='S', comm='codex', tags={},
                              argv=[b'codex', b'app-server', b'--listen', b'ws://127.0.0.1:0'])
        self.rows[102] = dict(ppid=101, state='S', comm='sleep',
                              tags={'CODEX_TOOL_CALL_ID': 'exec-1', 'CODEX_THREAD_ID': THREAD})
        self.rows[103] = dict(ppid=101, state='S', comm='sleep',
                              tags={'CODEX_TOOL_CALL_ID': 'exec-2', 'CODEX_THREAD_ID': 'other'})
        self.rows[104] = dict(ppid=owner, state='S', comm='sleep',
                              tags={'CODEX_TOOL_CALL_ID': 'exec-3', 'CODEX_THREAD_ID': 'other'})
        self.rows[105] = dict(ppid=101, state='S', comm='tail',
                              tags={'CODEX_TOOL_CALL_ID': 'exec-4', 'CODEX_THREAD_ID': 'grandchild'})
        self.rows[106] = dict(ppid=101, state='S', comm='tail',
                              tags={'CODEX_TOOL_CALL_ID': 'exec-5', 'CODEX_THREAD_ID': 'unrelated'})

    def test_daemon_client_checks_its_thread_tree_and_its_own_descendants(self):
        self.fds.return_value = []
        self.assertEqual(self.check('after_stop', edits=self.daemon_rows),
                         [{'pid': 102, 'command': 'sleep'}, {'pid': 104, 'command': 'sleep'},
                          {'pid': 105, 'command': 'tail'}])

    def test_daemon_client_without_spawn_edges_is_unchecked(self):
        self.fds.return_value = []
        self.db.execute('DROP TABLE thread_spawn_edges')
        with self.assertRaises(codex_tasks.Unchecked):
            self.check('after_stop', edits=self.daemon_rows)

    def test_daemon_root_read_error_is_unchecked(self):
        def edits(owner):
            self.daemon_rows(owner)
            self.rows[101]['error'] = FileNotFoundError()

        self.fds.return_value = []
        with self.assertRaises(codex_tasks.Unchecked):
            self.check('after_stop', edits=edits)

    def test_snapshot_uses_stat_parent_and_state_and_skips_disappearing_pids(self):
        with patch.object(procinfo, 'pids', return_value=[1, 100, 101]):
            self.stats.side_effect = [['S', '0'], ['Z', '1'], ProcessLookupError()]
            self.assertEqual(codex_tasks._process_table(), {1: (0, 'S'), 100: (1, 'Z')})

    def test_malformed_owner_stat_is_unchecked(self):
        with patch.object(procinfo, 'pids', return_value=[100]):
            self.stats.side_effect = IndexError
            with self.assertRaises(codex_tasks.Unchecked):
                codex_tasks.background_terminals({'owner_pid': 100, 'thread': THREAD})
