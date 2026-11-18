"""factory.py: the Factory view's read-only facts (DEPLOYED.md lines, latest tool calls, recent commits)."""
import json
import subprocess
import time
from unittest import mock
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import factory
from agents import claude_consume


def claude_readers():
    return {'claude': (lambda: {'messages': []}, lambda state, index, record: claude_consume(state['messages'], index, record, lambda text: text))}


def tool_use(name, data, stamp='2026-11-17T08:00:00Z'):
    return {'type': 'assistant', 'timestamp': stamp, 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'u-' + name, 'name': name, 'input': data}]}}


class DeployedTests(unittest.TestCase):
    def test_reads_each_date_format_of_the_log(self):
        iso = factory.parse_deployed_line('- 2026-11-17 08:35 IST · 5d59421 B188 new Claude tabs no longer stop (B12 too)')
        self.assertEqual(iso['commit'], '5d59421')
        self.assertEqual(iso['refs'], ['B12', 'B188'])
        self.assertTrue(iso['timed'])
        self.assertEqual(iso['at'], int(datetime(2026, 11, 17, 3, 5, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(iso['text'].startswith('5d59421 B188'))
        colon = factory.parse_deployed_line('2026-11-17 00:21 IST: b8636f0 Settings › Appearance')
        self.assertEqual(colon['commit'], 'b8636f0')
        self.assertEqual(colon['text'], 'b8636f0 Settings › Appearance')
        words = factory.parse_deployed_line('- 09 Nov 2026 11:35 a315cf8 Waiting on you sidebar')
        self.assertEqual(datetime.fromtimestamp(words['at']), datetime(2026, 11, 9, 11, 35))
        dated = factory.parse_deployed_line('- 2026-11-17 · Teams answer UI (8a90d86 + BriskBot acbea44)')
        self.assertFalse(dated['timed'])
        self.assertEqual(dated['text'], 'Teams answer UI (8a90d86 + BriskBot acbea44)')  # the leading T stays
        self.assertEqual(dated['commit'], '8a90d86')
        self.assertIsNone(factory.parse_deployed_line('  - Done: 30af7fe deploy guard'))
        self.assertIsNone(factory.parse_deployed_line('- 2026-13-40 bad date'))

    def test_reads_the_deploy_date_and_commit_with_shots(self):
        item = factory.parse_deployed_line('- 2026-11-18 05:30 UTC · abc1234 UI deploy · Shots: proof · Verified: none')
        self.assertEqual(item['at'], int(datetime(2026, 11, 18, 5, 30, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(item['commit'], 'abc1234')

    def test_keeps_only_recent_lines_newest_first_and_a_missing_file_gives_none(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'DEPLOYED.md'
            log.write_text('- 2026-11-01 aaaaaaa old\n- 2026-11-16 10:00 bbbbbbb yesterday B7\n- 2026-11-17 09:00 ccccccc today\n\nnot a line\n')
            now = datetime(2026, 11, 17, 12, 0).timestamp()
            found = factory.deployed(now, log)
            self.assertEqual([item['commit'] for item in found], ['ccccccc', 'bbbbbbb'])
            self.assertEqual(factory.deployed(now, Path(folder) / 'missing.md'), [])


class ToolTests(unittest.TestCase):
    def write(self, folder, records, junk=b''):
        path = Path(folder) / 'chat.jsonl'
        path.write_bytes(junk + b'\n'.join(json.dumps(record).encode() for record in records) + b'\n')
        return path

    def test_tail_drops_the_cut_first_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, [tool_use('Read', {'file_path': 'a.py'}), tool_use('Edit', {'file_path': 'b.py'})], junk=b'{"cut": "x' * 40 + b'\n')
            records = factory.tail_records(path, size=os.path.getsize(path) - 5)
            self.assertEqual([record['message']['content'][0]['name'] for record in records], ['Read', 'Edit'])

    def test_latest_tools_come_from_the_agent_parser_and_are_cached_until_the_file_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, [tool_use('Read', {'file_path': 'a.py'}), tool_use('Bash', {'command': 'cd server && python3 -m unittest -q'})])
            entry = {'id': '%1', 'agent': 'claude', 'transcript': str(path)}
            tools = factory.recent_tools(entry, claude_readers())
            self.assertEqual([(tool['name'], tool['title']) for tool in tools], [('Read', 'a.py'), ('Bash', 'cd server && python3 -m unittest -q')])
            self.assertEqual(tools[0]['at'], '2026-11-17T08:00:00Z')
            calls = []
            counting = {'claude': (lambda: {'messages': []}, lambda state, index, record: calls.append(index))}
            self.assertIs(factory.recent_tools(entry, counting), tools)  # unchanged file: no new read
            self.assertEqual(calls, [])
            with open(path, 'a') as stream:
                stream.write(json.dumps(tool_use('Edit', {'file_path': 'c.py'})) + '\n')
            self.assertEqual(factory.recent_tools(entry, claude_readers())[-1]['title'], 'c.py')

    def test_no_tools_for_unread_agents_missing_files_or_odd_records(self):
        self.assertEqual(factory.recent_tools({'id': '%1', 'agent': 'antigravity', 'transcript': '/x'}, claude_readers()), [])
        self.assertEqual(factory.recent_tools({'id': '%1', 'agent': 'claude', 'transcript': '/no/such/file'}, claude_readers()), [])
        def broken(state, index, record):
            raise KeyError('odd')
        self.assertEqual(factory.tools_from([{}], lambda: {'messages': []}, broken), [])


class PayloadTests(unittest.TestCase):
    def test_payload_stops_new_git_work_after_its_budget(self):
        calls = []

        def slow(command, **kwargs):
            calls.append(kwargs['timeout'])
            time.sleep(0.4)
            folder = command[2]
            out = folder + '/.git\n' if 'rev-parse' in command else ''
            return subprocess.CompletedProcess(command, 0, out, '')

        entries = [{'id': f'%{n}', 'agent': 'claude'} for n in range(8)]
        with mock.patch.object(factory, 'GIT_BUDGET', 1), mock.patch.dict(factory._root_cache, clear=True), mock.patch.dict(factory._commit_cache, clear=True):
            started = time.monotonic()
            factory.payload(entries, lambda entry: '/srv/budget' + entry['id'][1:], {}, run=slow)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertLess(len(calls), 6)
            self.assertTrue(all(timeout <= 1 for timeout in calls))

    def test_payload_joins_tools_deploys_and_commits_of_each_repository_once(self):
        runs = []
        def run(command, **kwargs):
            runs.append(command)
            if 'rev-parse' in command:
                return SimpleNamespace(returncode=0, stdout='/srv/repo/.git\n')
            return SimpleNamespace(returncode=0, stdout='abc1234\t1794884700\tLaunch: pass folders (B188)\nbad line\n')
        factory._root_cache.clear(); factory._commit_cache.clear()
        entries = [{'id': '%1', 'agent': 'claude', 'transcript': ''}, {'id': '%2', 'agent': 'claude', 'transcript': ''}, {'id': '%3', 'agent': 'shell'}]
        data = factory.payload(entries, lambda entry: '/srv/repo/wt-' + entry['id'], claude_readers(), run=run, now=1794884800)
        self.assertEqual(set(data['tools']), {'%1', '%2'})
        self.assertEqual(data['commits'], [{'hash': 'abc1234', 'at': 1794884700, 'subject': 'Launch: pass folders (B188)', 'refs': ['B188'], 'repo': 'repo'}])
        self.assertEqual(sum('log' in command for command in runs), 1)
        self.assertIn('deployed', data)


if __name__ == '__main__':
    unittest.main()
