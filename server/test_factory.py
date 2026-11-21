"""factory.py: the Factory view's read-only facts (DEPLOYED.md lines, latest tool calls, recent commits)."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import subprocess
import sys
import time
from unittest import mock
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import antigravity
import factory
import prefs_sync
import server
from agents import claude_consume


def claude_readers():
    return {'claude': (lambda: {'messages': []}, lambda state, index, record: claude_consume(state['messages'], index, record, lambda text: text))}


def tool_use(name, data, stamp='2026-11-17T08:00:00Z'):
    return {'type': 'assistant', 'timestamp': stamp, 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'u-' + name, 'name': name, 'input': data}]}}


def agy_step(position, **fields):
    # Synthetic Antigravity rows from the shape antigravity.py parses. No live agy capture.
    return {'type': 'PLANNER_RESPONSE', 'step_index': position, 'content': '', **fields}


class DeployedTests(unittest.TestCase):
    def test_timed_deploy_window_includes_the_first_instant_at_a_havana_midnight_transition(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(prefs_sync, 'FILE', root / 'prefs.json'))
        prefs_sync.save({'timeZone': 'America/Havana'})
        log = root / 'DEPLOYED.md'
        for day, now, stamps in (
            ('2026-11-01', '2026-11-03T17:00:00Z',
             ('2026-10-31 23:59 -0400', '2026-11-01 00:00 -0400', '2026-11-01 00:00 -0500')),
            ('2026-03-08', '2026-03-10T16:00:00Z',
             ('2026-03-07 23:59 -0500', '2026-03-08 01:00 -0400', '2026-03-08 01:01 -0400')),
        ):
            with self.subTest(day=day):
                log.write_text('\n'.join(f'- {stamp} · {index}' for index, stamp in enumerate(stamps)))
                rows = factory.deployed(datetime.fromisoformat(now).timestamp(), log)
                self.assertEqual([row['text'] for row in rows], ['2', '1'])

    def test_date_only_deploys_use_the_written_day_at_the_window_boundary(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(prefs_sync, 'FILE', root / 'prefs.json'))
        self.addCleanup(time.tzset)
        self.enterContext(mock.patch.dict(os.environ, {'TZ': 'Asia/Kolkata'}))
        time.tzset()
        prefs_sync.save({'timeZone': 'America/New_York'})
        log = root / 'DEPLOYED.md'
        log.write_text('- 2026-11-20 · ddddddd earlier\n'
                       '- 2026-11-21 · aaaaaaa deploy\n'
                       '- 2026-11-22 · bbbbbbb another deploy\n'
                       '- 2026-11-24 · ccccccc future\n')
        now = datetime(2026, 11, 23, 18, tzinfo=timezone.utc).timestamp()
        self.assertEqual([item['commit'] for item in factory.deployed(now, log)], ['bbbbbbb', 'aaaaaaa'])

    def test_writer_and_reader_preserve_the_later_new_york_instant(self):
        self.enterContext(mock.patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]))
        import verification

        at = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
        written = verification.line('deploy', 'none', now=at.astimezone(ZoneInfo('America/New_York')))
        parsed = factory.parse_deployed_line(written)
        self.assertEqual((parsed['at'], parsed['text']), (at.timestamp(), 'deploy · Verified: none'))
        self.assertIn('01:30 -0500 ·', written)

    def test_reads_numeric_and_legacy_deploy_offsets_and_preserves_unknown_names(self):
        for stamp, expected, text in (
            ('2026-11-22 02:10 +0530', '2026-11-21T20:40:00Z', 'deploy'),
            ('2026-11-22 02:10 +05:30', '2026-11-21T20:40:00Z', 'deploy'),
            ('2026-11-22 02:10 IST', '2026-11-21T20:40:00Z', 'deploy'),
            ('2026-11-22 02:10 GMT', '2026-11-22T02:10:00Z', 'deploy'),
            ('2026-11-22 02:10 EST', None, 'EST · deploy'),
        ):
            with self.subTest(stamp=stamp):
                row = factory.parse_deployed_line(f'- {stamp} · deploy')
                instant = datetime.fromisoformat(expected or stamp[:16]).timestamp()
                self.assertEqual((row['at'], row['text']), (instant, text))

    def test_reads_each_date_format_of_the_log(self):
        iso = factory.parse_deployed_line('- 2026-11-17 08:35 IST · 5d59421 B188 new Claude tabs no longer stop (B12 too)')
        self.assertEqual(iso['commit'], '5d59421')
        self.assertEqual(iso['refs'], ['B12', 'B188'])
        self.assertTrue(iso['timed'])
        self.assertEqual(iso['day'], '2026-11-17')
        self.assertEqual(iso['at'], int(datetime(2026, 11, 17, 3, 5, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(iso['text'].startswith('5d59421 B188'))
        colon = factory.parse_deployed_line('2026-11-17 00:21 IST: b8636f0 Settings › Appearance')
        self.assertEqual(colon['commit'], 'b8636f0')
        self.assertEqual(colon['text'], 'b8636f0 Settings › Appearance')
        words = factory.parse_deployed_line('- 09 Nov 2026 11:35 a315cf8 Waiting on you sidebar')
        self.assertEqual(words['day'], '2026-11-09')
        self.assertEqual(datetime.fromtimestamp(words['at']), datetime(2026, 11, 9, 11, 35))
        dated = factory.parse_deployed_line('- 2026-11-21 · Teams answer UI (8a90d86 + BriskBot acbea44)')
        self.assertFalse(dated['timed'])
        self.assertEqual(dated['day'], '2026-11-21')
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

    def test_recent_deploys_use_the_preference_day_boundary(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(prefs_sync, 'FILE', root / 'prefs.json'))
        log = root / 'DEPLOYED.md'
        log.write_text('- 2026-11-16 04:59 UTC · aaaaaaa before\n'
                       '- 2026-11-16 05:00 UTC · bbbbbbb boundary\n')
        now = datetime(2026, 11, 18, 18, tzinfo=timezone.utc).timestamp()
        prefs_sync.save({'timeZone': 'America/New_York'})
        self.assertEqual([item['commit'] for item in factory.deployed(now, log)], ['bbbbbbb'])
        prefs_sync.save({'timeZone': 'Pacific/Auckland'})
        self.assertEqual(factory.deployed(now, log), [])


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

    def test_recent_tools_reads_antigravity_calls_and_titles(self):
        stamp = '2026-11-19T09:00:00Z'
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, [
                {'type': 'USER_INPUT', 'step_index': 1, 'content': 'Run the tests'},
                agy_step(2, created_at=stamp, tool_calls=[
                    {'name': 'run_command', 'args': {'CommandLine': '"python3 -m unittest -q"'}},
                    {'name': 'view_file', 'args': {'AbsolutePath': '"/repo/test_factory.py"'}},
                ]),
            ])
            entry = {'id': '%1', 'agent': 'antigravity', 'transcript': str(path)}
            self.assertEqual(factory.recent_tools(entry, server.FACTORY_READERS), [
                {'name': 'run_command', 'title': 'python3 -m unittest -q', 'at': stamp, 'error': False, 'done': False},
                {'name': 'view_file', 'title': '/repo/test_factory.py', 'at': stamp, 'error': False, 'done': False},
            ])

    def test_recent_tools_orders_and_replaces_antigravity_steps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, [
                agy_step(3, tool_calls=[{'name': 'old_step_3'}]),
                agy_step(2, tool_calls=[{'name': 'step_2'}]),
                agy_step(3, tool_calls=[{'name': 'new_step_3_first'}, {'name': 'new_step_3_second'}]),
                agy_step(3, content='Text-only rewrite'),
            ])
            entry = {'id': '%1', 'agent': 'antigravity', 'transcript': str(path)}
            tools = factory.recent_tools(entry, server.FACTORY_READERS)
            self.assertEqual([tool['name'] for tool in tools], ['step_2', 'new_step_3_first', 'new_step_3_second'])

    def test_numbered_and_unnumbered_antigravity_steps_keep_their_calls(self):
        unnumbered = lambda name: {'type': 'PLANNER_RESPONSE', 'content': '', 'tool_calls': [{'name': name}]}
        for rows, expected in [
            ([agy_step(1, tool_calls=[{'name': 'step_1'}]), unnumbered('row_1')], ['step_1', 'row_1']),
            ([unnumbered('row_0'), agy_step(0, tool_calls=[{'name': 'step_0'}])], ['step_0', 'row_0']),
        ]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                entry = {'id': '%1', 'agent': 'antigravity', 'transcript': str(self.write(folder, rows))}
                self.assertEqual([tool['name'] for tool in factory.recent_tools(entry, server.FACTORY_READERS)], expected)

    def test_recent_tools_handles_empty_rewrites_and_invalid_antigravity_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, [
                agy_step(2, tool_calls=[{'name': 'removed'}]),
                agy_step(8, tool_calls=[{'name': 'later'}]),
                agy_step(2, tool_calls=[]),
                agy_step('9', tool_calls=[None, {'name': 42}, {'args': {'toolSummary': '"Inspect file"'}}]),
            ])
            entry = {'id': '%1', 'agent': 'antigravity', 'transcript': str(path)}
            tools = factory.recent_tools(entry, server.FACTORY_READERS)
            self.assertEqual([(tool['name'], tool['title']) for tool in tools], [('tool', 'Inspect file'), ('later', '')])

    def test_agy_tool_line_preserves_activity_text(self):
        for args, expected in [
            ({'CommandLine': '"pwd\nls"', 'toolAction': 'Inspect directory'}, 'Ran pwd'),
            ({'toolAction': '"Inspect file"', 'toolSummary': 'Read it'}, 'Inspect file'),
            ({'toolSummary': '"Read it"'}, 'Read it'),
            ({'AbsolutePath': '/repo/test_factory.py'}, 'view_file'),
            ({}, 'view_file'),
        ]:
            with self.subTest(args=args):
                self.assertEqual(antigravity.agy_tool_line({'name': 'view_file', 'args': args}), expected)

    def test_no_tools_for_unread_agents_missing_files_or_odd_records(self):
        self.assertEqual(factory.recent_tools({'id': '%1', 'agent': 'shell', 'transcript': '/x'}, claude_readers()), [])
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
        entries = [{'id': '%1', 'agent': 'claude', 'transcript': ''}, {'id': '%2', 'agent': 'claude', 'transcript': ''},
                   {'id': '%3', 'agent': 'shell'}, {'id': '%4', 'agent': 'antigravity'}]
        data = factory.payload(entries, lambda entry: '/srv/repo/wt-' + entry['id'], claude_readers(), run=run, now=1794884800)
        self.assertEqual(set(data['tools']), {'%1', '%2', '%4'})
        self.assertIn(['git', '-C', '/srv/repo/wt-%4', 'rev-parse', '--path-format=absolute', '--git-common-dir'], runs)
        self.assertEqual(data['commits'], [{'hash': 'abc1234', 'at': 1794884700, 'subject': 'Launch: pass folders (B188)', 'refs': ['B188'], 'repo': 'repo'}])
        self.assertEqual(sum('log' in command for command in runs), 1)
        self.assertIn('deployed', data)


if __name__ == '__main__':
    unittest.main()
