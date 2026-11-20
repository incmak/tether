"""Review folder selection with a private gate log and real Git worktrees."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import review_links
import server
from own_time import own_time


SESSION = '17b9c72a-a916-46a0-bc10-bc41a8c6249a'


class ReviewFolderTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = root / 'gate.log'
        self.start = root / 'start'
        self.work = root / 'other worktree'
        self.git('init', '-q', '-b', 'main', str(self.start), cwd=root)
        self.git('commit', '-qm', 'Start', '--allow-empty')
        self.start_head = self.git('rev-parse', 'HEAD')
        self.git('worktree', 'add', '-qb', 'work', str(self.work))
        self.git('commit', '-qm', 'Work', '--allow-empty', cwd=self.work)
        self.entry = {'id': '%13', 'agent': 'claude', 'thread': SESSION, 'cwd': str(self.start),
                      'process_start': '1234'}
        self.enterContext(patch.object(server.paths, 'COMMIT_GATE_LOG', self.log))
        self.enterContext(patch.object(server.agents, 'started_ms', return_value=100_000))

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                              cwd=cwd or self.start, capture_output=True, check=True, text=True).stdout.strip()

    def line(self, **fields):
        return (json.dumps({'event': 'check', 'via': 'commit-msg', 'source': 'claude', 'session': SESSION,
                            'folder': str(self.work), 'at': 101, 'head': self.start_head, **fields}) + '\n').encode()

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
        self.log.write_bytes(self.line(source='codex', at=103) + self.line(source='codex', at=102, folder=str(self.start)))
        work_head = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.assertEqual(server.review_commit(self.entry), work_head)
        self.git('commit', '-qm', 'Back in the start folder', '--allow-empty')
        with self.log.open('ab') as stream:
            stream.write(self.line(source='codex', at=104, folder=str(self.start)))
        self.assertEqual(server.review_commit(self.entry), self.git('rev-parse', 'HEAD'))

    def test_new_request_ignores_blocked_commit_but_accepts_observe_mode_record(self):
        self.log.write_bytes(self.line(folder=str(self.start)) + self.line(at=102, verdict='block'))
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
        self.log.write_bytes(self.line() + b'x' * 2_000_000 + b'\n' + self.line(folder=str(self.start), at=100))
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
        base = self.git('rev-parse', 'HEAD', cwd=self.work)
        (self.work / 'changed.txt').write_text('Fixed\n')
        self.git('add', 'changed.txt', cwd=self.work)
        self.git('commit', '-qm', 'Fix the bug', cwd=self.work)
        work_head = self.git('rev-parse', 'HEAD', cwd=self.work)
        self.log.write_bytes(self.line(head=base))

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
                stream.write(self.line(at=102, folder=str(self.start)))
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
            self.git('switch', '--detach', self.start_head, cwd=self.work)
            with self.log.open('ab') as stream:
                stream.write(self.line(at=103))
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
            self.git('commit', '-qm', 'Commit', cwd=folder)
            with self.log.open('ab') as stream:
                stream.write(self.line(at=102, folder=str(folder)))
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
