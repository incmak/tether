"""B362 review gate checks with isolated Git, review store and process state."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import review_gate
import review_links


PANE = '%12'
APPROVED = 'a' * 40
ZERO = '0' * 40
MAIN = 'refs/heads/main'
TAG = 'refs/tags/v1'
GIT_ENV = {
    'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
    'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
    'GIT_OPTIONAL_LOCKS': '0',
}


def link(commit=APPROVED, *, worker=PANE, reviewer='%20'):
    row = {'worker': worker, 'reviewer': reviewer}
    if commit is not None:
        row['approved_commit'] = commit
    return row


def ref_line(local, remote, *, destination=MAIN, source='refs/heads/work'):
    return f'{source} {local} {destination} {remote}\n'


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review gate ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)


class PaneIdentityTest(TemporaryCase):
    def process(self, pid, parent, *, environ=b'', comm='shell'):
        folder = self.root / str(pid)
        folder.mkdir()
        (folder / 'stat').write_text(f'{pid} ({comm}) S {parent} 0 0 0\n')
        (folder / 'environ').write_bytes(environ)
        (folder / 'comm').write_text(f'{comm}\n')

    def identity(self, environ=None, pid=5):
        with patch.object(review_gate.os, 'getpid', return_value=pid):
            return review_gate.pane_identity({} if environ is None else environ, self.root)

    def test_environment_takes_precedence_over_proc(self):
        self.assertEqual(self.identity({'TMUX_PANE': '%123456'}), '%123456')

    def test_malformed_environment_is_refused(self):
        for pane in ('', '12', '%1234567', '%2\n', '%１２'):
            with self.subTest(pane=pane), self.assertRaisesRegex(review_gate.GateRefused, 'TMUX_PANE'):
                self.identity({'TMUX_PANE': pane})

    def test_ancestor_supplies_pane_when_environment_was_stripped(self):
        self.process(5, 4, comm='process ) name')
        self.process(4, 3, environ=b'NOT_TMUX_PANE=%88\0PATH=/usr/bin\0')
        self.process(3, 1, environ=b'PATH=/usr/bin\0TMUX_PANE=%23\0')
        self.assertEqual(self.identity(), '%23')

    def test_malformed_ancestor_value_is_refused(self):
        self.process(5, 4)
        self.process(4, 3, environ=b'TMUX_PANE=%ABC\0')
        self.process(3, 1, environ=b'TMUX_PANE=%23\0')
        with self.assertRaisesRegex(review_gate.GateRefused, 'TMUX_PANE'):
            self.identity()

    def test_sandbox_init_is_refused(self):
        self.process(5, 1)
        self.process(1, 0, comm='bwrap')
        with self.assertRaisesRegex(review_gate.GateRefused, 'sandbox'):
            self.identity()

    def test_stat_read_error_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            self.identity()

    def test_ancestor_environ_read_error_is_refused(self):
        self.process(5, 4)
        self.process(4, 1)
        (self.root / '4' / 'environ').unlink()
        self.process(1, 0, comm='systemd')
        with self.assertRaises(review_gate.GateRefused):
            self.identity()

    def hidden(self, *pids):
        """Make /proc/<pid>/environ raise EACCES, as sshd, cron and the systemd user manager do for their user."""
        real = Path.read_bytes
        names = {self.root / str(pid) / 'environ' for pid in pids}

        def read_bytes(path):
            if path in names:
                raise PermissionError(13, 'Permission denied', str(path))
            return real(path)
        return patch.object(Path, 'read_bytes', read_bytes)

    def test_hidden_ancestor_between_process_and_host_init_needs_no_pane(self):
        # git <- shell <- systemd --user (environ EACCES) <- systemd: a user service, cron or ssh login.
        self.process(5, 4)
        self.process(4, 3)
        self.process(3, 1, comm='systemd')
        self.process(1, 0, comm='systemd')
        with self.hidden(3):
            self.assertIsNone(self.identity())

    def test_hidden_ancestor_below_the_pane_is_skipped(self):
        # git <- sudo (root, environ EACCES) <- agent shell with the pane <- tmux server.
        self.process(5, 4)
        self.process(4, 3, comm='sudo')
        self.process(3, 2, environ=b'TMUX_PANE=%23\0')
        self.process(2, 1, comm='tmux: server')
        self.process(1, 0, comm='systemd')
        with self.hidden(4):
            self.assertEqual(self.identity(), '%23')

    def test_hidden_pane_shell_under_tmux_is_refused(self):
        # The pane shell itself is hidden: a tmux server is in the chain, so the call is from some tab.
        self.process(5, 4)
        self.process(4, 3)
        self.process(3, 1, comm='tmux: server')
        self.process(1, 0, comm='systemd')
        with self.hidden(4, 3), self.assertRaises(review_gate.GateRefused):
            self.identity()

    def test_real_host_init_needs_no_pane(self):
        self.process(5, 4)
        self.process(4, 1)
        self.process(1, 0, comm='systemd')
        for name in ('systemd', 'init'):
            with self.subTest(name=name):
                (self.root / '1' / 'comm').write_text(f'{name}\n')
                self.assertIsNone(self.identity())


class StoreTest(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.store = self.root / 'review-links.json'

    def test_missing_store_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            review_gate.links_for(PANE, self.store)
        self.assertFalse(self.store.exists())

    def test_unreadable_store_is_refused(self):
        self.store.mkdir()
        with self.assertRaises(review_gate.GateRefused):
            review_gate.links_for(PANE, self.store)

    def test_invalid_json_is_refused(self):
        for malformed in (b'[{', b'\xff', b'[{"worker":"%12","worker":"%34","reviewer":"%20"}]',
                          b'[{"saved_at":NaN}]'):
            with self.subTest(malformed=malformed):
                self.store.write_bytes(malformed)
                with self.assertRaises(review_gate.GateRefused):
                    review_gate.links_for(PANE, self.store)

    def test_non_list_is_refused(self):
        self.store.write_text('{}')
        with self.assertRaises(review_gate.GateRefused):
            review_gate.links_for(PANE, self.store)

    def test_bad_row_is_refused_even_for_another_pane(self):
        for row in (None, 'row', {'worker': '%34'}):
            with self.subTest(row=row):
                self.store.write_text(json.dumps([link(), row]))
                with self.assertRaises(review_gate.GateRefused):
                    review_gate.links_for(PANE, self.store)

    def test_bad_approved_commit_is_refused_even_for_another_pane(self):
        for invalid in ('a' * 39, 'A' * 40, 'z' * 64, 7, None):
            with self.subTest(invalid=invalid):
                row = {**link(worker='%34'), 'approved_commit': invalid}
                self.store.write_text(json.dumps([link(), row]))
                with self.assertRaises(review_gate.GateRefused):
                    review_gate.links_for(PANE, self.store)

    def test_selects_same_live_rows_as_review_links_without_modifying_store(self):
        samples = [link(), link('b' * 64, worker='%13'), link(None, reviewer='%21'),
                   {**link(), 'cap': True}, {**link(), 'saved_at': 123.5},
                   {'worker': PANE, 'worker_thread': 'thread', 'cap': True},
                   {'worker': PANE, 'reviewer': '%22', 'worker_thread': 'thread'}]
        original = json.dumps(samples, indent=2).encode()
        self.store.write_bytes(original)
        for pane in (PANE, '%13', '%14'):
            with self.subTest(pane=pane):
                self.assertEqual(review_gate.links_for(pane, self.store),
                                 [row for row in samples if review_links._is_link(row) and row['worker'] == pane])
        self.assertEqual(self.store.read_bytes(), original)


class GitCase(TemporaryCase):
    object_format = 'sha1'

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, GIT_ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '--initial-branch=main', '--template=', f'--object-format={self.object_format}')
        self.initial = self.commit('base', 'base.txt')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()

    def commit(self, message, path, text=None):
        (self.repo / path).write_text(f'{message}\n' if text is None else text)
        self.git('add', '--', path)
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def branch(self, name, start):
        self.git('checkout', '-q', '-b', name, start)


class ApprovalTest(GitCase):
    def setUp(self):
        super().setUp()
        self.approved = self.commit('approved', 'worker.txt')

    def test_link_without_approval_is_refused(self):
        with self.assertRaisesRegex(review_gate.GateRefused, 'review (?:is )?not approved'):
            review_gate.check(self.repo, self.approved, pane=PANE, links=[link(None)])

    def test_two_links_that_disagree_are_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            review_gate.check(self.repo, self.approved, pane=PANE,
                              links=[link(self.approved), link(self.initial, reviewer='%21')])

    def test_two_links_that_agree_are_allowed(self):
        self.assertEqual(review_gate.check(self.repo, self.approved, pane=PANE,
                                          links=[link(self.approved), link(self.approved, reviewer='%21')]),
                         self.approved)

    def test_exact_match_is_allowed_and_commit_is_resolved(self):
        self.assertTrue(review_gate.commit_allowed(self.repo, self.approved, self.approved))
        self.assertTrue(review_gate.commit_allowed(self.repo, 'HEAD', self.approved))
        self.assertEqual(review_gate.check(self.repo, self.approved, pane=PANE, links=[link(self.approved)]),
                         self.approved)

    def test_descendant_is_refused(self):
        later = self.commit('later', 'later.txt')
        with self.assertRaisesRegex(review_gate.GateRefused, later[:12]):
            review_gate.check(self.repo, later, pane=PANE, links=[link(self.approved)])

    def test_rebase_is_refused(self):
        self.branch('advanced-main', self.initial)
        tip = self.commit('new main', 'main.txt')
        self.branch('worker', self.approved)
        self.git('rebase', tip)
        rebased = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(review_gate.GateRefused, rebased[:12]):
            review_gate.check(self.repo, rebased, pane=PANE, links=[link(self.approved)], base=tip)

    def test_git_timeout_is_refused(self):
        with patch.object(review_gate.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 20)) as run:
            with self.assertRaisesRegex(review_gate.GateRefused, 'timed out'):
                review_gate.check(self.repo, self.approved, pane=PANE, links=[link(self.approved)])
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        self.assertEqual(run.call_args.kwargs['env']['GIT_OPTIONAL_LOCKS'], '0')

    def test_unresolved_commit_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            review_gate.check(self.repo, 'f' * 40, pane=PANE, links=[link(self.approved)])


class Sha256Test(GitCase):
    object_format = 'sha256'

    def test_exact_approval_accepts_a_full_sha256_commit(self):
        approval = self.commit('approved', 'worker.txt')
        self.assertEqual(len(approval), 64)
        self.assertEqual(review_gate.check(self.repo, approval, pane=PANE, links=[link(approval)]), approval)


class PrePushTest(GitCase):
    def setUp(self):
        super().setUp()
        self.approved = self.commit('approved', 'worker.txt')

    def pre_push(self, *lines, approvals=None):
        rows = [link(self.approved)] if approvals is None else approvals
        with patch.object(review_gate, 'pane_identity', return_value=PANE), \
                patch.object(review_gate, 'links_for', return_value=rows):
            return review_gate.pre_push(self.repo, lines)

    def test_topic_push_from_a_sandbox_skips_identity_and_store(self):
        with patch.object(review_gate, 'pane_identity', side_effect=review_gate.GateRefused('sandbox')) as identity, \
                patch.object(review_gate, 'links_for') as store:
            review_gate.pre_push(self.repo, [ref_line(self.approved, ZERO, destination='refs/heads/backup'),
                                             ref_line(self.approved, ZERO, destination='refs/heads/topic\u00a0one')])
        identity.assert_not_called()
        store.assert_not_called()

    def test_main_fast_forward_pushed_hash_is_allowed_even_when_head_moved(self):
        self.commit('later', 'later.txt')
        self.pre_push(ref_line(self.approved, self.initial))

    def test_main_fast_forward_to_unapproved_hash_is_refused(self):
        later = self.commit('later', 'later.txt')
        with self.assertRaisesRegex(review_gate.GateRefused, later[:12]):
            self.pre_push(ref_line(later, self.initial))

    def test_non_fast_forward_is_refused_even_for_the_approved_hash(self):
        later = self.commit('later', 'later.txt')
        with self.assertRaises(review_gate.GateRefused):
            self.pre_push(ref_line(self.approved, later))

    def test_main_creation_from_a_linked_pane_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            self.pre_push(ref_line(self.approved, ZERO))

    def test_main_deletion_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            self.pre_push(ref_line(ZERO, self.approved, source='(delete)'))

    def test_tag_deletion_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            self.pre_push(ref_line(ZERO, self.approved, source='(delete)', destination=TAG))

    def test_tag_replacement_is_refused(self):
        with self.assertRaises(review_gate.GateRefused):
            self.pre_push(ref_line(self.approved, self.initial, source=TAG, destination=TAG))

    def test_annotated_tag_is_peeled_to_the_approved_commit(self):
        self.git('tag', '-a', 'v1', '-m', 'release', self.approved)
        tag = self.git('rev-parse', TAG)
        self.assertNotEqual(tag, self.approved)
        self.pre_push(ref_line(tag, ZERO, source=TAG, destination=TAG))

    def test_malformed_line_refuses_even_when_previous_line_was_a_topic_ref(self):
        for malformed in ('garbage\n', ref_line('garbage', ZERO), ref_line(self.approved, 'a' * 64),
                          ref_line(self.approved, ZERO, destination='garbage'),
                          ref_line(self.approved, ZERO, destination=MAIN + '\tgarbage')):
            with self.subTest(malformed=malformed), self.assertRaises(review_gate.GateRefused):
                self.pre_push(ref_line(self.approved, ZERO, destination='refs/heads/backup'), malformed)

    def test_no_live_link_allows_the_push(self):
        self.pre_push(ref_line(ZERO, self.approved, source='(delete)'), approvals=[])

    def test_no_pane_allows_the_push_and_does_not_read_store(self):
        with patch.object(review_gate, 'pane_identity', return_value=None), \
                patch.object(review_gate, 'links_for') as store:
            review_gate.pre_push(self.repo, [ref_line(self.approved, self.initial)])
        store.assert_not_called()

    def test_cli_refusal_exits_one_and_reports_to_stderr(self):
        errors = io.StringIO()
        with patch.object(review_gate, 'pane_identity', return_value=PANE), \
                patch.object(review_gate, 'links_for', return_value=[link(None)]), redirect_stderr(errors):
            self.assertEqual(review_gate.main(['check', '--commit', self.approved, '--repo', str(self.repo)]), 1)
        self.assertTrue(errors.getvalue().startswith('review gate:'), errors.getvalue())
        self.assertIn(self.approved[:12], errors.getvalue())


class MergeForwardTest(GitCase):
    def setUp(self):
        super().setUp()
        self.branch('worker', self.initial)
        self.approved = self.commit('approved', 'worker.txt')
        self.git('checkout', '-q', 'main')
        self.base = self.commit('main', 'main.txt')
        self.git('checkout', '-q', 'worker')

    def merge(self, other):
        self.git('merge', '--no-edit', other)
        return self.git('rev-parse', 'HEAD')

    def allowed(self, candidate, base):
        with patch.object(review_gate, 'MERGE_FORWARD', True):
            return review_gate.commit_allowed(self.repo, candidate, self.approved, base=base)

    def test_default_allows_a_clean_merge_forward_and_the_flag_turns_it_off(self):
        merged = self.merge(self.base)
        self.assertTrue(review_gate.commit_allowed(self.repo, merged, self.approved, base=self.base))
        with patch.object(review_gate, 'MERGE_FORWARD', False):
            self.assertFalse(review_gate.commit_allowed(self.repo, merged, self.approved, base=self.base))

    def test_clean_merge_with_verified_base_is_allowed_without_writing_refs_or_index(self):
        merged = self.merge(self.base)
        before = self.git('show-ref'), (self.repo / '.git/index').read_bytes()
        self.assertTrue(self.allowed(merged, self.base))
        self.assertEqual((self.git('show-ref'), (self.repo / '.git/index').read_bytes()), before)

    def test_two_clean_merges_are_allowed_with_latest_base(self):
        self.merge(self.base)
        self.git('checkout', '-q', 'main')
        new_base = self.commit('main again', 'second.txt')
        self.git('checkout', '-q', 'worker')
        merged = self.merge(new_base)
        self.assertTrue(self.allowed(merged, new_base))

    def test_conflict_resolved_merge_is_refused(self):
        self.git('checkout', '-q', 'main')
        base = self.commit('conflicting main', 'worker.txt')
        tree = self.git('rev-parse', f'{self.approved}^{{tree}}')
        merged = self.git('commit-tree', tree, '-p', self.approved, '-p', base, '-m', 'resolve conflict')
        self.assertFalse(self.allowed(merged, base))

    def test_extra_edit_in_merge_is_refused(self):
        self.merge(self.base)
        (self.repo / 'extra.txt').write_text('unreviewed\n')
        self.git('add', 'extra.txt')
        self.git('commit', '-q', '--amend', '--no-edit')
        self.assertFalse(self.allowed(self.git('rev-parse', 'HEAD'), self.base))

    def test_other_parent_must_be_an_ancestor_of_base(self):
        merged = self.merge(self.base)
        self.assertFalse(self.allowed(merged, self.initial))

    def test_missing_base_does_not_enable_merge_forward(self):
        self.assertFalse(self.allowed(self.merge(self.base), None))

    def test_depth_limit_allows_twenty_merges_and_refuses_twenty_one(self):
        # The remote parent is already in each subsequent merge. A clean recomputation has the same tree.
        merged = self.merge(self.base)
        tree = self.git('rev-parse', f'{merged}^{{tree}}')
        for depth in range(2, 22):
            merged = self.git('commit-tree', tree, '-p', merged, '-p', self.base, '-m', f'Merge {depth}')
            if depth >= 20:
                with self.subTest(depth=depth):
                    self.assertEqual(self.allowed(merged, self.base), depth == 20)


class HookTest(GitCase):
    ORIGINAL = b'#!/bin/bash\nwhile IFS= read -r ref; do printf "%s\\n" "$ref"; done\n# no final newline'

    def setUp(self):
        super().setUp()
        self.hook = self.repo / '.git/hooks/pre-push'
        self.hook.parent.mkdir(exist_ok=True)
        self.store = self.root / 'cache/review-links.json'
        self.gate = self.root / "fake 'gate'.py"
        self.captured = self.root / 'gate-input'
        self.gate.write_text(
            'import pathlib, sys\n'
            'assert sys.argv[1:] == ["pre-push", "origin", "path with spaces"]\n'
            f'pathlib.Path({str(self.captured)!r}).write_bytes(sys.stdin.buffer.read())\n'
        )

    def install(self, uninstall=False):
        return review_gate.install_hook(self.repo, uninstall=uninstall, gate=self.gate, store=self.store)

    def existing_hook(self):
        self.hook.write_bytes(self.ORIGINAL)
        self.hook.chmod(0o751)

    def invoke(self, refs):
        return subprocess.run([str(self.hook), 'origin', 'path with spaces'], cwd=self.repo, input=refs,
                              capture_output=True, timeout=5)

    def test_existing_hook_and_gate_both_read_every_ref_line(self):
        self.existing_hook()
        self.install()
        refs = (ref_line(self.initial, ZERO, destination='refs/heads/topic')
                + ref_line(self.initial, ZERO, destination=TAG)).encode()
        result = self.invoke(refs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, refs)
        self.assertEqual(self.captured.read_bytes(), refs)
        self.assertTrue(self.hook.read_bytes().startswith(b'#!/bin/bash\n'))
        self.assertEqual(stat.S_IMODE(self.hook.stat().st_mode), 0o751)

    def test_install_is_idempotent(self):
        self.existing_hook()
        self.install()
        installed = self.hook.read_bytes()
        self.install()
        self.assertEqual(self.hook.read_bytes(), installed)

    def test_uninstall_restores_original_bytes_exactly(self):
        self.existing_hook()
        self.install()
        self.install(uninstall=True)
        self.assertEqual(self.hook.read_bytes(), self.ORIGINAL)

    def test_missing_gate_refuses_without_running_scanner(self):
        self.existing_hook()
        self.install()
        self.gate.unlink()
        result = self.invoke(ref_line(self.initial, ZERO).encode())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_new_hook_and_store_are_created_with_the_right_modes(self):
        self.install()
        self.assertTrue(self.hook.read_bytes().startswith(b'#!/bin/sh\n'))
        self.assertTrue(self.hook.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(json.loads(self.store.read_text()), [])
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

    def test_install_leaves_existing_store_intact(self):
        self.store.parent.mkdir()
        existing = json.dumps([link()]).encode()
        self.store.write_bytes(existing)
        self.install()
        self.assertEqual(self.store.read_bytes(), existing)

    def test_install_and_uninstall_refuse_a_symlinked_hook(self):
        target = self.root / 'scanner'
        target.write_bytes(self.ORIGINAL)
        self.hook.symlink_to(target)
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall), self.assertRaises(review_gate.GateRefused):
                self.install(uninstall)
        self.assertTrue(self.hook.is_symlink())
        self.assertEqual(target.read_bytes(), self.ORIGINAL)

    def test_installer_refuses_to_insert_shell_code_into_a_non_shell_hook(self):
        original = b'#!/usr/bin/env python3\nprint("scanner")\n'
        self.hook.write_bytes(original)
        with self.assertRaisesRegex(review_gate.GateRefused, 'different language'):
            self.install()
        self.assertEqual(self.hook.read_bytes(), original)

    def test_git_push_uses_the_installed_gate_and_preserves_topic_backups(self):
        remote = self.root / 'remote.git'
        self.git('init', '-q', '--bare', '--template=', str(remote))
        self.git('push', str(remote), f'{self.initial}:{MAIN}')
        approved = self.commit('approved', 'worker.txt')
        later = self.commit('later', 'later.txt')
        with patch.dict(os.environ, {'TMUX_PANE': PANE, 'MOBILE_TMUX_CACHE': str(self.store.parent)}):
            review_gate.install_hook(self.repo, store=self.store)
            self.store.write_text(json.dumps([link(approved)]))
            self.git('push', str(remote), f'{approved}:{MAIN}')
            with self.assertRaises(subprocess.CalledProcessError) as refused:
                self.git('push', str(remote), f'{later}:{MAIN}')
            self.assertIn('review gate:', refused.exception.stderr)
            self.assertIn(later[:12], refused.exception.stderr)
            self.git('push', str(remote), f'{later}:refs/heads/backup')
        self.assertEqual(self.git('-C', str(remote), 'rev-parse', MAIN), approved)
        self.assertEqual(self.git('-C', str(remote), 'rev-parse', 'refs/heads/backup'), later)


if __name__ == '__main__':
    unittest.main()
