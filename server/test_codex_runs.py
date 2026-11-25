"""Pane-scoped run discovery and HTTP routing, using fixture files and fake processes only."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_runs
import server
from own_time import own_time

NOW = 1_793_865_600


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace('+00:00', 'Z')


def record(kind, payload, at=NOW - 10):
    return {'timestamp': stamp(at), 'type': kind, 'payload': payload}


def message(role, text, at=NOW - 10):
    return record('response_item', {'type': 'message', 'role': role,
                                   'content': [{'type': 'input_text' if role == 'user' else 'output_text',
                                                'text': text}]}, at)


class RunsTests(unittest.TestCase):
    def setUp(self):
        codex_runs._panes.clear()
        codex_runs._parsed.clear()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        (self.proc / 'uptime').write_text('1000 0\n')
        self.procs = {}
        self.links = {}
        self.readlink = Mock(side_effect=lambda path: self.links[Path(path)] if Path(path) in self.links
                             else self.missing())
        self.process(100, 1, ['zsh'])
        self.process(101, 100, ['claude'])
        self.process(110, 101, ['bash', '/bin/codex-lane', '--', 'task'])

    @staticmethod
    def missing():
        raise FileNotFoundError

    def process(self, pid, parent, argv):
        self.procs[pid] = (parent, argv)
        folder = self.proc / str(pid)
        (folder / 'fd').mkdir(parents=True, exist_ok=True)
        fields = ['0'] * 20
        fields[0], fields[1], fields[19] = 'S', str(parent), str(900 * os.sysconf('SC_CLK_TCK'))
        (folder / 'stat').write_text(f'{pid} (process name) ' + ' '.join(fields))

    def session(self, name='main', records=None, **meta):
        path = self.root / f'rollout-{name}.jsonl'
        header = {'id': name, 'cwd': '/work', 'timestamp': stamp(NOW - 60),
                  'originator': 'codex_exec', 'source': 'exec', **meta}
        records = records if records is not None else [message('user', 'Do the task')]
        path.write_text('\n'.join(json.dumps(r) for r in [record('session_meta', header, NOW - 60), *records]) + '\n')
        return path

    def bind(self, pid, path, fd=3):
        link = self.proc / str(pid) / 'fd' / str(fd)
        link.touch()
        self.links[link] = str(path)

    def runs(self, now=NOW, pane=100):
        return codex_runs.runs_for_pane(pane, dict(self.procs), now=now,
                                        readlink=self.readlink, proc=self.proc)

    def test_one_lane_prompt_steps_and_fields(self):
        path = self.session(records=[
            message('user', '# AGENTS.md instructions for /work\nRules', NOW - 50),
            message('user', '<environment_context>config</environment_context>', NOW - 49),
            message('developer', 'Private developer instructions', NOW - 48),
            message('user', '  Fix the task  ', NOW - 47),
            record('response_item', {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Check\nfirst'}],
                                     'encrypted_content': 'never expose'}, NOW - 46),
            record('response_item', {'type': 'function_call', 'name': 'exec_command',
                                     'arguments': json.dumps({'cmd': 'python3 check.py\necho ignored'})}, NOW - 45),
            record('event_msg', {'type': 'token_count', 'info': {'total': 1}}, NOW - 44),
            record('event_msg', {'type': 'item_completed', 'item': {'text': 'noise'}}, NOW - 43),
            record('response_item', {'type': 'custom_tool_call', 'name': 'apply_patch',
                                     'input': '*** Begin Patch\n*** Update File: a.py\n+secret body\n'
                                              '*** Add File: b.py\n*** End Patch'}, NOW - 42),
            record('response_item', {'type': 'custom_tool_call_output', 'output': 'private output'}, NOW - 41),
            message('assistant', 'I checked\nthe task', NOW - 40),
        ])
        self.bind(110, path)
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(set(run), {'id', 'pid', 'started', 'cwd', 'prompt', 'steps', 'state', 'last_activity', 'final'})
        self.assertEqual((run['id'], run['pid'], run['prompt'], run['cwd'], run['state']),
                         ('main', 110, 'Fix the task', '/work', 'running'))
        self.assertEqual(run['started'], (NOW - 60) * 1000)
        self.assertEqual(run['last_activity'], (NOW - 40) * 1000)
        self.assertIsNone(run['final'])
        self.assertEqual([(s['kind'], s['text']) for s in run['steps']], [
            ('thinking', 'Check first'), ('tool', 'exec_command python3 check.py'),
            ('tool', 'Edit a.py, b.py'), ('message', 'I checked the task')])
        self.assertEqual([s['at'] for s in run['steps']], [(NOW - n) * 1000 for n in (46, 45, 42, 40)])
        for private in ('never expose', 'Private developer', 'private output', 'secret body'):
            self.assertNotIn(private, json.dumps(runs))

    def test_node_native_nesting_uses_innermost_fd_once(self):
        self.process(111, 110, ['node', '/bin/codex.js', 'exec', 'task'])
        self.process(112, 111, ['/native/codex', 'exec', 'task'])
        self.bind(112, self.session())
        runs = self.runs()
        self.assertEqual([(r['id'], r['pid']) for r in runs], [('main', 110)])
        self.assertIn(self.proc / '112/fd/3', [c.args[0] for c in self.readlink.call_args_list])

    def test_another_pane_and_unowned_runs_are_excluded(self):
        self.bind(110, self.session())
        self.process(200, 1, ['zsh'])
        self.process(210, 200, ['codex', 'exec', 'other'])
        self.process(300, 1, ['codex', 'exec', 'unowned'])
        self.bind(210, self.session('other'))
        self.assertEqual([r['id'] for r in self.runs()], ['main'])
        self.assertEqual([r['id'] for r in self.runs(pane=200)], ['other'])

    def test_guardian_sources_are_ignored(self):
        self.bind(110, self.session('guardian', source={'subagent': {'thread_source': 'guardian_review'}}), 1)
        self.bind(110, self.session('guardian-thread', thread_source='guardian_review'), 2)
        self.bind(110, self.session(), 3)
        self.assertEqual(self.runs()[0]['id'], 'main')

    def test_starting_without_file_uses_process_start_and_cwd(self):
        self.links[self.proc / '110/cwd'] = '/work/pending'
        run = self.runs()[0]
        self.assertEqual((run['id'], run['state'], run['cwd']), ('pid-110', 'starting', '/work/pending'))
        self.assertEqual(run['started'], (NOW - 100) * 1000)
        self.bind(110, self.session())
        self.assertEqual(self.runs(NOW + 3)[0]['id'], 'main')

    def test_stalled_after_five_minutes_and_resumes(self):
        path = self.session(records=[message('user', 'Task', NOW)])
        self.bind(110, path)
        self.assertEqual(self.runs()[0]['state'], 'running')
        self.assertEqual(self.runs(NOW + 300)[0]['state'], 'running')
        self.assertEqual(self.runs(NOW + 303)[0]['state'], 'stalled')
        with path.open('a') as stream:
            stream.write(json.dumps(message('assistant', 'Update', NOW + 305)) + '\n')
        self.assertEqual(self.runs(NOW + 306)[0]['state'], 'running')

    def test_ended_done_rereads_once_then_expires(self):
        path = self.session()
        self.bind(110, path)
        self.runs()
        with path.open('a') as stream:
            stream.write(json.dumps(record('event_msg', {'type': 'task_complete',
                                                        'last_agent_message': ' Finished '}, NOW + 1)) + '\n')
        del self.procs[110]
        with patch.object(codex_runs, '_parse', wraps=codex_runs._parse) as parse:
            run = self.runs(NOW + 3)[0]
            self.assertEqual((run['state'], run['final'], run['last_activity']),
                             ('done', 'Finished', (NOW + 1) * 1000))
            self.assertEqual(self.runs(NOW + 6)[0]['state'], 'done')
        parse.assert_called_once_with(path, force=True)
        self.assertEqual(len(self.runs(NOW + 602)), 1)
        self.assertEqual(self.runs(NOW + 605), [])

    def test_ended_failed_without_completion_or_file(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                codex_runs._panes.clear()
                self.process(110, 101, ['codex', 'exec', 'task'])
                path = self.session()
                self.bind(110, path)
                self.runs()
                if missing:
                    path.unlink()
                del self.procs[110]
                self.assertEqual(self.runs(NOW + 3)[0]['state'], 'failed')

    def test_missing_session_identity_and_timestamp_keep_process_fallback_after_exit(self):
        self.bind(110, self.session(id='', timestamp=None))
        run = self.runs()[0]
        self.assertEqual((run['id'], run['started']), ('pid-110', (NOW - 100) * 1000))
        del self.procs[110]
        ended = self.runs(NOW + 3)[0]
        self.assertEqual((ended['id'], ended['started']), (run['id'], run['started']))
        self.assertEqual(ended['state'], 'failed')

    def test_keeps_only_three_ended_runs_newest_first(self):
        for index in range(5):
            pid = 110 + index
            self.process(pid, 101, ['codex', 'exec', 'task'])
            path = self.session(str(index), timestamp=stamp(NOW - 50 + index))
            self.bind(pid, path)
            self.runs(NOW + index * 6)
            del self.procs[pid]
            self.runs(NOW + index * 6 + 3)
        self.assertEqual([r['id'] for r in self.runs(NOW + 30)], ['4', '3', '2'])
        self.assertTrue(all(r['state'] == 'failed' for r in self.runs(NOW + 30)))

    def test_half_written_bad_and_wrong_shape_lines(self):
        path = self.session(records=[message('user', 'Task'), message('assistant', 'Ready')])
        with path.open('ab') as stream:
            stream.write(b'garbage\nnull\n[]\n{"type":"response_item","payload":null}\n\xff\n{"timestamp":')
        self.bind(110, path)
        run = self.runs()[0]
        self.assertEqual(run['state'], 'running')
        self.assertEqual([s['text'] for s in run['steps']], ['Ready'])

    def test_megabyte_file_reads_only_head_and_tail(self):
        path = self.session(records=[message('user', 'Task from head')])
        with path.open('a') as stream:
            stream.write(json.dumps(record('response_item', {'type': 'function_call_output',
                                                            'output': 'x' * (1024 * 1024)})) + '\n')
            for index in range(8):
                stream.write(json.dumps(message('assistant', f'Step {index}', NOW - 8 + index)) + '\n')
        self.bind(110, path)
        real_open = Path.open
        reads = []

        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)

        raw = path.read_bytes()

        def bounded_open(candidate, *args, **kwargs):
            return BoundedReader(raw) if candidate == path else real_open(candidate, *args, **kwargs)

        with patch.object(Path, 'open', bounded_open):
            run = self.runs()[0]
        self.assertEqual(reads, [codex_runs.HEAD, 1, codex_runs.TAIL])
        self.assertEqual(run['prompt'], 'Task from head')
        self.assertEqual([s['text'] for s in run['steps']], [f'Step {i}' for i in range(2, 8)])

    def test_prompt_falls_back_to_the_command_line(self):
        # A long session file can put the first user message past the head window.
        path = self.session(records=[message('assistant', 'Working', NOW - 5)])
        self.bind(110, path)
        self.assertEqual(self.runs()[0]['prompt'], 'task')
        self.assertEqual(codex_runs._argv_prompt(['codex', 'exec', '-']), '')
        self.assertEqual(codex_runs._argv_prompt(['codex-lane', '--', 'exec']), '')

    def test_parse_cache_uses_size_and_mtime(self):
        path = self.session()
        self.bind(110, path)
        with patch.object(Path, 'open', autospec=True, side_effect=Path.open) as opened:
            self.runs()
            initial = sum(c.args[0] == path for c in opened.call_args_list)
            self.runs(NOW + 3)
            self.assertEqual(sum(c.args[0] == path for c in opened.call_args_list), initial)
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            self.runs(NOW + 6)
            self.assertEqual(sum(c.args[0] == path for c in opened.call_args_list), initial + 1)
        with path.open('a') as stream:
            stream.write(json.dumps(message('assistant', 'New step', NOW + 7)) + '\n')
        self.assertEqual(self.runs(NOW + 9)[0]['steps'][-1]['text'], 'New step')

    def test_answer_cache_lasts_two_seconds_and_isolated_by_pane(self):
        self.bind(110, self.session())
        first = self.runs()
        self.readlink.reset_mock()
        self.procs.pop(110)
        self.assertEqual(self.runs(NOW + 1), first)
        self.readlink.assert_not_called()
        self.assertEqual(self.runs(NOW + 1, pane=200), [])
        self.assertEqual(self.runs(NOW + 2)[0]['state'], 'failed')

    def test_missing_unreadable_file_and_disappearing_fd(self):
        self.bind(110, self.root / 'rollout-missing.jsonl')
        self.bind(110, self.session(), 4)
        with patch.object(Path, 'open', side_effect=PermissionError):
            self.assertEqual(self.runs()[0]['state'], 'starting')
        self.links.clear()
        self.assertEqual(self.runs(NOW + 3)[0]['state'], 'starting')

    def test_limits_redaction_exec_and_event_steps(self):
        path = self.session(records=[
            message('user', 'p' * 500),
            record('response_item', {'type': 'custom_tool_call', 'name': 'exec',
                                     'input': 'const r = await tools.exec_command({cmd: "echo hi\\necho bye"});'}),
            record('response_item', {'type': 'function_call', 'name': 'exec_command',
                                     'arguments': json.dumps({'cmd': 'TOKEN=hidden echo hi'})}),
            record('response_item', {'type': 'reasoning', 'encrypted_content': 'secret'}),
            record('event_msg', {'type': 'exec_command_begin', 'command': ['ls', '-l']}),
            record('event_msg', {'type': 'agent_message', 'message': 'm' * 200}),
            record('event_msg', {'type': 'task_complete', 'last_agent_message': 'f' * 500}),
        ])
        self.bind(110, path)
        run = self.runs()[0]
        self.assertEqual(len(run['prompt']), 400)
        self.assertEqual(len(run['final']), 400)
        self.assertTrue(all(len(s['text']) <= 160 and '\n' not in s['text'] for s in run['steps']))
        self.assertEqual([s['text'] for s in run['steps'][:4]],
                         ['exec echo hi', 'exec_command TOKEN=[redacted] echo hi', 'Thinking', 'exec_command ls -l'])
        self.assertNotIn('hidden', json.dumps(run))
        self.assertEqual(run['state'], 'running', 'Completion alone must not mark a live process done')

    def handler(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = codex_runs.PATH + '?session=%251'
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        return handler

    def test_http_route_resolves_pane_counts_and_caches_process_scan(self):
        self.bind(110, self.session())
        handler = self.handler()
        original = codex_runs.runs_for_pane

        def fixture_runs(pid, procs, now):
            return original(pid, procs, now=now, proc=self.proc, readlink=self.readlink)

        with patch.object(server, 'choose', return_value={'id': '%1', 'pane_pid': 100}) as choose, \
                patch.object(codex_runs.codex_level, 'processes', return_value=self.procs) as processes, \
                patch.object(codex_runs, 'runs_for_pane', side_effect=fixture_runs), \
                own_time(codex_runs, time=Mock(return_value=NOW)):
            handler.do_GET()
            handler.do_GET()
        self.assertEqual(choose.call_args.args, ('%1',))
        processes.assert_called_once_with()
        status, body = handler.respond.call_args.args
        self.assertEqual((status, body['pane'], body['running']), (200, '%1', 1))
        self.assertEqual(body['runs'][0]['id'], 'main')

    def test_http_route_missing_pid_returns_empty_without_process_read(self):
        handler = self.handler()
        with patch.object(server, 'choose', return_value={'id': '%1'}), \
                patch.object(codex_runs.codex_level, 'processes') as processes:
            handler.do_GET()
        handler.respond.assert_called_once_with(200, {'pane': '%1', 'running': 0, 'runs': []})
        processes.assert_not_called()

    def test_http_count_includes_starting_and_stalled_and_excludes_ended(self):
        handler = self.handler()
        runs = [{'state': state} for state in ('running', 'stalled', 'starting', 'done', 'failed')]
        with patch.object(server, 'choose', return_value={'id': '%1', 'pane_pid': 100}), \
                patch.object(codex_runs.codex_level, 'processes', return_value={}), \
                patch.object(codex_runs, 'runs_for_pane', return_value=runs):
            handler.do_GET()
        handler.respond.assert_called_once_with(200, {'pane': '%1', 'running': 3, 'runs': runs})

    def test_http_route_auth_precedes_discovery_and_exact_path_only(self):
        handler = self.handler()
        handler.deny.return_value = True
        with patch.object(server, 'choose') as choose:
            handler.do_GET()
        choose.assert_not_called()
        handler.respond.assert_not_called()
        self.assertTrue(codex_runs.handles(handler.path))
        self.assertFalse(codex_runs.handles('/mobile/api/codex-runs-extra'))


if __name__ == '__main__':
    unittest.main()
