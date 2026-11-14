import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import agent_jira_cli
import jira_client
import paths
import work_api
import work_items
import work_jira
from jira_client import JiraError
from test_work_items import PROJECT, Handler

ME = 'acc-mak'
CONFIG = {'base': 'https://shop.atlassian.net', 'email': 'me@example.com', 'token': 'secret-token-123',
          'accountId': ME, 'name': 'Mak', 'guard': True}


def issue(status='In Progress', assignee=ME, name='Mak'):
    return {'fields': {'status': {'name': status},
                       'assignee': {'accountId': assignee, 'displayName': name} if assignee else None}}


def moves(*ends):
    return [{'id': str(10 + n), 'name': f'to {end}', 'to': {'name': end}, 'isAvailable': True}
            for n, end in enumerate(ends)]


class FakeJira:
    """issues: {key: [issue reads, in order; the last repeats]}. post: an exception for the transition, or None."""

    def __init__(self, issues, choices=None, post=None):
        self.issues = {k: list(v) for k, v in issues.items()}
        self.choices = choices if choices is not None else moves('In Review', 'Done', 'QA Testing')
        self.post = post
        self.calls = []

    def issue(self, key):
        self.calls.append(('issue', key))
        reads = self.issues[key]
        found = reads.pop(0) if len(reads) > 1 else reads[0]
        if isinstance(found, Exception):
            raise found
        return found

    def transitions(self, key):
        self.calls.append(('transitions', key))
        return self.choices

    def transition(self, key, transition_id):
        self.calls.append(('transition', key, transition_id))
        if self.post:
            raise self.post

    def posts(self):
        return [c for c in self.calls if c[0] == 'transition']


class Home(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        for name, place in (('TETHER', self.home / 'tether'), ('CONFIG', self.home / 'config')):
            patcher = mock.patch.object(paths, name, place)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        work_items._CACHE.clear()
        work_items.save_project(dict(PROJECT), 0, create=True)

    def done_item(self, refs=('BS-12',), qa=False):
        item = work_items.new('shop', 'Login fails', 'agent', refs=list(refs), qa=qa)
        return work_items.stage(item['id'], 'done', 'agent')


class Owner(Home):
    def test_moves_only_the_connected_accounts_ticket(self):
        jira = FakeJira({'BS-12': [issue()]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(result['status'], 'moved')
        self.assertEqual(jira.posts(), [('transition', 'BS-12', '11')])
        self.assertEqual(result['text'], 'Jira: BS-12 in Done.')

    def test_another_owner_is_never_moved(self):
        jira = FakeJira({'BS-12': [issue(assignee='acc-ana', name='Ana Lima')]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(jira.posts(), [])
        self.assertNotIn(('transitions', 'BS-12'), jira.calls)
        self.assertEqual(result['status'], 'owners')
        self.assertEqual(result['owners'], ['BS-12: Owned by Ana Lima: move it yourself or ask them.'])
        self.assertEqual(result['tickets'], [])

    def test_same_display_name_other_account_is_not_mine(self):
        jira = FakeJira({'BS-12': [issue(assignee='acc-other', name='Mak')]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(jira.posts(), [])
        self.assertEqual(result['status'], 'owners')

    def test_unassigned_is_never_moved(self):
        jira = FakeJira({'BS-12': [issue(assignee=None)]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(jira.posts(), [])
        self.assertEqual(result['owners'], ['BS-12 has no assignee: move it yourself.'])

    def test_mixed_tickets(self):
        jira = FakeJira({'BS-12': [issue()], 'BS-13': [issue(assignee='acc-ana', name='Ana')],
                         'BS-14': [issue(status='done', assignee='acc-ana', name='Ana')]})
        item = self.done_item(refs=('bs-12', 'BS-13', 'https://shop.atlassian.net/browse/BS-14', 'BS-12',
                                    'OT-3', 'https://example.com/x'))
        result = work_jira.move(item, work_items.project('shop'), CONFIG, jira)
        self.assertEqual(result['all'], ['BS-12', 'BS-13', 'BS-14'])
        self.assertEqual(result['tickets'], ['BS-12', 'BS-14'])  # BS-14 already there: no move needed
        self.assertEqual(jira.posts(), [('transition', 'BS-12', '11')])
        self.assertEqual(result['status'], 'owners')

    def test_project_jira_user_must_be_the_connected_account(self):
        work_items.save_project(dict(PROJECT, jiraUser='Someone Else'), work_items.registry()['revision'], create=False)
        jira = FakeJira({'BS-12': [issue()]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual((result['status'], jira.calls), ('failed', []))
        self.assertIn('Nothing moved', result['error'])


class Guard(Home):
    def test_reassigned_while_listing_moves_is_not_moved(self):
        jira = FakeJira({'BS-12': [issue(), issue(assignee='acc-ana', name='Ana')]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(jira.posts(), [])
        self.assertEqual(result['owners'], ['BS-12: Owned by Ana: move it yourself or ask them.'])

    def test_without_the_jira_guard_nothing_moves(self):
        jira = FakeJira({'BS-12': [issue()]})
        result = work_jira.move(self.done_item(), work_items.project('shop'), dict(CONFIG, guard=False), jira)
        self.assertEqual((result['status'], jira.calls), ('off', []))
        self.assertIn('Only Assignee', result['text'])
        jira_client.save(dict(CONFIG, guard=False))
        self.assertIn('Only Assignee', jira_client.status()['why'])


class Targets(Home):
    def test_qa_item_goes_to_qa_status(self):
        jira = FakeJira({'BS-12': [issue()]})
        result = work_jira.move(self.done_item(qa=True), work_items.project('shop'), CONFIG, jira)
        self.assertEqual((result['target'], jira.posts()), ('QA Testing', [('transition', 'BS-12', '12')]))

    def test_qa_without_qa_status_fails_without_calls(self):
        jira = FakeJira({'BS-12': [issue()]})
        result = work_jira.move(self.done_item(qa=True), dict(work_items.project('shop'), qaStatus=''), CONFIG, jira)
        self.assertEqual((result['status'], jira.calls), ('failed', []))
        self.assertIn('no QA status', result['text'])

    def test_no_tickets_is_none(self):
        self.assertIsNone(work_jira.move(self.done_item(refs=('OT-1',)), work_items.project('shop'), CONFIG,
                                         FakeJira({})))

    def test_not_connected_is_off(self):
        result = work_jira.move(self.done_item(), work_items.project('shop'))
        self.assertEqual(result['status'], 'off')
        self.assertIn('tether jira connect', result['text'])

    def test_no_transition_lists_the_choices(self):
        jira = FakeJira({'BS-12': [issue()]}, choices=moves('In Review', 'Blocked'))
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('no move to Done (it can go to Blocked, In Review)', result['error'])
        self.assertEqual(jira.posts(), [])


class Failures(Home):
    def test_401_stops_the_job(self):
        jira = FakeJira({'BS-12': [JiraError(jira_client.EXPIRED, 401)], 'BS-13': [issue()]})
        result = work_jira.move(self.done_item(refs=('BS-12', 'BS-13')), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], jira_client.EXPIRED)
        self.assertNotIn(('issue', 'BS-13'), jira.calls)

    def test_post_timeout_reads_again_and_counts_a_done_move(self):
        jira = FakeJira({'BS-12': [issue(), issue(), issue(status='Done')]}, post=JiraError('Jira did not answer (timed out).'))
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual(result['status'], 'moved')
        self.assertEqual(len(jira.posts()), 1)  # never posted twice

    def test_post_error_that_did_not_move_fails(self):
        jira = FakeJira({'BS-12': [issue()]}, post=JiraError('HTTP 500', 500))
        result = work_jira.move(self.done_item(), work_items.project('shop'), CONFIG, jira)
        self.assertEqual((result['status'], len(jira.posts())), ('failed', 1))
        self.assertEqual(result['error'], 'BS-12: HTTP 500')
        self.assertIn('Jira move failed: BS-12: HTTP 500', result['text'])

    def test_one_failed_ticket_does_not_stop_the_next(self):
        jira = FakeJira({'BS-12': [JiraError('HTTP 404', 404)], 'BS-13': [issue()]})
        result = work_jira.move(self.done_item(refs=('BS-12', 'BS-13')), work_items.project('shop'), CONFIG, jira)
        self.assertEqual((result['status'], result['tickets']), ('failed', ['BS-13']))


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b'', headers=None):
    return urllib.error.HTTPError('https://shop.atlassian.net/x', code, 'err', headers or {}, io.BytesIO(body))


class ClientTests(Home):
    def client(self, answers, **extra):
        self.sent, self.sleeps = [], []
        answers = list(answers)

        def open_url(request, timeout):
            self.sent.append(request)
            found = answers.pop(0)
            if isinstance(found, Exception):
                raise found
            return found
        return jira_client.Client(CONFIG, open_url=open_url, sleep=self.sleeps.append, **extra)

    def test_check_base(self):
        self.assertEqual(jira_client.check_base('https://Shop.atlassian.net/'), 'https://shop.atlassian.net')
        gateway = 'https://api.atlassian.com/ex/jira/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b'
        self.assertEqual(jira_client.check_base(gateway), gateway)
        for bad in ('http://shop.atlassian.net', 'https://shop.atlassian.net.evil.com', 'https://evil.com',
                    'https://user@shop.atlassian.net', 'https://shop.atlassian.net:8443', 'https://shop.atlassian.net/x',
                    'https://api.atlassian.com/other', 'https://shop.atlassian.net?a=1', ''):
            with self.assertRaises(ValueError, msg=bad):
                jira_client.check_base(bad)

    def test_redirects_are_not_followed(self):
        handler = jira_client._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, 'Found', {}, 'https://evil.com/'))
        self.assertTrue(any(isinstance(h, jira_client._NoRedirect) for h in jira_client.opener().handlers))

    def test_get_retries_429_then_answers(self):
        client = self.client([http_error(429, headers={'Retry-After': '3'}), Response(200, b'{"accountId": "x"}')])
        self.assertEqual(client.myself(), {'accountId': 'x'})
        self.assertEqual(self.sleeps, [3])
        self.assertTrue(self.sent[0].get_header('Authorization').startswith('Basic '))

    def test_post_is_never_retried(self):
        client = self.client([http_error(503)])
        with self.assertRaises(JiraError) as caught:
            client.transition('BS-12', '11')
        self.assertEqual((caught.exception.status, len(self.sent), self.sleeps), (503, 1, []))
        self.assertEqual(json.loads(self.sent[0].data), {'transition': {'id': '11'}})

    def test_401_says_reconnect_and_hides_the_token(self):
        client = self.client([http_error(401, b'{"errorMessages": ["secret-token-123 bad"]}')])
        with self.assertRaises(JiraError) as caught:
            client.issue('BS-12')
        self.assertEqual(str(caught.exception), jira_client.EXPIRED)

    def test_errors_never_carry_the_token(self):
        auth = 'Basic ' + __import__('base64').b64encode(b'me@example.com:secret-token-123').decode()
        body = json.dumps({'errorMessages': [f'bad secret-token-123 and {auth}']}).encode()
        client = self.client([http_error(403, body), OSError('reset by secret-token-123')])
        for call in (client.myself, lambda: client.transition('BS-12', '1')):
            with self.assertRaises(JiraError) as caught:
                call()
            self.assertNotIn('secret-token-123', str(caught.exception))
            self.assertNotIn(auth.split()[1], str(caught.exception))
            self.assertIn('[hidden]', str(caught.exception))

    def test_socket_timeout_is_the_time_left_and_a_late_answer_fails(self):
        now = [0.0]
        timeouts = []

        class Slow(Response):
            def read(self, limit):
                now[0] = 61
                return b'{}'

        def open_url(request, timeout):
            timeouts.append(timeout)
            return Slow(200, b'')
        client = jira_client.Client(CONFIG, open_url=open_url, clock=lambda: now[0], deadline=4)
        with self.assertRaises(JiraError) as caught:
            client.transition('BS-12', '1')
        self.assertEqual((timeouts, str(caught.exception)), ([4], jira_client.LATE))

    def test_a_long_error_is_hidden_before_it_is_shortened(self):
        auth = __import__('base64').b64encode(b'me@example.com:secret-token-123').decode()
        body = json.dumps({'errorMessages': ['x' * (400 - len(auth) + 1) + 'Basic ' + auth]}).encode()
        client = self.client([http_error(403, body)])
        with self.assertRaises(JiraError) as caught:
            client.myself()
        self.assertNotIn(auth.rstrip('=')[:20], str(caught.exception))

    def stuck(self):
        """A socket pair where Jira never sends: only the watchdog ends a read on `ours`."""
        import socket
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return ours

    def late(self, open_url):
        import time
        client = jira_client.Client(CONFIG, open_url=open_url, deadline=0.2)
        began = time.monotonic()
        with self.assertRaises(JiraError) as caught:
            client.transition('BS-12', '1')
        self.assertEqual(str(caught.exception), jira_client.LATE)
        self.assertLess(time.monotonic() - began, 2)

    def test_the_deadline_ends_a_stuck_body_read(self):
        ours = self.stuck()

        class Stuck(Response):
            def read(self, limit):
                return ours.recv(limit)

        def open_url(request, timeout):
            request.cut.add(ours)
            return Stuck(200, b'')
        self.late(open_url)

    def test_the_deadline_ends_a_stuck_error_read(self):
        ours = self.stuck()

        def open_url(request, timeout):
            request.cut.add(ours)
            raise urllib.error.HTTPError(request.full_url, 403, 'err', {}, ours.makefile('rb'))
        self.late(open_url)

    def test_the_deadline_ends_a_stuck_tls_handshake(self):
        import ssl
        import threading
        import time
        ours = self.stuck()
        cut = jira_client._Cut()
        cut.add(ours)
        self.addCleanup(cut.close)
        threading.Timer(0.2, cut.cut).start()
        began = time.monotonic()
        with self.assertRaises(OSError):  # TLS wraps (and detaches) ours, sends its hello and waits: the cut ends it
            ssl.create_default_context().wrap_socket(ours, server_hostname='shop.atlassian.net').close()
        self.assertLess(time.monotonic() - began, 2)

    def test_each_connection_socket_joins_the_watchdog(self):
        import socket
        ours = self.stuck()
        handler = jira_client._Https()
        made = {}
        handler.do_open = lambda factory, req, **kw: made.setdefault('conn', factory('shop.atlassian.net', timeout=1))
        request = urllib.request.Request('https://shop.atlassian.net/x')
        request.cut = jira_client._Cut()
        with mock.patch('socket.create_connection', return_value=ours):
            handler.https_open(request)
            made['conn']._create_connection(('shop.atlassian.net', 443))
        self.assertEqual(len(request.cut.socks), 1)
        wrapped = socket.socket(fileno=ours.detach())  # what TLS wrapping does: the plain socket object gives up its fd
        self.addCleanup(wrapped.close)
        request.cut.cut()
        self.assertEqual(wrapped.recv(10), b'')  # the wrapped socket is shut down too: a blocked read ends
        request.cut.close()
        self.assertEqual(request.cut.socks, [])
        self.assertTrue(any(isinstance(h, jira_client._Https) for h in jira_client.opener().handlers))
        self.assertIsInstance(socket.SHUT_RDWR, int)

    def test_a_cut_short_answer_is_a_network_error(self):
        import http.client

        class Short(Response):
            def read(self, limit):
                raise http.client.IncompleteRead(b'{"acc')
        client = self.client([Short(200, b''), Response(200, b'{"accountId": "x"}'), Short(200, b'')])
        self.assertEqual(client.myself(), {'accountId': 'x'})  # a GET tries again
        with self.assertRaises(JiraError) as caught:
            client.transition('BS-12', '1')
        self.assertEqual(caught.exception.status, 0)

    def test_bad_key_is_refused_before_a_call(self):
        client = self.client([])
        with self.assertRaises(JiraError):
            client.issue('BS-12/../../x')
        self.assertEqual(self.sent, [])

    def test_deadline_stops_calls(self):
        now = [0.0]
        client = self.client([Response(200, b'{}')], clock=lambda: now[0], deadline=5)
        now[0] = 6
        with self.assertRaises(JiraError):
            client.myself()
        self.assertEqual(self.sent, [])


class ConfigFile(Home):
    def test_save_load_status_never_shows_the_token(self):
        jira_client.save(dict(CONFIG))
        self.assertEqual(os.stat(jira_client.config_path()).st_mode & 0o777, 0o600)
        self.assertEqual(jira_client.load(), (CONFIG, ''))
        shown = jira_client.status()
        self.assertEqual(shown, {'connected': True, 'site': 'shop.atlassian.net', 'account': 'Mak', 'guard': True})
        self.assertNotIn('secret', json.dumps(shown))
        self.assertNotIn('example.com', json.dumps(shown))

    def test_open_file_is_refused(self):
        jira_client.save(dict(CONFIG))
        os.chmod(jira_client.config_path(), 0o644)
        config, why = jira_client.load()
        self.assertIsNone(config)
        self.assertIn('chmod 600', why)

    def test_symlink_is_refused(self):
        real = self.home / 'real.json'
        real.write_text(json.dumps(CONFIG))
        os.chmod(real, 0o600)
        jira_client.config_path().parent.mkdir(parents=True, exist_ok=True)
        os.symlink(real, jira_client.config_path())
        self.assertIsNone(jira_client.load()[0])

    def test_bad_base_is_refused(self):
        jira_client.save(dict(CONFIG, base='https://evil.com'))
        self.assertIn('broken', jira_client.load()[1])

    def test_missing(self):
        self.assertEqual(jira_client.load(), (None, jira_client.NOT_CONNECTED))
        self.assertEqual(jira_client.status(), {'connected': False, 'why': jira_client.NOT_CONNECTED})


class Cli(Home):
    def run_cli(self, argv, token, me=None, error=None):
        client = mock.Mock()
        client.return_value.myself.side_effect = error
        client.return_value.myself.return_value = me
        with mock.patch.object(jira_client, 'Client', client), mock.patch('sys.stdin', io.StringIO(token)), \
                mock.patch('sys.stdout', io.StringIO()) as out, mock.patch('sys.stderr', io.StringIO()) as err:
            code = agent_jira_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_connect_saves_only_after_jira_accepts(self):
        argv = ['connect', '--site', 'https://shop.atlassian.net', '--email', 'me@example.com']
        code, _, err = self.run_cli(argv, 'tok\n', error=JiraError(jira_client.EXPIRED, 401))
        self.assertEqual(code, 1)
        self.assertIn('Nothing saved', err)
        self.assertFalse(jira_client.config_path().exists())
        code, out, _ = self.run_cli(argv, 'tok\n', me={'accountId': ME, 'displayName': 'Mak'})
        self.assertEqual(code, 0)
        self.assertIn('Connected to shop.atlassian.net as Mak', out)
        self.assertNotIn('tok', out)
        self.assertEqual(jira_client.load()[0]['token'], 'tok')
        self.assertEqual(self.run_cli(['disconnect'], '')[0], 0)
        self.assertFalse(jira_client.config_path().exists())

    def test_connect_refuses_a_bad_site(self):
        code, _, err = self.run_cli(['connect', '--site', 'https://evil.com', '--email', 'me@example.com'], 'tok\n')
        self.assertEqual(code, 2)
        self.assertFalse(jira_client.config_path().exists())


class MoverTests(Home):
    def mover(self):
        return work_jira.Mover(every=0, start=False)

    def test_done_before_the_first_look_never_moves(self):
        self.done_item()
        mover = self.mover()
        self.assertEqual(mover.pending(), [])
        self.assertEqual(json.loads((paths.work() / 'jira-start.json').read_text()), {'T': 2})
        self.assertEqual(mover.pending(), [])

    def test_new_done_moves_once_and_writes_history(self):
        mover = self.mover()
        mover.pending()
        item = self.done_item()
        found = mover.pending()
        self.assertEqual(found, [(item['id'], work_items.find(item['id'])[1]['version'])])
        jira = FakeJira({'BS-12': [issue(assignee='acc-ana', name='Ana')]})
        mover.run(*found[0], config=CONFIG, client=jira)
        after = work_items.find(item['id'])[1]
        self.assertEqual(after['stage'], 'done')
        self.assertEqual(after['jira']['status'], 'owners')
        self.assertEqual(after['history'][-1]['text'], 'BS-12: Owned by Ana: move it yourself or ask them.')
        self.assertEqual(mover.pending(), [])

    def test_done_again_after_reopen_moves_again(self):
        mover = self.mover()
        mover.pending()
        item = self.done_item()
        mover.run(*mover.pending()[0], config=CONFIG, client=FakeJira({'BS-12': [issue()]}))
        work_items.stage(item['id'], 'building', 'agent')
        work_items.stage(item['id'], 'done', 'agent')
        self.assertEqual(len(mover.pending()), 1)

    def test_item_without_tickets_is_looked_at_once(self):
        mover = self.mover()
        mover.pending()
        self.done_item(refs=())
        found = mover.pending()
        self.assertIsNone(mover.run(*found[0], config=CONFIG, client=FakeJira({})))
        self.assertEqual(mover.pending(), [])

    def test_an_old_result_does_not_answer_a_newer_done(self):
        mover = self.mover()
        mover.pending()
        item = self.done_item()
        first = mover.pending()[0]

        class Reopen(FakeJira):  # while the old move lists transitions, the item reopens, gets QA and is Done again
            def transitions(self, key):
                work_items.stage(item['id'], 'building', 'agent')
                work_items.set_qa(item['id'], True, 'mak')
                work_items.stage(item['id'], 'done', 'agent')
                return super().transitions(key)
        mover.run(*first, config=CONFIG, client=Reopen({'BS-12': [issue()]}))
        self.assertEqual(mover.pending(), [(item['id'], work_items.find(item['id'])[1]['version'] - 1)])

    def test_a_done_between_the_queue_and_the_run_is_the_one_answered(self):
        mover = self.mover()
        mover.pending()
        item = self.done_item()
        first = mover.pending()[0]
        work_items.stage(item['id'], 'building', 'agent')
        work_items.set_qa(item['id'], True, 'mak')
        work_items.stage(item['id'], 'done', 'agent')
        jira = FakeJira({'BS-12': [issue()]})
        mover.run(*first, config=CONFIG, client=jira)  # reads the item once: QA and its Done together
        self.assertEqual((mover.pending(), work_items.find(item['id'])[1]['jira']['target']), ([], 'QA Testing'))

    def test_move_again_answers_the_last_done(self):
        mover = self.mover()
        mover.pending()
        item = self.done_item()
        mover.run(item['id'], None, config=CONFIG, client=FakeJira({'BS-12': [issue()]}))
        self.assertEqual(mover.pending(), [])

    def test_move_again_keeps_the_done_it_started_for(self):
        mover = self.mover()
        mover.pending()
        item = self.done_item()
        mover.run(*mover.pending()[0], config=CONFIG, client=FakeJira({'BS-12': [issue()]}))
        self.assertEqual(mover.pending(), [])

        class Reopen(FakeJira):  # while Move again lists transitions, the item reopens and is Done again
            def transitions(self, key):
                work_items.stage(item['id'], 'building', 'agent')
                work_items.stage(item['id'], 'done', 'agent')
                return super().transitions(key)
        mover.run(item['id'], None, config=CONFIG, client=Reopen({'BS-12': [issue()]}))
        self.assertEqual([i for i, _ in mover.pending()], [item['id']])

    def test_one_worker_for_many_callers(self):
        import threading
        mover = work_jira.Mover(every=0)
        started = []

        class Fake:  # a worker that never runs: counts how many workers the callers start
            def __init__(self, **kwargs):
                pass

            def start(self):
                started.append(self)
        gate = threading.Event()
        callers = [threading.Thread(target=lambda i=i: (gate.wait(), mover.queue(f'T-{i:03d}', i))) for i in range(1, 9)]
        for caller in callers:
            caller.start()
        with mock.patch.object(work_jira.threading, 'Thread', Fake):
            gate.set()
            for caller in callers:
                caller.join()
        self.assertEqual((len(started), mover.jobs.qsize()), (1, 8))

    def test_idle_worker_leaves_and_a_new_job_starts_another(self):
        mover = work_jira.Mover(every=0)
        with mock.patch.object(mover.jobs, 'get', side_effect=work_jira.queue.Empty):
            mover.thread = object()
            mover._work()
        self.assertIsNone(mover.thread)
        with mock.patch('threading.Thread.start') as start:
            mover.queue('T-001', 1)
        start.assert_called_once()

    def test_a_failing_job_does_not_stop_the_worker(self):
        mover = work_jira.Mover(every=0, start=False)
        mover.jobs.put(('T-001', 1))
        mover.jobs.put(('T-002', 2))
        ran = []

        def run(item_id, n):
            ran.append(item_id)
            if item_id == 'T-001':
                raise RuntimeError('cut short')
        mover.thread = object()
        with mock.patch.object(mover, 'run', side_effect=run), \
                mock.patch.object(mover.jobs, 'get', side_effect=[mover.jobs.get_nowait(), mover.jobs.get_nowait(),
                                                                 work_jira.queue.Empty]):
            mover._work()
        self.assertEqual((ran, mover.thread), (['T-001', 'T-002'], None))

    def test_queue_once_while_busy(self):
        mover = self.mover()
        self.assertEqual((mover.queue('T-001', 3), mover.queue('T-001', 3)), (1, 0))
        self.assertEqual(mover.jobs.qsize(), 1)

    def test_not_done_any_more_is_skipped(self):
        item = work_items.new('shop', 'Login fails', 'agent', refs=['BS-12'])
        jira = FakeJira({'BS-12': [issue()]})
        self.assertIsNone(self.mover().run(item['id'], 2, config=CONFIG, client=jira))
        self.assertEqual(jira.calls, [])


class ApiTests(Home):
    def call(self, name, body=None):
        handler = Handler(work_api.PREFIX + name, body)
        (work_api.get if body is None else work_api.post)(handler)
        return handler.sent

    def test_board_has_jira_state_without_secrets(self):
        jira_client.save(dict(CONFIG))
        status, payload = self.call('board')
        self.assertEqual(status, 200)
        self.assertEqual(payload['jira'], {'connected': True, 'site': 'shop.atlassian.net', 'account': 'Mak', 'guard': True})
        self.assertNotIn('secret-token', json.dumps(payload))

    def test_move_again(self):
        item = self.done_item()
        with mock.patch.object(work_jira.MOVER, 'queue', return_value=1) as queue:
            status, payload = self.call('jira', {'item': item['id'], 'version': item['version']})
        self.assertEqual((status, payload['queued']), (200, True))
        queue.assert_called_once_with(item['id'])

    def test_move_again_refuses_stale_not_done_and_no_tickets(self):
        item = self.done_item()
        self.assertEqual(self.call('jira', {'item': item['id'], 'version': item['version'] - 1})[0], 409)
        open_item = work_items.new('shop', 'Other', 'agent', refs=['BS-3'])
        self.assertEqual(self.call('jira', {'item': open_item['id'], 'version': open_item['version']})[0], 409)
        bare = self.done_item(refs=())
        self.assertEqual(self.call('jira', {'item': bare['id'], 'version': bare['version']})[0], 400)


if __name__ == '__main__':
    unittest.main()
