import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import agents
import procinfo
import terminal_input
import terminal_replies


class CodexMappingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE threads (id,rollout_path,cwd,archived,source,created_at_ms,updated_at_ms)')
        self.db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
            ('older', '/r/rollout-older.jsonl', '/work', 0, 'cli', 100000, 500000),
            ('newer', '/r/rollout-newer.jsonl', '/work', 0, 'cli', 200000, 600000),
            ('child', '/r/rollout-child.jsonl', '/work', 0, 'subagent', 200000, 700000)])

    def test_each_process_owns_its_open_transcript_despite_same_folder(self):
        for pid, expected in ((100, 'older'), (200, 'newer')):
            with patch.object(procinfo, 'argv', return_value=[b'codex']), \
                    patch.object(agents, 'started_ms', return_value=90000), \
                    patch.object(procinfo, 'fd_targets', return_value=[('7', f'/r/rollout-{expected}.jsonl'), ('8', '/r/rollout-child.jsonl')]):
                self.assertEqual(agents.codex_thread(self.db, pid, '1', '/work')[0], expected)

    def test_discovery_keeps_both_conversations_visible(self):
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout='%1\t$1\twork\t0.0\t100\t/work\n%2\t$1\twork\t1.0\t200\t/work\n'))
        with patch.object(agents.pane_view, 'zoom_repair_all'), \
                patch.object(procinfo, 'table', return_value={100: (1, 'codex'), 200: (1, 'codex')}), \
                patch.object(agents, 'process_start', side_effect=str), \
                patch.object(agents, 'started_ms', return_value=90000), \
                patch.object(procinfo, 'argv', return_value=[b'codex']), \
                patch.object(procinfo, 'fd_targets', side_effect=lambda pid: [('7', '/r/rollout-' + ('older' if pid == 100 else 'newer') + '.jsonl')]), \
                patch.object(agents, 'codex_database', return_value=self.db), \
                patch.object(agents.codex_provider, 'pane_profile', return_value=None), \
                patch.object(agents, 'codex_names', return_value={}):
            entries = agents.discover({}, run)
        self.assertEqual([entry['thread'] for entry in entries], ['older', 'newer'])
        self.assertFalse(any(entry.get('pending_conversation') for entry in entries))

    def test_real_open_rollout_wins_over_newer_thread(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout-owner.jsonl'
            path.write_text('')
            self.db.execute("INSERT INTO threads VALUES ('owner', ?, '/elsewhere', 0, 'cli', 1, 2)", (str(path),))
            with path.open('a'):
                self.assertEqual(agents.codex_thread(self.db, os.getpid(), '1', '/work'), ('owner', str(path)))

    def test_disappearing_fds_allow_existing_fallback(self):
        with patch.object(procinfo, 'fd_targets', side_effect=ProcessLookupError), \
                patch.object(procinfo, 'argv', return_value=[b'codex']), \
                patch.object(agents, 'started_ms', return_value=90000):
            self.assertEqual(agents.codex_thread(self.db, 100, '1', '/work')[0], 'newer')


DAEMON = '01a1eae5-ca2d-74f2-97f3-d4f02d0085b6'
GAMMA = '01a1eafe-c07e-7c71-8ae6-59f3176a4450'
LONE = '01a1eae3-a292-7530-8e9d-2cbb5c0ffee0'


class CodexDaemonBindingTests(unittest.TestCase):
    """Plain `codex` 0.169 panes are clients of one app-server daemon: no pane holds a rollout open. The pane title that
    Codex sets ("<thread name> | <folder>") is the client's own word for its thread."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE threads (id,rollout_path,cwd,archived,source,created_at_ms,updated_at_ms,name)')
        self.db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', [
            (DAEMON, '/r/beta.jsonl', '/srv/u/Documents', 0, 'vscode', 100_000, 300_000, 'Reply with beta'),
            (GAMMA, '/r/gamma.jsonl', '/srv/u/Documents', 0, 'vscode', 200_000, 400_000, 'Reply with gamma'),
            (LONE, '/r/lone.jsonl', '/srv/u/Documents', 0, 'vscode', 150_000, 160_000, None)])
        self.titles = {}
        self.panes = {}
        self.sessions = {}
        self.tmux_fails = False
        self.argv = {}
        self.items = None
        for target, kwargs in ((procinfo, {'fd_targets': lambda pid: []}),
                               (procinfo, {'argv': lambda pid: self.argv.get(pid, [b'codex'])}),
                               (agents, {'started_ms': lambda ticks: int(ticks)}),
                               (agents, {'codex_title_items': lambda: self.items}),
                               (agents, {'codex_database': lambda: SimpleNamespace(execute=self.db.execute, close=lambda: None)})):
            patcher = patch.multiple(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tmux(self, command, **_):
        if 'list-panes' in command and '#{pane_pid}' in command[-1]:
            if self.tmux_fails:
                raise subprocess.TimeoutExpired(command, 5)
            return SimpleNamespace(returncode=0, stdout=''.join(f'{pane}\t{1000 + pid}\t{self.sessions.get(pane, "main")}\t\t/srv/u/Documents\n'
                                                               for pane, pid in self.panes.items()))
        if 'list-panes' in command:
            if self.tmux_fails:
                return SimpleNamespace(returncode=1, stdout='')
            return SimpleNamespace(returncode=0, stdout=''.join(f'{pane}\t{title}\n' for pane, title in self.titles.items()))
        if 'display-message' in command:
            return SimpleNamespace(returncode=0, stdout=self.titles.get(command[command.index('-t') + 1], '') + '\n')
        return SimpleNamespace(returncode=0, stdout='')

    def send_check(self, entry, starts, processes=None):
        """terminal_input.verify_codex for `entry`, with the live panes {pane: (pid, start)} of `starts`."""
        self.panes = {pane: pid for pane, (pid, _) in starts.items()}
        table = dict(processes or {})
        for pid, _ in starts.values():
            table[1000 + pid] = (1, 'zsh')
            table[pid] = (1000 + pid, 'codex')
        with patch.object(procinfo, 'table', lambda run=None: table), \
                patch.object(agents, 'process_start', lambda pid: next(str(s) for p, s in starts.values() if p == pid)):
            terminal_input.verify_codex(entry, self.run_tmux)

    def bind(self, *panes, processes=None):
        entries = [{'id': pane, 'agent': 'codex', 'owner_pid': pid, 'process_start': str(start), 'cwd': '/srv/u/Documents'}
                   for pane, pid, start in panes]
        agents.bind_codex(self.db, entries, self.run_tmux, processes)
        return {entry['id']: entry for entry in entries}

    def test_two_daemon_panes_in_one_folder_keep_their_own_threads(self):
        self.titles = {'%11': 'Reply with beta | Documents', '%12': '\u2819 Reply with gamma | Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertEqual((entries['%11']['thread'], entries['%12']['thread']), (DAEMON, GAMMA))
        self.assertEqual(entries['%11']['transcript'], '/r/beta.jsonl')
        self.assertEqual(entries['%12']['codex_binding'], 'title')

    def test_clear_or_reused_pane_waits_instead_of_taking_a_named_thread(self):
        self.titles = {'%11': 'Reply with beta | Documents', '%12': 'Documents'}  # %12 after /clear, or a new process
        entries = self.bind(('%11', 11, 50_000), ('%12', 99, 390_000))
        self.assertEqual(entries['%11']['thread'], DAEMON)
        self.assertTrue(entries['%12']['pending_conversation'])
        self.assertNotIn('codex_candidates', entries['%12'])

    def test_resumed_unnamed_thread_is_bound(self):
        self.titles = {'%11': 'Documents'}
        self.argv[11] = [b'codex', b'resume', LONE.encode()]
        self.assertEqual(self.bind(('%11', 11, 500_000))['%11']['thread'], LONE)

    def test_title_outranks_the_resume_argument_after_resume_in_the_tui(self):
        self.titles = {'%11': 'Reply with gamma | Documents'}
        self.argv[11] = [b'codex', b'resume', LONE.encode()]
        self.assertEqual(self.bind(('%11', 11, 500_000))['%11']['thread'], GAMMA)

    def test_a_shared_name_is_settled_by_the_other_panes_binding(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.db.execute("UPDATE threads SET rollout_path='/r/rollout-gamma.jsonl' WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Fix tests | Documents', '%12': 'Fix tests | Documents'}
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-gamma.jsonl')] if pid == 12 else []):
            entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertEqual((entries['%12']['thread'], entries['%12']['codex_binding']), (GAMMA, 'open'))
        self.assertEqual((entries['%11']['thread'], entries['%11']['codex_binding']), (DAEMON, 'title'))

    def test_a_shared_name_is_not_settled_by_the_resume_argument(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.titles = {'%11': 'Fix tests | Documents'}
        self.argv[11] = [b'codex', b'resume', DAEMON.encode()]  # the start argument: /resume in the TUI may have switched
        self.assertTrue(self.bind(('%11', 11, 500_000))['%11']['pending_conversation'])

    def test_a_tie_is_a_guess_and_never_takes_a_bound_thread(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.titles = {'%11': 'Fix tests | Documents', '%12': 'Fix tests | Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        for entry in entries.values():
            self.assertTrue(entry['pending_conversation'])
            self.assertEqual(entry['codex_candidates'], [GAMMA, DAEMON])

    def test_panes_without_thread_titles_never_share_a_guess(self):
        self.items = ['spinner', 'project-name']
        self.titles = {'%11': 'Documents', '%12': 'Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertEqual([entry.get('codex_candidates') for entry in entries.values()], [[GAMMA, DAEMON, LONE]] * 2)
        self.assertTrue(all(entry['pending_conversation'] for entry in entries.values()))

    def test_one_pane_without_thread_titles_keeps_the_old_guess(self):
        self.items = ['project-name']
        self.titles = {'%11': 'Documents'}
        entries = self.bind(('%11', 11, 50_000))
        self.assertEqual((entries['%11']['thread'], entries['%11']['codex_binding']), (GAMMA, 'guess'))

    def test_thread_id_title_matches_a_cut_id(self):
        self.items = ['thread-id']
        self.titles = {'%11': LONE[:29] + '...'}
        self.assertEqual(self.bind(('%11', 11, 500_000))['%11']['thread'], LONE)

    def test_a_title_cut_at_the_limit_never_reads_as_fresh(self):
        self.items = ['thread-name', 'project-name', 'thread-id']
        for title in (('x' * 228) + ' | ' + LONE[:9], 'x' * 240):  # Codex cuts at 240 with no mark
            self.assertEqual(len(title), agents.TITLE_MAX)
            self.titles = {'%11': title}
            entry = self.bind(('%11', 11, 800_000), processes=self.daemon(LONE))['%11']
            self.assertEqual((entry['codex_title_read'], entry.get('codex_candidates')), (False, [LONE]))
            with self.assertRaisesRegex(RuntimeError, 'could not read'):
                self.send_check(dict(entry), {'%11': (11, 800_000)}, processes=self.daemon(LONE))

    def test_send_check_uses_the_current_title(self):
        entry = {'id': '%11', 'owner_pid': 11, 'process_start': '50000', 'cwd': '/srv/u/Documents', 'thread': DAEMON}
        self.titles = {'%11': 'Reply with beta | Documents'}
        self.send_check(entry, {'%11': (11, 50_000)})
        for title in ('Reply with gamma | Documents', 'Documents'):  # switched, or after /clear
            self.titles = {'%11': title}
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                self.send_check(entry, {'%11': (11, 50_000)})

    def test_send_check_refuses_when_the_title_cannot_be_read(self):
        entry = {'id': '%11', 'owner_pid': 11, 'process_start': '50000', 'cwd': '/srv/u/Documents', 'thread': LONE}
        self.titles = {'%11': '\u2819 Documents'}  # the first turn of a new thread: no name yet
        with self.assertRaisesRegex(RuntimeError, 'cannot confirm'):  # a folder guess shows the chat but never sends
            self.send_check(entry, {'%11': (11, 50_000)})
        self.tmux_fails = True
        with self.assertRaises(RuntimeError):
            self.send_check(entry, {'%11': (11, 50_000)})
        self.tmux_fails = False
        self.titles = {}  # tmux answers but not for this pane
        with self.assertRaisesRegex(RuntimeError, 'title'):
            self.send_check(entry, {'%11': (11, 50_000)})
        self.items = ['project-name']  # a layout without the thread: discovery may guess, sends wait
        self.titles = {'%11': 'Documents'}
        guessed = self.bind(('%11', 11, 50_000))['%11']
        self.assertEqual(guessed['codex_binding'], 'guess')
        with self.assertRaisesRegex(RuntimeError, 'terminal_title'):
            self.send_check(dict(entry, thread=guessed['thread']), {'%11': (11, 50_000)})

    def test_send_check_refuses_an_ambiguous_pending_pane(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.titles = {'%11': 'Fix tests | Documents'}
        entry = self.bind(('%11', 11, 50_000))['%11']
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(entry, {'%11': (11, 50_000)})

    def test_send_check_lets_a_new_session_send_its_first_message(self):
        self.titles = {'%11': 'Documents'}
        self.items = ['spinner', 'thread-id']
        self.titles = {'%11': '\u2819'}
        entry = self.bind(('%11', 11, 500_000))['%11']
        self.assertTrue(entry['pending_conversation'])
        self.send_check(entry, {'%11': (11, 500_000)})

    def test_send_check_agrees_with_discovery_when_another_pane_settles_a_tie(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.db.execute("UPDATE threads SET rollout_path='/r/rollout-gamma.jsonl' WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Fix tests | Documents', '%12': 'Fix tests | Documents'}
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-gamma.jsonl')] if pid == 12 else []):
            entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
            self.assertEqual(entries['%11']['thread'], DAEMON)
            self.send_check(entries['%11'], {'%11': (11, 50_000), '%12': (12, 60_000)})

    def test_send_check_keeps_an_open_rollout_when_ps_fails(self):
        self.db.execute("UPDATE threads SET rollout_path='/r/rollout-gamma.jsonl' WHERE id=?", (GAMMA,))
        entry = {'id': '%11', 'owner_pid': 11, 'process_start': '50000', 'cwd': '/srv/u/Documents', 'thread': GAMMA}

        def ps_times_out(run=None):
            raise subprocess.TimeoutExpired('ps', 5)
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-gamma.jsonl')] if pid == 11 else []), \
                patch.object(procinfo, 'table', ps_times_out):
            terminal_input.verify_codex(entry, self.run_tmux)
            with self.assertRaisesRegex(RuntimeError, 'could not check'):
                terminal_input.verify_codex(dict(entry, thread=DAEMON), self.run_tmux)

    def test_a_missing_title_never_lets_the_resume_argument_send(self):
        self.argv[11] = [b'codex', b'resume', DAEMON.encode()]
        entry = {'id': '%11', 'owner_pid': 11, 'process_start': '50000', 'cwd': '/srv/u/Documents', 'thread': DAEMON}
        self.titles = {}  # tmux answers without this pane; the pane may have switched to Gamma in the TUI
        with self.assertRaises(RuntimeError):
            self.send_check(entry, {'%11': (11, 50_000)}, processes=self.daemon(DAEMON, GAMMA))

    def test_an_unknown_name_never_passes_as_an_empty_chat(self):
        self.titles = {'%11': 'First unmatched name | Documents'}
        entry = self.bind(('%11', 11, 50_000))['%11']
        self.assertTrue(entry['pending_conversation'])
        self.titles = {'%11': 'Second unmatched name | Documents'}
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(entry, {'%11': (11, 50_000)})

    def test_action_required_with_a_thread_of_that_name_stays_unresolved(self):
        self.db.execute("UPDATE threads SET name='[ ! ] Action Required' WHERE id=?", (DAEMON,))
        self.titles = {'%11': '[ ! ] Action Required | Documents', '%12': 'Reply with gamma | Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertTrue(entries['%11']['pending_conversation'])
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(entries['%11'], {'%11': (11, 50_000), '%12': (12, 60_000)})
        self.items = ['thread-name', 'project-name']  # no activity item: Codex never shows "Action Required"
        self.assertEqual(self.bind(('%11', 11, 50_000))['%11']['thread'], DAEMON)

    def test_action_required_keeps_the_unnamed_thread_when_another_pane_owns_the_name(self):
        self.db.execute("UPDATE threads SET name='[ ! ] Action Required' WHERE id=?", (DAEMON,))
        self.titles = {'%11': '\u2819 [ ! ] Action Required | Documents', '%12': '[ ! ] Action Required | Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 800_000), processes=self.daemon(DAEMON, LONE))
        self.assertEqual((entries['%11']['thread'], entries['%12'].get('codex_candidates')), (DAEMON, [LONE]))
        self.assertTrue(entries['%12']['codex_unresolved'])

    def test_the_unnamed_candidate_survives_when_the_named_thread_is_in_another_folder(self):
        self.db.execute("UPDATE threads SET name='[ ! ] Action Required', cwd='/other/Documents' WHERE id=?", (GAMMA,))
        self.titles = {'%11': '[ ! ] Action Required | Documents', '%12': '\u2819 [ ! ] Action Required | Documents'}
        entries = self.bind(('%11', 11, 800_000), ('%12', 12, 50_000), processes=self.daemon(GAMMA, LONE))
        self.assertEqual((entries['%12']['thread'], entries['%11'].get('codex_candidates')), (GAMMA, [LONE]))

    def test_action_required_keeps_the_unnamed_thread_as_a_candidate(self):
        self.db.execute("UPDATE threads SET name='[ ! ] Action Required' WHERE id=?", (DAEMON,))
        self.titles = {'%11': '[ ! ] Action Required | Documents'}  # LONE is unnamed and waits for approval
        entry = self.bind(('%11', 11, 800_000), processes=self.daemon(DAEMON, LONE))['%11']
        self.assertEqual(sorted(entry['codex_candidates']), sorted([DAEMON, LONE]))

    def test_side_shells_do_not_count_in_the_send_check(self):
        self.titles = {'%11': 'Reply with gamma | Documents', '%40': 'Reply with gamma | Documents'}
        entry = self.bind(('%11', 11, 50_000))['%11']
        self.sessions = {'%40': agents.SIDE_SHELLS}
        self.send_check(entry, {'%11': (11, 50_000), '%40': (40, 60_000)})

    def test_an_unreadable_layout_never_uses_the_resume_argument(self):
        self.items = ['thread-title', 'project-name']
        self.argv[11] = [b'codex', b'resume', DAEMON.encode()]
        self.titles = {'%11': 'Reply with gamma | Documents'}  # switched to Gamma in the TUI
        entry = self.bind(('%11', 11, 500_000), processes=self.daemon(DAEMON, GAMMA))['%11']
        self.assertNotEqual(entry.get('codex_binding'), 'resume')
        with self.assertRaises(RuntimeError):
            self.send_check(dict(entry, thread=DAEMON), {'%11': (11, 500_000)}, processes=self.daemon(DAEMON, GAMMA))

    def test_a_promoted_tie_takes_its_thread_from_an_earlier_guess(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.db.execute("UPDATE threads SET rollout_path='/r/rollout-gamma.jsonl' WHERE id=?", (GAMMA,))
        self.titles = {'%10': 'vim', '%11': 'Fix tests | Documents'}  # %10: a title that names nothing
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-gamma.jsonl')] if pid == 12 else []):
            entries = self.bind(('%10', 10, 50_000), ('%11', 11, 55_000), ('%12', 12, 60_000))
        self.assertEqual((entries['%11']['thread'], entries['%12']['thread']), (DAEMON, GAMMA))
        self.assertNotIn(entries['%10']['thread'], (DAEMON, GAMMA))

    def test_a_tie_is_not_promoted_when_another_pane_could_show_the_thread(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.db.execute("UPDATE threads SET rollout_path='/r/rollout-gamma.jsonl' WHERE id=?", (GAMMA,))
        self.titles = {'%10': 'Fix tests | Documents', '%11': 'Fix tests | Documents'}
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-gamma.jsonl')] if pid == 12 else []):
            entries = self.bind(('%10', 10, 50_000), ('%11', 11, 55_000), ('%12', 12, 60_000))
        self.assertTrue(entries['%10']['pending_conversation'] and entries['%11']['pending_conversation'])

    def test_codex_inside_a_claude_pane_does_not_count_in_the_send_check(self):
        self.titles = {'%11': 'Reply with gamma | Documents', '%41': ''}
        entry = self.bind(('%11', 11, 50_000))['%11']
        self.send_check(entry, {'%11': (11, 50_000), '%41': (41, 60_000)}, processes={2041: (1041, 'claude')})
        with patch.object(agents, 'claude_registries', lambda processes: [{'pid': 1041, 'tmux': '/tmp/tmux-1/default,1,0.%41'}]):
            self.send_check(entry, {'%11': (11, 50_000), '%41': (41, 60_000)})  # Claude named by version (macOS)
        with patch.object(agents, 'claude_registries', lambda processes: [{'pid': 11, 'tmux': '/tmp/tmux-1/default,1,0.%99'}]):
            self.send_check(entry, {'%11': (11, 50_000)})  # a stale registry of a reused pid points at another pane

    def test_a_daemon_scan_failure_refuses_instead_of_widening(self):
        self.argv[11] = [b'codex', b'resume', DAEMON.encode()]
        self.titles = {'%11': '\u2819 Documents'}  # unnamed thread
        self.db.execute("UPDATE threads SET name=NULL WHERE id=?", (DAEMON,))
        entry = self.bind(('%11', 11, 500_000), processes=self.daemon(DAEMON))['%11']
        self.assertEqual((entry['thread'], entry['codex_binding']), (DAEMON, 'resume'))
        def unreadable(pid):
            if pid == 500:
                raise TimeoutError('lsof timed out')
            return []
        processes = self.daemon()  # the daemon still runs (its own patch of fd_targets goes first)
        with patch.object(procinfo, 'fd_targets', unreadable), self.assertRaisesRegex(RuntimeError, 'could not check'):
            self.send_check(entry, {'%11': (11, 500_000)}, processes=processes)

    def test_a_folder_guess_among_loaded_threads_reads_but_never_sends(self):
        self.db.execute("UPDATE threads SET name=NULL WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.titles = {'%11': '\u2819 Documents'}
        entry = self.bind(('%11', 11, 50_000), processes=self.daemon(DAEMON, GAMMA))['%11']
        self.assertEqual(entry['codex_binding'], 'guess')
        self.assertEqual(sorted(entry['codex_candidates']), sorted([DAEMON, GAMMA]))
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(entry, {'%11': (11, 50_000)}, processes=self.daemon(DAEMON, GAMMA))

    def test_a_resume_argument_shows_the_chat_but_never_sends(self):
        self.argv[11] = [b'codex', b'resume', LONE.encode()]
        self.titles = {'%11': '\u2819 Documents'}
        entry = self.bind(('%11', 11, 500_000), processes=self.daemon(LONE))['%11']
        self.assertEqual((entry['thread'], entry['codex_binding']), (LONE, 'resume'))
        with self.assertRaisesRegex(RuntimeError, 'cannot confirm'):
            self.send_check(entry, {'%11': (11, 500_000)}, processes=self.daemon(LONE))

    def test_a_fresh_pane_never_shows_a_closed_panes_thread(self):
        # plain `codex` started 20 s after GAMMA began in a pane that has closed; the daemon still holds GAMMA
        self.db.execute("UPDATE threads SET name=NULL, created_at_ms=780000, updated_at_ms=790000 WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Documents'}
        entry = self.bind(('%11', 11, 800_000), processes=self.daemon(GAMMA))['%11']
        self.assertTrue(entry['pending_conversation'])
        self.assertEqual(entry['codex_candidates'], [GAMMA])
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):  # GAMMA might be open here: no first message yet
            self.send_check(entry, {'%11': (11, 800_000)}, processes=self.daemon(GAMMA))
        entry = self.bind(('%11', 11, 800_000), processes=self.daemon())['%11']  # the daemon unloads it
        self.assertNotIn('codex_candidates', entry)
        self.send_check(entry, {'%11': (11, 800_000)}, processes=self.daemon())

    def test_an_old_unnamed_thread_resumed_in_the_tui_never_passes_as_new(self):
        self.titles = {'%11': 'Documents'}  # `/resume` inside plain `codex` opened LONE, created long before
        entry = self.bind(('%11', 11, 800_000), processes=self.daemon(LONE))['%11']
        self.assertEqual((entry['pending_conversation'], entry['codex_candidates']), (True, [LONE]))
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(entry, {'%11': (11, 800_000)}, processes=self.daemon(LONE))
        self.titles = {'%11': 'Documents', '%12': 'Documents'}
        self.argv[12] = [b'codex', b'resume', LONE.encode()]  # another pane holds LONE by its argument: not a candidate
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-' + LONE + '.jsonl')] if pid in (12, 500) else []):
            entries = self.bind(('%11', 11, 800_000), ('%12', 12, 700_000))
        self.assertNotIn('codex_candidates', entries['%11'])

    def test_a_stale_startup_argument_never_hides_a_thread_another_pane_resumed(self):
        self.argv[11] = [b'codex', b'resume', LONE.encode()]  # pane 1 started on LONE, then ran /clear
        self.titles = {'%11': 'Documents', '%12': 'Documents'}  # pane 2 ran /resume LONE in its TUI
        entries = self.bind(('%11', 11, 700_000), ('%12', 12, 800_000), processes=self.daemon(LONE))
        self.assertEqual(entries['%11']['codex_binding'], 'resume')
        self.assertEqual((entries['%12']['pending_conversation'], entries['%12']['codex_candidates']), (True, [LONE]))
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(entries['%12'], {'%11': (11, 700_000), '%12': (12, 800_000)}, processes=self.daemon(LONE))

    def test_the_footer_folder_keeps_an_older_candidate(self):
        self.db.execute("UPDATE threads SET cwd='/other/Documents' WHERE id=?", (LONE,))
        self.titles = {'%11': 'Documents'}
        with patch.object(agents, 'footer_cwd', lambda run, pane: '/other/Documents'):
            entry = self.bind(('%11', 11, 800_000), processes=self.daemon(LONE))['%11']
            self.assertEqual((entry['pending_conversation'], entry['codex_candidates']), (True, [LONE]))
            with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
                self.send_check(entry, {'%11': (11, 800_000)}, processes=self.daemon(LONE))

    def test_a_stale_startup_argument_never_hides_a_thread_from_the_fallback(self):
        self.db.execute("UPDATE threads SET cwd='/other/Documents', created_at_ms=650000 WHERE id=?", (LONE,))
        self.argv[11] = [b'codex', b'resume', LONE.encode()]  # pane 1 started on LONE, then ran /clear
        self.titles = {'%11': 'Documents', '%12': 'Documents'}  # older pane 2 ran /resume LONE; its footer is cut off
        entries = self.bind(('%11', 11, 700_000), ('%12', 12, 600_000), processes=self.daemon(LONE))
        self.assertIn(LONE, entries['%12'].get('codex_candidates', []))
        with self.assertRaisesRegex(RuntimeError, 'cannot'):
            self.send_check(entries['%12'], {'%11': (11, 700_000), '%12': (12, 600_000)}, processes=self.daemon(LONE))

    def test_a_shown_thread_id_outranks_an_older_startup_argument(self):
        self.items = ['activity', 'thread-id', 'project-name']
        self.argv[11] = [b'codex', b'resume', LONE.encode()]
        self.titles = {'%12': LONE[:29] + '... | Documents'}  # tmux gives no title for pane 1
        entries = self.bind(('%11', 11, 700_000), ('%12', 12, 800_000), processes=self.daemon(LONE))
        self.assertEqual((entries['%12']['thread'], entries['%12']['codex_binding']), (LONE, 'title'))
        self.send_check(entries['%12'], {'%11': (11, 700_000), '%12': (12, 800_000)}, processes=self.daemon(LONE))

    def test_an_unnamed_thread_of_another_folder_in_the_project_blocks_a_fresh_send(self):
        self.db.execute("UPDATE threads SET cwd='/work/Repo/lib' WHERE id=?", (LONE,))
        self.titles = {'%11': 'Repo'}  # the pane runs in /work/Repo/app and resumed LONE in the TUI
        entry = self.bind(('%11', 11, 800_000), processes=self.daemon(LONE))['%11']
        self.assertEqual((entry['pending_conversation'], entry['codex_candidates']), (True, [LONE]))

    def test_one_name_in_two_folders_is_a_tie_under_the_daemon(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.db.execute("UPDATE threads SET cwd='/other/Documents' WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Fix tests | Documents'}  # switched to the /other/Documents thread in the TUI
        entry = self.bind(('%11', 11, 50_000), processes=self.daemon(DAEMON, GAMMA))['%11']
        self.assertEqual(sorted(entry['codex_candidates']), sorted([DAEMON, GAMMA]))
        with self.assertRaisesRegex(RuntimeError, 'cannot tell'):
            self.send_check(dict(entry), {'%11': (11, 50_000)}, processes=self.daemon(DAEMON, GAMMA))

    def test_a_thread_of_another_folder_matches_by_its_own_project(self):
        self.db.execute("UPDATE threads SET cwd='/work/Repo/lib' WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Reply with gamma | Repo'}  # the pane runs in /srv/u/Documents and opened GAMMA in the TUI
        entry = self.bind(('%11', 11, 50_000), processes=self.daemon(DAEMON, GAMMA))['%11']
        self.assertEqual((entry['thread'], entry['codex_binding'], entry['codex_title_read']), (GAMMA, 'title', True))
        self.send_check(entry, {'%11': (11, 50_000)}, processes=self.daemon(DAEMON, GAMMA))

    def test_a_name_that_starts_with_a_spinner_frame(self):
        self.db.execute("UPDATE threads SET name=? WHERE id=?", ('\u2819 Fix tests', DAEMON))
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id=?", (GAMMA,))
        self.items = ['thread-name', 'project-name']  # no activity item: the frame is part of the name
        self.titles = {'%11': '\u2819 Fix tests | Documents'}
        self.assertEqual(self.bind(('%11', 11, 50_000))['%11']['thread'], DAEMON)
        self.items = None  # the default layout: an idle "\u2819 Fix tests" and a busy "Fix tests" look the same
        self.assertTrue(self.bind(('%11', 11, 50_000))['%11']['pending_conversation'])

    def test_a_loaded_thread_behind_many_newer_rows_stays_a_candidate(self):
        extra = [(f'01a1eb00-0000-7000-8000-0000000000{n:02}', f'/r/x{n}.jsonl', '/srv/u/Documents', 0, 'cli', 100_000, 500_000 + n, None)
                 for n in range(60)]
        self.db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', extra)
        self.items = ['project-name']
        self.titles = {'%11': 'Documents', '%12': 'Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000), processes=self.daemon(LONE, DAEMON))
        self.assertEqual(entries['%11']['codex_candidates'], [DAEMON, LONE])
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))  # no daemon: every row since the start
        self.assertEqual(len(entries['%11']['codex_candidates']), 63)

    def test_pane_titles_reads_every_pane_once(self):
        self.titles = {'%1': 'a | b', '%2': 'c'}
        self.assertEqual(agents.pane_titles(self.run_tmux), {'%1': 'a | b', '%2': 'c'})

    def daemon(self, *threads):
        """A Codex app-server (pid 500) that holds these threads loaded."""
        for thread in (DAEMON, GAMMA, LONE):
            self.db.execute("UPDATE threads SET rollout_path='/r/rollout-' || id || '.jsonl' WHERE id=?", (thread,))
        self.argv[500] = [b'codex', b'app-server', b'--listen', b'unix://', b'--managed-daemon']
        loaded = [(str(index), f'/r/rollout-{thread}.jsonl') for index, thread in enumerate(threads)]
        patcher = patch.object(procinfo, 'fd_targets', lambda pid: loaded if pid == 500 else [])
        patcher.start()
        self.addCleanup(patcher.stop)
        return {500: (1, 'codex'), 11: (2, 'codex'), 12: (3, 'codex')}

    def test_read_title_follows_the_codex_format(self):
        cwd = '/srv/u/Documents'
        self.assertEqual(agents.read_title('\u2819 Reply with beta | Documents', cwd),
                         {('name', 'Reply with beta'), ('name', '\u2819 Reply with beta')})  # busy, or a name with a frame
        self.assertEqual(agents.read_title('\u2819 Documents', cwd), {('name', None)})
        self.assertEqual(agents.read_title('Fix | notes | Documents', cwd), {('name', 'Fix | notes')})
        self.assertEqual(agents.read_title('Documents | Documents', cwd), {('name', 'Documents')})
        self.assertEqual(agents.read_title('Plan | u', '/srv/u/Documents'), {('name', 'Plan')})  # the git root is a parent
        self.assertEqual(agents.read_title('[ ! ] Action Required | Plan | Documents', cwd),
                         {('name', 'Plan'), ('name', '[ ! ] Action Required | Plan')})
        self.assertEqual(agents.read_title('\u25cf \u2819 Plan | Documents', cwd), {('name', 'Plan'), ('name', '\u25cf \u2819 Plan')})
        self.assertIsNone(agents.read_title('vim notes.txt', cwd))
        self.items = ['project-name', 'activity', 'thread-title']  # a layout Tether cannot read
        self.assertIsNone(agents.read_title('Documents \u2819 Plan', cwd))
        self.items = ['thread-name', 'project']
        self.assertEqual(agents.read_title('Plan | Documents', cwd), {('name', 'Plan')})

    def test_two_threads_with_one_name_wait_and_fail_the_send_check(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.titles = {'%11': 'Fix tests | Documents'}
        entry = self.bind(('%11', 11, 50_000))['%11']
        self.assertTrue(entry['pending_conversation'])
        self.assertEqual(entry['codex_candidates'], [GAMMA, DAEMON])
        bound = {'id': '%11', 'owner_pid': 11, 'process_start': '50000', 'cwd': '/srv/u/Documents', 'thread': DAEMON}
        with self.assertRaises(RuntimeError):
            self.send_check(bound, {'%11': (11, 50_000)})

    def test_the_daemon_settles_a_shared_name(self):
        self.db.execute("UPDATE threads SET name='Fix tests' WHERE id IN (?,?)", (DAEMON, GAMMA))
        self.titles = {'%11': 'Fix tests | Documents'}
        entry = self.bind(('%11', 11, 50_000), processes=self.daemon(DAEMON))['%11']
        self.assertEqual((entry['thread'], entry['codex_binding']), (DAEMON, 'title'))

    def test_a_shared_name_in_another_folder_is_not_a_tie(self):
        self.db.execute("UPDATE threads SET name='Reply with beta', cwd='/srv/u/notes' WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Reply with beta | Documents'}
        entry = self.bind(('%11', 11, 50_000), processes=self.daemon(DAEMON, GAMMA))['%11']
        self.assertEqual((entry['thread'], entry['codex_binding']), (DAEMON, 'title'))

    def test_codex_whitespace_and_hidden_characters_still_match(self):
        self.db.execute("UPDATE threads SET name=? WHERE id=?", ('  Fix\u200b   the\ttests ', DAEMON))
        self.titles = {'%11': 'Fix the tests | Documents'}
        self.assertEqual(self.bind(('%11', 11, 50_000))['%11']['thread'], DAEMON)

    def test_a_long_name_matches_its_cut_form(self):
        name = 'Find why the nightly build drops every second artifact upload'
        self.db.execute("UPDATE threads SET name=? WHERE id=?", (name, DAEMON))
        self.titles = {'%11': name[:45] + '... | Documents'}
        self.assertEqual(self.bind(('%11', 11, 50_000))['%11']['thread'], DAEMON)

    def test_a_named_title_without_a_thread_never_falls_back_to_a_guess(self):
        self.titles = {'%11': 'Reply  with  delta | Documents'}
        entry = self.bind(('%11', 11, 50_000))['%11']
        self.assertTrue(entry['pending_conversation'])
        self.assertNotIn('codex_candidates', entry)

    def test_names_with_separators_and_folder_names(self):
        self.db.execute("UPDATE threads SET name='Fix | notes' WHERE id=?", (DAEMON,))
        self.db.execute("UPDATE threads SET name='notes' WHERE id=?", (GAMMA,))
        self.db.execute("UPDATE threads SET name='Documents' WHERE id=?", (LONE,))
        self.titles = {'%11': 'Fix | notes | Documents', '%12': 'Documents | Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertEqual((entries['%11']['thread'], entries['%12']['thread']), (DAEMON, LONE))

    def test_clear_after_resume_of_an_unnamed_thread_drops_the_old_thread(self):
        self.titles = {'%11': 'Documents'}
        self.argv[11] = [b'codex', b'resume', LONE.encode()]
        self.assertEqual(self.bind(('%11', 11, 500_000), processes=self.daemon(LONE))['%11']['thread'], LONE)
        self.daemon()  # after /clear the daemon unloads the old thread
        entry = self.bind(('%11', 11, 500_000), processes={500: (1, 'codex'), 11: (2, 'codex')})['%11']
        self.assertTrue(entry['pending_conversation'])

    def test_an_open_rollout_outranks_an_older_panes_title(self):
        self.db.execute("UPDATE threads SET rollout_path='/r/rollout-gamma.jsonl' WHERE id=?", (GAMMA,))
        self.titles = {'%11': 'Reply with gamma | Documents'}
        with patch.object(procinfo, 'fd_targets', lambda pid: [('7', '/r/rollout-gamma.jsonl')] if pid == 12 else []):
            entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertEqual((entries['%12']['thread'], entries['%12']['codex_binding']), (GAMMA, 'open'))
        self.assertTrue(entries['%11']['pending_conversation'])

    def test_every_candidate_is_kept(self):
        extra = [(f'01a1eb00-0000-7000-8000-00000000000{n}', f'/r/x{n}.jsonl', '/srv/u/Documents', 0, 'cli', 100_000, 100_000 + n, None)
                 for n in range(6)]
        self.db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', extra)
        self.items = ['project-name']
        self.titles = {'%11': 'Documents', '%12': 'Documents'}
        entries = self.bind(('%11', 11, 50_000), ('%12', 12, 60_000))
        self.assertEqual(len(entries['%11']['codex_candidates']), 9)


class CodexTerminalNoiseTests(unittest.TestCase):
    def test_slash_reply_does_not_collect_user_prompt_or_spinner(self):
        before = 'Ready\n› /goal\n  GPT medium · Context 7% used'
        after = 'Ready\nGoal started\n› See I started goal in codex\n  but it is missing in Tether\n• Working (1s • esc to interrupt)\n› Ask Codex to do anything\n  GPT medium · Context 7% used'
        self.assertEqual(terminal_replies.unwrap(terminal_replies.added_lines(before, after)), 'Goal started')

    def test_command_starting_a_turn_does_not_duplicate_transcript_output(self):
        screen = '› /goal Finish cleanup\n• I will check the code.\n• Working (1s • esc to interrupt)'
        with patch.object(terminal_replies, 'add') as add:
            self.assertIsNone(terminal_replies.record({}, '', lambda _: screen, sleep=lambda _: None))
        add.assert_not_called()

    def test_old_polluted_notices_are_hidden_without_changing_saved_data(self):
        notices = [{'id': 'terminal:bad', 'role': 'assistant', 'time': '2026-11-06T00:00:00Z', 'text': 'partial input Working (1s • esc to interrupt)'},
                   {'id': 'terminal:ok', 'role': 'assistant', 'time': '2026-11-06T00:00:00Z', 'text': 'Goal started'}]
        with patch.object(terminal_replies, 'saved', return_value=notices):
            self.assertEqual([m['id'] for m in terminal_replies.merge({'agent': 'codex'}, [])], ['terminal:ok'])
        self.assertEqual(len(notices), 2)


class CodexSendTests(unittest.TestCase):
    def test_upload_paths_are_messages_and_only_commands_capture_terminal_replies(self):
        import server
        for message, expected in (('/uploads/shot.png See this bug', False), ('/fixtures/screenshots/shot.png See this', False), ('/goal Finish this task', True), ('/status', True), ('/model', False)):
            with self.subTest(message=message), patch.object(server, 'submit') as submit, \
                    patch.object(server, 'capture', return_value='') as capture, \
                    patch.object(server.threading, 'Thread') as thread:
                server.send_text({'agent': 'codex', 'id': '%fixture'}, message, state='Idle')
                self.assertEqual(capture.called, expected)
                self.assertEqual(thread.called, expected)
                submit.assert_called_once_with(message, {'agent': 'codex', 'id': '%fixture'}, user=False)
