"""The broadcast broker: skip panes that wait on the user, re-check each pane right before its send, report per pane.
No test touches a real tmux pane: sends are fakes, or terminal_input.submit with a fake `run`."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import http.client
import importlib.machinery
import importlib.util
import io
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import broadcast
import server
import terminal_input
from terminal_input import DraftError


def pane(identifier, agent='claude', **extra):
    return dict({'id': identifier, 'agent': agent, 'writable': True, 'thread': 't' + identifier[1:], 'label': 'work:0.' + identifier[1:],
                 'tmux_session': 'work', 'cwd': '/w'}, **extra)


class Fake:
    """Pane states and a send log, in call order, so a test can see the status read right before each send."""
    def __init__(self, states, fail=None):
        self.states, self.fail, self.log = dict(states), fail or {}, []

    def status(self, entry):
        self.log.append(('status', entry['id']))
        return {'state': self.states[entry['id']]}

    def send(self, entry, text, state):
        self.log.append(('send', entry['id'], text))
        if entry['id'] in self.fail:
            raise self.fail[entry['id']]
        return 'queued' if state == 'Working' else 'submitted'

    def run(self, text, panes, entries, **options):
        return broadcast.deliver(text, panes, entries=lambda: entries, status=self.status, send=self.send, **options)


class BrokerTests(unittest.TestCase):
    def test_reports_each_pane_and_never_types_into_one_that_waits(self):
        entries = [pane('%1'), pane('%2', 'codex'), pane('%3'), pane('%4'), pane('%5', 'shell'), pane('%6', 'antigravity')]
        fake = Fake({'%1': 'Idle', '%2': 'Working', '%3': 'Approval', '%4': 'Input needed', '%6': 'Idle'},
                    fail={'%6': DraftError('There is an unsent draft.')})
        out = fake.run('hello', ['%1', '%2', '%3', '%4', '%5', '%6', '%9', '%1'], entries)
        results = {item['pane']: (item['result'], item.get('reason', '')) for item in out['results']}
        self.assertEqual(out['via'], 'direct')
        self.assertEqual([item['pane'] for item in out['results']], ['%1', '%2', '%3', '%4', '%5', '%6', '%9'])  # %1 once
        self.assertEqual(results['%1'], ('sent', ''))
        self.assertEqual(results['%2'], ('sent_busy', ''))
        self.assertEqual(results['%3'], ('skipped', 'Waits on an approval.'))
        self.assertEqual(results['%4'], ('skipped', 'Waits on a question or a picker.'))
        self.assertEqual(results['%5'][0], 'skipped')
        self.assertEqual(results['%6'], ('skipped', 'Its input box has an unsent draft.'))
        self.assertEqual(results['%9'], ('skipped', 'The pane has ended.'))
        sent = [step[1] for step in fake.log if step[0] == 'send']
        self.assertEqual(sent, ['%1', '%2', '%6'])

    def test_state_is_read_right_before_each_send(self):
        fake = Fake({'%1': 'Idle', '%2': 'Idle'})
        original = fake.send

        def send(entry, text, state):  # while %1 gets the text, an approval opens in %2
            if entry['id'] == '%1':
                fake.states['%2'] = 'Approval'
            return original(entry, text, state)
        fake.send = send
        out = fake.run('go', ['%1', '%2'], [pane('%1'), pane('%2')])
        self.assertEqual(fake.log, [('status', '%1'), ('send', '%1', 'go'), ('status', '%2')])
        self.assertEqual([item['result'] for item in out['results']], ['sent', 'skipped'])

    def test_failures_and_stale_conversations(self):
        fake = Fake({'%1': 'Idle', '%2': 'Idle', '%3': 'Idle'},
                    fail={'%1': RuntimeError('An approval appeared during delivery. Check Termius before retrying.'),
                          '%3': RuntimeError('Respond to the pending approval before sending a message.')})
        out = fake.run('x', [{'pane': '%1', 'thread': 't1'}, {'pane': '%2', 'thread': 'old'}, {'pane': '%3', 'thread': 't3'}],
                       [pane('%1'), pane('%2'), pane('%3')])
        self.assertEqual([(item['result'], item['reason']) for item in out['results']],
                         [('failed', 'An approval appeared during delivery. Check Termius before retrying.'),
                          ('skipped', 'The pane changed conversations. Reload and choose again.'),
                          ('skipped', 'Waits on an approval.')])
        self.assertNotIn(('send', '%2', 'x'), fake.log)

    def test_default_targets_are_the_idle_agents_and_never_the_sender(self):
        fake = Fake({'%1': 'Idle', '%2': 'Working', '%3': 'Idle', '%4': 'Approval'})
        entries = [pane('%1'), pane('%2'), pane('%3', 'codex'), pane('%4'), pane('%5', 'shell')]
        out = fake.run('x', None, entries, exclude='%1')
        self.assertEqual([(item['pane'], item['result']) for item in out['results']], [('%3', 'sent')])
        out = fake.run('x', ['%1'], entries, exclude='%1')
        self.assertEqual(out['results'][0]['reason'], 'This is the sending pane.')

    def test_bad_requests_are_refused(self):
        fake = Fake({'%1': 'Idle'})
        for text, panes, options in (('', ['%1'], {}), ('a\x1b[2J', ['%1'], {}), ('x', ['work'], {}), ('x', '%1', {}),
                                     ('x', ['%1'], {'via': 'carrier pigeon'}), ('x' * 20001, ['%1'], {})):
            with self.assertRaises(ValueError):
                fake.run(text, panes, [pane('%1')], **options)
        self.assertEqual(fake.log, [])

    def test_claude_master_gets_one_message_with_the_targets(self):
        entries = [pane('%1', pane_name='boss'), pane('%2', 'codex', pane_name='api'), pane('%3'), pane('%4', 'codex')]
        fake = Fake({'%1': 'Idle', '%2': 'Working', '%3': 'Approval', '%4': 'Idle'})
        out = fake.run('ship it', ['%1', '%2', '%3', '%4'], entries, via='claude', master='%1', cli='/opt/tether/tether')
        sends = [step for step in fake.log if step[0] == 'send']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1], '%1')
        text = sends[0][2]
        self.assertIn('%2 · api · Codex', text)
        self.assertIn('%4 ·', text)
        self.assertNotIn('%3 ·', text)  # it waits on an approval: skipped here, the master never sees it
        self.assertNotIn('- %1 ·', text)
        self.assertIn('/opt/tether/tether broadcast --panes', text)
        self.assertTrue(text.endswith('Message:\nship it'))
        self.assertEqual(out['master']['result'], 'sent')
        self.assertEqual([(item['pane'], item['result']) for item in out['results']],
                         [('%1', 'skipped'), ('%2', 'handed_off'), ('%3', 'skipped'), ('%4', 'handed_off')])

    def test_a_master_that_waits_hands_nothing_off(self):
        fake = Fake({'%1': 'Approval', '%2': 'Idle'})
        out = fake.run('x', ['%2'], [pane('%1'), pane('%2', 'codex')], via='claude', master='%1')
        self.assertEqual(out['master']['result'], 'skipped')
        self.assertEqual(out['results'][0], dict(out['results'][0], result='skipped', reason='The master did not get the message.'))
        self.assertFalse([step for step in fake.log if step[0] == 'send'])
        with self.assertRaises(ValueError):
            fake.run('x', ['%1'], [pane('%1'), pane('%2', 'codex')], via='claude', master='%2')  # not a Claude pane


class GuardedPathTests(unittest.TestCase):
    """With the real terminal_input.submit, an approval on screen stops the send before anything is pasted or typed."""
    APPROVAL = ('Accessing workspace:\n\n /srv/app\n\n Quick safety check: Is this a\n project you created or one you\n'
                ' trust?\n\n ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel')

    def test_an_approval_screen_is_skipped_without_typing(self):
        commands = []

        def run(command, **options):
            commands.append(command[1])
            return SimpleNamespace(stdout=self.APPROVAL if command[1] == 'capture-pane' else '0\tclaude', returncode=0)
        self.assertTrue(terminal_input.approval(self.APPROVAL))
        with patch.object(terminal_input, 'verify'), patch.object(terminal_input, 'leave_scrollback'):
            result = broadcast.send_one('%1', pane('%1'), 'hello', lambda entry: {'state': 'Idle'},
                                        lambda entry, text, state: terminal_input.submit(entry, text, run=run))
        self.assertEqual(result['result'], 'skipped')
        self.assertEqual(result['reason'], 'Waits on an approval.')
        self.assertFalse({'load-buffer', 'paste-buffer', 'send-keys'} & set(commands))

    def test_a_small_pane_reports_the_full_reason_without_typing(self):
        commands = []
        def run(command, **options):
            commands.append(command)
            output = {'capture-pane': 'reply\nmore reply\noutput\nstatus\n',
                      'display-message': '100\t5\t100\t5\twork\tBugs\tlatest\n',
                      'list-windows': ''}[command[1]]
            return SimpleNamespace(stdout=output, returncode=0)
        entry = pane('%1')
        reason = terminal_input.small_pane_reason(entry, run)
        with patch.object(terminal_input, 'verify'), patch.object(terminal_input, 'leave_scrollback'):
            result = broadcast.send_one('%1', entry, 'hello', lambda entry: {'state': 'Idle'},
                                        lambda entry, text, state: terminal_input.submit(entry, text, run=run))
        self.assertEqual(result['result'], 'failed')
        self.assertEqual(result['reason'], reason)
        self.assertFalse({'load-buffer', 'paste-buffer', 'send-keys'} & {command[1] for command in commands})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        proxy = patch.object(server.auth, 'MODE', 'proxy')  # the endpoint, not sign-in (see test_auth.py)
        proxy.start()
        self.addCleanup(proxy.stop)
        self.sent = []
        entries = [pane('%1'), pane('%2', 'codex'), pane('%3')]
        states = {'%1': 'Idle', '%2': 'Working', '%3': 'Approval'}
        for name, value in (('sessions', lambda: entries), ('live_status', lambda entry: {'state': states[entry['id']]}),
                            ('submit', lambda message, config: self.sent.append((config['id'], message))),
                            ('agent_folder', lambda entry: '/w')):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def post(self, body, origin=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {'Content-Type': 'application/json', 'X-Codex-Mobile': '1'}
        if origin:
            headers['Origin'] = f'http://127.0.0.1:{self.port}'
        connection.request('POST', '/mobile/api/broadcast', json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_sends_through_the_composer_path_and_reports_each_pane(self):
        with patch.object(server.shell_log, 'folder_note', return_value=''):
            status, data = self.post({'message': ' hi all ', 'panes': [{'pane': '%1', 'thread': 't1'}, {'pane': '%2', 'thread': 't2'}, {'pane': '%3', 'thread': 't3'}]})
        self.assertEqual(status, 200)
        self.assertEqual([(item['pane'], item['result']) for item in data['results']], [('%1', 'sent'), ('%2', 'sent_busy'), ('%3', 'skipped')])
        self.assertEqual(self.sent, [('%1', 'hi all'), ('%2', 'hi all')])

    def test_refuses_bad_input_and_cross_site_posts(self):
        status, data = self.post({'message': '', 'panes': ['%1']})
        self.assertEqual((status, data['error']), (400, 'Enter a message of up to 20000 characters.'))
        status, _ = self.post({'message': 'x', 'panes': ['%1']}, origin=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.sent, [])


class CliTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        loader = importlib.machinery.SourceFileLoader('tether_cli_broadcast', str(root / 'tether'))
        self.cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.cli)

    def test_posts_to_the_local_broker_and_never_targets_its_own_pane(self):
        seen = {}

        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def opener(request, timeout):
            seen.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data))
            return Reply(json.dumps({'via': 'direct', 'results': [{'pane': '%2', 'name': 'api', 'agent': 'codex', 'result': 'sent_busy'}]}).encode())
        args = self.cli.argparse.Namespace(text='hello', panes=['%2,%3', '%4'], master=None, json=False)
        with patch.dict(self.cli.os.environ, {'MOBILE_TMUX_PORT': '7999', 'TMUX_PANE': '%1'}), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            code = self.cli.broadcast(args, opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(seen['url'], 'http://127.0.0.1:7999/mobile/api/broadcast')
        self.assertEqual(seen['headers']['Origin'], 'http://127.0.0.1:7999')
        self.assertEqual(seen['headers']['X-codex-mobile'], '1')
        self.assertEqual(seen['body'], {'message': 'hello', 'via': 'direct', 'master': None, 'exclude': '%1', 'panes': ['%2', '%3', '%4']})
        self.assertIn('%2\tapi\tsent while busy', out.getvalue())


if __name__ == '__main__':
    unittest.main()
