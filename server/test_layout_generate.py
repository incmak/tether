import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import custom_layouts as layouts
import layout_generate as generation
from test_custom_layouts import GOOD, changed
from own_time import own_time


class Handler:
    def __init__(self, path, body=None):
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class GenerateTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.folder = Path(folder)
        self.enterContext(patch.object(layouts.paths, 'LAYOUTS', self.folder))
        self.enterContext(patch.object(generation.shutil, 'which', side_effect=lambda name: '/bin/' + name))
        self.run = self.enterContext(patch.object(generation.subprocess, 'run'))
        self.threads = []
        real_thread = threading.Thread

        def thread(*args, **kwargs):
            item = real_thread(*args, **kwargs)
            self.threads.append(item)
            return item

        self.enterContext(patch.object(generation.threading, 'Thread', side_effect=thread))
        with generation._lock:
            generation._jobs.clear()
        layouts._last_good.clear()
        self.addCleanup(self.join_threads)

    def join_threads(self):
        for thread in self.threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), 'Mock layout worker did not finish.')

    def reply(self, data=GOOD, notes='A clear cockpit.'):
        return SimpleNamespace(returncode=0, stdout=json.dumps({'result': json.dumps({'layout': data, 'notes': notes})}))

    def complete(self, job):
        self.join_threads()
        return generation.poll(job)

    def test_valid_reply_finishes_as_an_unsaved_draft(self):
        self.run.return_value = self.reply()
        handler = Handler(layouts.PATH + '/generate', {'prompt': 'a cockpit'})
        layouts.post(handler)
        self.assertEqual(handler.status, 202)
        job = handler.body['job']
        result = self.complete(job)
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['layout']['id'], 'cockpit')
        self.assertEqual(result['notes'], 'A clear cockpit.')
        self.assertEqual(list(self.folder.iterdir()), [])
        command = self.run.call_args.args[0]
        self.assertIn('--system-prompt', command)
        self.assertEqual(command[command.index('--model') + 1], 'sonnet')
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertEqual(command[command.index('--output-format') + 1], 'json')
        self.assertEqual(self.run.call_args.kwargs['stdin'], generation.subprocess.DEVNULL)
        self.assertEqual(self.run.call_args.kwargs['timeout'], 180)
        self.assertFalse(Path(self.run.call_args.kwargs['cwd']).exists())
        context = json.loads(command[2])
        self.assertEqual(context['manifest'], layouts.MANIFEST)
        self.assertEqual(len(context['examples']), 2)
        poll = Handler(layouts.PATH + '/generate?job=' + job)
        layouts.get(poll)
        self.assertEqual(poll.body, result)
        self.assertEqual(poll.status, 200)

    def test_invalid_reply_then_valid_retries_with_the_validator_message(self):
        self.run.side_effect = [self.reply(changed(style={'radius': 80})), self.reply()]
        result = self.complete(generation.start('a cockpit'))
        self.assertEqual(result['state'], 'done')
        self.assertEqual(self.run.call_count, 2)
        self.assertIn('Fix this error and return the whole layout again: style.radius', self.run.call_args.args[0][2])

    def test_two_invalid_replies_fail_with_the_validator_message(self):
        self.run.return_value = self.reply(changed(style={'radius': 80}))
        result = self.complete(generation.start('a cockpit'))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error'], 'style.radius must be a whole number from 0 to 24.')
        self.assertEqual(self.run.call_count, 2)

    def test_running_job_refuses_a_second_post_with_409(self):
        entered, release = threading.Event(), threading.Event()

        def wait(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Mock worker wait timed out.')
            return self.reply()

        self.run.side_effect = wait
        first = generation.start('a cockpit')
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(generation.poll(first), {'state': 'running'})
            handler = Handler(layouts.PATH + '/generate', {'prompt': 'another'})
            layouts.post(handler)
            self.assertEqual(handler.status, 409)
        finally:
            release.set()
        self.assertEqual(self.complete(first)['state'], 'done')

    def test_ids_are_slugged_and_unique_against_files_ids_and_reserved(self):
        self.folder.joinpath('cockpit.json').write_text('{invalid')
        layouts.save(changed(id='cockpit-2'))
        self.run.return_value = self.reply()
        result = self.complete(generation.start('a cockpit'))
        self.assertEqual(result['layout']['id'], 'cockpit-3')
        self.run.return_value = self.reply(changed(name='VSCode'))
        self.assertEqual(self.complete(generation.start('an IDE'))['layout']['id'], 'vscode-2')
        self.assertEqual(generation.draft_id('!!!'), 'layout')
        self.assertLessEqual(len(generation.draft_id('x' * 100)), 40)

    def test_codex_fallback_uses_last_message_read_only_and_closed_stdin(self):
        self.enterContext(patch.object(generation.shutil, 'which', side_effect=lambda name: '/bin/codex' if name == 'codex' else None))

        def codex(command, **kwargs):
            Path(command[command.index('-o') + 1]).write_text('Answer:\n' + json.dumps({'layout': GOOD, 'notes': 'Calm.'}))
            return SimpleNamespace(returncode=0, stdout='events')

        self.run.side_effect = codex
        self.assertEqual(self.complete(generation.start('a cockpit'))['state'], 'done')
        command = self.run.call_args.args[0]
        self.assertEqual(command[:4], ['/bin/codex', 'exec', '--sandbox', 'read-only'])
        self.assertEqual(self.run.call_args.kwargs['stdin'], generation.subprocess.DEVNULL)

    def test_missing_agent_timeout_and_failed_process_finish_cleanly(self):
        for failure in (generation.subprocess.TimeoutExpired('agent', 180), OSError('missing')):
            with self.subTest(failure=failure):
                self.run.side_effect = failure
                self.assertEqual(self.complete(generation.start('a cockpit'))['state'], 'failed')
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='private details')
        self.assertNotIn('private', self.complete(generation.start('a cockpit'))['error'])
        with patch.object(generation.shutil, 'which', return_value=None):
            self.assertIn('Install Claude or Codex', self.complete(generation.start('a cockpit'))['error'])

    def test_bad_replies_retry_and_never_leave_the_job_running(self):
        for raw in ('not json', '{"result":"no layout"}', '{"result":"{\\"layout\\": []}"}'):
            self.run.return_value = SimpleNamespace(returncode=0, stdout=raw)
            self.assertEqual(self.complete(generation.start('a cockpit'))['state'], 'failed')

    def test_prompt_base_and_job_bounds(self):
        for prompt in ('', ' ', 'x' * 2001, None, []):
            with self.assertRaises(layouts.Bad):
                generation.start(prompt)
        for base in ([], {'id': '../bad'}, dict(GOOD, script='x')):
            with self.assertRaises(layouts.Bad):
                generation.start('x', base)
        self.run.return_value = self.reply()
        base = generation.built_ins()[0]
        first = generation.start('x', base)
        self.complete(first)
        self.assertEqual(json.loads(self.run.call_args.args[0][2])['base']['id'], 'classic')
        for _ in range(3):
            self.complete(generation.start('x'))
        self.assertEqual(len(generation._jobs), 3)
        self.assertIsNone(generation.poll(first))
        with own_time(generation, monotonic=Mock(return_value=time.monotonic() + 601)):
            self.assertIsNone(generation.poll(next(iter(generation._jobs))))
        self.assertEqual(generation._jobs, {})


class LayoutRouteTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        from test_tts import handler
        self.handler = handler
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(layouts.paths, 'LAYOUTS', Path(folder) / 'layouts'))
        layouts._last_good.clear()
        self.start = self.enterContext(patch.object(generation, 'start', return_value='mock-job'))

    def test_new_posts_pass_through_auth_and_dispatch(self):
        for path, body, status in (('/generate', {'prompt': 'Telegram'}, 202),
                                   ('/save', {'layout': changed(id='cockpit')}, 200),
                                   ('/delete', {'id': 'cockpit'}, 200)):
            value = self.handler(layouts.PATH + path, body)
            self.server.Handler.do_POST(value)
            value.deny.assert_called_once_with()
            value.send_response.assert_called_once_with(status)
        self.start.assert_called_once_with('Telegram', None)

    def test_cookie_origin_and_write_header_protect_every_write(self):
        import auth
        for path in ('/generate', '/save', '/delete'):
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                value = self.handler(layouts.PATH + path, {})
                value.headers.update(headers)
                self.server.Handler.do_POST(value)
                value.send_response.assert_called_once_with(403)
            with tempfile.TemporaryDirectory() as folder, patch.object(auth, 'MODE', 'token'), \
                    patch.object(auth, 'TOKEN_FILE', Path(folder) / 'token'), patch.object(auth, 'TAILSCALE', False):
                for cookie in ('', auth.COOKIE + '=forged'):
                    value = self.handler(layouts.PATH + path, {})
                    value.client_address = ('127.0.0.1', 12345)
                    value.deny = self.server.Handler.deny.__get__(value)
                    value.headers['Cookie'] = cookie
                    self.server.Handler.do_POST(value)
                    value.send_response.assert_called_once_with(401)
        self.start.assert_not_called()

    def test_good_cookie_reaches_generate_and_bad_bodies_are_refused(self):
        import auth
        with tempfile.TemporaryDirectory() as folder, patch.object(auth, 'MODE', 'token'), \
                patch.object(auth, 'TOKEN_FILE', Path(folder) / 'token'), patch.object(auth, 'TAILSCALE', False):
            value = self.handler(layouts.PATH + '/generate', {'prompt': 'quiet'})
            value.client_address = ('127.0.0.1', 12345)
            value.deny = self.server.Handler.deny.__get__(value)
            value.headers['Cookie'] = auth.COOKIE + '=' + auth.session_value()
            self.server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(202)
        for body in ([], None, 'bad', {'layout': None}):
            handler = Handler(layouts.PATH + '/save', body)
            layouts.post(handler)
            self.assertIn(handler.status, (400, 413))
        handler = Handler(layouts.PATH + '/save', {})
        handler.headers['Content-Length'] = '65537'
        layouts.post(handler)
        self.assertEqual(handler.status, 413)
        handler = Handler(layouts.PATH + '/generate?job=missing')
        layouts.get(handler)
        self.assertEqual(handler.status, 404)
