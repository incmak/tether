"""Saved answers stay in temporary projects, never in the user's configuration."""
from datetime import datetime
import hashlib
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

import approval
import autonomy
import codex_level
import decisions
import paths
from test_approval import HAS_CRYPTO, Handler, PasskeyCase

PID = 'abcdef012345'
OTHER = '123456abcdef'


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ('CONFIG', 'TETHER'):
            patcher = mock.patch.object(paths, name, root / name.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        for pid in (PID, OTHER):
            (paths.TETHER / 'projects' / pid).mkdir(parents=True)
        self.path = decisions.path(PID)
        self.now = datetime(2026, 11, 12, 9, 30)

    def record(self, question='Choose?', answer='Default', pid=PID):
        return decisions.record(pid, question, answer, now=self.now)

    def test_format_header_id_and_newest_first(self):
        first = self.record()
        second = self.record('Proceed?', 'Yes')
        expected_id = hashlib.sha1(b'2026-11-12|Choose?|Default').hexdigest()[:8]
        self.assertEqual(first, {'id': expected_id, 'date': '2026-11-12', 'question': 'Choose?', 'answer': 'Default'})
        self.assertEqual(self.path.read_text(), decisions.HEADER
                         + f'- 2026-11-12 [{first["id"]}]: Q: Choose? -> A: Default\n'
                         + f'- 2026-11-12 [{second["id"]}]: Q: Proceed? -> A: Yes\n')
        self.assertEqual(decisions.entries(PID), [second, first])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_control_characters_whitespace_and_dedupe_after_cleanup(self):
        first = self.record('  A\n\tB\x00\x1b\x7f\x85 C\u200b?  ', '  Y\r\nes\t please\x00\u202e  ')
        self.assertEqual((first['question'], first['answer']), ('A B C?', 'Y es please'))
        with mock.patch.object(decisions.os, 'replace') as replace:
            self.assertEqual(self.record('A B C?', 'Y es please'), first)
        replace.assert_not_called()
        self.assertEqual(decisions.entries(PID), [first])

    def test_question_and_answer_are_capped_separately(self):
        row = self.record('q' * 250, 'a' * 550)
        self.assertEqual((row['question'], row['answer']), ('q' * 200, 'a' * 500))
        self.assertEqual(decisions.entries(PID), [row])

    def test_empty_question_or_answer_is_rejected(self):
        for question, answer in (('', 'yes'), ('\x00\u200b\n', 'yes'), ('question', ' \x00\t'),
                                 (None, 'yes'), ('question', None)):
            with self.subTest(question=question, answer=answer), self.assertRaises(ValueError):
                self.record(question, answer)
        self.assertFalse(self.path.exists())

    def test_delimiters_in_question_and_answer_round_trip(self):
        row = self.record('What does -> A: mean?', 'Use -> A: for the answer.')
        self.assertEqual(decisions.entries(PID), [row])
        self.record(row['question'], row['answer'])
        self.assertEqual(decisions.entries(PID), [row])

    def test_oldest_entries_drop_after_one_hundred(self):
        rows = [self.record(f'Question {index}', f'Answer {index}') for index in range(105)]
        self.assertEqual(decisions.entries(PID), list(reversed(rows[5:])))
        self.assertEqual(len(self.path.read_text().splitlines()), 102)

    def test_projects_are_isolated_and_global_file_is_untouched(self):
        paths.CONFIG.mkdir()
        old = paths.CONFIG / 'decisions.md'
        old.write_text('Old global answers.\n')
        self.assertEqual(decisions.entries(PID), [])
        self.assertEqual(decisions.entries(OTHER), [])
        row = self.record()
        self.record('Other?', 'No', pid=OTHER)
        self.assertEqual(decisions.entries(PID), [row])
        self.assertEqual(decisions.entries(OTHER)[0]['question'], 'Other?')
        self.assertEqual(old.read_text(), 'Old global answers.\n')

    def test_delete_and_remove_file_after_last_entry(self):
        self.assertFalse(decisions.delete(PID, '00000000'))
        first, second = self.record(), self.record('Again?', 'Yes')
        self.assertFalse(decisions.delete(PID, '00000000'))
        self.assertTrue(decisions.delete(PID, first['id']))
        self.assertEqual(decisions.entries(PID), [second])
        self.assertTrue(decisions.delete(PID, second['id']))
        self.assertFalse(self.path.exists())
        self.assertEqual(decisions.entries(PID), [])
        self.assertFalse(decisions.delete(PID, second['id']))

    def test_bad_and_missing_project_ids(self):
        for pid in (None, [], '../x', 'x', 'ABCDEF012345', 'abcdef012345\n', '000000000000'):
            for operation in (decisions.path, decisions.entries, lambda pid: self.record(pid=pid),
                              lambda pid: decisions.delete(pid, '00000000')):
                with self.subTest(pid=pid, operation=operation), self.assertRaises(ValueError):
                    operation(pid)
        self.assertFalse((paths.TETHER / 'projects/000000000000').exists())

    def test_malformed_lines_are_ignored(self):
        first = self.record()
        with self.path.open('a') as stream:
            stream.write('- old format: Q: Bad -> A: No\n')
            stream.write('- 2026-99-99 [1234abcd]: Q: Bad -> A: No\n')
            stream.write('- 2026-11-12 [not-an-id]: Q: Bad -> A: No\n')
            stream.write('- 2026-11-12 [1234abcd]: Q:  -> A: No\n')
        self.assertEqual(decisions.entries(PID), [first])

    def test_atomic_replace_and_mode(self):
        self.record()
        old = self.path.read_text()
        self.path.chmod(0o644)
        replace = decisions.os.replace
        def check(temporary, destination):
            self.assertEqual(Path(temporary).parent, self.path.parent)
            self.assertEqual(destination, self.path)
            self.assertEqual(self.path.read_text(), old)
            self.assertIn('Q: Again? -> A: Yes\n', Path(temporary).read_text())
            self.assertEqual(stat.S_IMODE(Path(temporary).stat().st_mode), 0o600)
            replace(temporary, destination)
        with mock.patch.object(decisions.os, 'replace', side_effect=check) as replaced:
            self.record('Again?', 'Yes')
        replaced.assert_called_once()
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_replace_preserves_old_file_and_removes_temporary_file(self):
        self.record()
        old = self.path.read_text()
        with mock.patch.object(decisions.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.record('Again?', 'Yes')
        self.assertEqual(self.path.read_text(), old)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_answers_do_not_overwrite_each_other(self):
        errors = []
        def write(index):
            try:
                self.record(f'Question {index}')
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=write, args=(index,)) for index in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({row['question'] for row in decisions.entries(PID)},
                         {f'Question {index}' for index in range(12)})


@unittest.skipUnless(HAS_CRYPTO, 'cryptography is not installed')
class DecisionApproval(PasskeyCase):
    def test_record_refused_before_write_and_accepted_with_assertion_delete_stays_open(self):
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        payload = {'pane': '%12', 'question': 'Choose?', 'answer': 'Default'}
        route = autonomy.PATH + '/decisions'
        with mock.patch.object(codex_level, '_tmux', return_value=SimpleNamespace(returncode=0, stdout=PID)) as tmux:
            handler = Handler(route, payload)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['approval']), (403, 'no-passkey'))
            self.assertFalse(decisions.path(PID).exists())
            self.assertFalse(paths.CONFIG.exists())
            tmux.assert_not_called()
            self.add()
            handler = Handler(route, payload)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['approval']), (403, 'required'))
            self.assertFalse(decisions.path(PID).exists())
            proof = self.assertion('decision-save', payload)
            handler = Handler(route, dict(payload, approval=proof))
            autonomy.post(handler)
            self.assertEqual(handler.status, 200, handler.body)
            row = handler.body['entry']
            self.assertEqual(decisions.entries(PID), [row])
        handler = Handler(route, {'project': PID, 'delete': row['id']})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body), (200, {'decisions': []}))
        self.assertFalse(decisions.path(PID).exists())


if __name__ == '__main__':
    unittest.main()
