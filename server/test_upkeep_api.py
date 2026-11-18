"""upkeep_api.py: the HTTP routes of the clean-up list and the daily ops report, and the monitor call."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import lesson_candidates
import lessons
import ops_agent
import paths
import projects
import prune
import upkeep_api

PID = 'd' * 12
DAY = 86400


class Handler:
    def __init__(self, path, body=None, raw=None):
        raw = json.dumps(body).encode() if raw is None and body is not None else raw or b''
        self.path, self.headers, self.rfile = path, {'Content-Length': str(len(raw))}, io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


def get(path):
    handler = Handler(path)
    upkeep_api.get(handler)
    return handler.status, handler.body


def post(path, body=None, raw=None):
    handler = Handler(path, body, raw)
    upkeep_api.post(handler)
    return handler.status, handler.body


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ('CONFIG', 'TETHER', 'CACHE'):
            patch = mock.patch.object(paths, name, root / name.lower())
            patch.start()
            self.addCleanup(patch.stop)
        projects.bundle(PID).mkdir(parents=True)
        projects.set_name(PID, 'tether')
        projects.write_note(PID, 'old.md', '# Old note\n')
        old = time.time() - 45 * DAY
        os.utime(projects.bundle(PID) / 'notes' / 'old.md', (old, old))

    def test_routes(self):
        for path in ('prune', 'ops', 'ops/note', 'prune?x=1'):
            self.assertTrue(upkeep_api.handles(upkeep_api.PREFIX + path))
        for path in ('', 'other', 'prune/x'):
            self.assertFalse(upkeep_api.handles(upkeep_api.PREFIX + path))

    def test_prune_check_remove_restore_and_keep(self):
        self.assertEqual(get(upkeep_api.PREFIX + 'prune')[1]['notes'], [])
        status, body = post(upkeep_api.PREFIX + 'prune', {'action': 'check'})
        self.assertEqual((status, [note['name'] for note in body['notes']]), (200, ['old.md']))
        request = {'action': 'remove', 'kind': 'note', 'project': PID, 'id': 'old.md'}
        self.assertEqual(post(upkeep_api.PREFIX + 'prune', request)[0], 400)  # no version stamp: nothing goes
        self.assertEqual(post(upkeep_api.PREFIX + 'prune', dict(request, mark='old'))[0], 409)
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())
        status, body = post(upkeep_api.PREFIX + 'prune', dict(request, mark=body['notes'][0]['mark']))
        self.assertEqual((status, body['notes'], len(body['removed'])), (200, [], 1))
        self.assertFalse((projects.bundle(PID) / 'notes' / 'old.md').exists())
        status, body = post(upkeep_api.PREFIX + 'prune', {'action': 'restore', 'token': body['removed'][0]['token']})
        self.assertEqual((status, body['removed']), (200, []))
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())
        self.assertEqual(post(upkeep_api.PREFIX + 'prune', {'action': 'keep', 'kind': 'note', 'project': PID, 'id': 'old.md'})[0], 404)

    def test_prune_refuses_bad_requests_and_removes_nothing(self):
        post(upkeep_api.PREFIX + 'prune', {'action': 'check'})
        for body in ({}, {'action': 'remove'}, {'action': 'remove', 'kind': 'note', 'project': PID, 'id': 'old.md', 'mark': 'x', 'all': True},
                     {'action': 'remove-all'}, {'action': 'restore', 'token': 5}, {'action': 'check', 'force': True},
                     {'action': 'remove', 'kind': 'note', 'project': '../x', 'id': 'old.md', 'mark': 'x'}, []):
            status, answer = post(upkeep_api.PREFIX + 'prune', body)
            self.assertEqual(status, 400, body)
            self.assertIn('error', answer)
        self.assertEqual(post(upkeep_api.PREFIX + 'prune', raw=b'{')[0], 400)
        self.assertEqual(post(upkeep_api.PREFIX + 'prune', raw=b'x' * 5000)[0], 400)
        self.assertEqual(post(upkeep_api.PREFIX + 'prune', {'action': 'restore', 'token': 'nope'})[0], 404)
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())

    def test_ops_switch_run_candidates_and_note(self):
        status, body = get(upkeep_api.PREFIX + 'ops')
        self.assertEqual((status, body['enabled'], body['runs'], body['candidates']), (200, False, [], []))
        self.assertTrue(post(upkeep_api.PREFIX + 'ops', {'enabled': True})[1]['enabled'])
        self.assertTrue(ops_agent.enabled())
        self.assertEqual(post(upkeep_api.PREFIX + 'ops', {'enabled': 'yes'})[0], 400)
        self.assertTrue(post(upkeep_api.PREFIX + 'ops', {'action': 'run'})[1]['busy'])
        self.assertTrue(ops_agent.load()['request'])
        first = lesson_candidates.propose(PID, 'When a commit has no stamp, write Verified: none with the reason.', 'avoid', 'claude')['candidate']
        second = lesson_candidates.propose(PID, 'When a review pauses twice, split the change first.', 'avoid', 'claude')['candidate']
        status, body = post(upkeep_api.PREFIX + 'ops', {'action': 'accept', 'project': PID, 'id': first['id']})
        self.assertEqual((status, [row['id'] for row in body['candidates']]), (200, [second['id']]))
        self.assertEqual([row['text'] for row in lessons.entries(PID)], [first['text']])
        self.assertEqual(post(upkeep_api.PREFIX + 'ops', {'action': 'dismiss', 'project': PID, 'id': second['id']})[1]['candidates'], [])
        self.assertEqual(post(upkeep_api.PREFIX + 'ops', {'action': 'dismiss', 'project': PID, 'id': second['id']})[0], 404)
        for body in ({'action': 'accept', 'project': PID, 'id': 'x'}, {'action': 'accept', 'project': 'x', 'id': first['id']},
                     {'action': 'run', 'day': '2026-01-01'}, {'enabled': True, 'action': 'run'}):
            self.assertEqual(post(upkeep_api.PREFIX + 'ops', body)[0], 400, body)
        projects._write(ops_agent.note_path(PID, '2026-11-17'), '# Postmortem\n')
        status, body = get(upkeep_api.PREFIX + f'ops/note?project={PID}&day=2026-11-17')
        self.assertEqual((status, body['text']), (200, '# Postmortem\n'))
        self.assertEqual(get(upkeep_api.PREFIX + f'ops/note?project={PID}&day=2026-11-16')[0], 404)
        self.assertEqual(get(upkeep_api.PREFIX + f'ops/note?project={PID}&day=..%2F..%2Fx')[0], 400)
        self.assertEqual(post(upkeep_api.PREFIX + 'ops/note', {'x': 1})[0], 405)

    def test_one_failing_job_does_not_stop_the_others(self):
        upkeep = upkeep_api.Upkeep(lambda *args: None, lambda: {'low': 'off'})
        with mock.patch.object(upkeep.sweeper, 'observe', side_effect=RuntimeError('boom')), \
                mock.patch.object(upkeep.pruner, 'observe') as pruner, mock.patch.object(upkeep.ops, 'observe') as ops, \
                mock.patch('builtins.print') as shown:
            upkeep.observe([], None, None)
        self.assertEqual((pruner.call_count, ops.call_count), (1, 1))
        self.assertIn('worktree sweep failed', shown.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
