"""Codex 0.170 worker rollouts and thread index, with synthetic text and IDs."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import paths
import subagents


PARENT, WORKER, NESTED, OTHER, FOREIGN, MISMATCH = (
    f'00000000-0000-7000-8000-{index:012}' for index in range(1, 7))
NOW = datetime(2026, 11, 18, 6, 6, tzinfo=timezone.utc).timestamp()


def record(kind, second, **payload):
    stamp = datetime.fromtimestamp(NOW + second, timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'type': kind, 'timestamp': stamp, 'payload': payload}


class CodexSubagentTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(home)
        (self.root / 'sessions').mkdir()
        self.enterContext(patch.object(paths, 'CODEX', self.root))
        self.enterContext(patch.object(subagents, '_owner_since', return_value=NOW - 100))
        self.enterContext(patch.object(subagents.time, 'time', return_value=NOW))
        self.db = sqlite3.connect(self.root / 'state_5.sqlite')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, model TEXT,
                                  reasoning_effort TEXT, tokens_used INTEGER, source TEXT);
            CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT);
        ''')
        parent = self.thread(PARENT, 'vscode', [record('session_meta', -100, id=PARENT, source='vscode')])
        self.config = {'agent': 'codex', 'thread': PARENT, 'transcript': str(parent)}

    def thread(self, identifier, source, records):
        path = self.root / 'sessions' / f'rollout-{identifier}.jsonl'
        self.append(path, *records)
        self.db.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)',
                        (identifier, str(path), 'gpt-6.2-sol', 'high', 1000, json.dumps(source)))
        self.db.commit()
        return path

    def append(self, path, *records):
        with path.open('a') as stream:
            stream.writelines(json.dumps(item) + '\n' for item in records)

    def event(self, kind, second, turn='worker-turn', **fields):
        return record('event_msg', second, type=kind, turn_id=turn, **fields)

    def worker(self, identifier=WORKER, parent=PARENT, *, actual_parent=None, edge='open',
               name='/root/check_files', depth=1, fork=True):
        source = {'subagent': {'thread_spawn': {'parent_thread_id': actual_parent or parent,
                                              'depth': depth, 'agent_path': name,
                                              'agent_role': 'worker', 'agent_nickname': 'Curie'}}}
        history = [record('session_meta', -90, id=parent, source='vscode'),
                   self.event('task_started', -30, 'parent-turn'),
                   record('response_item', -30, type='function_call', name='exec_command',
                          call_id='inherited', arguments='{}'),
                   self.event('task_complete', -30, 'parent-turn', last_agent_message='Parent report')]
        header = record('session_meta', -31, id=identifier, source=source, cli_version='0.170.0')
        if fork:
            header['payload']['subagent_history_start_ordinal'] = len(history) + 1
        path = self.thread(identifier, source, [header, *(history if fork else []),
                                               self.event('task_started', -29)])
        self.db.execute('INSERT INTO thread_spawn_edges VALUES (?, ?, ?)', (parent, identifier, edge))
        self.db.commit()
        return path

    def entries(self, config=None):
        return {entry['id']: entry for entry in subagents.listing(config or self.config)['agents']}

    def test_tracks_live_turn_completion_and_followup_on_open_edge(self):
        path = self.worker()
        self.assertIn(WORKER, self.entries())
        running = self.entries()[WORKER]
        self.assertEqual((running['state'], running['ended'], running['tools']), ('running', None, 0))
        self.assertEqual((running['description'], running['type'], running['can_stop']), ('check_files', 'worker', False))
        self.append(path, self.event('task_complete', -20, 'parent-turn', last_agent_message='Unrelated completion'))
        self.assertEqual(self.entries()[WORKER]['state'], 'running')
        self.append(path, self.event('task_complete', -10, last_agent_message='Worker report'))
        finished = self.entries()[WORKER]
        self.assertEqual((finished['state'], finished['ended'], finished['seconds']),
                         ('finished', '2026-11-18T06:05:50Z', 21))
        self.append(path, self.event('task_started', -5, 'followup'))
        self.assertEqual(self.entries()[WORKER]['state'], 'running')
        self.assertIsNone(subagents.detail(self.config, WORKER)['report'])
        self.append(path, self.event('turn_aborted', -2, 'followup', reason='interrupted'))
        self.assertEqual(self.entries()[WORKER]['state'], 'stopped')

    def test_worker_details_exclude_inherited_items_and_runtime_context(self):
        path = self.worker()
        self.append(path,
                    record('response_item', -25, type='message', role='user',
                           content=[{'type': 'input_text', 'text': 'Runtime context'}]),
                    record('response_item', -24, type='agent_message', author='/root', recipient='/root/check_files',
                           content=[{'type': 'input_text', 'text': 'Inspect the files'},
                                    {'type': 'encrypted_content', 'encrypted_content': 'opaque'}]),
                    record('response_item', -23, type='message', role='assistant', phase='commentary',
                           content=[{'type': 'output_text', 'text': 'Checking files'}]),
                    record('response_item', -22, type='function_call', name='exec_command',
                           call_id='worker-call', arguments='{"cmd":"false"}'),
                    record('response_item', -21, type='function_call_output', call_id='worker-call',
                           output='Process exited with code 1'),
                    self.event('task_complete', -10, last_agent_message='Worker report'))
        self.assertIn(WORKER, self.entries())
        detail = subagents.detail(self.config, WORKER)
        self.assertEqual((detail['prompt'], detail['report'], detail['tools']), ('Inspect the files', 'Worker report', 1))
        self.assertEqual(detail['steps'][0], {'kind': 'text', 'text': 'Checking files'})
        self.assertEqual((detail['steps'][1]['id'], detail['steps'][1]['status'], detail['steps'][1]['is_error']),
                         ('worker-call', 'complete', True))
        tool = subagents.tool(self.config, WORKER, 'worker-call')
        self.assertEqual(tool['status'], 'complete')
        self.assertIn('Process exited with code 1', tool['detail'])
        with self.assertRaises(ValueError):
            subagents.tool(self.config, WORKER, 'inherited')

    def test_limits_listing_and_detail_to_selected_conversation_workers(self):
        self.worker()
        self.worker(NESTED, WORKER, name='/root/check_files/child', depth=2, fork=False)
        self.thread(OTHER, 'vscode', [record('session_meta', -100, id=OTHER, source='vscode')])
        self.worker(FOREIGN, OTHER, fork=False)
        self.worker(MISMATCH, actual_parent=OTHER, fork=False)
        entries = self.entries()
        self.assertEqual(set(entries), {WORKER, NESTED})
        self.assertIsNone(entries[WORKER]['parent'])
        self.assertEqual((entries[NESTED]['parent'], entries[NESTED]['depth']), (WORKER, 2))
        for identifier in (PARENT, FOREIGN, MISMATCH, '../worker'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                subagents.detail(self.config, identifier)
        self.assertEqual(self.entries(dict(self.config, thread=OTHER)), {})

    def test_closed_or_lost_owner_worker_is_stopped(self):
        self.worker(edge='closed', fork=False)
        self.assertIn(WORKER, self.entries())
        self.assertEqual(self.entries()[WORKER]['state'], 'stopped')
        self.db.execute("UPDATE thread_spawn_edges SET status='open'")
        self.db.commit()
        with patch.object(subagents, '_owner_since', return_value=NOW - 10):
            self.assertEqual(self.entries()[WORKER]['state'], 'stopped')

    def test_replaced_rollout_does_not_reuse_cached_worker_identity(self):
        path = self.worker(fork=False)
        self.assertIn(WORKER, self.entries())
        other = self.worker(FOREIGN, OTHER, fork=False, name='/root/' + 'foreign' * 200)
        path.write_bytes(other.read_bytes())
        self.assertNotIn(WORKER, self.entries())
        with self.assertRaises(ValueError):
            subagents.detail(self.config, WORKER)

    def test_missing_spawn_table_falls_back_to_empty_map(self):
        self.db.execute('DROP TABLE thread_spawn_edges')
        self.assertEqual(self.entries(), {})


if __name__ == '__main__':
    unittest.main()
