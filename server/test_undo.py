"""Undo and redo (undo.py, undo_store.py, undo_api.py). The tmux tests run a private tmux server in its own socket folder
(TMUX_TMPDIR), never the user's tmux. Reopened agent tabs get a harmless command instead of claude or codex."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import auto_resume
import groups
import groups_api
import paths
import prefs_sync
import projects
import review_links
import undo
import undo_api
import tmux_merge
import undo_store
from test_tmux_isolation import private_tmux


class Private(unittest.TestCase):
    """Private state folders for every test."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        for target, name, value in ((paths, 'TETHER', self.temp / 'tether'), (paths, 'CACHE', self.temp / 'cache'),
                                    (prefs_sync, 'FILE', self.temp / 'cache' / 'prefs.json'),
                                    (review_links, 'FILE', self.temp / 'cache' / 'review-links.json'),
                                    (undo_store, '_server', lambda: 'test:1'), (undo_store, '_ENABLED', True)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
            self.addCleanup(patch.stop)


class StoreTests(Private):
    def test_successful_close_offers_the_checkpoint_without_writing_another_step(self):
        tab = {'pane': '%1', 'name': 'Test tab', 'server': '10:20', 'options': {}, 'session_options': {}}
        with undo_store.acting('user'), undo_store.step():
            identifier = undo.close_tab('%1', mock.Mock(), tab, 'Test tab', close=lambda: None)
            headers = undo_api.headers(200)
        step, = undo_store.entries()
        self.assertEqual(headers, {'X-Tether-Undo': identifier, 'X-Tether-Undo-Label': 'Closed%20tab%20%60Test%20tab%60'})
        self.assertEqual(identifier, step['id'])

    def test_off_until_enabled_writes_nothing_and_reads_no_tmux(self):
        # Other test modules and tools import the routes: they must never write the real history (server.py enables).
        run = mock.Mock(side_effect=AssertionError('no tmux read while recording is off'))
        with mock.patch.object(undo_store, '_ENABLED', False):
            self.assertFalse(undo_store.recording())
            with undo_store.step():
                undo_store.record('prefs.order', 'Reordered tabs', key='order', sub=None, old=[1], new=[1])
            undo.record_tab_start('%1', run)
            self.assertIsNone(undo.name_state('%1', run))
            self.assertIsNone(undo.capture_safe('%1', run, entries=run))
            self.assertIsNone(undo.capture_session_safe('$1', run, entries=run))
            undo.record_merge('pane', '%1', '%2', run)({'session': '$2'})
        run.assert_not_called()
        self.assertFalse((paths.TETHER / 'undo' / 'history.json').exists())

    def test_close_capture_errors_never_stop_the_close(self):
        def broken():
            raise ValueError('session list failed')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(undo.capture_safe('%1', mock.Mock(), entries=broken))
            self.assertIsNone(undo.capture_session_safe('$1', mock.Mock(), entries=broken))

    def test_record_saves_one_entry_per_action_outside_a_step(self):
        undo_store.record('prefs.order', 'Reordered tabs', key='order', sub=None, old=[1, 2], new=[2, 1])
        found = undo_store.entries()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['label'], 'Reordered tabs')
        self.assertEqual(found[0]['who'], {'by': 'tether'})
        self.assertEqual(found[0]['state'], 'done')
        self.assertEqual(found[0]['server'], 'test:1')
        self.assertEqual(oct(os.stat(paths.TETHER / 'undo' / 'history.json').st_mode & 0o777), '0o600')

    def test_limit_and_age(self):
        for number in range(undo_store.LIMIT + 5):
            undo_store.record('prefs.order', f'Step {number}', key='order', sub=None, old=[], new=[])
        found = undo_store.entries()
        self.assertEqual(len(found), undo_store.LIMIT)
        self.assertEqual(found[-1]['label'], f'Step {undo_store.LIMIT + 4}')
        later = time.time() + undo_store.MAX_AGE + 1
        self.assertEqual(undo_store.entries(now=later), [])

    def test_step_collects_and_titles(self):
        with undo_store.acting('agent', 'Worker'), undo_store.step():
            undo_store.record('group.create', 'Made stack `A`', session='$1')
            undo_store.title('Started tab `B`')
            undo_store.record('tab.start', 'Started tab `B`', pane='%1', session='$1')
        found = undo_store.entries()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['label'], 'Started tab `B`')
        self.assertEqual([a['kind'] for a in found[0]['actions']], ['group.create', 'tab.start'])
        self.assertEqual(found[0]['who'], {'by': 'agent', 'tab': 'Worker'})

    def test_failed_step_records_nothing(self):
        with self.assertRaises(RuntimeError):
            with undo_store.step():
                undo_store.record('group.create', 'Made stack', session='$1')
                raise RuntimeError('launch failed')
        self.assertEqual(undo_store.entries(), [])

    def test_failed_inner_step_drops_only_its_actions_and_keeps_the_first_actor(self):
        with undo_store.acting('user'), undo_store.step():
            undo_store.record('pane.name', 'Named tab', pane='%1')
            try:
                with undo_store.acting('agent', 'Helper'), undo_store.step():
                    undo_store.record('group.create', 'Made stack', session='$1')
                    raise ValueError('no')
            except ValueError:
                pass
        found = undo_store.entries()
        self.assertEqual([a['kind'] for a in found[0]['actions']], ['pane.name'])
        self.assertEqual(found[0]['who'], {'by': 'user'})

    def test_inner_actor_wins_when_it_acts_first(self):
        with undo_store.acting('user'), undo_store.step():
            with undo_store.acting('agent', 'Helper'), undo_store.step():
                undo_store.record('tab.start', 'Started tab', pane='%1', session='$1')
        self.assertEqual(undo_store.entries()[0]['who'], {'by': 'agent', 'tab': 'Helper'})

    def test_quiet_records_nothing(self):
        with undo_store.quiet():
            undo_store.record('pane.name', 'Named tab', pane='%1')
            with undo_store.step():
                undo_store.record('pane.name', 'Named tab', pane='%1')
        self.assertEqual(undo_store.entries(), [])

    def test_flush_names_the_step_in_headers(self):
        with undo_store.step():
            undo_store.record('pane.name', 'Renamed tab `a` to `b c`', pane='%1')
            headers = undo_api.headers(200)
            self.assertEqual(undo_api.headers(200), {})  # nothing new since
        self.assertRegex(headers['X-Tether-Undo'], r'^u[0-9a-f]{12}$')
        self.assertEqual(headers['X-Tether-Undo-Label'], 'Renamed%20tab%20%60a%60%20to%20%60b%20c%60')
        self.assertEqual(len(undo_store.entries()), 1)

    def test_error_answer_names_no_step(self):
        with undo_store.step():
            undo_store.record('pane.name', 'Named tab', pane='%1')
            self.assertEqual(undo_api.headers(409), {})

    def test_a_new_user_step_ends_redo_but_a_tether_step_does_not(self):
        undo_store.record('prefs.order', 'One', key='order', sub=None, old=[], new=[])

        def undone(found):
            found[0].update(state='undone', redo=True, undone_at=time.time())
        undo_store.edit(undone)
        undo_store.record('prefs.order', 'Tether', key='order', sub=None, old=[], new=[])
        self.assertTrue(undo_store.entries()[0]['redo'])
        with undo_store.acting('user'):
            undo_store.record('prefs.order', 'User', key='order', sub=None, old=[], new=[])
        self.assertFalse(undo_store.entries()[0]['redo'])

    def test_bad_file_reads_as_empty(self):
        path = paths.TETHER / 'undo' / 'history.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"entries": [{"id": 1}], "version": 1}')
        self.assertEqual(undo_store.entries(), [])
        path.write_text('not json')
        self.assertEqual(undo_store.entries(), [])


class OrderTests(Private):
    def test_only_a_permutation_is_a_reorder(self):
        undo.record_order('order', ['a', 'b'], ['a', 'b', 'c'])
        undo.record_order('order', ['a', 'b'], ['a', 'b'])
        undo.record_order('paneOrder', {'s': ['%1', '%2']}, {'s': ['%1']})
        self.assertEqual(undo_store.entries(), [])
        undo.record_order('paneOrder', {'s': ['%1', '%2']}, {'s': ['%2', '%1']})
        self.assertEqual(undo_store.entries()[0]['label'], 'Reordered tabs')

    def test_undo_and_redo_order(self):
        prefs_sync.save({'order': ['b', 'a'], 'paneOrder': {'s': ['%2', '%1']}})
        undo.record_order('order', ['a', 'b'], ['b', 'a'])
        env = undo.Env()
        self.assertEqual(undo.run('undo', None, env)['status'], 'done')
        self.assertEqual(prefs_sync.load()['prefs']['order'], ['a', 'b'])
        self.assertEqual(undo.run('redo', None, env)['status'], 'done')
        self.assertEqual(prefs_sync.load()['prefs']['order'], ['b', 'a'])
        undo.record_order('paneOrder', {'s': ['%1', '%2']}, {'s': ['%2', '%1']})
        undo.run('undo', None, env)
        self.assertEqual(prefs_sync.load()['prefs']['paneOrder'], {'s': ['%1', '%2']})

    def test_order_that_changed_again_refuses(self):
        prefs_sync.save({'order': ['b', 'a']})
        undo.record_order('order', ['a', 'b'], ['b', 'a'])
        prefs_sync.save({'order': ['a', 'b']})
        with self.assertRaisesRegex(undo.Refused, 'order changed'):
            undo.run('undo', None, undo.Env())

    def test_nothing_to_undo(self):
        with self.assertRaisesRegex(undo.Refused, 'Nothing to undo'):
            undo.run('undo', None, undo.Env())
        with self.assertRaisesRegex(undo.Refused, 'Nothing to redo'):
            undo.run('redo', None, undo.Env())

    def test_targets_newest_done_and_last_undone(self):
        prefs_sync.save({'order': ['c', 'b', 'a']})
        undo.record_order('order', ['a', 'b', 'c'], ['b', 'a', 'c'])
        undo.record_order('order', ['b', 'a', 'c'], ['c', 'b', 'a'])
        first, second = [entry['id'] for entry in undo_store.entries()]
        self.assertEqual(undo.next_target('undo'), second)
        undo.run('undo', None, undo.Env())
        self.assertEqual(undo.next_target('undo'), first)
        self.assertEqual(undo.next_target('redo'), second)
        undo.run('undo', None, undo.Env())
        self.assertEqual(prefs_sync.load()['prefs']['order'], ['a', 'b', 'c'])
        self.assertEqual(undo.next_target('redo'), first)  # the last one undone comes back first
        with self.assertRaisesRegex(undo.Refused, 'already undone'):
            undo.run('undo', first, undo.Env())


class CloseRedoTests(Private):
    def test_failed_pending_close_is_removed_before_the_next_undo(self):
        for session in (False, True):
            with self.subTest(session=session):
                undo_store.edit(list.clear)
                prefs_sync.save({'order': ['b', 'a']})
                earlier = undo_store.record('prefs.order', 'Earlier step', key='order', sub=None,
                                            old=['a', 'b'], new=['b', 'a'])
                tab = {'pane': '%2', 'name': 'Fixture', 'server': '1:2'}
                env = undo.Env(run=mock.Mock())
                if session:
                    undo_store.record('session.close', 'Close', saved={'session_id': '$2', 'tabs': [tab]}, pending=True)
                else:
                    undo_store.record('tab.close', 'Close', tab=tab, pending=True)
                original = undo_store.entries()
                with mock.patch.object(undo, 'kill_outcome', return_value=None):
                    with self.assertRaisesRegex(undo.Refused, 'Try Undo again'):
                        undo.run('undo', None, env)
                    self.assertEqual(undo.history(env)['next_undo'], original[-1]['id'])
                    self.assertEqual(undo_store.entries(), original)
                with mock.patch.object(undo, 'kill_outcome', return_value=False):
                    subject = 'session' if session else 'tab'
                    with self.assertRaisesRegex(undo.Refused, f'The {subject} did not close. Nothing to undo.'):
                        undo.run('undo', None, env)
                    self.assertEqual(undo_store.entries(), [earlier])
                self.assertEqual(undo.run('undo', None, env)['id'], earlier['id'])
                self.assertEqual(prefs_sync.load()['prefs']['order'], ['a', 'b'])

    def test_checkpoint_only_ends_redo_for_a_close_that_happened_or_is_unknown(self):
        for outcome in ('stale', 'closed', 'unknown'):
            with self.subTest(outcome=outcome):
                undo_store.edit(list.clear)
                prefs_sync.save({'order': ['b', 'a']})
                undo.record_order('order', ['a', 'b'], ['b', 'a'])
                env = undo.Env(run=mock.Mock())
                earlier = undo.run('undo', None, env)['id']
                before = undo_store.entries()
                tab = {'pane': '%2', 'name': 'Fixture', 'server': '1:2'}

                def close():
                    self.assertEqual(undo.next_target('redo'), earlier)
                    if outcome == 'stale':
                        raise undo.helper_tabs.Stale('The tab changed.')
                    if outcome == 'unknown':
                        raise subprocess.TimeoutExpired('tmux', 5)

                with (undo_store.acting('user'), mock.patch.object(undo, 'note_released'),
                      mock.patch.object(undo, 'kill_outcome', return_value=None)):
                    if outcome == 'closed':
                        undo.close_tab('%2', env.run, tab, 'Fixture', close=close)
                    else:
                        with self.assertRaises(undo.CloseInterrupted) as failure:
                            undo.close_tab('%2', env.run, tab, 'Fixture', close=close)
                        self.assertEqual(failure.exception.status, 409 if outcome == 'stale' else 503)
                if outcome == 'closed':
                    self.assertFalse(undo_store.entries()[0]['redo'])
                    self.assertIsNone(undo.next_target('redo'))
                else:
                    if outcome == 'unknown':
                        self.assertIsNone(undo.next_target('redo'))
                        with mock.patch.object(undo, 'kill_outcome', return_value=None):
                            self.assertFalse(undo.history(env)['entries'][-1]['redo'])
                            with self.assertRaisesRegex(undo.Refused, 'cannot be redone'):
                                undo.run('redo', earlier, env)
                        with mock.patch.object(undo, 'kill_outcome', return_value=False):
                            history = undo.history(env)
                        self.assertEqual([row['id'] for row in history['entries']], [earlier])
                        self.assertEqual(history['next_redo'], earlier)
                    self.assertEqual(undo_store.entries(), before)
                    self.assertEqual(undo.run('redo', None, env)['id'], earlier)
                    self.assertEqual(prefs_sync.load()['prefs']['order'], ['b', 'a'])

    def test_a_failed_checkpoint_does_not_evict_history_at_the_limit(self):
        with mock.patch.object(undo_store, 'LIMIT', 1):
            before = undo_store.record('prefs.order', 'Earlier step', key='order', sub=None, old=[], new=[])
            identifier = undo_store.checkpoint(None, 'Tentative close', [{'kind': 'tab.close'}],
                                               server='1:2', status='staged')
            undo_store.checkpoint(identifier, 'Tentative close', [], server='1:2')
            self.assertEqual(undo_store.entries(), [before])

    def test_tab_and_session_redo_timeout_keep_only_the_close_that_took_effect(self):
        for kind, key, target, outcome in (
            (kind, key, target, outcome)
            for kind, key, target in (('tab.close', 'tab', '%2'), ('session.close', 'saved', '$2'))
            for outcome in (True, False, None)
        ):
            with self.subTest(kind=kind, outcome=outcome), contextlib.ExitStack() as stack:
                undo_store.edit(list.clear)
                is_session = kind == 'session.close'
                tab = {'pane': '%2', 'server': '1:2'}
                captured = {'session_id': '$2', 'tabs': [tab]} if is_session else tab
                undo_store.record(kind, 'Closed fixture', **{key: captured})
                entry = undo_store.entries()[-1]
                entry['state'] = 'undone'
                entry['redo'] = True
                entry['actions'][0].update(reopened=target, reopened_server='1:2')
                undo_store.edit(lambda found: found.__setitem__(-1, entry))
                env = undo.Env(run=mock.Mock())
                handler = undo.KINDS[kind]
                stack.enter_context(mock.patch.object(handler, 'check'))
                stack.enter_context(mock.patch.object(handler, 'effects', return_value=[]))
                stack.enter_context(mock.patch.object(undo, 'capture_session' if is_session else 'capture',
                                                       return_value=captured))
                stack.enter_context(mock.patch.object(undo, '_server', return_value='1:2'))
                stack.enter_context(mock.patch.object(undo, '_show', return_value=['$3']))
                for name in ('_restack_released', '_restore_reviews', 'note_released'):
                    stack.enter_context(mock.patch.object(undo, name, return_value=None))
                reopen = stack.enter_context(mock.patch.object(undo, '_reopen_session' if is_session else '_reopen',
                                                                return_value=['%3'] if is_session else '%3'))
                stack.enter_context(mock.patch.object(undo.auto_resume, 'forget_pane'))
                stack.enter_context(mock.patch.object(undo.helper_close, 'release_reviews'))
                checkpoint = []
                gone = False

                def close(*_):
                    nonlocal gone
                    checkpoint.append(undo_store.find(undo_store.entries(), entry['id']))
                    gone = outcome
                    if outcome is False:
                        raise subprocess.CalledProcessError(1, 'tmux')
                    raise subprocess.TimeoutExpired('tmux', 5)

                stack.enter_context(mock.patch.object(undo, '_close_session' if is_session else '_close',
                                                       side_effect=close))
                observed = stack.enter_context(mock.patch.object(undo, 'kill_outcome', side_effect=lambda *_,
                                                                 **__: gone))
                if outcome is None:
                    with self.assertRaises(undo.Refused):
                        undo.run('redo', entry['id'], env)
                    self.assertEqual(undo.history(env, check=False)['next_undo'], entry['id'])
                    gone = True
                    self.assertEqual(undo.run('undo', entry['id'], env)['state'], 'undone')
                    reopen.assert_called_once()
                elif outcome:
                    try:
                        undo.run('redo', entry['id'], env)
                    except undo.Refused:
                        pass
                    self.assertEqual(undo.run('undo', entry['id'], env)['state'], 'undone')
                    reopen.assert_called_once()
                else:
                    with self.assertRaises(undo.Refused):
                        undo.run('redo', entry['id'], env)
                    self.assertEqual(undo_store.find(undo_store.entries(), entry['id']), entry)
                    self.assertEqual(undo.next_target('redo'), entry['id'])
                    reopen.assert_not_called()
                observed.assert_any_call(target, '1:2', env.run, session=is_session)
                self.assertEqual(checkpoint[0]['redo_close']['pending']['target'], target)
                action, = checkpoint[0]['redo_close']['pending']['actions']
                self.assertTrue(action['pending'])
                self.assertEqual(action[key], captured)


class ApiTests(Private):
    def call(self, body):
        data = json.dumps(body).encode()
        answers = []
        handler = SimpleNamespace(headers={'Content-Length': str(len(data))}, rfile=io.BytesIO(data),
                                  respond=lambda status, payload: answers.append((status, payload)))
        undo_api.post(handler, undo.Env(), mock.MagicMock())
        return answers[0]

    def test_post_answers(self):
        self.assertEqual(self.call({'action': 'drop'})[0], 400)
        self.assertEqual(self.call({'action': 'undo', 'confirmed': True})[0], 400)
        self.assertEqual(self.call({'action': 'undo'}), (409, {'error': 'Nothing to undo.'}))
        prefs_sync.save({'order': ['b', 'a']})
        undo.record_order('order', ['a', 'b'], ['b', 'a'])
        status, payload = self.call({'action': 'undo'})
        self.assertEqual((status, payload['status'], payload['label']), (200, 'done', 'Reordered sessions'))

    def test_get_lists_newest_first_with_reasons(self):
        prefs_sync.save({'order': ['b', 'a']})
        undo.record_order('order', ['a', 'b'], ['b', 'a'])
        undo.record_order('order', ['x', 'y'], ['y', 'x'])
        answers = []
        undo_api.get(SimpleNamespace(path=undo_api.PATH, respond=lambda status, payload: answers.append(payload)), undo.Env())
        rows = answers[0]['entries']
        self.assertEqual([row['why'] for row in rows], ['The order changed again since.', None])
        self.assertEqual((answers[0]['next_undo'], answers[0]['next_undo_label']), (rows[0]['id'], 'Reordered sessions'))
        self.assertIsNone(answers[0]['next_redo_label'])
        undo_api.get(SimpleNamespace(path=undo_api.PATH + '?brief=1', respond=lambda status, payload: answers.append(payload)), undo.Env())
        self.assertNotIn('why', answers[1]['entries'][0])  # no checks: cheap enough to poll

    def test_route_is_a_checked_write(self):
        import server
        self.assertIn(undo_api.PATH, server.TMUX_WRITES)


class TmuxBase(Private):
    def setUp(self):
        super().setUp()
        self.private = private_tmux(self, 'tun', {'SHELL': '/bin/sh'})
        self.run_tmux = self.private.run
        self.tmux = self.private.tmux
        for target, name, value in ((paths, 'SCOPE_TMUX', False),
                                    (undo_store, '_server', lambda: auto_resume.server_id(run=self.run_tmux)[1])):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.folder = str(self.temp)
        self.a = self.new('alpha')
        self.tmux('new-window', '-t', '=alpha:', '-c', self.folder)
        self.b = self.new('beta')
        self.a1, self.a2 = self.panes(self.a)
        self.b1, = self.panes(self.b)
        self.env = undo.Env(run=self.run_tmux)

    def new(self, name):
        self.tmux('new-session', '-d', '-s', name, '-x', '120', '-y', '40', '-c', self.folder, '/bin/sh')
        return self.tmux('display-message', '-p', '-t', f'={name}:', '#{session_id}').stdout.strip()

    def panes(self, session):
        return self.tmux('list-panes', '-s', '-t', session + ':', '-F', '#{pane_id}').stdout.split()

    def show(self, target, fmt):
        return self.tmux('display-message', '-p', '-t', target, fmt).stdout.rstrip('\n')

    def label(self, pane):
        return self.show(pane, '#{@group}')

    def names(self, session):
        return [g['name'] for g in groups.read(session, self.run_tmux)]

    def go(self, direction, confirmed, entry):
        """confirmed=True: say yes to what the confirm answer shows, as the page does (it sends back its token)."""
        result = undo.run(direction, entry, self.env)
        if confirmed and result['status'] == 'confirm':
            result = undo.run(direction, result['id'], self.env, confirmed=result['token'])
        return result

    def undo(self, confirmed=False, entry=None):
        return self.go('undo', confirmed, entry)

    def redo(self, confirmed=False, entry=None):
        return self.go('redo', confirmed, entry)

    def close(self, pane, entry=None):
        """As the close routes do: capture, close inside groups.closing (a Tether stack left with only its main session
        dissolves), then record."""
        tab = undo.capture(pane, self.run_tmux, entry)
        with groups.closing(pane, self.run_tmux):
            self.tmux('kill-pane', '-t', pane)
        undo.record_tab_close(tab, self.run_tmux)
        return tab

    def restarted(self):
        """tmux restarted: pane and session ids now belong to another server."""
        patch = mock.patch.object(undo, '_server', lambda run: '999:999')
        patch.start()
        self.addCleanup(patch.stop)


class GroupTests(TmuxBase):
    def test_create_with_a_tab_undo_and_redo(self):
        run = self.run_tmux
        with undo_store.acting('user'):
            group = undo.group_create(self.a, 'Docs', run, pane=self.a2, by_user=True)
        self.assertEqual(self.undo()['status'], 'done')  # not critical: no confirmation
        self.assertEqual(self.names(self.a), [])
        self.assertEqual(self.label(self.a2), '')
        self.assertEqual(self.redo()['status'], 'done')
        self.assertEqual(groups.read(self.a, run)[0]['id'], group['id'])
        self.assertEqual(self.label(self.a2), group['id'])

    def test_create_undo_refuses_when_another_tab_joined(self):
        run = self.run_tmux
        group = undo.group_create(self.a, 'Docs', run, pane=self.a2)
        groups.move(self.a, self.a1, group['id'], run)
        with self.assertRaisesRegex(undo.Refused, 'other tabs'):
            self.undo()
        self.assertEqual(self.names(self.a), ['Docs'])  # nothing changed

    def test_rename_undo_redo_and_refusal(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        undo.group_rename(self.a, group['id'], 'Guides', run)
        self.assertEqual(undo_store.entries()[-1]['label'], 'Renamed stack `Docs` to `Guides`')
        self.undo()
        self.assertEqual(self.names(self.a), ['Docs'])
        self.redo()
        self.assertEqual(self.names(self.a), ['Guides'])
        groups.rename(self.a, group['id'], 'Other', run)
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Other`'):
            self.undo()

    def test_move_undo_and_redo_keep_the_by_user_mark(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        undo.group_move(self.a, self.a2, group['id'], run, by_user=True)
        self.assertEqual(self.show(self.a2, '#{@group_by}'), 'user')
        self.undo()
        self.assertEqual((self.label(self.a2), self.show(self.a2, '#{@group_by}')), ('', ''))
        self.redo()
        self.assertEqual((self.label(self.a2), self.show(self.a2, '#{@group_by}')), (group['id'], 'user'))

    def test_move_undo_refuses_when_the_tab_moved_since(self):
        run = self.run_tmux
        docs, api = groups.create(self.a, 'Docs', run), groups.create(self.a, 'API', run)
        undo.group_move(self.a, self.a2, docs['id'], run)
        groups.move(self.a, self.a2, api['id'], run)
        with self.assertRaisesRegex(undo.Refused, 'moved to another stack'):
            self.undo()
        self.assertEqual(self.label(self.a2), api['id'])

    def test_move_undo_brings_back_an_auto_stack_the_move_dropped(self):
        run = self.run_tmux
        auto = groups.create(self.a, 'Helpers', run, pane=self.a2, auto=True)
        undo.group_move(self.a, self.a2, '', run, by_user=True)
        self.assertEqual(self.names(self.a), [])  # the empty auto stack went
        self.undo()
        self.assertEqual(groups.read(self.a, run)[0]['id'], auto['id'])
        self.assertEqual(self.label(self.a2), auto['id'])

    def test_ungroup_of_several_tabs_asks_first_and_brings_the_bundle_back(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        groups.move(self.a, self.a1, group['id'], run)
        groups.move(self.a, self.a2, group['id'], run)
        pid = groups.context(self.a, group['id'], run)
        undo.group_ungroup(self.a, group['id'], run, by_user=True)
        self.assertFalse(projects.bundle(pid).exists())
        asked = self.undo()
        self.assertEqual(asked['status'], 'confirm')
        self.assertIn('2 tabs', asked['effects'][0])
        self.assertEqual(self.names(self.a), [])  # nothing ran before the yes
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(self.names(self.a), ['Docs'])
        self.assertEqual({self.label(self.a1), self.label(self.a2)}, {group['id']})
        self.assertTrue(projects.bundle(pid).exists())

    def test_ungroup_undo_refuses_when_the_name_is_taken(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run, pane=self.a2)
        undo.group_ungroup(self.a, group['id'], run)
        groups.create(self.a, 'docs', run)
        with self.assertRaisesRegex(undo.Refused, 'Another stack is named'):
            self.undo()

    def test_remove_undo(self):
        run = self.run_tmux
        groups.create(self.a, 'First', run)
        group = groups.create(self.a, 'Empty', run)
        groups.create(self.a, 'Last', run)
        undo.group_remove(self.a, group['id'], run)
        self.undo()
        self.assertEqual(self.names(self.a), ['First', 'Empty', 'Last'])  # its old place

    def test_routes_record_as_the_user(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        data = json.dumps({'session': self.a, 'pane': self.a2, 'group': group['id']}).encode()
        answers = []
        handler = SimpleNamespace(path=groups_api.PREFIX + 'move', headers={'Content-Length': str(len(data))},
                                  rfile=io.BytesIO(data), respond=lambda status, body: answers.append(status))
        with undo_store.acting('user'), undo_store.step():
            groups_api.post(handler, run=run)
        self.assertEqual(answers, [200])
        entry = undo_store.entries()[-1]
        self.assertEqual((entry['who'], entry['actions'][0]['kind']), ({'by': 'user'}, 'group.move'))

    def test_other_tmux_server_refuses(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        undo.group_rename(self.a, group['id'], 'Guides', run)

        def moved(found):
            found[-1]['server'] = '1:1'
        undo_store.edit(moved)
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            self.undo()


class NameTests(TmuxBase):
    def test_pane_name_undo_redo_and_refusal(self):
        run = self.run_tmux
        before = undo.name_state(self.a2, run)
        self.tmux('set-option', '-p', '-t', self.a2, '@chat_name', 'Header')
        self.tmux('set-option', '-p', '-t', self.a2, '@chat_name_by', 'user')
        undo.record_pane_name(self.a2, before, 'Header', run)
        self.assertEqual(undo_store.entries()[-1]['label'], 'Named tab `Header`')
        windows = []
        self.env.name_window = lambda pane, name: windows.append((pane, name))
        self.undo()
        self.assertEqual(self.show(self.a2, '#{@chat_name}|#{@chat_name_by}'), '|')
        self.assertEqual(windows, [(self.a2, '')])
        self.redo()
        self.assertEqual(self.show(self.a2, '#{@chat_name}|#{@chat_name_by}'), 'Header|user')
        self.tmux('set-option', '-p', '-t', self.a2, '@chat_name', 'Agent name')
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Agent name`'):
            self.undo()

    def test_session_name_undo_and_taken_name(self):
        self.tmux('rename-session', '-t', self.a + ':', 'gamma')
        undo.record_session_name(self.a, 'alpha', 'gamma')
        self.undo()
        self.assertEqual(self.show(self.a + ':', '#{session_name}'), 'alpha')
        self.redo()
        self.tmux('rename-session', '-t', self.b + ':', 'alpha')
        with self.assertRaisesRegex(undo.Refused, 'Another session is named `alpha`'):
            self.undo()


class CloseAndStartTests(TmuxBase):
    def test_close_of_a_shell_undo_asks_then_reopens_in_place(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run, pane=self.a2)
        self.tmux('set-option', '-p', '-t', self.a2, '@chat_name', 'Notes')
        tab = self.close(self.a2)
        self.assertEqual(tab['agent'], 'shell')
        asked = self.undo()
        self.assertEqual(asked['status'], 'confirm')
        self.assertIn('Opens a new shell tab `Notes`', asked['effects'][0])
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        new = [pane for pane in self.panes(self.a) if pane != self.a1]
        self.assertEqual(len(new), 1)
        self.assertEqual(self.show(new[0], '#{@chat_name}|#{@group}'), f'Notes|{group["id"]}')
        asked = self.redo()
        self.assertEqual(asked['status'], 'confirm')
        self.assertIn('Closes shell tab `Notes`', asked['effects'][0])
        self.redo(confirmed=True)
        self.assertEqual(self.panes(self.a), [self.a1])

    def test_close_of_an_agent_without_a_conversation_refuses(self):
        self.close(self.a2, {'id': self.a2, 'agent': 'claude', 'thread': None, 'pending_conversation': True})
        with self.assertRaisesRegex(undo.Refused, 'no conversation yet'):
            self.undo()

    def test_close_of_an_agent_resumes_its_conversation(self):
        thread = '12345678-1234-1234-1234-123456789abc'
        self.close(self.a2, {'id': self.a2, 'agent': 'codex', 'thread': thread, 'codex_profile': 'work',
                             'transcript': ''})
        sent = []
        with mock.patch('history.find_chat', return_value={'id': thread}), \
                mock.patch.object(undo, 'resume_command', lambda *args, **kw: sent.append((args, kw)) or 'true'):
            asked = self.undo()
            self.assertIn('Starts Codex again', asked['effects'][0])
            self.undo(confirmed=True)
        self.assertEqual(sent, [((thread,), {'agent': 'codex', 'profile': 'work'})])

    def test_reopen_refuses_when_the_conversation_is_open(self):
        thread = '12345678-1234-1234-1234-123456789abc'
        self.close(self.a2, {'id': self.a2, 'agent': 'claude', 'thread': thread})
        self.env.entries = lambda: [{'id': self.b1, 'thread': thread, 'pane_name': 'Elsewhere'}]
        with self.assertRaisesRegex(undo.Refused, 'open in tab `Elsewhere`'):
            self.undo()

    def test_close_reopens_in_a_new_session_when_its_session_closed(self):
        self.tmux('set-option', '-p', '-t', self.b1, '@chat_name', 'Solo')
        self.close(self.b1)
        self.assertNotIn('beta', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())
        self.undo(confirmed=True)
        self.assertIn('beta', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_tab_start_undo_warns_about_work_and_unsent_text(self):
        new = self.tmux('new-window', '-t', self.a + ':', '-P', '-F', '#{pane_id}', '-c', self.folder).stdout.strip()
        with undo_store.acting('user'), undo_store.step():
            undo.record_tab_start(new, self.run_tmux)
        entry = undo_store.entries()[-1]
        self.assertTrue(entry['label'].startswith('Started tab'))
        self.env.entries = lambda: [{'id': new, 'agent': 'claude', 'pane_name': 'Helper', 'thread': None}]
        self.env.status = lambda e: {'state': 'Working'}
        self.env.draft = lambda e: ['half a prompt']
        self.env.web_draft = lambda pane: 'typed in Tether' if pane == new else ''
        asked = self.undo()
        text = ' '.join(asked['effects'])
        self.assertIn('Closes tab `Helper`. Claude in it stops.', text)
        self.assertIn('unfinished work is lost', text)
        self.assertIn('half a prompt', text)
        self.assertIn('typed in Tether', text)
        self.undo(confirmed=True)
        self.assertNotIn(new, self.panes(self.a))
        self.env.entries = lambda: []
        with self.assertRaisesRegex(undo.Refused, 'no conversation yet'):
            self.redo()

    def test_session_start_label_and_undo(self):
        pane = self.tmux('new-session', '-d', '-s', 'delta', '-c', self.folder, '-P', '-F', '#{pane_id}').stdout.strip()
        undo.record_tab_start(pane, self.run_tmux)
        self.assertEqual(undo_store.entries()[-1]['label'], 'Made session `delta`')
        self.undo(confirmed=True)
        self.assertNotIn('delta', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())
        self.redo(confirmed=True)  # a shell comes back as a fresh shell
        self.assertIn('delta', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_tab_start_undo_refuses_when_the_tab_closed(self):
        new = self.tmux('new-window', '-t', self.a + ':', '-P', '-F', '#{pane_id}').stdout.strip()
        undo.record_tab_start(new, self.run_tmux)
        self.tmux('kill-pane', '-t', new)
        with self.assertRaisesRegex(undo.Refused, 'has closed'):
            self.undo()

    def test_session_close_undo_reopens_its_tabs_in_order(self):
        run = self.run_tmux
        self.tmux('set-option', '-p', '-t', self.a1, '@chat_name', 'One')
        self.tmux('set-option', '-p', '-t', self.a2, '@chat_name', 'Two')
        saved = undo.capture_session(self.a, run, [])
        undo.record_session_close(saved)
        self.tmux('kill-session', '-t', self.a + ':')
        self.assertEqual(undo_store.entries()[-1]['label'], 'Closed session `alpha` (2 tabs)')
        asked = self.undo()
        self.assertEqual(asked['effects'][0], 'Makes session `alpha` again.')
        self.undo(confirmed=True)
        session = self.show('=alpha:', '#{session_id}')
        names = [self.show(pane, '#{@chat_name}') for pane in self.panes(session)]
        self.assertEqual(names, ['One', 'Two'])
        self.redo(confirmed=True)
        self.assertNotIn('alpha', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_session_close_undo_refuses_when_the_name_is_taken(self):
        saved = undo.capture_session(self.b, self.run_tmux, [])
        undo.record_session_close(saved)
        self.tmux('kill-session', '-t', self.b + ':')
        self.new('beta')
        with self.assertRaisesRegex(undo.Refused, 'named `beta` is open'):
            self.undo()


class MergeTests(TmuxBase):
    def move_stack_root_and_coordinator(self):
        run = self.run_tmux
        stack = groups.create(self.a, 'Stack', run, pane=self.a2, auto=True, root=self.a2)
        groups.move(self.a, self.a1, stack['id'], run)
        self.tmux('set-option', '-p', '-t', self.a2, '@tether_role', 'coordinator-needs')
        self.tmux('set-option', '-p', '-t', self.b1, '@tether_role', 'coordinator-off')
        noted = undo.record_merge('pane', self.a2, self.b, run)
        noted(tmux_merge.move_pane(self.a2, self.b, run))
        return stack

    def test_undo_and_redo_restore_and_clear_the_coordinator_and_stack_root(self):
        stack = self.move_stack_root_and_coordinator()
        run = self.run_tmux

        self.assertEqual(self.show(self.a2, '#{@tether_role}'), '')
        self.assertNotIn('root', groups.read(self.a, run)[0])
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}|#{@tether_role}'),
                         f'{self.a}|{stack["id"]}|coordinator-needs')
        self.assertEqual(groups.read(self.a, run), [stack])

        self.assertEqual(self.redo(confirmed=True)['status'], 'done')
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}|#{@tether_role}'), f'{self.b}||')
        self.assertNotIn('root', groups.read(self.a, run)[0])
        self.assertEqual(self.show(self.b1, '#{@tether_role}'), 'coordinator-off')

    def test_undo_preserves_a_new_coordinator_and_stack_root(self):
        stack = self.move_stack_root_and_coordinator()
        self.tmux('set-option', '-p', '-t', self.a1, '@tether_role', 'coordinator')
        new_stack = dict(stack, root=self.a1)
        with groups.LOCK():
            groups.write(self.a, [new_stack], self.run_tmux)

        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}|#{@tether_role}'), f'{self.a}|{stack["id"]}|')
        self.assertEqual(self.show(self.a1, '#{@tether_role}'), 'coordinator')
        self.assertEqual(groups.read(self.a, self.run_tmux), [new_stack])

    def test_moved_tab_goes_back_after_a_yes(self):
        import tmux_merge
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run, pane=self.a2)
        noted = undo.record_merge('pane', self.a2, self.b, run)
        noted(tmux_merge.move_pane(self.a2, self.b, run))
        self.assertEqual(self.show(self.a2, '#{session_id}'), self.b)
        asked = self.undo()
        self.assertEqual(asked['status'], 'confirm')
        self.assertIn('back to session `alpha`', asked['effects'][0])
        self.undo(confirmed=True)
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}'), f'{self.a}|{group["id"]}')
        result = self.redo(confirmed=True)
        self.assertNotIn('warnings', result)
        self.assertEqual(self.show(self.a2, '#{session_id}'), self.b)

    def test_broken_out_tab_goes_back(self):
        import tmux_merge
        run = self.run_tmux
        noted = undo.record_merge('break', self.a2, None, run)
        noted(tmux_merge.break_out(self.a2, run))
        self.assertNotEqual(self.show(self.a2, '#{session_id}'), self.a)
        self.undo(confirmed=True)
        self.assertEqual(self.show(self.a2, '#{session_id}'), self.a)

    def test_undo_restores_roots_of_an_unstacked_tab_and_redo_refreshes_them(self):
        run = self.run_tmux
        for kind, count in (('break', 1), ('break', 2), ('pane', 2)):
            with self.subTest(kind=kind, stacks=count):
                source = self.new(f'{kind}-{count}')
                self.tmux('new-window', '-t', source + ':', '-c', self.folder)
                _, pane = self.panes(source)
                target = self.b if kind == 'pane' else None
                stacks = [groups.create(source, f'Stack {i}', run, auto=True, root=pane) for i in range(count)]
                self.assertEqual(self.label(pane), '')
                noted = undo.record_merge(kind, pane, target, run)
                noted(tmux_merge.merge(kind, pane, target, run))
                self.assertNotEqual(self.show(pane, '#{session_id}'), source)
                self.assertEqual([stack.get('root') for stack in groups.read(source, run)], [None] * count)
                self.assertEqual(self.undo(confirmed=True)['status'], 'done')
                self.assertEqual(self.show(pane, '#{session_id}|#{@group}'), f'{source}|')
                self.assertEqual(groups.read(source, run), stacks)

                with groups.LOCK():
                    stacks[0].pop('root')
                    groups.write(source, stacks, run)
                stacks.append(groups.create(source, 'New stack', run, auto=True, root=pane))
                self.assertEqual(self.redo(confirmed=True)['status'], 'done')
                self.assertEqual([stack.get('root') for stack in groups.read(source, run)], [None] * len(stacks))
                self.assertEqual(self.undo(confirmed=True)['status'], 'done')
                self.assertEqual(groups.read(source, run), stacks)

    def test_redo_of_breakout_returns_its_warning(self):
        run = self.run_tmux
        noted = undo.record_merge('break', self.a2, None, run)
        noted(tmux_merge.break_out(self.a2, run))
        self.undo(confirmed=True)
        warning = 'The pane moved, but its new session kept an extra shell tab.'
        result = {'session': '$10', 'name': 'split', 'pane': self.a2, 'warning': warning}
        with mock.patch.object(tmux_merge, 'break_out', return_value=result) as break_out:
            redone = self.redo(confirmed=True)
        break_out.assert_called_once_with(self.a2, run)
        self.assertEqual(redone['status'], 'done')
        self.assertEqual(redone['warnings'], [warning])

    def test_merged_sessions_are_listed_but_refuse(self):
        noted = undo.record_merge('session', self.b, self.a, self.run_tmux)
        noted({'session': self.a})
        rows = undo.history(self.env)['entries']
        self.assertIn('cannot split merged sessions', rows[0]['why'])
        with self.assertRaisesRegex(undo.Refused, 'cannot split'):
            self.undo()


class ReviewFixTests(TmuxBase):
    """The reviewer's findings of 2026-11-15, one test each."""

    def test_redo_of_a_close_refuses_after_a_tmux_restart(self):
        self.close(self.a2)
        self.undo(confirmed=True)
        reopened = undo_store.entries()[-1]['actions'][0]['reopened']
        self.restarted()
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            self.redo(confirmed=True)
        self.assertIn(reopened, self.panes(self.a))

    def test_session_close_redo_refuses_after_a_tmux_restart(self):
        saved = undo.capture_session(self.b, self.run_tmux, [])
        undo.record_session_close(saved)
        self.tmux('kill-session', '-t', self.b + ':')
        self.undo(confirmed=True)
        self.restarted()
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            self.redo(confirmed=True)
        self.assertIn('beta', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_tab_start_undo_after_a_redo_checks_the_server_of_the_new_pane(self):
        pane = self.tmux('new-window', '-t', self.a + ':', '-P', '-F', '#{pane_id}', '-c', self.folder).stdout.strip()
        undo.record_tab_start(pane, self.run_tmux)
        self.undo(confirmed=True)
        self.redo(confirmed=True)
        action = undo_store.entries()[-1]['actions'][0]
        self.assertTrue(action['pane_server'])
        self.restarted()
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            self.undo(confirmed=True)
        self.assertIn(action['pane'], self.panes(self.a))

    def test_reopen_refuses_when_its_session_was_renamed(self):
        self.close(self.a2)
        self.tmux('rename-session', '-t', self.a + ':', 'gamma')
        with self.assertRaisesRegex(undo.Refused, 'renamed to `gamma`'):
            self.undo(confirmed=True)

    def test_reopen_refuses_when_another_session_took_the_name(self):
        self.close(self.b1)  # beta closes with its last tab
        other = self.new('beta')
        with self.assertRaisesRegex(undo.Refused, 'different session is named `beta`'):
            self.undo(confirmed=True)
        self.assertEqual(self.panes(other), [self.panes(other)[0]])

    def test_reopen_goes_back_to_its_old_place_and_keeps_the_session_state(self):
        run = self.run_tmux
        keep = groups.create(self.a, 'Keep', run, pane=self.a1)
        gone = groups.create(self.a, 'Gone', run)
        self.tmux('set-option', '-t', self.a, '@project', 'old')
        self.close(self.a1)  # the first window
        groups.remove(self.a, gone['id'], run)  # the person removed this stack since
        self.tmux('set-option', '-t', self.a, '@project', 'new')
        self.undo(confirmed=True)
        new = undo_store.entries()[-1]['actions'][0]['reopened']
        self.assertEqual(self.panes(self.a), [new, self.a2])  # in front again
        self.assertEqual(self.label(new), keep['id'])
        self.assertEqual(self.names(self.a), ['Keep'])  # its own stack only, not the removed one
        self.assertEqual(self.show(self.a + ':', '#{@project}'), 'new')

    def test_check_and_change_hold_the_stack_and_preference_locks(self):
        import threading
        group = groups.create(self.a, 'Docs', self.run_tmux)
        undo.group_rename(self.a, group['id'], 'Notes', self.run_tmux)
        held, original = [], undo.GroupRename.apply

        def apply(a, d, env):
            def probe():
                held.append(not groups._THREADS.acquire(timeout=0.2) and not prefs_sync.LOCK.acquire(timeout=0.2))
                with undo_store.acting('user'):  # a step of the person, saved while the undo runs
                    undo_store.record('prefs.order', 'Reordered sessions', key='order', sub=None, old=[], new=[])
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            return original(a, d, env)
        with mock.patch.object(undo.GroupRename, 'apply', staticmethod(apply)):
            self.undo()
        self.assertEqual(held, [True])
        renamed = next(e for e in undo_store.entries() if e['actions'][0]['kind'] == 'group.rename')
        self.assertEqual(renamed['state'], 'undone')
        self.assertFalse(renamed['redo'])  # the newer step of the person ended redo, and it stays ended
        self.assertIsNone(undo.next_target('redo'))

    def test_a_yes_counts_only_for_the_effects_it_saw(self):
        pane = self.tmux('new-window', '-t', self.a + ':', '-P', '-F', '#{pane_id}', '-c', self.folder).stdout.strip()
        undo.record_tab_start(pane, self.run_tmux)
        self.env.entries = lambda: [{'id': pane, 'agent': 'claude', 'pane_name': 'Helper', 'thread': None}]
        first = undo.run('undo', None, self.env)
        self.assertEqual(undo.run('undo', None, self.env, confirmed='forged')['status'], 'confirm')
        self.env.status = lambda e: {'state': 'Working'}  # it started a turn while the dialog was open
        again = undo.run('undo', first['id'], self.env, confirmed=first['token'])
        self.assertEqual(again['status'], 'confirm')
        self.assertNotEqual(again['token'], first['token'])
        self.assertIn(pane, self.panes(self.a))
        self.assertEqual(undo.run('undo', first['id'], self.env, confirmed=again['token'])['status'], 'done')
        self.assertNotIn(pane, self.panes(self.a))

    def test_ungroup_redo_refuses_when_the_stack_has_other_tabs(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        groups.move(self.a, self.a1, group['id'], run)
        groups.move(self.a, self.a2, group['id'], run)
        undo.group_ungroup(self.a, group['id'], run)
        self.undo(confirmed=True)
        extra = self.tmux('new-window', '-t', self.a + ':', '-P', '-F', '#{pane_id}').stdout.strip()
        groups.move(self.a, extra, group['id'], run)
        with self.assertRaisesRegex(undo.Refused, 'other tabs now'):
            self.redo(confirmed=True)
        self.assertEqual(self.label(extra), group['id'])

    def test_move_undo_refuses_when_a_stack_was_renamed(self):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run)
        undo.group_move(self.a, self.a2, group['id'], run, by_user=True)
        groups.rename(self.a, group['id'], 'Notes', run)
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Notes`'):
            self.undo()
        self.assertEqual(self.label(self.a2), group['id'])

    def test_create_redo_refuses_when_the_tab_left_the_session(self):
        run = self.run_tmux
        undo.group_create(self.a, 'Docs', run, pane=self.a2)
        self.undo()
        self.tmux('move-window', '-s', self.a2, '-t', self.b + ':')
        with self.assertRaisesRegex(undo.Refused, 'another session'):
            self.redo()

    def test_history_lock_failure_refuses(self):
        with mock.patch.object(undo_store.fcntl, 'flock', side_effect=OSError('no locks')):
            with self.assertRaises(undo_store.LockError):
                undo_store.entries()
            self.assertIsNone(undo_store.record('prefs.order', 'Reordered sessions', key='order', sub=None, old=[], new=[]))
            answers = []
            undo_api.get(SimpleNamespace(path=undo_api.PATH, respond=lambda status, payload: answers.append((status, payload))), self.env)
        self.assertEqual(answers[0][0], 503)
        self.assertIn('could not lock', answers[0][1]['error'])
        self.assertEqual(undo_store.entries(), [])

    def test_move_undo_that_drops_an_empty_tether_stack_brings_it_back_on_redo(self):
        run = self.run_tmux
        auto = groups.create(self.a, 'Helpers', run, auto=True)
        undo.group_move(self.a, self.a2, auto['id'], run)
        self.undo()
        self.assertEqual(self.names(self.a), [])  # it emptied, and Tether drops its own empty stacks
        self.redo()
        self.assertEqual(self.names(self.a), ['Helpers'])
        self.assertEqual(self.label(self.a2), auto['id'])



class ReviewRound2Tests(TmuxBase):
    """The reviewer's second-round findings of 2026-11-15, one test or more each."""

    # 1. Pane and session ids repeat after a tmux restart.
    def test_reopen_after_a_restart_refuses_when_a_session_has_its_name(self):
        self.close(self.a2)
        self.restarted()
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted since, so session `alpha`'):
            self.undo(confirmed=True)
        self.assertEqual(self.panes(self.a), [self.a1])

    def test_reopen_after_a_restart_makes_a_new_session_when_the_name_is_free(self):
        self.close(self.b1)  # beta closes with its last tab
        self.restarted()
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertIn('beta', self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_a_step_saved_without_a_server_id_refuses(self):
        group = groups.create(self.a, 'Docs', self.run_tmux)
        with mock.patch.object(undo_store, '_server', lambda: None):
            undo.group_rename(self.a, group['id'], 'Notes', self.run_tmux)
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            self.undo()
        self.assertEqual(self.names(self.a), ['Notes'])

    # 2. The wrappers read the old state, change it and record under one hold of the stack lock.
    def test_wrappers_record_while_they_hold_the_stack_lock(self):
        import threading
        run, held, original = self.run_tmux, [], undo_store.record

        def record(*args, **kwargs):
            def probe():
                got = groups._THREADS.acquire(timeout=0.2)
                if got:
                    groups._THREADS.release()
                held.append(not got)
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            return original(*args, **kwargs)
        with mock.patch.object(undo_store, 'record', record):
            docs = undo.group_create(self.a, 'Docs', run, pane=self.a2)
            undo.group_rename(self.a, docs['id'], 'Notes', run)
            undo.group_move(self.a, self.a1, docs['id'], run)
            undo.group_ungroup(self.a, docs['id'], run)
            other = groups.create(self.a, 'Other', run)
            undo.group_remove(self.a, other['id'], run)
        self.assertEqual(held, [True] * 5)

    # 3. A yes binds to the entry as the dialog saw it, not only to the effects text.
    def test_a_yes_does_not_carry_over_to_a_new_pane_with_the_same_effects(self):
        self.close(self.a2)
        self.undo(confirmed=True)
        asked = undo.run('redo', None, self.env)
        self.assertEqual(asked['status'], 'confirm')
        self.redo(confirmed=True)  # another page redid and undid it meanwhile: the tab has a new pane id
        self.undo(confirmed=True)
        again = undo.run('redo', asked['id'], self.env, confirmed=asked['token'])
        self.assertEqual(again['status'], 'confirm')
        self.assertEqual(again['effects'], asked['effects'])  # the same words, a different tab
        self.assertNotEqual(again['token'], asked['token'])
        self.assertEqual(len(self.panes(self.a)), 2)

    # 4. A tab reopened in its open session goes back only to the same stack.
    def test_reopen_refuses_when_its_stack_was_renamed(self):
        keep = groups.create(self.a, 'Keep', self.run_tmux, pane=self.a2, by_user=True)
        self.close(self.a2)
        groups.rename(self.a, keep['id'], 'Other', self.run_tmux)
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Other`'):
            self.undo(confirmed=True)
        self.assertEqual(self.panes(self.a), [self.a1])

    def test_reopen_refuses_when_another_stack_took_the_name(self):
        keep = groups.create(self.a, 'Keep', self.run_tmux, pane=self.a2, by_user=True)
        self.close(self.a2)
        groups.remove(self.a, keep['id'], self.run_tmux)
        groups.create(self.a, 'Keep', self.run_tmux)
        with self.assertRaisesRegex(undo.Refused, 'another stack is named `Keep`'):
            self.undo(confirmed=True)
        self.assertEqual(self.panes(self.a), [self.a1])

    def test_reopen_drops_a_label_with_no_saved_stack(self):
        self.tmux('set-option', '-p', '-t', self.a2, '@group', 'g0000abcd')  # points to no stack
        self.close(self.a2)
        self.undo(confirmed=True)
        new = undo_store.entries()[-1]['actions'][0]['reopened']
        self.assertEqual(self.label(new), '')

    # 5. The stack store refuses when it cannot take its file lock.
    def test_stack_write_refuses_without_the_file_lock(self):
        with mock.patch.object(groups.fcntl, 'flock', side_effect=OSError('no locks')):
            with self.assertRaises(groups.GroupError) as caught:
                groups.create(self.a, 'Docs', self.run_tmux)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.names(self.a), [])

    # 6. Undo of a new stack puts the tab back in the stack it came from, and brings that stack back when it went.
    def test_create_undo_brings_back_the_stack_the_tab_came_from(self):
        run = self.run_tmux
        helpers = groups.create(self.a, 'Helpers', run, pane=self.a2, auto=True)
        undo.group_create(self.a, 'Docs', run, pane=self.a2, by_user=True)
        groups.drop_lone_auto(run)  # the sweeper removes the emptied Tether stack
        self.assertEqual(self.names(self.a), ['Docs'])
        self.undo()
        self.assertEqual(self.names(self.a), ['Helpers'])
        self.assertEqual(self.label(self.a2), helpers['id'])
        self.redo()
        self.assertEqual(self.label(self.a2), groups.read(self.a, run)[-1]['id'])

    def test_create_undo_refuses_when_the_old_stack_name_is_taken(self):
        run = self.run_tmux
        groups.create(self.a, 'Helpers', run, pane=self.a2, auto=True)
        undo.group_create(self.a, 'Docs', run, pane=self.a2, by_user=True)
        groups.drop_lone_auto(run)
        groups.create(self.a, 'Helpers', run)
        self.assertIn('another stack has its name', undo.history(self.env)['entries'][0]['why'])  # History says why
        with self.assertRaisesRegex(undo.Refused, 'another stack has its name'):
            self.undo()
        self.assertEqual(sorted(self.names(self.a)), ['Docs', 'Helpers'])



class ReviewRound3Tests(TmuxBase):
    """The reviewer's third-round findings of 2026-11-15."""

    def test_create_undo_refuses_when_the_source_stack_was_renamed(self):
        run = self.run_tmux
        helpers = groups.create(self.a, 'Helpers', run, pane=self.a2, by_user=True)
        undo.group_create(self.a, 'Docs', run, pane=self.a2, by_user=True)
        groups.rename(self.a, helpers['id'], 'Other', run)
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Other`'):
            self.undo()
        self.assertNotEqual(self.label(self.a2), helpers['id'])

    def test_create_redo_refuses_when_the_source_stack_was_renamed(self):
        run = self.run_tmux
        helpers = groups.create(self.a, 'Helpers', run, pane=self.a2, by_user=True)
        undo.group_create(self.a, 'Docs', run, pane=self.a2, by_user=True)
        self.undo()
        groups.rename(self.a, helpers['id'], 'Other', run)
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Other`'):
            self.redo()
        self.assertEqual(self.label(self.a2), helpers['id'])

    def test_steps_saved_out_of_order_keep_the_order_of_their_changes(self):
        import threading
        changed, saved_second = threading.Event(), threading.Event()

        def first():  # a request whose change comes first but whose answer is sent last
            with undo_store.acting('user'), undo_store.step():
                undo_store.record('prefs.order', 'B', key='order', sub=None, old=['a', 'b'], new=['b', 'a'])
                changed.set()
                saved_second.wait(5)
        thread = threading.Thread(target=first)
        thread.start()
        changed.wait(5)
        with undo_store.acting('user'), undo_store.step():
            undo_store.record('prefs.order', 'C', key='order', sub=None, old=['b', 'a'], new=['a', 'b'])
        saved_second.set()
        thread.join()
        found = undo_store.entries()
        self.assertEqual([entry['label'] for entry in found], ['B', 'C'])
        self.assertEqual(undo.next_target('undo', found), found[-1]['id'])

    def test_undo_answers_503_when_the_stacks_cannot_be_locked(self):
        groups.create(self.a, 'Docs', self.run_tmux)
        undo.group_rename(self.a, groups.read(self.a, self.run_tmux)[0]['id'], 'Notes', self.run_tmux)
        data = json.dumps({'action': 'undo'}).encode()
        answers = []
        handler = SimpleNamespace(headers={'Content-Length': str(len(data))}, rfile=io.BytesIO(data),
                                  respond=lambda status, payload: answers.append((status, payload)))
        with mock.patch.object(groups.fcntl, 'flock', side_effect=OSError('no locks')):
            undo_api.post(handler, self.env, mock.MagicMock())
        self.assertEqual(answers, [(503, {'error': groups.LOCK_FAILED})])
        self.assertEqual(self.names(self.a), ['Notes'])



class ReviewRound4Tests(Private):
    """The reviewer's fourth-round findings of 2026-11-15: a step that saves after its change."""

    def test_a_step_keeps_the_server_of_its_change(self):
        with mock.patch.object(undo_store, '_server', lambda: 'old:1'):
            with undo_store.step():
                undo_store.record('session.name', 'Renamed', session='$1', old='a', new='b')
                undo_store._server = lambda: 'new:2'  # tmux restarted before the request ended
        self.assertEqual(undo_store.entries()[-1]['server'], 'old:1')

    def test_a_step_with_changes_on_two_servers_has_no_server(self):
        servers = iter(['old:1', 'new:2'])
        with mock.patch.object(undo_store, '_server', lambda: next(servers, 'new:2')):
            with undo_store.step():
                undo_store.record('session.name', 'Renamed', session='$1', old='a', new='b')
                undo_store.record('session.name', 'Renamed', session='$1', old='b', new='c')
        self.assertIsNone(undo_store.entries()[-1]['server'])
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            undo.run('undo', None, undo.Env())

    def test_a_step_saved_late_keeps_the_redo_of_a_later_undo(self):
        import threading
        changed, undone = threading.Event(), threading.Event()

        def first():  # request A: its change comes first, its answer last
            with undo_store.acting('user'), undo_store.step():
                undo_store.record('prefs.order', 'A', key='order', sub=None, old=['x', 'y'], new=['y', 'x'])
                changed.set()
                undone.wait(5)
        thread = threading.Thread(target=first)
        thread.start()
        changed.wait(5)
        prefs_sync.save({'order': ['b', 'a']})
        with undo_store.acting('user'):
            undo.record_order('order', ['a', 'b'], ['b', 'a'])  # request B
        self.assertEqual(undo.run('undo', None, undo.Env())['status'], 'done')
        undone.set()
        thread.join()
        b = next(e for e in undo_store.entries() if e['label'] == 'Reordered sessions')
        self.assertTrue(b['redo'])
        self.assertEqual(undo.next_target('redo'), b['id'])

    def test_a_step_saved_during_an_undo_but_changed_before_it_keeps_redo(self):
        prefs_sync.save({'order': ['b', 'a']})
        undo.record_order('order', ['a', 'b'], ['b', 'a'])
        original = undo.PrefsOrder.apply

        def apply(a, d, env):
            with undo_store.acting('user'):  # a request that changed before this undo and saves now
                undo_store._save('Earlier', [{'kind': 'prefs.order', 'key': 'order', 'sub': None, 'old': [], 'new': []}],
                                 at=time.time() - 5)
            return original(a, d, env)
        with mock.patch.object(undo.PrefsOrder, 'apply', staticmethod(apply)):
            undo.run('undo', None, undo.Env())
        b = next(e for e in undo_store.entries() if e['label'] == 'Reordered sessions')
        self.assertEqual((b['state'], b['redo']), ('undone', True))


class Stacked:
    def stack(self):
        """Stack "Helpers" for main session a1, with helper a2."""
        run = self.run_tmux
        group = groups.create_unique(self.a, 'Helpers', run, root=self.a1)
        groups.move(self.a, self.a1, group['id'], run)
        groups.move(self.a, self.a2, group['id'], run)
        self.tmux('set-option', '-p', '-t', self.a2, '@chat_name', 'Helper')
        return group

    def reopened(self):
        return next(pane for pane in self.panes(self.a) if pane != self.a1)


class DissolveTests(Stacked, TmuxBase):
    """A close or a move that leaves a Tether-made stack with only its main session dissolves it (groups.drop_if_alone):
    the main session goes to Unstacked. Undo puts it back in the stack; redo dissolves the stack again."""

    def test_close_undo_puts_the_main_session_back_and_redo_dissolves_again(self):
        group = self.stack()
        tab = self.close(self.a2)
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))  # dissolved
        self.assertEqual(tab['released'], [self.a1])
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(self.names(self.a), ['Helpers'])
        self.assertEqual((self.label(self.a1), self.label(self.reopened())), (group['id'], group['id']))
        self.assertEqual(self.redo(confirmed=True)['status'], 'done')
        self.assertEqual((self.names(self.a), self.label(self.a1), self.panes(self.a)), ([], '', [self.a1]))
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')  # and back again, from the redo's record
        self.assertEqual((self.label(self.a1), self.label(self.reopened())), (group['id'], group['id']))

    def test_close_undo_refuses_when_the_main_session_moved_since(self):
        run = self.run_tmux
        self.stack()
        self.close(self.a2)
        other = groups.create(self.a, 'Other', run)
        groups.move(self.a, self.a1, other['id'], run, by_user=True)
        with self.assertRaisesRegex(undo.Refused, 'left stack `Helpers` when this tab closed, and it was moved since'):
            self.undo(confirmed=True)
        self.assertEqual(self.panes(self.a), [self.a1])  # nothing reopened

    def test_close_undo_refuses_when_the_main_session_was_put_in_unstacked_by_hand(self):
        self.stack()
        self.close(self.a2)
        groups.move(self.a, self.a1, '', self.run_tmux, by_user=True)
        with self.assertRaisesRegex(undo.Refused, 'moved since'):
            self.undo(confirmed=True)

    def test_close_undo_without_the_main_session_brings_the_stack_back_alone(self):
        group = self.stack()
        self.close(self.a2)
        self.tmux('new-window', '-t', '=alpha:', '-c', self.folder)  # the session stays open
        self.tmux('kill-pane', '-t', self.a1)  # the main session closed since: nothing to put back
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        reopened = [p for p in self.panes(self.a) if self.label(p) == group['id']]
        self.assertEqual(len(reopened), 1)

    def test_close_of_a_tab_that_leaves_other_helpers_releases_nothing(self):
        run = self.run_tmux
        group = self.stack()
        self.tmux('new-window', '-t', '=alpha:', '-c', self.folder)
        third = [p for p in self.panes(self.a) if p not in (self.a1, self.a2)][0]
        groups.move(self.a, third, group['id'], run)
        tab = self.close(self.a2)
        self.assertEqual((tab['released'], self.names(self.a)), ([], ['Helpers']))
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(len(groups.panes_in(self.a, group['id'], run)), 3)

    def test_move_out_dissolves_and_undo_redo_follow(self):
        run = self.run_tmux
        group = self.stack()
        undo.group_move(self.a, self.a2, '', run, by_user=True)
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))
        self.assertEqual(self.undo()['status'], 'done')
        self.assertEqual((self.names(self.a), self.label(self.a1), self.label(self.a2)),
                         (['Helpers'], group['id'], group['id']))
        self.assertEqual(self.redo()['status'], 'done')
        self.assertEqual((self.names(self.a), self.label(self.a1), self.label(self.a2)), ([], '', ''))
        self.assertEqual(self.undo()['status'], 'done')
        self.assertEqual((self.label(self.a1), self.label(self.a2)), (group['id'], group['id']))

    def test_the_sweep_records_nothing(self):
        """inventory.md: the sweep after a close outside Tether is not a Tether step; a close undo re-adds the stack."""
        self.stack()
        self.tmux('kill-pane', '-t', self.a2)
        before = len(undo_store.entries())
        self.assertEqual(groups.drop_lone_auto(self.run_tmux), [(self.a, 'Helpers')])
        self.assertEqual(len(undo_store.entries()), before)


class ReviewRound6Tests(Stacked, TmuxBase):
    """Round 6: released tabs bind to their tmux server; move undo and tab-start redo follow the dissolve too; the search
    menu and close --self record their steps."""
    def test_released_tabs_of_another_tmux_server_are_left_alone(self):
        group = self.stack()
        self.close(self.a2)
        self.restarted()
        self.tmux('rename-session', '-t', self.a, 'other')  # the old name is free: the tab reopens in a new session
        other = groups.create(self.a, 'Mine', self.run_tmux)
        groups.move(self.a, self.a1, other['id'], self.run_tmux, by_user=True)  # an unrelated tab, as tmux sees it
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(self.label(self.a1), other['id'])
        new = self.tmux('display-message', '-p', '-t', '=alpha:', '#{pane_id}').stdout.strip()
        self.assertEqual(self.label(new), group['id'])

    def test_move_undo_refuses_when_the_main_session_was_placed_by_hand(self):
        run = self.run_tmux
        self.stack()
        undo.group_move(self.a, self.a2, '', run, by_user=True)
        groups.move(self.a, self.a1, '', run, by_user=True)
        with self.assertRaisesRegex(undo.Refused, 'left stack `Helpers` when this tab moved out, and it was moved since'):
            self.undo()
        self.assertEqual((self.names(self.a), self.label(self.a2)), ([], ''))  # nothing changed

    def test_tab_start_undo_dissolves_and_redo_puts_the_main_session_back(self):
        group = self.stack()
        undo.record_tab_start(self.a2, self.run_tmux)
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual((self.names(self.a), self.label(self.a1), self.panes(self.a)), ([], '', [self.a1]))
        self.assertEqual(self.redo(confirmed=True)['status'], 'done')
        self.assertEqual((self.label(self.a1), self.label(self.reopened())), (group['id'], group['id']))

    def test_tab_start_redo_refuses_when_the_main_session_moved_since(self):
        self.stack()
        undo.record_tab_start(self.a2, self.run_tmux)
        self.undo(confirmed=True)
        groups.move(self.a, self.a1, '', self.run_tmux, by_user=True)
        with self.assertRaisesRegex(undo.Refused, 'moved since'):
            self.redo(confirmed=True)

    def entries(self):
        return [{'id': pane, 'tmux_session': session, 'label': f'{name}:1.0', 'agent': 'claude'}
                for session, name in ((self.a, 'alpha'), (self.b, 'beta')) for pane in self.panes(session)]

    def test_search_menu_start_records_the_tab_and_the_stack_it_made(self):
        import quick_tab
        starter = quick_tab.Starter(self.run_tmux)
        self.tmux('rename-session', '-t', self.a, quick_tab.DEFAULT_SESSION)

        def start(anchor, body, folder, run, cwd, group, *, by_user):
            pane = self.tmux('new-window', '-P', '-F', '#{pane_id}', '-t', self.a + ':', '-c', self.folder).stdout.strip()
            groups.move(self.a, pane, group, run, by_user=by_user)
            return pane, 'New'
        entries = [dict(e, label=f'{quick_tab.DEFAULT_SESSION}:1.0') if e['tmux_session'] == self.a else e
                   for e in self.entries()]
        with mock.patch.object(quick_tab.delegate, 'start', start), undo_store.acting('user'), undo_store.step():
            answer = starter.start({'request': 'r' * 16, 'default': True, 'message': 'hi'}, entries, {},
                                   lambda entry: self.folder)
            starter.start({'request': 'r' * 16, 'default': True, 'message': 'hi'}, entries, {}, lambda entry: self.folder)
        found = undo_store.entries()
        self.assertEqual(len(found), 1)  # the repeat answer records nothing
        self.assertEqual([a['kind'] for a in found[0]['actions']], ['group.create', 'tab.start'])
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertNotIn(answer['pane'], self.panes(self.a))
        self.assertEqual(self.names(self.a), [])

    def test_search_menu_move_to_another_session_undoes_in_one_step(self):
        import quick_tab
        run = self.run_tmux
        starter = quick_tab.Starter(run)
        mine = groups.create(self.b, 'Mine', run)
        starter.started[self.a2] = starter.clock()
        with undo_store.acting('user'), undo_store.step():
            starter.move({'pane': self.a2, 'session': self.b, 'group': mine['id']}, self.entries(), {self.b: [mine]})
        self.assertEqual((self.show(self.a2, '#{session_id}'), self.label(self.a2)), (self.b, mine['id']))
        self.assertEqual([a['kind'] for a in undo_store.entries()[-1]['actions']], ['merge.pane', 'group.move'])
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual((self.show(self.a2, '#{session_id}'), self.label(self.a2)), (self.a, ''))

    def test_close_self_records_an_agent_step(self):
        import self_close
        self.stack()
        entry = {'id': self.a2, 'agent': 'shell', 'thread': None, 'pane_name': 'Helper'}
        clock = iter(range(0, 10000, 5))
        result = self_close.watch(entry, self.run_tmux, lambda e: {'state': 'Idle'}, sessions=lambda: [entry],
                                  lock=threading.Lock(), fresh=lambda e: {'state': 'Idle'}, sleep=lambda s: None,
                                  now=lambda: next(clock))
        self.assertEqual(result, 'closed')
        found = undo_store.entries()[-1]
        self.assertEqual((found['who'], found['actions'][0]['kind']), ({'by': 'agent', 'tab': 'Helper'}, 'tab.close'))
        self.assertEqual(found['actions'][0]['tab']['released'], [self.a1])  # its stack dissolved



class StableNameTests(TmuxBase):
    """A tab without a Tether name is named by its session, as Tether shows it, never by tmux's window name: tmux
    renames the window after each command (a new shell runs `locale` first), so the confirm text changed under load."""
    def rename_window(self, pane, name):
        self.tmux('rename-window', '-t', pane, name)

    def test_redo_of_a_close_names_the_tab_by_its_session(self):
        self.close(self.a2)
        self.undo(confirmed=True)
        new = [pane for pane in self.panes(self.a) if pane != self.a1][0]
        self.rename_window(new, 'locale')
        first = self.redo()
        self.rename_window(new, 'sh')
        second = self.redo()
        self.assertEqual(first['effects'][0], 'Closes shell tab `alpha`.')
        self.assertEqual(second['effects'], first['effects'])

    def test_undo_of_a_start_names_the_tab_by_its_session(self):
        self.rename_window(self.a2, 'locale')
        undo.record_tab_start(self.a2, self.run_tmux)
        self.assertEqual(undo_store.entries()[-1]['label'], 'Started tab `alpha`')
        asked = self.undo()
        self.assertEqual(asked['status'], 'confirm')
        self.assertTrue(asked['effects'][0].startswith('Closes tab `alpha`.'), asked['effects'])
        self.assertEqual(self.show(self.a2, '#{window_name}'), 'locale')  # the name tmux shows is still a command



class ReviewRound7Tests(Stacked, TmuxBase):
    # 1. A record that fails after the step happened raises nothing, and saves no part of the step.
    def failing(self, marker):
        def run(command, **options):
            if 'display-message' in command and marker in command[-1]:
                raise subprocess.TimeoutExpired(command, 5)
            return self.run_tmux(command, **options)
        return run

    def test_a_failed_start_record_raises_nothing_and_drops_the_whole_step(self):
        before = len(undo_store.entries())
        with undo_store.step():
            undo.group_create(self.a, 'Fresh', self.run_tmux)
            undo.record_tab_start(self.a2, self.failing('#{session_windows}'))
        self.assertIn('Fresh', self.names(self.a))  # the steps happened
        self.assertEqual(len(undo_store.entries()), before)  # no stack-only entry to undo alone

    def test_a_failed_move_record_returns_the_move(self):
        group = groups.create(self.a, 'Docs', self.run_tmux)
        before = len(undo_store.entries())
        with mock.patch.object(undo, '_group_name', side_effect=subprocess.TimeoutExpired(['tmux'], 5)):
            undo.group_move(self.a, self.a2, group['id'], self.run_tmux, by_user=True)
        self.assertEqual(self.label(self.a2), group['id'])
        self.assertEqual(len(undo_store.entries()), before)

    # 2. A search-menu move to a stack of another session: redo checks the stack move where the session move puts the tab.
    def search_move(self):
        run = self.run_tmux
        inbox = groups.create(self.b, 'Inbox', run)
        with undo_store.step():
            noted = undo.record_merge('pane', self.a2, self.b, run)
            noted(tmux_merge.move_pane(self.a2, self.b, run))
            undo.group_move(self.b, self.a2, inbox['id'], run, by_user=True)
        return inbox

    def test_a_cross_session_search_move_undoes_and_redoes(self):
        inbox = self.search_move()
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}'), f'{self.a}|')
        self.assertEqual(self.redo(confirmed=True)['status'], 'done')
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}'), f'{self.b}|{inbox["id"]}')

    def test_its_redo_refuses_when_the_tab_was_stacked_by_hand_since(self):
        self.search_move()
        self.undo(confirmed=True)
        other = groups.create(self.a, 'Other', self.run_tmux)
        groups.move(self.a, self.a2, other['id'], self.run_tmux, by_user=True)
        with self.assertRaisesRegex(undo.Refused, 'moved to another stack since'):
            self.redo(confirmed=True)
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}'), f'{self.a}|{other["id"]}')

    # 3. A move out of the session, then the sweep dissolved the stack it left: undo brings the stack and its main
    # session back.
    def moved_out(self, kind):
        run = self.run_tmux
        group = self.stack()
        noted = undo.record_merge(kind, self.a2, self.b if kind == 'pane' else None, run)
        noted(tmux_merge.move_pane(self.a2, self.b, run) if kind == 'pane' else tmux_merge.break_out(self.a2, run))
        groups.drop_lone_auto(run)
        self.assertEqual((self.names(self.a), self.label(self.a1)), ([], ''))  # dissolved
        return group

    def test_undo_of_a_session_move_brings_back_the_dissolved_stack(self):
        for kind in ('pane', 'break'):
            with self.subTest(kind=kind):
                group = self.moved_out(kind)
                self.assertEqual(self.undo(confirmed=True)['status'], 'done')
                self.assertEqual(self.names(self.a), ['Helpers'])
                self.assertEqual((self.label(self.a1), self.show(self.a2, '#{session_id}|#{@group}')),
                                 (group['id'], f'{self.a}|{group["id"]}'))
                self.redo(confirmed=True)
                groups.drop_lone_auto(self.run_tmux)
                self.assertEqual(self.undo(confirmed=True)['status'], 'done')  # again, from the same record
                self.assertEqual((self.label(self.a1), self.label(self.a2)), (group['id'], group['id']))
                for pane in (self.a1, self.a2):  # the stack goes with its last tab, for the next kind
                    groups.move(self.a, pane, '', self.run_tmux)

    def test_that_undo_refuses_when_the_main_session_was_placed_by_hand_since(self):
        self.moved_out('pane')
        groups.move(self.a, self.a1, '', self.run_tmux, by_user=True)
        with self.assertRaisesRegex(undo.Refused, 'left stack `Helpers` when this tab moved out, and it was moved since'):
            self.undo(confirmed=True)
        self.assertEqual(self.show(self.a2, '#{session_id}'), self.b)  # nothing changed



class ReviewRound8Tests(Stacked, TmuxBase):
    # 1. The stack a tab left for another session goes back only by its id and its name, both ways.
    def moved(self, kind):
        run = self.run_tmux
        group = groups.create(self.a, 'Docs', run, pane=self.a2)
        noted = undo.record_merge(kind, self.a2, self.b if kind == 'pane' else None, run)
        noted(tmux_merge.move_pane(self.a2, self.b, run) if kind == 'pane' else tmux_merge.break_out(self.a2, run))
        return group

    def test_session_move_undo_refuses_when_its_stack_was_renamed(self):
        for kind in ('pane', 'break'):
            with self.subTest(kind=kind):
                group = self.moved(kind)
                with undo_store.acting('user'):
                    undo.group_rename(self.a, group['id'], 'Notes', self.run_tmux)  # a newer recorded step
                with self.assertRaisesRegex(undo.Refused, 'renamed to `Notes`'):
                    self.undo(confirmed=True, entry=undo_store.entries()[-2]['id'])
                self.assertNotEqual(self.show(self.a2, '#{session_id}'), self.a)  # nothing moved
                groups.remove(self.a, group['id'], self.run_tmux)
                tmux_merge.move_pane(self.a2, self.a, self.run_tmux)

    def test_session_move_redo_refuses_when_its_stack_was_renamed(self):
        group = self.moved('pane')
        self.undo(confirmed=True)
        groups.rename(self.a, group['id'], 'Notes', self.run_tmux)
        with self.assertRaisesRegex(undo.Refused, 'renamed to `Notes`'):
            self.redo(confirmed=True)
        self.assertEqual(self.show(self.a2, '#{session_id}|#{@group}'), f'{self.a}|{group["id"]}')

    # 2. A failed read after a stack was made returns the stack and records nothing.
    def test_failed_reads_after_a_create_return_the_stack(self):
        made = {'create': lambda: undo.group_create(self.a, 'One', self.run_tmux, pane=self.a2),
                'find_or_create': lambda: undo.group_find_or_create(self.a, 'Three', self.run_tmux)}
        for name, make in made.items():
            with self.subTest(name=name):
                before = len(undo_store.entries())
                with mock.patch.object(undo, '_group_place', side_effect=subprocess.TimeoutExpired(['tmux'], 5)), \
                        undo_store.step():
                    group = make()
                self.assertIn(group['name'], self.names(self.a))
                self.assertEqual(len(undo_store.entries()), before)

    def test_failed_reads_around_a_rename_record_nothing_and_raise_nothing(self):
        with mock.patch.object(undo, '_show', side_effect=subprocess.TimeoutExpired(['tmux'], 5)):
            self.assertIsNone(undo.name_state(self.a2, self.run_tmux))
            self.assertIsNone(undo.session_of(self.a2, self.run_tmux))


class ReviewRound9Tests(Stacked, TmuxBase):
    # 1. The session a tab left closed since: undo refuses and changes nothing; Tether does not make the session again.
    def test_undo_refuses_when_the_old_session_closed(self):
        run = self.run_tmux
        gamma = self.new('gamma')
        g1, = self.panes(gamma)
        groups.create(gamma, 'Solo', run, pane=g1)
        noted = undo.record_merge('pane', g1, self.b, run)
        noted(tmux_merge.move_pane(g1, self.b, run))  # its last tab: gamma closes
        noted = undo.record_merge('break', self.a2, None, run)
        noted(tmux_merge.break_out(self.a2, run))
        self.tmux('kill-session', '-t', self.a)
        for entry, tab, name in ((undo_store.entries()[-1], self.a2, 'alpha'), (undo_store.entries()[-2], g1, 'gamma')):
            with self.subTest(name=name):
                before = (self.tmux('list-sessions', '-F', '#{session_id}|#{session_name}').stdout,
                          self.show(tab, '#{session_id}|#{@group}'))
                with self.assertRaisesRegex(undo.Refused, f'Session `{name}` closed after this move, so the tab '
                                                          'cannot go back'):
                    self.undo(confirmed=True, entry=entry['id'])
                self.assertEqual((self.tmux('list-sessions', '-F', '#{session_id}|#{session_name}').stdout,
                                  self.show(tab, '#{session_id}|#{@group}')), before)
                self.assertEqual(undo_store.find(undo_store.entries(), entry['id'])['state'], 'done')

    def test_redo_refuses_when_the_session_it_moved_to_closed(self):
        run = self.run_tmux
        noted = undo.record_merge('pane', self.a2, self.b, run)
        noted(tmux_merge.move_pane(self.a2, self.b, run))
        self.assertEqual(self.undo(confirmed=True)['status'], 'done')
        self.tmux('kill-session', '-t', self.b)
        with self.assertRaisesRegex(undo.Refused, 'Session `beta` closed after this move, so the tab cannot go there'):
            self.redo(confirmed=True)
        self.assertEqual(self.show(self.a2, '#{session_id}'), self.a)
        self.assertEqual(undo_store.entries()[-1]['state'], 'undone')

    # 2. A snapshot that cannot be read records nothing of the step.
    def test_an_unreadable_snapshot_drops_the_whole_step(self):
        run = self.run_tmux
        inbox = groups.create(self.b, 'Inbox', run)
        before = len(undo_store.entries())
        real = undo._show
        short = lambda r, target, fmt: ['x'] if '#{@group_by}' in fmt else real(r, target, fmt)
        with mock.patch.object(undo, '_show', side_effect=short), undo_store.step():
            noted = undo.record_merge('pane', self.a2, self.b, run)
            noted(tmux_merge.move_pane(self.a2, self.b, run))
            undo.group_move(self.b, self.a2, inbox['id'], run, by_user=True)
        self.assertEqual(self.show(self.a2, '#{session_id}'), self.b)  # the move happened
        self.assertEqual(len(undo_store.entries()), before)  # no half step to undo


if __name__ == '__main__':
    unittest.main()
