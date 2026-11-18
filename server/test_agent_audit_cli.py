from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import agent_audit_cli as cli
import audit
import paths


class AuditCliTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.enterContext(mock.patch.object(paths, 'CONFIG', Path(root.name)))
        self.addCleanup(time.tzset)
        self.enterContext(mock.patch.dict(os.environ, TZ='UTC-05:30'))
        time.tzset()

    def output(self, *arguments):
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(cli.main(['list', *arguments]), 0)
        return stream.getvalue()

    def fixture(self):
        at = datetime(2026, 11, 18, 20, 15, 54, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(audit.time, 'time', return_value=at) as clock:
            first = audit.record('setting.codex_level', 'Codex level 3 → 1', old=3, new=1, project='Tether',
                                 actor={'source': 'ui', 'device': 'Chrome on Mac'})
            clock.return_value += 1
            second = audit.record('backlog.start', 'B56 started: Audit', project='Tether',
                                  actor={'source': 'agent-cli', 'tab': 'Audit Log', 'device': 'Chrome on Mac'})
        return first, second

    def test_text_format_and_actor_name(self):
        self.fixture()
        self.assertEqual(self.output(),
                         '2026-11-19 01:45:55  backlog.start  B56 started: Audit  · Audit Log (agent-cli)\n'
                         '2026-11-19 01:45:54  setting.codex_level  Codex level 3 → 1  · Chrome on Mac (ui)\n')

    def test_json_and_filters_use_local_dates(self):
        first, second = self.fixture()
        output = self.output('--kind', 'settings,backlog', '--project', 'tether', '--actor', 'mac',
                             '--since', '2026-11-19', '--until', '2026-11-20', '--limit', '1', '--json')
        self.assertEqual(json.loads(output), {'entries': [second], 'next': second['id']})
        self.assertEqual(json.loads(self.output('--text', 'LEVEL', '--json')), {'entries': [first], 'next': None})
        self.assertEqual(self.output('--until', '2026-11-19'), 'Nothing recorded.\n')

    def test_empty_and_invalid_invocations(self):
        self.assertEqual(self.output(), 'Nothing recorded.\n')
        for arguments in (['--since', '20261119'], ['--until', '2026-02-30'], ['--limit', '999']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                cli.main(['list', *arguments])
            self.assertEqual(error.exception.code, 2)
