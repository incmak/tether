"""Review folder selection with a private gate log and real Git worktrees."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import review_links
import review_scope
import server
from own_time import own_time


SESSION = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'


class ReviewFolderTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = root / 'gate.log'
        self.enterContext(patch.object(server.paths, 'CACHE', root / 'cache'))
        self.start = root / 'start'
        self.work = root / 'other worktree'
        self.git('init', '-q', '-b', 'main', str(self.start), cwd=root)
        self.start_check = self.gate_fields(self.start, 'Start', unborn=True)
        self.git('commit', '-qm', 'Start', '--allow-empty')
        self.start_head = self.git('rev-parse', 'HEAD')
        self.git('worktree', 'add', '-qb', 'work', str(self.work))
        self.work_check = self.gate_fields(self.work, 'Work')
        self.git('commit', '-qm', 'Work', '--allow-empty', cwd=self.work)
        self.entry = {'id': '%13', 'agent': 'claude', 'thread': SESSION, 'cwd': str(self.start),
                      'process_start': '1234'}
        self.enterContext(patch.object(server.paths, 'COMMIT_GATE_LOG', self.log))
        self.enterContext(patch.object(server.agents, 'started_ms', return_value=100_000))

    def gate_fields(self, folder, message='Aborted attempt', *, unborn=False):
        path = Path(self.git('rev-parse', '--absolute-git-dir', cwd=folder))
        info = path.stat()
        key = server.commit_gate._message_key(create=True)
        return {'folder': str(folder), 'head': None if unborn else self.git('rev-parse', 'HEAD', cwd=folder),
                'tree': self.git('write-tree', cwd=folder), 'reflog': server.commit_gate._head_reflog_position(folder),
                'git_dir': {'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino},
                'messages': [server.commit_gate._message_digest(key, message)]}

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                              cwd=cwd or self.start, capture_output=True, check=True, text=True).stdout.strip()

    def test_repo_identity_is_shared_by_worktrees_and_changes_with_the_repository(self):
        # B368: auto-merge compares the repository a review saw with the live one.
        found = review_scope.repo_identity(str(self.work))
        self.assertEqual(found, review_scope.repo_identity(str(self.start)))
        self.assertEqual(found['path'], str((self.start / '.git').resolve()))
        common = self.start / '.git'
        common.rename(self.start / '.git-aside')
        shutil.copytree(self.start / '.git-aside', common, symlinks=True)
        moved = review_scope.repo_identity(str(self.work))
        self.assertEqual(moved['path'], found['path'])
        self.assertNotEqual(moved, found)  # the same path, another directory
        for folder in (None, '', str(self.start.parent), str(self.start.parent / 'missing')):
            with self.subTest(folder=folder):
                self.assertIsNone(review_scope.repo_identity(folder))

    def line(self, **fields):
        return (json.dumps({'event': 'check', 'via': 'commit-msg', 'source': 'claude', 'session': SESSION,
                            'at': 101, **self.work_check, **fields}) + '\n').encode()

    def test_aborted_new_request_uses_older_landed_worktree(self):
        self.entry['agent'] = 'codex'
        self.log.write_bytes(self.line(source='codex') + self.line(source='codex', at=102,
                                                                  **self.gate_fields(self.start)))
        self.assertEqual(server.review_folder(self.entry), str(self.work))

    def test_aborted_follow_up_uses_older_landed_worktree(self):
        entry = {**self.entry, 'review_after': 100, 'review_folder': str(self.start)}
        self.log.write_bytes(self.line() + self.line(at=102, **self.gate_fields(self.start)))
        self.assertEqual(server.review_folder(entry), str(self.work))
        self.assertEqual(server.review_folder({**entry, 'review_after': 101}), str(self.start))

    def test_aborted_amend_binds_only_after_it_lands(self):
        self.log.write_bytes(self.line(**self.start_check)
                             + self.line(at=102, **self.gate_fields(self.work, 'Amended work')))
        self.assertEqual(server.review_folder(self.entry), str(self.start))
        self.git('commit', '--amend', '--allow-empty', '-qm', 'Amended work', cwd=self.work)
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        with patch.object(server.commit_gate, 'record_landed', wraps=server.commit_gate.record_landed) as landed:
            self.assertEqual(server.review_folder(self.entry), str(self.work))
            landed.assert_not_called()

    def test_switch_at_same_head_after_aborted_amend_uses_both_review_fallbacks(self):
        older = self.work.parent / 'older'
        self.git('worktree', 'add', '-qb', 'older', str(older))
        older_check = self.gate_fields(older, 'Older landed commit')
        self.git('commit', '--allow-empty', '-qm', 'Older landed commit', cwd=older)

        self.git('branch', 'same-head', cwd=self.work)
        check = self.gate_fields(self.work)
        hooks = self.work.parent / 'hooks'
        hooks.mkdir()
        abort = hooks / 'commit-msg'
        abort.write_text('#!/bin/sh\nexit 1\n')
        abort.chmod(0o755)
        self.assertRaises(subprocess.CalledProcessError, self.git, '-c', f'core.hooksPath={hooks}',
                          'commit', '--amend', '-qm', 'Aborted message-only amend', cwd=self.work)
        self.git('switch', '-q', 'same-head', cwd=self.work)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.work), check['head'])
        self.log.write_bytes(self.line(**older_check) + self.line(at=102, **check))
        follow_up = {**self.entry, 'review_after': 100, 'review_folder': str(self.start)}
        with self.subTest('landing evidence'):
            self.assertIsNone(server.commit_gate.record_landed(check))
        for entry in (self.entry, follow_up):
            with self.subTest(follow_up='review_after' in entry):
                self.assertEqual(server.review_folder(entry), str(self.start))

    def test_replacement_worktree_is_rejected_with_warm_and_cold_cache(self):
        self.log.write_bytes(self.line())
        self.assertEqual(server.review_folder(self.entry), str(self.work))  # Warm the cache.
        self.git('worktree', 'move', str(self.work), str(self.work.parent / 'moved'))
        self.git('worktree', 'add', '-qb', 'replacement', str(self.work), 'work')
        # Git kept the original metadata directory intact. Stat alone still finds the old inode.
        identity = self.work_check['git_dir']
        self.assertEqual(Path(identity['path']).stat().st_ino, identity['ino'])
        with patch.object(server.commit_gate, '_git', wraps=server.commit_gate._git) as git:
            self.assertEqual(server.review_folder(self.entry), str(self.start))
            git.assert_not_called()
        with patch.object(server, 'REVIEW_GATE_LANDED', {}):
            self.assertEqual(server.review_folder(self.entry), str(self.start))
        self.assertIsNone(server.commit_gate.record_landed(self.work_check))

    def test_failed_tree_lookup_uses_fallback_even_with_older_confirmed_row(self):
        older = self.work.parent / 'older'
        self.git('worktree', 'add', '-qb', 'older', str(older))
        check = self.gate_fields(older, 'Older landed commit')
        self.git('commit', '--allow-empty', '-qm', 'Older landed commit', cwd=older)
        self.log.write_bytes(self.line(**check) + self.line(at=102))
        git = server.commit_gate._git

        def fail_tree_lookup(folder, *args, **kwargs):
            if str(folder) == str(self.work) and (args[0] == 'log' and '--stdin' in args
                                                 or args[-1].endswith('^{tree}')):
                return None
            return git(folder, *args, **kwargs)

        with patch.object(server.commit_gate, '_git', side_effect=fail_tree_lookup):
            with self.subTest('landing evidence'):
                self.assertIsNone(server.commit_gate.record_landed(self.work_check))
            with self.subTest('review folder'):
                self.assertEqual(server.review_folder(self.entry), str(self.start))

    def test_review_batches_tree_lookups_across_branch_switches(self):
        (self.work / 'staged.txt').write_text('Staged for a commit that never happens\n')
        self.git('add', 'staged.txt', cwd=self.work)
        self.git('branch', 'alternate', cwd=self.work)
        check = self.gate_fields(self.work)
        for branch in ('alternate', 'work') * 25:
            self.git('switch', '-q', branch, cwd=self.work)
        self.log.write_bytes(b''.join(self.line(at=101 + offset, **check) for offset in range(8)))
        with patch.object(server.commit_gate, '_git', wraps=server.commit_gate._git) as git:
            self.assertEqual(server.review_folder(self.entry), str(self.start))
            self.assertLessEqual(git.call_count, 3 * 8)
            batches = [call.kwargs['input'].splitlines() for call in git.call_args_list if '--stdin' in call.args]
            self.assertEqual(batches, [[check['head'].encode('ascii')]] * 8)

    def test_in_flight_root_commit_can_land_after_an_unknown_result(self):
        new_worktree = self.work.parent / 'unborn'
        self.git('init', '-q', '-b', 'main', str(new_worktree))
        check = self.gate_fields(new_worktree, 'Root commit', unborn=True)
        self.log.write_bytes(self.line(**check))
        self.assertIsNone(server.commit_gate.record_landed(check))
        with patch.object(server.commit_gate, 'record_landed', wraps=server.commit_gate.record_landed) as landed:
            self.assertEqual(server.review_folder(self.entry), str(self.start))
            self.git('commit', '--allow-empty', '-qm', 'Root commit', cwd=new_worktree)
            self.assertEqual(server.review_folder(self.entry), str(new_worktree))
            self.assertEqual(landed.call_count, 2)

    def test_in_flight_commit_has_no_scope_until_it_lands(self):
        self.check_in_flight_commit(land=True)

    def test_in_flight_commit_that_exits_without_landing_uses_older_row(self):
        self.check_in_flight_commit(land=False)

    def test_landing_cache_does_not_confirm_a_same_time_row_with_another_message(self):
        self.log.write_bytes(self.line())
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        self.log.write_bytes(self.line(messages=['0' * 32]))
        self.assertEqual(server.review_folder(self.entry), str(self.start))

    def check_in_flight_commit(self, *, land):
        pending = self.work.parent / 'pending'
        self.git('worktree', 'add', '-qb', 'pending', str(pending))
        self.log.write_bytes(self.line())
        fifo = self.work.parent / 'release-hook'
        os.mkfifo(fifo)
        hooks = self.work.parent / 'hooks'
        hooks.mkdir()
        gate = hooks / 'gate.py'
        gate.write_text(
            f'import sys\nsys.path.insert(0, {str(Path(server.commit_gate.__file__).parent)!r})\n'
            f'import commit_gate\ncommit_gate._source_session = lambda *_: ("claude", {SESSION!r})\n'
            'commit_gate.commit_msg(sys.argv[1])\n'
        )
        hook = hooks / 'commit-msg'
        hook.write_text(
            f'#!/bin/sh\n{shlex.quote(sys.executable)} {shlex.quote(str(gate))} "$1" || exit 2\n'
            f'printf "ready\\n"\nread release < {shlex.quote(str(fifo))} || exit 2\nexit "$release"\n'
        )
        hook.chmod(0o755)
        env = dict(os.environ, MOBILE_TMUX_CACHE=str(server.paths.CACHE),
                   TETHER_COMMIT_GATE_LOG=str(self.log))
        follow_up = {**self.entry, 'review_after': 100, 'review_folder': str(self.work)}
        with subprocess.Popen(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={hooks}',
                               'commit', '--allow-empty', '-m', 'Pending commit'], cwd=pending, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) as git:
            self.assertIn('ready\n', git.stderr)
            with fifo.open('w') as release:
                row = json.loads(self.log.read_bytes().splitlines()[-1])
                self.assertEqual(row['git_pid'], git.pid)
                self.assertEqual(server.commit_gate.record_landed(row), 'pending')
                started = row['git_started']
                wrong_start = started - 1 if isinstance(started, float) else str(started) + '0'
                self.assertIs(server.commit_gate.record_landed({**row, 'git_started': wrong_start}), False)
                for entry in (self.entry, follow_up):
                    with self.subTest(follow_up='review_after' in entry):
                        self.assertIsNone(server.review_folder(entry))
                        self.assertIsNone(server.review_scope_of(entry))
                        self.assertIsNone(server.review_scope_of(entry, light=True))
                        self.assertIsNone(server.review_commit(entry))
                        self.assertIsNone(server.review_work_mark(entry))
                if land:
                    read_entries = server.commit_gate._head_reflog_entries

                    def finish_after_reflog_read(*args):
                        entries = read_entries(*args)
                        release.write('0\n')
                        release.flush()
                        self.assertEqual(git.wait(), 0)
                        return entries

                    with patch.object(server.commit_gate, '_head_reflog_entries', side_effect=finish_after_reflog_read):
                        self.assertEqual(server.commit_gate.record_landed(row), 'pending')
                else:
                    release.write('1\n')
            self.assertEqual(git.wait(), 0 if land else 1)
        self.assertIs(server.commit_gate.record_landed(row), land)
        expected = pending if land else self.work
        for entry in (self.entry, follow_up):
            self.assertEqual(server.review_folder(entry), str(expected))
            self.assertEqual(server.review_scope_of(entry)['commit'], self.git('rev-parse', 'HEAD', cwd=expected))

    def test_review_walk_shares_one_git_deadline(self):
        check = self.gate_fields(self.start)
        self.log.write_bytes(self.line() + b''.join(self.line(at=102 + offset, **check) for offset in range(7)))
        elapsed = 0.0
        git = server.commit_gate._git

        def advance_command(*args, **kwargs):
            nonlocal elapsed
            result = git(*args, **kwargs)
            elapsed += 0.3
            return result

        with (own_time(server, monotonic=lambda: elapsed),
              own_time(server.commit_gate, monotonic=lambda: elapsed),
              patch.object(server.commit_gate, '_git', side_effect=advance_command)):
            self.assertEqual(server.review_folder(self.entry), str(self.start))

    def test_expired_landing_deadline_skips_git(self):
        with own_time(server.commit_gate, monotonic=lambda: 3), patch.object(server.commit_gate, '_git') as git:
            self.assertIsNone(server.commit_gate.record_landed(self.work_check, deadline=3))
            git.assert_not_called()

    def test_rebased_commit_still_binds_after_another_attempt_aborts(self):
        (self.work / 'changed.txt').write_text('Work\n')
        self.git('add', 'changed.txt', cwd=self.work)
        landed = self.line(**self.gate_fields(self.work, 'Changed work'))
        self.git('commit', '-qm', 'Changed work', cwd=self.work)
        old_commit = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.git('commit', '-qm', 'Advance the base', '--allow-empty')
        self.git('rebase', 'main', cwd=self.work)
        rebased = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.assertNotEqual(rebased, old_commit)
        self.log.write_bytes(landed + self.line(at=102, **self.gate_fields(self.start)))
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        self.assertEqual(server.review_commit(self.entry), rebased)

    def test_legacy_row_falls_back_instead_of_using_older_landed_row(self):
        legacy = json.loads(self.line(at=102))
        del legacy['reflog']
        self.log.write_bytes(self.line() + (json.dumps(legacy) + '\n').encode())
        self.assertEqual(server.review_folder(self.entry), str(self.start))
        entry = {**self.entry, 'review_after': 100, 'review_folder': str(self.start)}
        self.assertEqual(server.review_folder(entry), str(self.start))

    def test_keeps_only_the_eight_newest_records_per_identity(self):
        aborted = self.gate_fields(self.start)
        self.log.write_bytes(self.line() + b''.join(self.line(at=at, **aborted) for at in range(108, 101, -1)))
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        with self.log.open('ab') as stream:
            stream.write(self.line(at=109, **aborted))
        self.assertEqual(server.review_folder(self.entry), str(self.start))

    def test_missing_corrupt_unmatched_and_unreadable_log_fall_back(self):
        self.log.write_bytes(self.line())
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        self.log.unlink()
        self.assertEqual(server.review_folder(self.entry), str(self.start))
        for data in (b'\xff\n{broken\n', self.line(session='some-other-session'),
                     self.line(folder=str(self.work.parent / 'removed')),
                     self.line(folder=str(self.work.parent))):
            with self.subTest(data=data):
                self.log.write_bytes(data)
                self.assertEqual(server.review_folder(self.entry), str(self.start))
        # A valid latest record with a removed folder must fall back, even if an older folder still exists.
        self.log.write_bytes(self.line() + self.line(at=102, folder=str(self.work.parent / 'removed')))
        self.assertEqual(server.review_folder(self.entry), str(self.start))

    def test_newest_record_wins_for_codex_and_the_worker_can_return_to_start(self):
        self.entry['agent'] = 'codex'
        self.log.write_bytes(self.line(source='codex', at=103) + self.line(source='codex', at=102, **self.start_check))
        work_head = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.assertEqual(server.review_commit(self.entry), work_head)
        check = self.gate_fields(self.start, 'Back in the start folder')
        self.git('commit', '-qm', 'Back in the start folder', '--allow-empty')
        with self.log.open('ab') as stream:
            stream.write(self.line(source='codex', at=104, **check))
        self.assertEqual(server.review_commit(self.entry), self.git('rev-parse', 'HEAD'))

    def test_new_request_ignores_blocked_commit_but_accepts_observe_mode_record(self):
        self.log.write_bytes(self.line(**self.start_check) + self.line(at=102, verdict='block'))
        self.assertEqual(server.review_folder(self.entry), str(self.start))
        with self.log.open('ab') as stream:
            stream.write(self.line(at=103, verdict='would_block'))
        self.assertEqual(server.review_folder(self.entry), str(self.work))

    def test_ignores_partial_malformed_and_wrong_type_records(self):
        bad = b'{bad json\n[]\n' + b''.join(self.line(**{'at': 104, 'folder': str(self.start), **fields}) for fields in (
            {'folder': []}, {'source': []}, {'session': []}, {'at': True}, {'at': '104'}, {'at': float('nan')},
            {'head': 42}, {'via': 'post-tool-use'}, {'source': 'codex'},
        ))
        self.log.write_bytes(self.line() + bad + self.line(at=105, folder=str(self.start)).rstrip(b'\n'))
        self.assertEqual(server.review_folder(self.entry), str(self.work))

    def test_tail_is_bounded_and_cached_until_metadata_changes(self):
        # The valid row at the front must never be found through a long run of malformed data.
        self.log.write_bytes(self.line() + b'x' * 2_000_000 + b'\n' + self.line(at=100, **self.start_check))
        with patch('server.open', wraps=open, create=True) as opened, self.log.open('rb') as stream, \
                patch.object(stream, 'read', wraps=stream.read) as read:
            opened.return_value = stream
            self.assertEqual(server.review_folder(self.entry), str(self.start))
            self.assertEqual(server.review_folder(self.entry), str(self.start))
            opened.assert_called_once()
            read.assert_called_once()
            self.assertEqual(read.call_args.args[0], 256 * 1024)
        with self.log.open('ab') as stream:
            stream.write(self.line())
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        info = self.log.stat()
        self.log.write_bytes(self.log.read_bytes().replace(self.line(), self.line(session='x' * len(SESSION))))
        self.assertEqual(self.log.stat().st_size, info.st_size)
        os.utime(self.log, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        self.assertEqual(server.review_folder(self.entry), str(self.start))

    def test_resumed_or_unknown_sessions_do_not_inherit_a_commit_folder(self):
        self.log.write_bytes(self.line(at=99) + self.line(at=100))
        self.assertEqual(server.review_folder(self.entry), str(self.start))
        with self.log.open('ab') as stream:
            stream.write(self.line(at=100.001))
        self.assertEqual(server.review_folder(self.entry), str(self.work))
        for fields in ({'agent': 'antigravity'}, {'thread': 'pending-13-1234'}, {'pending_conversation': True},
                       {'process_start': ''}, {'codex_unresolved': True}):
            with self.subTest(fields=fields):
                self.assertEqual(server.review_folder({**self.entry, **fields}), str(self.start))
        with patch.object(server.agents, 'started_ms', side_effect=ValueError('Invalid process start')):
            self.assertEqual(server.review_folder(self.entry), str(self.start))

    def test_request_checklist_and_approval_use_the_commit_worktree(self):
        self.entry['writable'] = True
        reviewer = {'id': '%12', 'agent': 'codex', 'thread': '0f8fad5b-d9cb-469f-a165-70867728950e', 'writable': True}
        root = self.work.parent
        self.enterContext(patch.object(review_links, 'FILE', root / 'review-links.json'))
        self.enterContext(patch.object(review_links, '_last_sent', {}))
        self.enterContext(patch.object(review_links.audit, 'record'))
        self.enterContext(patch.object(server, 'REVIEW_SCOPES', {}))
        review_links.add(worker=self.entry['id'], reviewer=reviewer['id'], reviewer_thread=reviewer['thread'],
                         name='Fix the bug', agent='claude', worker_thread=SESSION)
        (self.work / 'REVIEW.md').write_text('Review rules\n')
        (self.work / 'docs').mkdir()
        (self.work / 'docs/reliability-checklist.md').write_text(
            '## R1: Check the changed file\n- Paths: ^changed.txt$\n- Check: Read the diff.\n- Incident: B363\n')
        self.git('add', '-A', cwd=self.work)
        self.git('commit', '-qm', 'Review rules and checklist', cwd=self.work)
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD', cwd=self.work)
        (self.work / 'changed.txt').write_text('Fixed\n')
        self.git('add', 'changed.txt', cwd=self.work)
        check = self.gate_fields(self.work, 'Fix the bug')
        self.git('commit', '-qm', 'Fix the bug', cwd=self.work)
        work_head = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.log.write_bytes(self.line(**check))

        send = Mock()
        answer = None

        def observe(worker_state='Idle'):
            review_links.observe([self.entry, reviewer],
                                 lambda entry: {'state': worker_state if entry is self.entry else 'Idle'},
                                 send, lambda entry: 'I fixed it.', review_reply=lambda entry, request: answer,
                                 scope=server.review_scope_of, commit=server.review_commit,
                                 work_mark=server.review_work_mark)

        with own_time(review_links, monotonic=Mock(side_effect=range(0, 1200, 10))):
            observe('Working')
            observe()
            text = send.call_args.args[1]
            self.assertIn(f"Commit under review: {work_head} in '{self.work}'", text)
            self.assertIn(f"git -C '{self.work}' show origin/main:REVIEW.md", text)
            self.assertIn('R1 Check the changed file: Read the diff.', text)
            saved = json.loads(review_links.FILE.read_text())
            link = next(row for row in saved if row.get('reviewer') == reviewer['id'])
            self.assertEqual(link['work_mark'], server.review_work_mark({'cwd': str(self.work)}))
            # Once sent, the approval still checks the worktree named in the request.
            with self.log.open('ab') as stream:
                stream.write(self.line(at=102, **self.start_check))
            answer = f'APPROVED {work_head}'
            observe()
            self.assertEqual(send.call_args.args[0], self.entry)
            self.assertIn(f"git -C '{self.work}' rev-parse HEAD", send.call_args.args[1])
            approval = review_links.approved_commits()[self.entry['id']][reviewer['id']]
            self.assertEqual(approval['commit'], work_head)

            observe('Working')  # The approval's follow-up turn.
            observe()
            answer = None
            observe('Working')
            observe()
            self.assertIn(f'Commit under review: {self.start_head} in {self.start}.', send.call_args.args[1])

            # The folder that the request named has changed HEAD. A different worktree at the old hash
            # cannot make that approval current.
            self.git('commit', '-qm', 'More work', '--allow-empty')
            check = self.gate_fields(self.work, 'More work in the linked worktree')
            self.git('commit', '-qm', 'More work in the linked worktree', '--allow-empty', cwd=self.work)
            self.git('switch', '--detach', self.start_head, cwd=self.work)
            with self.log.open('ab') as stream:
                stream.write(self.line(at=103, **check))
            answer = f'APPROVED {self.start_head}'
            sent = send.call_count
            observe()
            self.assertEqual(send.call_count, sent)
            observe()
            self.assertEqual(send.call_args.args[0], reviewer)
            self.assertIn(f"Commit under review: {self.start_head} in '{self.work}'", send.call_args.args[1])

    def approve_dirty_then_commit(self, commit_in_review_folder):
        """Approve uncommitted work in the review folder, then commit either there or in a third worktree."""
        reviewer = {'id': '%12', 'agent': 'codex', 'thread': '0f8fad5b-d9cb-469f-a165-70867728950e', 'writable': True}
        root = self.work.parent
        self.entry['writable'] = True
        self.enterContext(patch.object(review_links, 'FILE', root / 'review-links.json'))
        self.enterContext(patch.object(review_links, '_last_sent', {}))
        self.enterContext(patch.object(review_links.audit, 'record'))
        self.enterContext(patch.object(server, 'REVIEW_SCOPES', {}))
        review_links.add(worker=self.entry['id'], reviewer=reviewer['id'], reviewer_thread=reviewer['thread'],
                         name='Fix the bug', agent='claude', worker_thread=SESSION)
        third = root / 'third worktree'
        self.git('worktree', 'add', '-qb', 'third', str(third))
        (self.work / 'changed.txt').write_text('Fixed\n')  # Uncommitted work in the review folder.
        work_head = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.log.write_bytes(self.line())
        send = Mock()
        answer = None

        def observe(worker_state='Idle'):
            review_links.observe([self.entry, reviewer],
                                 lambda entry: {'state': worker_state if entry is self.entry else 'Idle'},
                                 send, lambda entry: 'I fixed it.', review_reply=lambda entry, request: answer,
                                 scope=server.review_scope_of, commit=server.review_commit,
                                 work_mark=server.review_work_mark)

        with own_time(review_links, time=lambda: 101.5, monotonic=Mock(side_effect=range(0, 1200, 10))):
            observe('Working')
            observe()
            self.assertIn(f"Commit under review: {work_head} in '{self.work}'", send.call_args.args[1])
            answer = f'APPROVED {work_head}'
            observe()
            self.assertEqual(send.call_args.args[0], self.entry)  # The approval of uncommitted work reached the tab.
            answer = None
            if commit_in_review_folder:
                folder = self.work
                self.git('add', 'changed.txt', cwd=folder)
            else:
                folder = third
                (folder / 'other.txt').write_text('Other work\n')
                self.git('add', 'other.txt', cwd=folder)
                (self.work / 'changed.txt').unlink()
            check = self.gate_fields(folder, 'Commit')
            self.git('commit', '-qm', 'Commit', cwd=folder)
            with self.log.open('ab') as stream:
                stream.write(self.line(at=102, **check))
            observe('Working')
            observe()
            text = send.call_args.args[1]
            self.assertEqual(send.call_args.args[0], reviewer)
            self.assertIn(f"Commit under review: {self.git('rev-parse', 'HEAD', cwd=folder)} in '{folder}'", text)
            return work_head, text

    def test_confirm_note_only_for_the_folder_the_approval_covered(self):
        work_head, text = self.approve_dirty_then_commit(commit_in_review_folder=True)
        self.assertIn(f'You approved this work as commit {work_head[:12]} plus uncommitted changes.', text)

    def test_no_confirm_note_for_a_commit_in_another_worktree(self):
        work_head, text = self.approve_dirty_then_commit(commit_in_review_folder=False)
        self.assertNotIn('plus uncommitted changes', text)

    def test_idle_close_reads_scope_even_when_the_fingerprint_matches_cache(self):
        clean = dict(commit=self.start_head, dirty=False, folder=str(self.start))
        edited = dict(clean, dirty=True)
        with patch.object(server, 'REVIEW_SCOPES', {}), \
                patch.object(server.review_scope, 'fingerprint', return_value='same'), \
                patch.object(server.review_scope, 'scope', side_effect=[clean, edited]) as snapshot:
            self.assertFalse(server.review_scope_of(self.entry)['dirty'])
            self.assertFalse(server.review_scope_of(self.entry)['dirty'])
            snapshot.assert_called_once()
            self.assertTrue(server.HELPER_CLOSER.scope(self.entry)['dirty'])
            self.assertEqual(snapshot.call_count, 2)

    def test_idle_close_scope_omits_review_metadata(self):
        with patch.object(server.review_scope, 'checklist') as checklist, \
                patch.object(server.review_scope, 'ui_paths') as ui_paths:
            scope = server.HELPER_CLOSER.scope(self.entry)
        self.assertEqual(scope, dict(commit=self.start_head, dirty=False, folder=str(self.start),
                                     mark=server.review_scope.fingerprint(self.start)))
        checklist.assert_not_called()
        ui_paths.assert_not_called()
