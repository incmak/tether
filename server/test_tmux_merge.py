"""tmux_merge against a fake runner or a real server on a private socket."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pathlib import Path

import paths
import projects
import side_shells
import tmux_merge


class FakeTmux:
    def __init__(self):
        self.sessions = {'$1': {'@1': ['%1'], '@2': ['%2']}, '$2': {'@3': ['%3']}}
        self.names = {'$1': 'alpha', '$2': 'beta'}
        self.pane_names = {'%1': 'Review'}
        self.markers = {}
        self.projects = {}
        self.calls = []
        self.faults = {}

    def fail(self, command, *, applied=False, nonzero=False, times=1, skip=0):
        self.faults[command] = [None] * skip + [(applied, nonzero)] * times

    def run(self, argv, *, capture_output, text, timeout):
        executable, command, *args = argv
        assert executable == paths.TMUX and capture_output and text and timeout == 5, argv
        self.calls.append(argv)
        faults = self.faults.get(command)
        fault = faults.pop(0) if faults else None
        if fault:
            applied, nonzero = fault
            if applied:
                self.execute(command, args)
            if nonzero:
                return subprocess.CompletedProcess(argv, 1, '', 'tmux reported an error')
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, self.execute(command, args), '')

    def panes(self, session):
        return [pane for panes in self.sessions[session].values() for pane in panes]

    def where(self, target):
        for session, windows in self.sessions.items():
            for window, panes in windows.items():
                if target == window or target in panes:
                    return session, window
        return '', ''

    def execute(self, command, args):
        if command == 'list-sessions':
            return ''.join(self.format(args[-1], session) + '\n' for session in self.sessions)
        if command == 'new-session':
            if ';' in args:
                split = args.index(';')
                args, option = args[:split], args[split + 1:]
                assert option[:3] == ['set-option', '-p', '@tether_breakout'], option
                self.markers['%4'] = option[3]
            self.names['$3'] = args[args.index('-s') + 1]
            self.sessions['$3'] = {'@4': ['%4']}
            return self.format(args[-1], '$3', '@4', '%4') + '\n'
        if command == 'list-panes' and '-a' in args:
            return ''.join(self.format(args[-1], session, pane=pane) + '\n'
                           for session in self.sessions for pane in self.panes(session))

        target = args[args.index('-t') + 1]
        session = target.removesuffix(':')
        if session.startswith('='):
            session = next((sid for sid, name in self.names.items() if name == session[1:]), '')
        if command == 'display-message':
            if target.endswith(':'):
                window = next(iter(self.sessions.get(session, {})), '')
            else:
                session, window = self.where(target)
            panes = self.sessions[session][window] if window else []
            pane = target if target in panes else next(iter(panes), '')
            return self.format(args[-1], session, window, pane) + '\n'
        if command == 'list-panes':
            return ''.join(self.format(args[-1], session, pane=pane) + '\n' for pane in self.panes(session))
        if command == 'list-windows':
            return ''.join(window + '\n' for window in self.sessions[session])
        if command == 'kill-pane':
            self.remove_pane(target)
        elif command == 'break-pane':
            pane = args[args.index('-s') + 1]
            self.remove_pane(pane)
            self.sessions[session]['@5'] = [pane]
        elif command == 'move-window':
            window = args[args.index('-s') + 1]
            source, _ = self.where(window)
            self.sessions[session][window] = self.sessions[source].pop(window)
            self.drop_empty_session(source)
        elif command == 'set-option':
            if args[-2] == '@chat_name':
                self.pane_names[target] = args[-1]
            if args[-2] == '@project':
                self.projects[target] = args[-1]
        else:
            raise AssertionError((command, args))
        return ''

    def format(self, fmt, session, window='', pane=''):
        fields = {'session_id': session, 'session_name': self.names.get(session, ''), 'window_id': window,
                  'pane_id': pane, 'pane_current_path': '/tmp', '@chat_name': self.pane_names.get(pane, ''),
                  '@project': self.projects.get(session, ''), '@tether_breakout': self.markers.get(pane, '')}
        return '\t'.join(fields[field[2:-1]] for field in fmt.split('\t'))

    def remove_pane(self, pane):
        session, window = self.where(pane)
        self.sessions[session][window].remove(pane)
        self.markers.pop(pane, None)
        if not self.sessions[session][window]:
            del self.sessions[session][window]
        self.drop_empty_session(session)

    def drop_empty_session(self, session):
        if not self.sessions[session]:
            del self.sessions[session]
            del self.names[session]


class TmuxMergeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTmux()
        self.run_tmux = self.fake.run
        scope = mock.patch.object(paths, 'SCOPE_TMUX', False)
        scope.start()
        self.addCleanup(scope.stop)
        self.addCleanup(self.assert_exact_kills)

    def calls(self, command):
        return [argv for argv in self.fake.calls if argv[1] == command]

    def assert_exact_kills(self):
        self.assertEqual(self.calls('kill-server'), [])
        self.assertEqual(self.calls('kill-session'), [])
        for _, _, flag, target in self.calls('kill-pane'):
            self.assertEqual(flag, '-t')
            self.assertEqual(target, '%4')

    def break_out(self):
        return tmux_merge.break_out('%1', self.run_tmux)

    def assert_broke_out(self, result):
        self.assertEqual(result, {'session': '$3', 'name': 'Review', 'pane': '%1'})
        self.assertEqual(self.fake.panes('$3'), ['%1'])
        self.assertEqual(self.fake.panes('$1'), ['%2'])
        self.assertEqual(set(self.fake.sessions), {'$1', '$2', '$3'})

    def test_break_pane_timeout_after_move_finishes_breakout(self):
        self.fake.fail('break-pane', applied=True)
        self.assert_broke_out(self.break_out())

    def test_break_pane_timeout_before_move_cleans_up_session_by_id(self):
        self.fake.fail('break-pane')
        with self.assertRaisesRegex(RuntimeError, r'^Could not move the pane\.$'):
            self.break_out()
        # By the placeholder's pane id: the session closes with its last pane, and a late break-pane would survive.
        self.assertEqual(self.calls('kill-session'), [])
        self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']])
        self.assertEqual(set(self.fake.sessions), {'$1', '$2'})
        self.assertEqual(self.fake.panes('$1'), ['%1', '%2'])

    def test_break_pane_timeout_with_failed_kill_warns_when_session_is_gone(self):
        self.fake.fail('break-pane')

        def run(argv, **kwargs):
            if argv[1] == 'kill-pane':
                # The placeholder moved to another session; neither kill reaches it.
                self.fake.calls.append(argv)
                if '@4' in self.fake.sessions.get('$3', {}):
                    self.fake.sessions['$1']['@9'] = self.fake.sessions['$3'].pop('@4')
                    del self.fake.sessions['$3']
                return subprocess.CompletedProcess(argv, 1, '', 'tmux reported an error')
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        with self.assertRaisesRegex(RuntimeError, r'^Could not move the pane\. A session “Review” may be left open\.$'):
            self.break_out()
        self.assertNotIn('$3', self.fake.sessions)
        self.assertIn('%4', self.fake.panes('$1'))

    def test_break_pane_nonzero_after_move_finishes_breakout(self):
        self.fake.fail('break-pane', applied=True, nonzero=True)
        self.assert_broke_out(self.break_out())

    def test_placeholder_kill_timeout_after_removal_needs_no_retry(self):
        self.fake.fail('kill-pane', applied=True)
        self.assert_broke_out(self.break_out())
        self.assertEqual(len(self.calls('kill-pane')), 1)

    def test_placeholder_kill_times_out_twice_and_warns(self):
        self.fake.fail('kill-pane', times=2)
        result = self.break_out()
        self.assertEqual(result['session'], '$3')
        self.assertIn('extra shell tab', result['warning'])
        self.assertEqual(self.fake.panes('$3'), ['%4', '%1'])
        self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']] * 2)

    def test_placeholder_kill_does_not_retry_if_the_marker_changes(self):
        self.fake.fail('kill-pane')

        def run(argv, **kwargs):
            try:
                return self.fake.run(argv, **kwargs)
            except subprocess.TimeoutExpired:
                if argv[1] == 'kill-pane':
                    self.fake.markers['%4'] = 'new-marker'
                raise

        self.run_tmux = run
        result = self.break_out()
        self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']])
        self.assertEqual(result['warning'], 'A session “Review” may be left open.')
        self.assertEqual(self.fake.panes('$3'), ['%4', '%1'])

    def test_breakout_reports_moved_and_warns_when_ungroup_times_out(self):
        self.fake.fail('kill-pane', times=2)
        self.fake.fail('set-option')
        with self.assertRaisesRegex(RuntimeError, r'^The pane moved to “Review”, but Tether could not finish\. '
                                    r'Refresh the list\. .*extra shell tab'):
            self.break_out()
        self.assertEqual(self.fake.panes('$3'), ['%4', '%1'])

    def test_breakout_reports_moved_and_warns_when_ungroup_fails(self):
        self.fake.fail('kill-pane', times=2)
        self.fake.fail('set-option', nonzero=True)
        with self.assertRaisesRegex(RuntimeError, r'The pane moved to “Review”, but Tether could not finish\. '
                                    r'Refresh the list\. .*extra shell tab'):
            self.break_out()
        self.assertEqual(self.fake.panes('$3'), ['%4', '%1'])

    def test_breakout_sets_its_pane_marker_in_the_creation_command(self):
        self.assert_broke_out(self.break_out())
        new_session, = self.calls('new-session')
        self.assertEqual(new_session[-5:-1], [';', 'set-option', '-p', '@tether_breakout'])
        self.assertRegex(new_session[-1], r'^[0-9a-f]{16}$')
        self.assertNotIn('-e', new_session)
        self.assertEqual(self.calls('set-environment'), [])

    def test_new_session_timeout_without_output_leaves_marked_pane_open(self):
        self.fake.fail('new-session', applied=True)
        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. A session “Review” may be left open\.$'):
            self.break_out()
        self.assertEqual(self.calls('kill-pane'), [])
        self.assertEqual(self.fake.panes('$3'), ['%4'])

    def test_unterminated_creation_record_is_ignored(self):
        for status in (None, 1, 0):
            with self.subTest(status=status):
                self.fake = FakeTmux()

                def run(argv, **kwargs):
                    result = self.fake.run(argv, **kwargs)
                    if argv[1] == 'new-session':
                        self.fake.sessions['$3']['@4'] = ['%10']
                        self.fake.markers['%10'] = self.fake.markers.pop('%4')
                        if status is None:
                            raise subprocess.TimeoutExpired(argv, 5, output=b'$3\t%1')
                        return subprocess.CompletedProcess(argv, status, '$3\t%1', '')
                    return result

                self.run_tmux = run
                message = r'^Could not start a session\.'
                message += r' A session “Review” may be left open\.$'
                with self.assertRaisesRegex(RuntimeError, message):
                    self.break_out()
                self.assertEqual(self.calls('kill-pane'), [])
                self.assertEqual(self.calls('break-pane'), [])
                self.assertEqual(self.fake.panes('$1'), ['%1', '%2'])
                self.assertEqual(self.fake.panes('$3'), ['%10'])

    def test_failed_placeholder_kill_warns_when_session_is_gone(self):
        def run(argv, **kwargs):
            if argv[1] == 'kill-pane':
                # The placeholder moved to another session; the kill does not reach it.
                self.fake.calls.append(argv)
                self.fake.sessions['$1']['@9'] = self.fake.sessions['$3'].pop('@4')
                del self.fake.sessions['$3']
                return subprocess.CompletedProcess(argv, 1, '', 'tmux reported an error')
            result = self.fake.run(argv, **kwargs)
            if argv[1] == 'new-session':
                return subprocess.CompletedProcess(argv, 1, '$3\t%4\n', 'hook failed')
            return result

        self.run_tmux = run
        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. A session “Review” may be left open\.$'):
            self.break_out()
        self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']])
        self.assertIn('%4', self.fake.panes('$1'))

    def test_new_session_timeout_warns_if_ownership_read_fails(self):
        for nonzero in (False, True):
            with self.subTest(nonzero=nonzero):
                self.fake = FakeTmux()

                def run(argv, **kwargs):
                    result = self.fake.run(argv, **kwargs)
                    if argv[1] == 'new-session':
                        raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
                    return result

                self.run_tmux = run
                self.fake.fail('display-message', nonzero=nonzero, skip=3)
                with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. A session .* may be left open\.$'):
                    self.break_out()
                self.assertEqual(self.calls('kill-pane'), [])
                self.assertEqual(self.fake.panes('$3'), ['%4'])

    def test_new_session_timeout_keeps_a_window_moved_into_the_session(self):
        def run(argv, **kwargs):
            if argv[1] == 'new-session':
                result = self.fake.run(argv, **kwargs)
                self.fake.execute('move-window', ['-s', '@3', '-t', '$3:'])
                raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. '
                                    r'The new session “Review” stays open\. Close it from the sidebar\.$'):
            self.break_out()
        self.assertEqual(self.fake.panes('$3'), ['%3'])

    def test_new_session_timeout_cleans_up_a_renamed_session(self):
        def run(argv, **kwargs):
            if argv[1] == 'new-session':
                result = self.fake.run(argv, **kwargs)
                self.fake.names['$3'] = 'renamed'
                raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\.$'):
            self.break_out()
        self.assertEqual(set(self.fake.sessions), {'$1', '$2'})

    def test_new_session_failure_with_a_record_cleans_up_the_marked_pane(self):
        for output, nonzero in ((b'$3\t%4\n', False), ('$3\t%4\n', False), ('$3\t%4\n', True),
                                ('$3\t%4\nextra output\n', True), ('$3\t%4\n$2\t%3', True)):
            with self.subTest(output=output, nonzero=nonzero):
                self.fake = FakeTmux()

                def run(argv, **kwargs):
                    if argv[1] == 'new-session':
                        self.fake.run(argv, **kwargs)
                        if nonzero:
                            return subprocess.CompletedProcess(argv, 1, output, 'set-option failed')
                        raise subprocess.TimeoutExpired(argv, 5, output=output)
                    return self.fake.run(argv, **kwargs)

                self.run_tmux = run
                with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\.$'):
                    self.break_out()
                self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']])
                self.assertEqual(set(self.fake.sessions), {'$1', '$2'})

    def test_new_session_failure_warns_when_stdout_has_conflicting_ids(self):
        def run(argv, **kwargs):
            if argv[1] == 'new-session':
                self.fake.run(argv[:argv.index(';')], **kwargs)
                return subprocess.CompletedProcess(argv, 1, '$3\t%4\n$2\t%3\n', 'set-option failed')
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        with self.assertRaisesRegex(RuntimeError, 'Review.*may be left open'):
            self.break_out()
        self.assertEqual(self.calls('kill-pane'), [])
        self.assertEqual(self.fake.panes('$3'), ['%4'])
        self.assertEqual(self.fake.panes('$2'), ['%3'])

    def test_new_session_cleanup_warns_if_the_session_stays_open(self):
        def run(argv, **kwargs):
            result = self.fake.run(argv, **kwargs)
            if argv[1] == 'new-session':
                raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
            return result

        self.run_tmux = run
        self.fake.fail('kill-pane')
        with self.assertRaisesRegex(RuntimeError, 'The new session “Review” stays open. Close it from the sidebar.'):
            self.break_out()
        self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']])
        self.assertEqual(self.fake.panes('$3'), ['%4'])

    def test_new_session_cleanup_warns_if_session_read_times_out(self):
        def run(argv, **kwargs):
            result = self.fake.run(argv, **kwargs)
            if argv[1] == 'new-session':
                raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
            return result

        self.run_tmux = run
        self.fake.fail('list-sessions', skip=1)
        with self.assertRaisesRegex(RuntimeError, 'A session “Review” may be left open'):
            self.break_out()
        self.assertEqual(self.calls('kill-pane'), [[paths.TMUX, 'kill-pane', '-t', '%4']])
        self.assertNotIn('$3', self.fake.sessions)

    def test_new_session_timeout_before_creation_leaves_other_sessions_alone(self):
        self.fake.names['$2'] = 'Review old'
        self.fake.fail('new-session')
        with self.assertRaisesRegex(RuntimeError, 'Review.*may be left open'):
            self.break_out()
        self.assertEqual(self.calls('kill-session'), [])
        self.assertEqual(self.calls('kill-pane'), [])
        self.assertEqual(set(self.fake.sessions), {'$1', '$2'})

    def test_failed_breakout_keeps_a_session_with_an_added_pane(self):
        self.fake.fail('break-pane', nonzero=True)

        def run(argv, **kwargs):
            if argv[1] == 'break-pane':
                self.fake.sessions['$3']['@6'] = ['%6']
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        with self.assertRaisesRegex(RuntimeError, 'The new session “Review” stays open. Close it from the sidebar.'):
            self.break_out()
        self.assertEqual(self.calls('kill-session'), [])
        self.assertEqual(self.fake.panes('$3'), ['%6'])  # only the placeholder this call made goes

    def test_failed_breakout_keeps_the_session_when_the_pane_read_times_out(self):
        self.fake.fail('break-pane', nonzero=True)
        self.fake.fail('display-message', skip=3)
        with self.assertRaisesRegex(RuntimeError, 'confirm.*Review.*left open'):
            self.break_out()
        self.assertEqual(self.calls('kill-session'), [])

    def test_break_pane_that_lands_late_keeps_the_pane(self):
        self.fake.fail('break-pane')

        def run(argv, **kwargs):
            if argv[1] == 'kill-pane':  # tmux applies the timed-out break-pane only now
                self.fake.execute('break-pane', ['-d', '-s', '%1', '-t', '$3:'])
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        self.assert_broke_out(self.break_out())
        self.assertEqual(self.calls('kill-session'), [])

    def test_failed_cleanup_names_the_session_that_stays_open(self):
        self.fake.fail('break-pane')
        self.fake.fail('kill-pane', times=2)
        with self.assertRaisesRegex(RuntimeError, 'The new session “Review” stays open. Close it from the sidebar.'):
            self.break_out()
        self.assertEqual(self.fake.panes('$3'), ['%4'])

    def test_break_pane_that_lands_during_failed_cleanup_warns_after_the_move(self):
        self.fake.fail('break-pane')
        self.fake.fail('kill-pane', times=2)

        def run(argv, **kwargs):
            if argv[1] == 'kill-pane' and len(self.calls('kill-pane')) == 1:
                self.fake.execute('break-pane', ['-d', '-s', '%1', '-t', '$3:'])
            return self.fake.run(argv, **kwargs)

        self.run_tmux = run
        result = self.break_out()
        self.assertEqual(result['session'], '$3')
        self.assertIn('extra shell tab', result['warning'])
        self.assertEqual(self.fake.panes('$3'), ['%4', '%1'])
        self.assertEqual(len(self.calls('kill-pane')), 2)

    def test_new_session_failure_preserves_a_competing_session(self):
        for nonzero in (False, True):
            with self.subTest(nonzero=nonzero):
                self.fake = FakeTmux()

                def run(argv, **kwargs):
                    if argv[1] == 'new-session':
                        # A competing call creates the name during this call's new-session attempt.
                        self.fake.names['$3'] = 'Review'
                        self.fake.sessions['$3'] = {'@4': ['%4']}
                        if nonzero:
                            return subprocess.CompletedProcess(argv, 1, '', 'duplicate session')
                        raise subprocess.TimeoutExpired(argv, 5)
                    return self.fake.run(argv, **kwargs)

                self.run_tmux = run
                message = r'^Could not start a session\.$' if nonzero else 'Review.*may be left open'
                with self.assertRaisesRegex(RuntimeError, message):
                    self.break_out()
                self.assertEqual(self.calls('kill-pane'), [])
                self.assertEqual(self.fake.panes('$3'), ['%4'])

    def test_move_pane_timeout_after_move_ungroups_and_names_pane(self):
        self.fake.pane_names.clear()
        self.fake.fail('break-pane', applied=True)
        result = tmux_merge.move_pane('%2', '$2', self.run_tmux)
        self.assertEqual(result, {'session': '$2', 'name': 'beta', 'pane': '%2'})
        self.assertEqual(self.fake.panes('$2'), ['%3', '%2'])
        self.assertIn([paths.TMUX, 'set-option', '-p', '-u', '-t', '%2', '@group'], self.calls('set-option'))
        self.assertEqual(self.fake.pane_names['%2'], 'alpha')

    def test_move_pane_timeout_before_move_reports_failure(self):
        self.fake.fail('break-pane')
        with self.assertRaisesRegex(RuntimeError, 'Could not move the pane'):
            tmux_merge.move_pane('%2', '$2', self.run_tmux)
        self.assertEqual(self.fake.panes('$1'), ['%1', '%2'])

    def test_move_pane_reports_moved_when_ungroup_fails(self):
        self.fake.fail('set-option', nonzero=True)
        with self.assertRaisesRegex(RuntimeError, 'The pane moved to “beta”, but Tether could not finish'):
            tmux_merge.move_pane('%2', '$2', self.run_tmux)
        self.assertEqual(self.fake.panes('$2'), ['%3', '%2'])

    def test_move_pane_reports_moved_when_naming_times_out(self):
        self.fake.fail('set-option', skip=1)  # ungroup succeeds, naming does not
        with self.assertRaisesRegex(RuntimeError, r'^The pane moved to “beta”, but Tether could not finish\. '
                                    r'Refresh the list\.$'):
            tmux_merge.move_pane('%2', '$2', self.run_tmux)
        self.assertEqual(self.fake.panes('$2'), ['%3', '%2'])

    def test_merge_reports_uncertain_first_move_when_read_times_out(self):
        self.fake.fail('move-window', applied=True)

        def run(argv, **kwargs):
            if argv[1] == 'display-message' and '#{window_id}\t#{session_id}' in argv:
                raise subprocess.TimeoutExpired(argv, 5)
            return self.fake.run(argv, **kwargs)

        with mock.patch.object(tmux_merge.groups, 'merge_into'):
            with self.assertRaisesRegex(RuntimeError, r'^Could not confirm the move\. Refresh the list\.$'):
                tmux_merge.merge_session('$1', '$2', run)
        self.assertEqual(self.fake.panes('$2'), ['%3', '%1'])
        self.assertEqual(self.fake.panes('$1'), ['%2'])

    def test_merge_reports_moved_when_naming_times_out(self):
        self.fake.fail('set-option')
        with mock.patch.object(tmux_merge.groups, 'merge_into'):
            with self.assertRaisesRegex(RuntimeError, r'^The windows moved to “beta”, but Tether could not finish\. '
                                        r'Refresh the list\.$'):
                tmux_merge.merge_session('$1', '$2', self.run_tmux)
        self.assertEqual(self.fake.panes('$2'), ['%3', '%1', '%2'])
        self.assertEqual(set(self.fake.sessions), {'$2'})

    def test_merge_reports_moved_when_naming_fails(self):
        self.fake.fail('set-option', nonzero=True)
        with mock.patch.object(tmux_merge.groups, 'merge_into'):
            with self.assertRaisesRegex(RuntimeError, 'The windows moved to “beta”, but Tether could not finish'):
                tmux_merge.merge_session('$1', '$2', self.run_tmux)
        self.assertEqual(self.fake.panes('$2'), ['%3', '%1', '%2'])

    def test_merge_does_not_archive_when_the_project_switch_is_rejected(self):
        for nonzero in (False, True):
            with self.subTest(nonzero=nonzero):
                self.fake = FakeTmux()
                self.fake.projects = {'$1': 'a1a1a1a1a1a1', '$2': 'b2b2b2b2b2b2'}
                self.fake.fail('set-option', nonzero=nonzero, skip=1)  # the first write names %2
                with mock.patch.object(tmux_merge.groups, 'merge_into'), mock.patch.object(projects, 'archive') as archive:
                    with self.assertRaisesRegex(RuntimeError, 'The windows moved to “beta”, but Tether could not finish'):
                        tmux_merge.merge_session('$1', '$2', self.fake.run, keep='source')
                    archive.assert_not_called()
                self.assertEqual(self.fake.panes('$2'), ['%3', '%1', '%2'])
                self.assertEqual(self.fake.projects['$2'], 'b2b2b2b2b2b2')

    def test_merge_timeout_before_second_move_reports_partial_result(self):
        self.fake.fail('move-window', skip=1)

        def merge_groups(source, target, run):
            self.assertEqual(self.calls('move-window'), [])

        with mock.patch.object(tmux_merge.groups, 'merge_into', side_effect=merge_groups) as merge:
            with self.assertRaisesRegex(RuntimeError, '(?i)part.*session.*check both sessions'):
                tmux_merge.merge_session('$1', '$2', self.run_tmux)
        merge.assert_called_once_with('$1', '$2', self.run_tmux)
        self.assertEqual(self.fake.panes('$2'), ['%3', '%1'])
        self.assertEqual(self.fake.panes('$1'), ['%2'])

    def test_merge_timeout_after_first_move_finishes_both_moves(self):
        self.fake.fail('move-window', applied=True)
        with mock.patch.object(tmux_merge.groups, 'merge_into'):
            result = tmux_merge.merge_session('$1', '$2', self.run_tmux)
        self.assertEqual(result, {'session': '$2', 'name': 'beta', 'pane': '%3'})
        self.assertEqual(self.fake.panes('$2'), ['%3', '%1', '%2'])
        self.assertEqual(set(self.fake.sessions), {'$2'})


@unittest.skipUnless(shutil.which(paths.TMUX) or os.path.exists(paths.TMUX), 'tmux is not installed')
class TmuxMergeTests(unittest.TestCase):
    def setUp(self):
        self.socket_dir = tempfile.mkdtemp(prefix='tmg', dir='/tmp')  # short: long socket paths fail
        self.env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        self.socket = str(Path(self.socket_dir) / 'tmux.sock')
        self.addCleanup(shutil.rmtree, self.socket_dir, ignore_errors=True)
        self.addCleanup(self.tmux, 'kill-server')
        scope = mock.patch.object(paths, 'SCOPE_TMUX', False)
        scope.start()
        self.addCleanup(scope.stop)
        self.a = self.new('alpha')
        self.tmux('split-window', '-t', '=alpha:')
        self.tmux('new-window', '-t', '=alpha:')
        self.b = self.new('beta')

    def run_tmux(self, argv, **kwargs):
        # Use the private socket and skip user hooks and commands from the user's tmux config.
        return subprocess.run([argv[0], '-S', self.socket, '-f', '/dev/null', *argv[1:]], env=self.env, **kwargs)

    def tmux(self, *args):
        return self.run_tmux([paths.TMUX, *args], capture_output=True, text=True, timeout=5)

    def new(self, name):
        result = self.tmux('new-session', '-d', '-s', name, '-x', '120', '-y', '40')
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.tmux('display-message', '-p', '-t', f'={name}:', '#{session_id}').stdout.strip()

    def panes(self, session):
        return self.tmux('list-panes', '-s', '-t', session + ':', '-F', '#{pane_id}').stdout.split()

    def sessions(self):
        return sorted(self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_merge_session_moves_every_window_and_closes_the_source(self):
        moving = self.panes(self.a)
        result = tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual((result['session'], result['name']), (self.b, 'beta'))
        self.assertEqual(self.sessions(), ['beta'])
        self.assertEqual(set(moving) - set(self.panes(self.b)), set())
        # The split window keeps both panes together.
        layout = self.tmux('list-windows', '-t', self.b + ':', '-F', '#{window_panes}').stdout.split()
        self.assertEqual(sorted(layout), ['1', '1', '2'])

    def with_projects(self):
        home = tempfile.mkdtemp(prefix='tmgp')
        self.addCleanup(shutil.rmtree, home, True)
        patch = mock.patch.object(paths, 'TETHER', Path(home) / 'tether')
        patch.start()
        self.addCleanup(patch.stop)
        ids = {}
        for session, pid, text in ((self.a, 'a1a1a1a1a1a1', 'Alpha rules.'), (self.b, 'b2b2b2b2b2b2', 'Beta rules.')):
            self.tmux('set-option', '-t', session, '@project', pid)
            projects.set_instructions(pid, text, 0)
            ids[session] = pid
        return ids[self.a], ids[self.b]

    def project(self, session):
        return self.tmux('show-options', '-v', '-t', session, '@project').stdout.strip()

    def archived(self):
        folder = projects.root() / '.archive'
        return sorted(item.name[:12] for item in folder.iterdir()) if folder.exists() else []

    def test_merge_asks_which_project_when_both_have_text(self):
        alpha, beta = self.with_projects()
        with self.assertRaises(ValueError) as asked:
            tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual(str(asked.exception), tmux_merge.CHOOSE)
        self.assertEqual(self.sessions(), ['alpha', 'beta'])  # nothing moved
        with self.assertRaises(ValueError):
            tmux_merge.merge('session', self.a, self.b, run=self.run_tmux, keep='both')
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux, keep='source')
        self.assertEqual(self.sessions(), ['beta'])
        self.assertEqual(self.project(self.b), alpha)
        self.assertEqual(self.archived(), [beta])
        self.assertIn('Alpha rules.', projects.render(alpha))

    def test_merge_keeps_the_target_project(self):
        alpha, beta = self.with_projects()
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux, keep='target')
        self.assertEqual(self.project(self.b), beta)
        self.assertEqual(self.archived(), [alpha])

    def test_merge_keeps_the_only_project_with_text(self):
        alpha, beta = self.with_projects()
        projects.set_instructions(beta, '', 1)  # beta is empty now: agents get nothing from it
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual(self.project(self.b), alpha)
        self.assertEqual(self.archived(), [beta])

    def test_merge_into_a_session_with_no_project_takes_the_source_project(self):
        alpha, _ = self.with_projects()
        self.tmux('set-option', '-u', '-t', self.b, '@project')
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        self.assertEqual(self.project(self.b), alpha)
        self.assertEqual(self.archived(), [])

    def test_moving_a_pane_changes_no_project(self):
        alpha, beta = self.with_projects()
        tmux_merge.merge('pane', self.panes(self.a)[0], self.b, run=self.run_tmux)
        self.assertEqual((self.project(self.a), self.project(self.b)), (alpha, beta))
        self.assertEqual(self.archived(), [])

    def test_move_pane_keeps_its_id(self):
        pane = self.panes(self.a)[0]
        result = tmux_merge.merge('pane', pane, self.b, run=self.run_tmux)
        self.assertEqual(result, {'session': self.b, 'name': 'beta', 'pane': pane})
        self.assertIn(pane, self.panes(self.b))
        self.assertNotIn(pane, self.panes(self.a))

    def test_break_out_makes_a_new_session_with_only_that_pane(self):
        pane = self.panes(self.a)[0]
        result = tmux_merge.merge('break', pane, None, run=self.run_tmux)
        self.assertEqual(self.panes(result['session']), [pane])
        self.assertEqual(len(self.sessions()), 3)
        self.assertRegex(result['name'], r'^split-\d{6}$')

    def timing_out(self, command, applied):
        """run_tmux, but the first `command` times out: after real tmux ran it (applied) or without running it."""
        left = [command]

        def run(argv, **kwargs):
            if argv[1:2] == left:
                left.clear()
                if applied:
                    self.run_tmux(argv, **kwargs)
                raise subprocess.TimeoutExpired(argv, 5)
            return self.run_tmux(argv, **kwargs)
        return run

    def test_real_break_pane_timeout_after_apply_finishes(self):
        pane = self.panes(self.a)[0]
        result = tmux_merge.break_out(pane, run=self.timing_out('break-pane', applied=True))
        self.assertEqual(self.panes(result['session']), [pane])  # the placeholder shell is gone
        self.assertNotIn('warning', result)
        self.assertEqual(len(self.sessions()), 3)

    def test_real_new_session_timeout_cleans_up_after_rename(self):
        pane = self.panes(self.a)[0]

        def run(argv, **kwargs):
            result = self.run_tmux(argv, **kwargs)
            if argv[1] == 'new-session':
                self.assertEqual(result.returncode, 0, result.stderr)
                session, _ = result.stdout.strip().split('\t')
                renamed = self.tmux('rename-session', '-t', session, 'renamed')
                self.assertEqual(renamed.returncode, 0, renamed.stderr)
                raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
            return result

        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\.$'):
            tmux_merge.break_out(pane, run=run)
        self.assertEqual(self.sessions(), ['alpha', 'beta'])
        self.assertIn(pane, self.panes(self.a))

    def test_real_new_session_cleanup_keeps_a_window_moved_into_it(self):
        pane = self.panes(self.a)[0]
        other_pane, = self.panes(self.b)
        window = self.tmux('display-message', '-p', '-t', other_pane, '#{window_id}').stdout.strip()

        def run(argv, **kwargs):
            result = self.run_tmux(argv, **kwargs)
            if argv[1] == 'new-session':
                self.assertEqual(result.returncode, 0, result.stderr)
                session, placeholder = result.stdout.strip().split('\t')
                start = self.tmux('display-message', '-p', '-t', placeholder, '#{pane_start_command}')
                self.assertEqual((start.returncode, start.stdout), (0, '\n'))
                moved = self.tmux('move-window', '-d', '-s', window, '-t', session + ':')
                self.assertEqual(moved.returncode, 0, moved.stderr)
                raise subprocess.TimeoutExpired(argv, 5, output=result.stdout)
            return result

        self.tmux('set-option', '-p', '-t', pane, '@chat_name', 'Review')
        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. '
                                    r'The new session “Review” stays open\. Close it from the sidebar\.$'):
            tmux_merge.break_out(pane, run=run)
        self.assertEqual(self.sessions(), ['Review', 'alpha'])
        dest = self.tmux('display-message', '-p', '-t', other_pane, '#{session_id}').stdout.strip()
        self.assertEqual(self.panes(dest), [other_pane])

    def test_real_new_session_duplicate_keeps_the_unmarked_session(self):
        pane = self.panes(self.a)[0]
        self.tmux('set-option', '-p', '-t', pane, '@chat_name', 'Review')
        competing = []

        def run(argv, **kwargs):
            if argv[1] == 'new-session':
                competing.append(self.new('Review'))
            return self.run_tmux(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\.$'):
            tmux_merge.break_out(pane, run=run)
        self.assertEqual(self.sessions(), ['Review', 'alpha', 'beta'])
        self.assertEqual(len(self.panes(competing[0])), 1)

    def test_real_new_session_leaves_unmarked_pane_open_when_setting_the_marker_fails(self):
        pane = self.panes(self.a)[0]
        self.tmux('set-option', '-p', '-t', pane, '@chat_name', 'Review')
        created_panes = []

        def run(argv, **kwargs):
            if argv[1] == 'new-session':
                argv = [*argv[:argv.index(';')], ';', 'set-option', '-p', 'not-an-option', 'token']
                result = self.run_tmux(argv, **kwargs)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertRegex(result.stdout, r'^\$[0-9]+\t%[0-9]+\n$')
                created_panes.append(result.stdout.split()[1])
                return result
            self.assertNotEqual(argv[1], 'kill-pane')
            return self.run_tmux(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. A session “Review” may be left open\.$'):
            tmux_merge.break_out(pane, run=run)
        self.assertEqual(self.sessions(), ['Review', 'alpha', 'beta'])
        self.assertEqual(self.panes('=Review'), created_panes)

    def test_real_new_session_hook_marks_a_different_pane(self):
        pane = self.panes(self.a)[0]
        self.tmux('set-option', '-p', '-t', pane, '@chat_name', 'Review')
        hook = self.tmux('set-hook', '-g', 'after-new-session', 'split-window')
        self.assertEqual(hook.returncode, 0, hook.stderr)
        created_panes = []

        def run(argv, **kwargs):
            self.assertNotEqual(argv[1], 'kill-pane')
            result = self.run_tmux(argv, **kwargs)
            if argv[1] == 'new-session':
                self.assertEqual(result.returncode, 0, result.stderr)
                session, printed = result.stdout.split()
                created_panes.extend(self.panes(session))
                other, = set(created_panes) - {printed}
                for target, expected in ((printed, '\n'), (other, argv[-1] + '\n')):
                    marker = self.tmux('display-message', '-p', '-t', target, '#{@tether_breakout}')
                    self.assertEqual((marker.returncode, marker.stdout), (0, expected))
            return result

        result = tmux_merge.break_out(pane, run=run)
        self.assertEqual(result['warning'], 'A session “Review” may be left open.')
        self.assertCountEqual(self.panes(result['session']), [pane, *created_panes])

    def test_real_new_session_hook_timeout_does_not_hide_a_leftover(self):
        pane = self.panes(self.a)[0]
        self.tmux('set-option', '-p', '-t', pane, '@chat_name', 'Review')
        hook = self.tmux('set-hook', '-g', 'after-new-session', 'wait-for breakout-test-release')
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.addCleanup(self.tmux, 'wait-for', '-S', 'breakout-test-release')

        def run(argv, **kwargs):
            if argv[1] == 'new-session':
                kwargs['timeout'] = 0.5  # The hook waits on a signal, independent of the client deadline.
                try:
                    return self.run_tmux(argv, **kwargs)
                except subprocess.TimeoutExpired as exc:
                    self.assertRegex(exc.stdout, rb'^\$[0-9]+\t%[0-9]+\n$')
                    _, placeholder = exc.stdout.decode().strip().split('\t')
                    marker = self.tmux('display-message', '-p', '-t', placeholder, '#{@tether_breakout}')
                    self.assertEqual((marker.returncode, marker.stdout), (0, '\n'))
                    raise
            return self.run_tmux(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, r'^Could not start a session\. A session “Review” may be left open\.$'):
            tmux_merge.break_out(pane, run=run)
        self.assertIn('Review', self.sessions())
        self.assertIn(pane, self.panes(self.a))

    def test_real_breakout_reports_moved_and_warns_when_ungroup_times_out(self):
        pane = self.panes(self.a)[0]
        self.tmux('set-option', '-p', '-t', pane, '@chat_name', 'Review')
        run_with_ungroup_timeout = self.timing_out('set-option', applied=False)

        def run(argv, **kwargs):
            if argv[1] == 'kill-pane':
                raise subprocess.TimeoutExpired(argv, 5)
            return run_with_ungroup_timeout(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, r'^The pane moved to “Review”, but Tether could not finish\. '
                                    r'Refresh the list\. .*extra shell tab'):
            tmux_merge.break_out(pane, run=run)
        self.assertEqual(self.sessions(), ['Review', 'alpha', 'beta'])
        destination = self.tmux('display-message', '-p', '-t', '=Review:', '#{session_id}').stdout.strip()
        self.assertIn(pane, self.panes(destination))
        self.assertEqual(len(self.panes(destination)), 2)

    def test_real_break_pane_timeout_before_apply_leaves_no_session(self):
        pane = self.panes(self.a)[0]
        with self.assertRaisesRegex(RuntimeError, r'^Could not move the pane\.$'):
            tmux_merge.break_out(pane, run=self.timing_out('break-pane', applied=False))
        self.assertEqual(self.sessions(), ['alpha', 'beta'])
        self.assertIn(pane, self.panes(self.a))

    def test_real_placeholder_kill_timeout_after_apply_gives_no_warning(self):
        pane = self.panes(self.a)[0]
        result = tmux_merge.break_out(pane, run=self.timing_out('kill-pane', applied=True))
        self.assertEqual(self.panes(result['session']), [pane])
        self.assertNotIn('warning', result)

    def names(self, session):
        out = self.tmux('list-panes', '-s', '-t', session + ':', '-F', '#{pane_id}\t#{@chat_name}\t#{@chat_name_at}').stdout
        return {line.split('\t')[0]: tuple(line.split('\t')[1:]) for line in out.splitlines()}

    def test_merged_panes_keep_the_session_name(self):
        first, second, third = self.panes(self.a)
        self.tmux('set-option', '-p', '-t', second, '@chat_name', 'review')
        self.tmux('set-option', '-p', '-t', self.panes(self.b)[0], '@chat_name', 'alpha')  # already used in the target
        tmux_merge.merge('session', self.a, self.b, run=self.run_tmux)
        names = self.names(self.b)
        # A name the user gave stays; the others take the old session name, never a name the target already has.
        # No time is stored, so a later /rename in Claude or Codex still wins (agents.list_panes).
        self.assertEqual((names[first], names[second], names[third]), (('alpha 2', ''), ('review', ''), ('alpha 3', '')))

    def test_moved_pane_keeps_the_session_name_and_breaks_out_under_it(self):
        pane = self.panes(self.a)[0]
        tmux_merge.merge('pane', pane, self.b, run=self.run_tmux)
        self.assertEqual(self.names(self.b)[pane], ('alpha', ''))
        result = tmux_merge.merge('break', pane, None, run=self.run_tmux)
        self.assertEqual(result['name'], 'alpha 2')  # 'alpha' is still a session
        self.assertEqual(self.panes(result['session']), [pane])

    def test_numbered_session_gives_no_name(self):
        self.tmux('rename-session', '-t', self.a + ':', '7')
        pane = self.panes(self.a)[0]
        tmux_merge.merge('pane', pane, self.b, run=self.run_tmux)
        self.assertEqual(self.names(self.b)[pane], ('', ''))

    def test_refusals(self):
        lone = self.panes(self.b)[0]
        with self.assertRaisesRegex(ValueError, 'already a session'):
            tmux_merge.merge('break', lone, None, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'different session'):
            tmux_merge.merge('session', self.a, self.a, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'already in that session'):
            tmux_merge.merge('pane', lone, self.b, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'gone'):
            tmux_merge.merge('session', '$999', self.b, run=self.run_tmux)
        for bad in ('beta', '$1;kill-server', '%1 x', None):
            with self.assertRaises(ValueError):
                tmux_merge.merge('session', bad, self.b, run=self.run_tmux)
            with self.assertRaises(ValueError):
                tmux_merge.merge('pane', bad, self.b, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            tmux_merge.merge('swap', self.a, self.b, run=self.run_tmux)
        self.assertEqual(self.sessions(), ['alpha', 'beta'])

    def test_terminal_panel_session_is_never_a_source_or_target(self):
        shells = self.new(side_shells.SESSION)
        with self.assertRaisesRegex(ValueError, 'Terminal panel'):
            tmux_merge.merge('session', self.a, shells, run=self.run_tmux)
        with self.assertRaisesRegex(ValueError, 'Terminal panel'):
            tmux_merge.merge('pane', self.panes(shells)[0], self.b, run=self.run_tmux)
        self.assertEqual(self.sessions(), ['alpha', 'beta', side_shells.SESSION])


if __name__ == '__main__':
    unittest.main()
