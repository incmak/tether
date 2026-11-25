import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import io
import os
import signal
import subprocess
from unittest.mock import patch
import tempfile
import time
import unittest
from pathlib import Path

import live


def rows(*records):
    return ''.join(json.dumps(record) + '\n' for record in records)


def bash(identifier, command, background=True, stamp='2026-10-31T10:00:00Z'):
    return {'type': 'assistant', 'timestamp': stamp, 'message': {'content': [{'type': 'tool_use', 'id': identifier, 'name': 'Bash',
            'input': {'command': command, 'description': 'Run ' + command, 'run_in_background': background}}]}}


def result(identifier, task, output='/tmp/x.output'):
    return {'type': 'user', 'toolUseResult': {'backgroundTaskId': task}, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': identifier,
            'content': f'Command running in background with ID: {task}. Output is being written to: {output}. '}]}}


def notice(task, status='completed'):
    return {'type': 'user', 'message': {'content': f'<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n</task-notification>'}}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'chat.jsonl'
        live._states.clear()

    def tearDown(self):
        self.folder.cleanup()

    def write(self, text):
        with open(self.path, 'a') as stream:
            stream.write(text)

    def test_running_shells_stay_and_finished_ones_go(self):
        out = Path(self.folder.name) / 'b1.output'
        out.write_text('line 1\nline 2\n')
        self.write(rows(bash('t1', 'npm test'), result('t1', 'b1', str(out)), bash('t2', 'npm run build'), result('t2', 'b2'), bash('t3', 'ls', background=False)))
        tasks = live.read(self.path, now=time.time())['tasks']
        self.assertEqual([task['id'] for task in tasks], ['b1', 'b2'])
        self.assertEqual(tasks[0]['output'], 'line 1\nline 2')
        self.assertEqual(tasks[0]['kind'], 'shell')
        self.write(rows(notice('b2')))  # read again from where the last read stopped
        self.assertEqual([task['id'] for task in live.read(self.path)['tasks'] if task['state'] == 'running'], ['b1'])

    def test_notices_in_queue_records_and_exit_lines_end_tasks(self):
        done = Path(self.folder.name) / 'b3.output'
        done.write_text('ok\n[exited with code 0]\n')
        queued = {'type': 'queue-operation', 'operation': 'enqueue', 'content': '<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>'}
        self.write(rows(queued, bash('t1', 'a'), result('t1', 'b1'), bash('t2', 'b'), result('t2', 'b2'), bash('t3', 'c'), result('t3', 'b3', str(done))))
        self.assertEqual([task['id'] for task in live.read(self.path)['tasks'] if task['state'] == 'running'], ['b2'])

    def test_monitors_end_on_expiry_notice_or_time_limit(self):
        start = {'type': 'assistant', 'timestamp': '2026-10-31T10:00:00Z', 'message': {'content': [{'type': 'tool_use', 'id': 'm1', 'name': 'Monitor', 'input': {'command': 'tail -f log', 'description': 'Watch deploys'}}]}}
        started = {'type': 'user', 'toolUseResult': {'taskId': 'bm1', 'timeoutMs': 1800000}, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'm1', 'content': 'Monitor started (task bm1, expires in 30m)'}]}}
        self.write(rows(start, started))
        begin = live._iso('2026-10-31T10:00:00Z')
        self.assertEqual([task['kind'] for task in live.read(self.path, now=begin + 60)['tasks']], ['monitor'])
        self.assertEqual([task for task in live.read(self.path, now=begin + 3600)['tasks'] if task['state'] == 'running'], [])
        self.write(rows({'type': 'user', 'message': {'content': '<task-notification>\n<task-id>bm1</task-id>\n<event>[Monitor expired after 30m]</event>\n</task-notification>'}}))
        self.assertEqual([task for task in live.read(self.path, now=begin + 60)['tasks'] if task['state'] == 'running'], [])

    def test_goal_keeps_the_condition_and_the_last_real_check(self):
        goal = lambda **extra: {'type': 'attachment', 'timestamp': '2026-10-31T10:00:00Z', 'attachment': dict({'type': 'goal_status', 'met': False, 'condition': 'ship phase 1'}, **extra)}
        self.write(rows(goal(sentinel=True), goal(reason='Tests still fail.'), goal(sentinel=True)))
        state = live.read(self.path)['goal']
        self.assertEqual((state['condition'], state['met'], state['reason']), ('ship phase 1', False, 'Tests still fail.'))

    def test_a_half_written_line_waits_for_the_next_read(self):
        line = json.dumps(bash('t1', 'sleep 9'))
        self.write(line[:20])
        self.assertEqual([task for task in live.read(self.path)['tasks'] if task['state'] == 'running'], [])
        self.write(line[20:] + '\n' + rows(result('t1', 'b1')))
        self.assertEqual(len([task for task in live.read(self.path)['tasks'] if task['state'] == 'running']), 1)


    def task_thread(self, output):
        self.write(rows(bash('t1', 'sleep 30'), result('t1', 'b1', str(output))))
        return patch.object(live.history, 'transcript_files', return_value=iter([(self.path, None)]))

    def test_tail_caps_bytes_lines_and_strips_ansi(self):
        out = Path(self.folder.name) / 'b1.output'
        out.write_text('x' * (live.TAIL_BYTES * 2) + '\n' + ''.join(f'\x1b[31mline {i}\x1b[0m\n' for i in range(60)))
        with self.task_thread(out):
            output = live.shell_output('thread', 'b1')['output']
        self.assertEqual(output.splitlines(), [f'line {i}' for i in range(20, 60)])
        self.assertIsNone(live._tail(str(out) + '.missing'))
        with self.task_thread(out), self.assertRaises(ValueError):
            live.shell_output('thread', '../b1')
        with self.task_thread(out), self.assertRaises(ValueError):
            live.shell_output('thread', 'foreign')

    def test_output_fallback_and_ended_tasks_stay_visible(self):
        record = bash('t1', 'watch') | {'cwd': '/work/app', 'sessionId': 'chat-id'}
        self.write(rows(record, result('t1', 'b1', '/unused'), notice('b1')))
        state = live.read(self.path)
        self.assertEqual(state['tasks'][0]['state'], 'ended')
        self.assertEqual(live._states[self.path]['tasks']['b1']['output'], f'/tmp/claude-{os.getuid()}/-work-app/chat-id/tasks/b1.output')

    def test_routes_bind_tasks_to_the_selected_pane(self):
        import server
        handler = object.__new__(server.Handler)
        handler.deny = lambda: False
        handler.respond = unittest.mock.Mock()
        handler.path = '/mobile/api/shell-output?session=%251&task=b1'
        with patch.object(server, 'choose', return_value={'agent': 'claude', 'thread': 'chat'}) as choose, patch.object(live, 'shell_output', return_value={'output': 'ok'}) as output:
            handler.do_GET()
            choose.assert_called_once_with('%1')
            output.assert_called_once_with('chat', 'b1')
            handler.respond.assert_called_with(200, {'output': 'ok'})
        body = json.dumps({'session': '%1', 'task': 'b1'}).encode()
        handler.path = '/mobile/api/stop-task'
        handler.headers = {'Content-Length': str(len(body)), 'Origin': 'http://localhost', 'Host': 'localhost', 'X-Codex-Mobile': '1'}
        handler.rfile = io.BytesIO(body)
        with patch.object(server, 'choose', return_value={'agent': 'claude', 'thread': 'chat'}), patch.object(live, 'stop_task', return_value={'accepted': True}) as stop:
            handler.do_POST()
            stop.assert_called_once_with('chat', 'b1')
            handler.respond.assert_called_with(200, {'accepted': True})
        handler.rfile = io.BytesIO(body)
        handler.headers['X-Codex-Mobile'] = ''
        with patch.object(live, 'stop_task') as stop:
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 403)
            stop.assert_not_called()

    def test_match_and_stop_only_our_child(self):
        out = Path(self.folder.name) / 'b1.output'
        with out.open('wb') as stream:
            child = subprocess.Popen(['sleep', '30'], stdout=stream, stderr=stream, start_new_session=True)
        try:
            self.assertEqual(live._groups(str(out)), {child.pid})
            self.assertEqual(live._safe_group(child.pid, live._processes()).keys(), {child.pid})
            with self.task_thread(out):
                self.assertEqual(live.stop_task('thread', 'b1'), {'accepted': True})
            child.wait(timeout=5)
            self.assertEqual(child.returncode, -signal.SIGTERM)
            self.assertEqual(live.read(self.path)['tasks'][0]['state'], 'ended')
        finally:
            if child.poll() is None:
                child.kill(); child.wait()

    def test_stop_refusals_never_signal(self):
        out = Path(self.folder.name) / 'b1.output'
        out.touch()
        with patch.object(live.os, 'killpg') as kill:
            with self.task_thread(out), self.assertRaises(ValueError):
                live.stop_task('thread', 'b1')
            with self.task_thread(out), self.assertRaises(ValueError):
                live.stop_task('thread', 'foreign')
            base = {'uid': os.getuid(), 'group': 4321, 'start': '1', 'parent': 1, 'args': ['sleep', '30']}
            for pid, extra in ((1, {}), (os.getpid(), {}), (4321, {'args': ['claude']}), (4321, {'args': ['tmux']}), (4321, {'args': ['python3', '/app/server.py']}), (4321, {'uid': os.getuid() + 1})):
                with self.subTest(pid=pid, extra=extra), self.assertRaises(ValueError):
                    live._safe_group(4321, {pid: base | extra})
            with self.assertRaises(ValueError):
                live._safe_group(1, {4321: base | {'group': 1}})
            with self.assertRaises(ValueError):
                live._safe_group(4321, {})
            kill.assert_not_called()

    def test_stderr_match_and_shared_group_refusal(self):
        out = Path(self.folder.name) / 'b1.output'
        with out.open('wb') as stream:
            child = subprocess.Popen(['sleep', '30'], stderr=stream)
        try:
            self.assertIn(os.getpgid(child.pid), live._groups(str(out)))
            with self.task_thread(out), patch.object(live.os, 'killpg') as kill, self.assertRaises(ValueError):
                live.stop_task('thread', 'b1')
            kill.assert_not_called()
        finally:
            child.terminate(); child.wait(timeout=5)

    def test_kill_escalation_rechecks_group(self):
        out = Path(self.folder.name) / 'b1.output'
        with out.open('wb') as stream:
            child = subprocess.Popen(['python3', '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'], stdout=stream, stderr=stream, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while 'ready' not in out.read_text() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn('ready', out.read_text())
            with self.task_thread(out):
                live.stop_task('thread', 'b1')
            child.wait(timeout=5)
            self.assertEqual(child.returncode, -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()


if __name__ == '__main__':
    unittest.main()
