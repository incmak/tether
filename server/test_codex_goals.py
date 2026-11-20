import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live
import paths
import server


class CodexGoalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.patch = patch.object(paths, 'CODEX', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with sqlite3.connect(self.root / 'goals_1.sqlite') as db:
            db.execute('CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, objective TEXT, status TEXT, token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)')
            db.execute("INSERT INTO thread_goals VALUES ('thread-1', 'Finish cleanup', 'active', 100000, 1200, 45, 1000, 2000)")
        self.config = {'agent': 'codex', 'thread': 'thread-1', 'id': '%fixture'}

    def test_live_endpoint_reads_codex_goal(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/mobile/api/live?session=%25fixture'
        handler.deny = lambda: False
        replies = []
        handler.respond = lambda status, data: replies.append((status, data))
        with patch.object(server, 'choose', return_value=self.config):
            handler.do_GET()
        self.assertEqual(replies[0][0], 200)
        self.assertIsNotNone(replies[0][1]['goal'])
        self.assertEqual(replies[0][1]['goal']['condition'], 'Finish cleanup')

    def test_footer_exposes_status_and_live_budget(self):
        goal = live.codex_goal(self.config)
        self.assertEqual(goal['status'], 'active')
        self.assertEqual(goal['token_budget'], 100000)
        self.assertEqual(goal['tokens_used'], 1200)
        self.assertEqual(goal['set_at'], 1)
        for status in ('paused', 'blocked', 'usage_limited', 'budget_limited', 'complete'):
            with sqlite3.connect(self.root / 'goals_1.sqlite') as db:
                db.execute('UPDATE thread_goals SET status=?', (status,))
            footer = server.codex_footer(self.config, {})
            self.assertEqual(footer['goal_status'], status)
            self.assertEqual(footer['goal'], status)
            self.assertEqual(live.codex_goal(self.config)['met'], status == 'complete')

    def test_absent_ambiguous_and_unsupported_conversations(self):
        for config in ({**self.config, 'thread': 'other'}, {**self.config, 'ambiguous': True}, {**self.config, 'thread': None}, {**self.config, 'agent': 'claude'}):
            self.assertIsNone(live.codex_goal(config))
        (self.root / 'goals_1.sqlite').unlink()
        self.assertIsNone(live.codex_goal(self.config))
        self.assertFalse((self.root / 'goals_1.sqlite').exists())

    def test_unavailable_schema_is_optional(self):
        (self.root / 'goals_1.sqlite').write_bytes(b'invalid database')
        self.assertIsNone(live.codex_goal(self.config))
