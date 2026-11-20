"""Panel actions use mocked writes, except for one isolated tmux key probe."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import needs
import prompt_actions as actions
import push
import server
import terminal_input as terminal


ENTRY = {'id': '%8', 'agent': 'codex', 'owner_pid': 123, 'process_start': '42',
         'thread': 'thread-a', 'writable': True}
META = ('%8', '40', '10', '1', '0', '0', '0', '0')


def frame(title='Usage · Plus', tabs='Overview  Usage  Messages', body=None,
          footer=('pgup/pgdn scroll', 'tab report · z all')):
    rows = ['\x1b[1m ' + title + '\x1b[0m', '\x1b[1;4m ' + tabs + '\x1b[0m', ' ' + '─' * 38]
    rows += body if body is not None else ['  Token activity', '  Su ██  ░░', '  中文  😀  │  42  ', '', '']
    rows += ['\x1b[2m ' + row + '\x1b[0m' for row in footer]
    return '\n'.join(rows) + '\n'


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.ledger = actions.Ledger(clock=lambda: 100)

    def test_analytics_only_exposes_proven_actions_and_keeps_rows(self):
        found = self.ledger.observe(ENTRY, frame(), META)
        self.assertEqual(found['reason'], 'info')
        self.assertFalse(found['requiresResponse'])
        self.assertEqual(found['hints'], ['pgup/pgdn scroll', 'tab report', 'z all'])
        self.assertEqual([(a['label'], a['key'], a['enabled']) for a in found['actions']],
                         [('Scroll up', 'PgUp', True), ('Scroll down', 'PgDn', True),
                          ('Next report', 'Tab', True), ('All reports', 'z', True)])
        self.assertIn('  中文  😀  │  42  \n', found['body'])
        self.assertNotIn('pgup', found['body'])
        self.assertNotIn('_key', json.dumps(found))

    def test_second_title_and_unknown_effect_use_same_adapter(self):
        found = self.ledger.observe(ENTRY, frame(title='Build measurements', tabs='Latency  Memory',
                                                footer=('Home/End scroll report', 'tab next report · z mystery · esc back')), META)
        self.assertEqual([a['key'] for a in found['actions']], ['Home', 'End', 'Tab', 'z', 'Esc'])
        self.assertFalse(found['actions'][3]['enabled'])
        self.assertIn('disabledReason', found['actions'][3])
        self.assertTrue(found['actions'][-1]['enabled'])

    def test_faint_rgb_and_osc_preserve_text(self):
        source = frame(body=['\x1b[2;38;2;12;34;56m  中文  😀 ███\x1b[0m',
                             '  \x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\  │', '', '', ''])
        found = self.ledger.observe(ENTRY, source, META)
        self.assertIn('  中文  😀 ███\n  link  │\n', found['body'])
        self.assertNotIn('\x1b', found['body'])

    def test_filter_cursor_copy_mode_modal_disable(self):
        for meta, body in [(META[:4] + ('1',) + META[5:], None),
                           (META[:5] + ('1',) + META[6:], None),
                           (META, ['Filter: name', '', '', '', '']),
                           (META, ['╭─ Confirm ─╮', '', '', '', '']),
                           (META, ['› typed pgup/pgdn scroll', '', '', '', ''])]:
            with self.subTest(meta=meta, body=body):
                found = self.ledger.observe(ENTRY, frame(body=body), meta)
                self.assertTrue(found['actions'])
                self.assertTrue(all(not a['enabled'] and a['disabledReason'] for a in found['actions']))

    def test_fake_history_code_reply_and_composer_do_not_authorize(self):
        for source in ['reply\npgup/pgdn scroll\ntab report · z all\n› ',
                       frame() + '› typed text\n', '```\n' + frame() + '```\n',
                       'User: how do I use this?\n' + frame(),
                       frame().replace('\x1b[2m', '')]:
            self.assertIsNone(self.ledger.observe(ENTRY, source, META))
        self.assertIsNone(self.ledger.observe(ENTRY, frame(), META[:3] + ('0',) + META[4:]))
        self.assertIsNone(self.ledger.observe(dict(ENTRY, agent='claude'), frame(), META))

    def test_widths_wrapping_clipping_and_short_height(self):
        for width in (13, 23, 40, 80, 120):
            meta = (META[0], str(width), *META[2:])
            # A native renderer omits pairs that do not fit. Complete pairs
            # remain usable even when only one of the two footer rows fits.
            footer = ('tab report', 'z all') if width == 13 else ('pgup/pgdn scroll', 'tab report · z all')
            found = self.ledger.observe(ENTRY, frame(footer=footer), meta)
            self.assertTrue(all(a['enabled'] for a in found['actions']))
        wrapped = frame(footer=('pgup/pgdn', 'scroll', 'tab report ·', 'z all'))
        found = self.ledger.observe(ENTRY, wrapped, META)
        self.assertEqual(found['hints'], ['pgup/pgdn scroll', 'tab report', 'z all'])
        clipped = self.ledger.observe(ENTRY, frame(footer=('pgup/pgdn scr…', 'tab rep… · z al…')), META)
        self.assertTrue(all(not a['enabled'] for a in clipped['actions']))
        self.assertIsNone(self.ledger.observe(ENTRY, '\n'.join(frame().splitlines()[-6:]), META))

    def test_remaps_chords_and_localized_labels_fail_closed(self):
        found = self.ledger.observe(ENTRY, frame(footer=('Home/End scroll · C-u scroll · k/j scroll',
                                                       'tab/1–7 report · z tous · esc back')), META)
        self.assertEqual([a['key'] for a in found['actions']], ['Home', 'End', 'Tab', 'z', 'Esc'])
        self.assertFalse(found['actions'][3]['enabled'])
        self.assertNotIn('PgUp', [a['key'] for a in found['actions']])
        remapped = self.ledger.observe(ENTRY, frame(footer=('Down/Up scroll', 'Z all · tab report')), META)
        self.assertEqual([(a['label'], a['key']) for a in remapped['actions']],
                         [('Scroll up', '↓'), ('Scroll down', '↑'), ('Next report', 'Tab')])

    def test_rgb_data_never_supplies_dim_or_active_style(self):
        self.assertIsNone(self.ledger.observe(ENTRY, frame().replace('\x1b[2m', '\x1b[38;2;2;1;4m'), META))
        self.assertIsNone(self.ledger.observe(ENTRY, frame().replace('\x1b[1;4m', '\x1b[38;2;1;4;48m'), META))

    def test_dim_body_hints_stay_in_body_and_styles_cross_rows(self):
        source = frame(body=['', '', '\x1b[2mz all\x1b[0m', '\x1b[2mesc back\x1b[0m', '\x1b[2mtab report\x1b[0m'])
        found = self.ledger.observe(ENTRY, source, META)
        self.assertNotIn('Esc', [a['key'] for a in found['actions']])
        self.assertIn('esc back', found['body'])
        inherited = frame().replace('\x1b[0m\n\x1b[2m', '\n')
        self.assertEqual(self.ledger.observe(ENTRY, inherited, META)['hints'],
                         ['pgup/pgdn scroll', 'tab report', 'z all'])

    def test_revision_tracks_spaces_styles_focus_actions_and_observed_reopen(self):
        first = self.ledger.observe(ENTRY, frame(), META)
        for source, meta in [(frame().replace('  Token', ' Token'), META),
                             (frame().replace('42', '43'), META),
                             (frame(), META[:4] + ('1',) + META[5:]),
                             (frame(footer=('pgup/pgdn scroll', 'tab report')), META)]:
            found = self.ledger.observe(ENTRY, source, meta)
            self.assertNotEqual(found['revision'], first['revision'])
            self.assertEqual(found['promptId'], first['promptId'])
        self.ledger.observe(ENTRY, '› ', META)
        self.assertNotEqual(self.ledger.observe(ENTRY, frame(), META)['promptId'], first['promptId'])

    def test_usage_footer_does_not_become_option_detail(self):
        for footer in ('enter open · esc back', 'enter inspect · esc return', 'enter open\n· esc back'):
            menu = terminal.menu_question(('Usage\n› 1. Plan usage\n  2. View analytics\n'
                                           '     Read charts and tables\n' + footer).splitlines())
            self.assertEqual(menu['options'][-1]['detail'], 'Read charts and tables')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.ledger = actions.Ledger(clock=lambda: self.now)
        self.source, self.meta, self.entry = frame(), META, dict(ENTRY)
        self.sent = []
        self.found = self.ledger.observe(self.entry, self.source, self.meta)
        self.verify = self.enterContext(patch.object(terminal, 'verify'))

    def run_tmux(self, argv, **kwargs):
        if 'capture-pane' in argv:
            self.assertIn('-e', argv)
            return subprocess.CompletedProcess(argv, 0, self.source)
        if 'display-message' in argv:
            return subprocess.CompletedProcess(argv, 0, '\t'.join(self.meta))
        if 'send-keys' in argv:
            self.sent.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, '')
        raise AssertionError(argv)

    def body(self, request='req-1', key='PgDn'):
        return {'session': '%8', 'thread': 'thread-a', 'promptId': self.found['promptId'],
                'expectedRevision': self.found['revision'],
                'actionId': next(a['id'] for a in self.found['actions'] if a['key'] == key), 'requestId': request}

    def perform(self, body=None):
        return self.ledger.perform(body or self.body(), lambda _: self.entry, run=self.run_tmux)

    def test_one_mapped_key_refreshes_handle_and_deduplicates_lost_response(self):
        body = self.body()
        first = self.perform(body)
        self.assertEqual(first[0], 200)
        self.assertEqual(self.sent, ['NPage'])
        self.assertNotEqual(first[1]['attention']['promptId'], self.found['promptId'])
        self.assertEqual(self.perform(body), first)
        self.assertEqual(self.sent, ['NPage'])
        self.assertEqual(self.perform(self.body(request='req-2'))[0], 409)
        self.assertEqual(self.sent, ['NPage'])

    def test_every_supported_hint_uses_one_canonical_key(self):
        self.source = frame(footer=('pgup/pgdn scroll · up/down scroll · Home/End scroll', 'tab report · z all · esc back'))
        for display, canonical in [('PgUp', 'PPage'), ('PgDn', 'NPage'), ('↑', 'Up'), ('↓', 'Down'),
                                   ('Home', 'Home'), ('End', 'End'), ('Tab', 'Tab'), ('z', 'z'), ('Esc', 'Escape')]:
            self.found = self.ledger.observe(self.entry, self.source, self.meta)
            body = self.body(request=canonical, key=display)
            body['key'] = 'C-c'  # Browser-supplied keys have no authority.
            before = len(self.sent)
            self.assertEqual(self.perform(body)[0], 200)
            self.assertEqual(self.sent[before:], [canonical])

    def test_two_clients_same_handle_only_one_delivery(self):
        barrier = threading.Barrier(2, timeout=5)
        results = []
        def client(request):
            barrier.wait()
            with server.SEND_LOCK:
                results.append(self.perform(self.body(request)))
        threads = [threading.Thread(target=client, args=(name,)) for name in ('client-1', 'client-2')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        self.assertEqual(self.sent, ['NPage'])

    def test_stale_body_hints_process_thread_geometry_focus_and_modal_send_nothing(self):
        for change in ('body', 'hint', 'process', 'thread', 'geometry', 'focus', 'modal', 'approval', 'menu', 'copy', 'gone', 'expired'):
            with self.subTest(change=change):
                self.source, self.meta, self.entry = frame(), META, dict(ENTRY)
                self.now = 100
                self.ledger.forget('%8')
                self.found = self.ledger.observe(self.entry, self.source, self.meta)
                if change == 'body': self.source = self.source.replace('42', '43')
                elif change == 'hint': self.source = frame(footer=('pgup/pgdn scroll', 'tab report'))
                elif change == 'process': self.entry['process_start'] = '43'
                elif change == 'thread': self.entry['thread'] = 'thread-b'
                elif change == 'geometry': self.meta = (META[0], '80', *META[2:])
                elif change == 'focus': self.meta = META[:4] + ('1',) + META[5:]
                elif change == 'modal': self.source = frame(body=['╭─ Confirm ─╮', '', '', '', ''])
                elif change == 'approval': self.source = 'Do you want to proceed?\n› 1. Yes\n  2. No\n'
                elif change == 'menu': self.source = 'Usage\n› 1. Plan\n  2. Analytics\nenter open · esc back\n'
                elif change == 'copy': self.meta = META[:5] + ('1',) + META[6:]
                elif change == 'gone': self.source = '› '
                elif change == 'expired': self.now = 131
                result = self.perform(self.body(request=change))
                self.assertEqual(result[0], 409)
                self.assertIn('attention', result[1])
        self.assertEqual(self.sent, [])

    def test_filter_z_is_disabled_and_never_sent(self):
        self.source = frame(body=['Filter: hello', '', '', '', ''])
        self.found = self.ledger.observe(self.entry, self.source, META)
        self.assertEqual(self.perform(self.body(key='z'))[0], 409)
        self.assertEqual(self.sent, [])

    def test_owner_copy_mode_or_conversation_verify_failure(self):
        self.verify.side_effect = RuntimeError('The agent restarted. Reload its tab before sending.')
        self.assertEqual(self.perform()[0], 409)
        self.assertEqual(self.sent, [])

    def test_focus_or_expiry_change_during_final_owner_check_refuses(self):
        for change in ('focus', 'expiry'):
            self.now, self.meta = 100, META
            self.ledger.forget('%8')
            self.found = self.ledger.observe(self.entry, self.source, self.meta)
            def verify(*_):
                if self.verify.call_count % 2 == 0:
                    if change == 'focus': self.meta = META[:4] + ('1',) + META[5:]
                    else: self.now = 131
            self.verify.reset_mock()
            self.verify.side_effect = verify
            self.assertEqual(self.perform(self.body(request=change))[0], 409)
        self.assertEqual(self.sent, [])

    def test_unknown_action_pane_revision_and_request_reuse(self):
        for field in ('actionId', 'expectedRevision', 'promptId'):
            body = self.body(request=field)
            body[field] = 'wrong'
            self.assertEqual(self.perform(body)[0], 409)
        self.entry['id'] = '%9'
        self.meta = ('%9', *META[1:])
        self.assertEqual(self.perform(self.body(request='pane'))[0], 409)
        self.assertEqual(self.sent, [])

    def test_uncertain_send_is_consumed_and_not_retried(self):
        original = self.run_tmux
        def run(argv, **kwargs):
            if 'send-keys' in argv:
                self.sent.append(argv[-1])
                raise subprocess.TimeoutExpired(argv, 5)
            return original(argv, **kwargs)
        body = self.body()
        first = self.ledger.perform(body, lambda _: self.entry, run=run)
        self.assertEqual(first[0], 409)
        self.assertEqual(self.ledger.perform(body, lambda _: self.entry, run=run), first)
        self.assertEqual(self.sent, ['NPage'])

    def test_geometry_change_during_capture_refuses(self):
        original = self.run_tmux
        def run(argv, **kwargs):
            result = original(argv, **kwargs)
            if 'capture-pane' in argv:
                self.meta = (META[0], '80', *META[2:])
            return result
        self.assertEqual(self.ledger.perform(self.body(), lambda _: self.entry, run=run)[0], 409)
        self.assertEqual(self.sent, [])

    def test_missing_session_and_read_only_session_refuse(self):
        with self.subTest('missing'):
            def choose(_):
                raise ValueError('That session has ended. Select another tab.')
            self.assertEqual(self.ledger.perform(self.body(), choose, run=self.run_tmux)[0], 409)
        self.entry['writable'] = False
        self.assertEqual(self.perform(self.body('read-only'))[0], 409)
        self.assertEqual(self.sent, [])

    def test_request_id_cannot_be_reused_with_another_action(self):
        self.assertEqual(self.perform()[0], 200)
        self.assertEqual(self.perform(self.body(key='Tab'))[0], 409)
        self.assertEqual(self.sent, ['NPage'])

    def test_route_delivers_once_and_returns_fresh_conflict(self):
        perform = self.ledger.perform
        with patch.object(actions, 'ledger', self.ledger), patch.object(server, 'choose', return_value=self.entry), \
                patch.object(self.ledger, 'perform', side_effect=lambda body, choose: perform(body, choose, run=self.run_tmux)):
            def request(body):
                h = object.__new__(server.Handler)
                h.path = '/mobile/api/prompt-action'
                raw = json.dumps(body).encode()
                h.headers = {'Host': 'phone.test', 'Origin': 'https://phone.test', 'X-Codex-Mobile': '1', 'Content-Length': str(len(raw))}
                h.rfile = io.BytesIO(raw)
                h.respond, h.deny = Mock(), Mock(return_value=False)
                h.do_POST()
                return h.respond.call_args.args
            first = request(self.body())
            self.assertEqual(first[0], 200)
            self.assertEqual(request(self.body()), first)
            conflict = request(self.body(request='another'))
            self.assertEqual(conflict[0], 409)
            self.assertIsNotNone(conflict[1]['attention'])
        self.assertEqual(self.sent, ['NPage'])

    def test_malformed_and_bounded_memory(self):
        for value in (None, '', 3, [], 'a' * 257):
            for field in self.body():
                body = self.body()
                body[field] = value
                with self.assertRaises(ValueError):
                    self.perform(body)
        for pane in range(actions.LIMIT + 5):
            self.ledger.observe(dict(ENTRY, id='%' + str(pane)), frame(), META)
        self.assertEqual(len(self.ledger.surfaces), actions.LIMIT)


class IntegrationTests(unittest.TestCase):
    def test_route_auth_contract_and_locked_delivery(self):
        body = {'session': '%8', 'thread': 'thread-a', 'promptId': 'p', 'expectedRevision': 'r', 'actionId': 'a', 'requestId': 'q'}
        def handler():
            h = object.__new__(server.Handler)
            h.path = '/mobile/api/prompt-action'
            raw = json.dumps(body).encode()
            h.headers = {'Host': 'phone.test', 'Origin': 'https://phone.test', 'X-Codex-Mobile': '1', 'Content-Length': str(len(raw))}
            h.rfile = io.BytesIO(raw)
            h.respond, h.deny = Mock(), Mock(return_value=False)
            return h
        def perform(value, choose):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertEqual(value, body)
            self.assertIs(choose, server.choose)
            return 200, {'ok': True, 'attention': None}
        with patch.object(actions.ledger, 'perform', side_effect=perform) as send:
            h = handler()
            h.do_POST()
            h.respond.assert_called_once_with(200, {'ok': True, 'attention': None})
            for case in ('cookie', 'origin', 'header'):
                h = handler()
                if case == 'cookie': h.deny.return_value = True
                elif case == 'origin': h.headers['Origin'] = 'https://evil.test'
                else: del h.headers['X-Codex-Mobile']
                h.do_POST()
            send.assert_called_once()
        h = handler()
        with patch.object(actions.ledger, 'perform', side_effect=ValueError('Invalid action.')):
            h.do_POST()
        self.assertEqual(h.respond.call_args.args[0], 400)

    def test_info_state_needs_and_push_are_read_only(self):
        ledger = actions.Ledger()
        meta_run = Mock(return_value=subprocess.CompletedProcess([], 0, '\t'.join(META)))
        with patch.object(actions, 'ledger', ledger), patch.object(terminal, 'transcript_busy', return_value=False):
            status = terminal._live_status(ENTRY, meta_run, frame())
        self.assertEqual(status['state'], 'Idle')
        self.assertFalse(status['attention']['requiresResponse'])
        self.assertIsNone(needs.summary(status))
        send = Mock()
        notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100)
        notifier.observe(ENTRY, status)
        send.assert_not_called()
        with patch.object(actions, 'ledger', ledger), patch.object(terminal, 'transcript_busy', return_value=True):
            status = terminal._live_status(ENTRY, meta_run, frame())
        self.assertEqual(status['state'], 'Working')
        self.assertIsNone(needs.summary(status))
        with patch.dict(terminal._attention_seen, clear=True):
            entry = dict(ENTRY, agent='claude')
            terminal.attention(entry, 'Settings Status Config\nUsage\nEsc to cancel', False, now=0)
            found = terminal.attention(entry, 'Settings Status Config\nUsage\nEsc to cancel', False, now=4)
        self.assertFalse(found['requiresResponse'])

    @unittest.skipUnless(shutil.which('tmux'), 'tmux is unavailable')
    def test_private_socket_page_keys_are_terminal_sequences(self):
        # The pane process has a 10-second timeout. The only tmux server touched
        # is this private socket, which is always stopped in finally.
        with tempfile.TemporaryDirectory(prefix='p1-') as folder:
            socket, output = str(Path(folder) / 's'), Path(folder) / 'keys'
            script = Path(folder) / 'reader.py'
            painted = '\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H' + frame().rstrip('\n').replace('\n', '\r\n')
            script.write_text('import os,tty\nfrom pathlib import Path\ntty.setraw(0)\n'
                              + 'os.write(1, ' + repr(painted.encode()) + ')\n'
                              + 'Path("ready").touch()\ndata=b""\nwhile len(data)<8:\n data+=os.read(0,8-len(data))\nPath("keys").write_bytes(data)\n')
            def tmux(*args):
                return subprocess.run(['tmux', '-S', socket, '-f', '/dev/null', *args], check=True, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
            try:
                pane = tmux('new-session', '-d', '-x', '40', '-y', '10', '-c', folder, '-P', '-F', '#{pane_id}', 'timeout 10s python3 reader.py').stdout.strip()
                deadline = time.monotonic() + 5
                while not (Path(folder) / 'ready').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((Path(folder) / 'ready').exists())
                capture = tmux('capture-pane', '-p', '-e', '-t', pane).stdout
                meta = tuple(tmux('display-message', '-p', '-t', pane, actions.META).stdout.strip().split('\t'))
                found = actions.Ledger().observe(dict(ENTRY, id=pane), capture, meta)
                self.assertIsNotNone(found)
                self.assertEqual([a['key'] for a in found['actions']], ['PgUp', 'PgDn', 'Tab', 'z'])
                self.assertTrue(all(a['enabled'] for a in found['actions']))
                for key in ('PPage', 'NPage'):
                    tmux('send-keys', '-t', pane, key)
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(output.read_bytes(), b'\x1b[5~\x1b[6~')
            finally:
                subprocess.run(['tmux', '-S', socket, 'kill-server'], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)


if __name__ == '__main__':
    unittest.main()
