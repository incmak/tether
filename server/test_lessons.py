"""Feedback storage uses temporary projects, including cross-process writers."""
import json
import multiprocessing
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import lessons
import paths

PID = 'abcdef012345'


def process_add(root, text, barrier):
    paths.CONFIG, paths.TETHER = Path(root) / 'config', Path(root) / 'tether'
    barrier.wait(timeout=5)
    lessons.add(PID, text, 'avoid', 'user', 'codex', force_new=True)


class Lessons(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('CONFIG', 'TETHER'):
            self.enterContext(patch.object(paths, name, self.root / name.lower()))
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)

    def add(self, text='When a check fails, inspect its output.', **options):
        return lessons.add(PID, text, options.get('kind', 'avoid'), options.get('source', 'user'),
                           options.get('agent', 'codex'), options.get('force_new', False))

    def test_settings_default_invalid_and_private(self):
        self.assertTrue(lessons.enabled())
        paths.CONFIG.mkdir()
        file = paths.CONFIG / 'learning.json'
        for raw in ('bad', '[]', '{}', '{"enabled": 1}'):
            file.write_text(raw)
            self.assertTrue(lessons.enabled())
        lessons.set_enabled(False)
        self.assertFalse(lessons.enabled())
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            lessons.set_enabled(1)

    def test_clean_rules_and_denied_phrases(self):
        self.assertEqual(lessons.clean(' when  checks\n fail,\x00\u202e read output. '), 'when checks fail, read output.')
        for text in (None, 'When short', 'Use tests for errors.', 'When ' + 'x' * 196):
            with self.subTest(text=text), self.assertRaises(ValueError):
                lessons.clean(text)
        denied = ('approval', 'passkey', 'autonomy', 'level', 'permission', 'force-push', 'force push',
                  '--no-verify', 'delete', 'rm -rf', 'drop table', 'money', 'pay', 'purchase', 'billing',
                  'secret', 'token', 'password', 'credential', 'release', 'publish', 'without asking',
                  "don't ask", 'do not ask', 'no need to ask', 'skip review', 'skip the review',
                  'ignore the rules', 'ignore the instructions', 'override')
        for word in denied:
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, 'cannot include'):
                lessons.clean('When unsure, ' + word.upper() + ' instead.')
        lessons.clean('When payment text appears, display it clearly.')

    def test_add_similar_new_reinforce_and_user_changes(self):
        row = self.add()['lesson']
        self.assertRegex(row['id'], '^[0-9a-f]{8}$')
        before = lessons.path(PID).read_bytes()
        result = self.add('When a check fails, inspect output.')
        self.assertEqual((result['status'], result['candidates'][0]['id']), ('similar', row['id']))
        self.assertEqual(lessons.path(PID).read_bytes(), before)
        self.assertEqual(self.add(force_new=True)['status'], 'added')
        self.assertEqual(self.add(kind='keep')['status'], 'added')
        lessons.reinforce(PID, row['id'])
        lessons.reinforce(PID, row['id'])
        self.assertTrue(lessons.entries(PID)[0]['stuck'])
        self.assertEqual(lessons.entries(PID)[0]['hits'], 3)
        lessons.edit(PID, row['id'], 'When checks fail, read every error first.')
        lessons.propose_global(PID, row['id'])
        self.assertTrue(lessons.entries(PID)[0]['proposed_global'])
        self.assertTrue(lessons.remove(PID, row['id']))
        for operation in (lessons.remove, lessons.reinforce, lessons.propose_global, lessons.promote):
            self.assertFalse(operation(PID, row['id']))

    def test_cap_eviction_rank_and_all_user_refusal(self):
        for index in range(60):
            self.add(f'When case {index} occurs, inspect the exact output.', source='agent', force_new=True)
        worst = lessons.entries(PID)[-1]['id']
        self.add('When naming checks, use clear names.', source='reviewer', kind='keep', force_new=True)
        self.assertEqual(len(lessons.entries(PID)), 60)
        self.assertNotIn(worst, [row['id'] for row in lessons.entries(PID)])
        self.assertEqual(lessons.entries(PID)[0]['source'], 'reviewer')
        for row in lessons.entries(PID):
            lessons.remove(PID, row['id'])
        for index in range(60):
            self.add(f'When case {index} occurs, inspect the exact output.', force_new=True)
        with self.assertRaisesRegex(ValueError, 'This project has 60 lessons. Remove one in Tether first.'):
            self.add('When naming checks, use clear names.', force_new=True)
        self.assertEqual(len(lessons.entries(PID)), 60)

    def test_source_kind_hits_last_hit_order(self):
        for source in ('agent', 'reviewer', 'user'):
            for kind in ('keep', 'avoid'):
                self.add(f'When {source} {kind} applies, check the output.', source=source, kind=kind, force_new=True)
        self.assertEqual([(r['source'], r['kind']) for r in lessons.entries(PID)],
                         [(s, k) for s in ('user', 'reviewer', 'agent') for k in ('avoid', 'keep')])
        first = self.add(force_new=True)['lesson']
        with patch.object(lessons.time, 'time', return_value=first['last_hit'] + 100):
            second = self.add(force_new=True)['lesson']
        self.assertEqual(lessons.entries(PID)[0]['id'], second['id'])
        lessons.reinforce(PID, first['id'])
        self.assertEqual(lessons.entries(PID)[0]['id'], first['id'])

    def test_prompt_limits_global_first_and_promotion(self):
        row = self.add(source='reviewer')['lesson']
        lessons.promote(PID, row['id'])
        self.assertEqual(lessons.entries(PID), [])
        self.assertEqual(lessons.global_entries()[0]['source'], 'reviewer')
        for index in range(12):
            self.add('When case ' + str(index) + ' appears, ' + 'x' * 130, force_new=True)
        selected = lessons.for_prompt(PID)
        self.assertEqual(selected[0]['id'], row['id'])
        self.assertLessEqual(len(selected), 8)
        self.assertLessEqual(sum(len(r['text']) for r in selected), 1200)
        self.assertTrue(lessons.remove_global(row['id']))
        self.assertFalse(lessons.remove_global(row['id']))
        for row in lessons.entries(PID):
            lessons.edit(PID, row['id'], 'When errors occur, read the output.')
        self.assertEqual(len(lessons.for_prompt(PID)), 8)

    def test_global_cap_refuses_user_and_evicts_agent(self):
        for index in range(30):
            row = self.add(f'When global case {index} occurs, check it.', force_new=True)['lesson']
            lessons.promote(PID, row['id'])
        row = self.add('When another case occurs, check it.', force_new=True)['lesson']
        with self.assertRaisesRegex(ValueError, '30 lessons'):
            lessons.promote(PID, row['id'])
        self.assertEqual(len(lessons.entries(PID)), 1)
        lessons.remove_global(lessons.global_entries()[0]['id'])
        agent = self.add(source='agent', force_new=True)['lesson']
        lessons.promote(PID, agent['id'])
        lessons.promote(PID, row['id'])
        self.assertEqual(len(lessons.global_entries()), 30)
        self.assertNotIn(agent['id'], [r['id'] for r in lessons.global_entries()])

    def test_atomic_failure_and_private_files(self):
        self.add()
        file = lessons.path(PID)
        old = file.read_bytes()
        with patch.object(lessons.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.add(force_new=True)
        self.assertEqual(file.read_bytes(), old)
        self.assertFalse(list(file.parent.glob('.lessons-*')))
        for file in file.parent.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        for pid in ('../x', None, '000000000000'):
            with self.assertRaises(ValueError):
                lessons.entries(pid)

    def test_two_processes_preserve_both_adds(self):
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(2)
        workers = [context.Process(target=process_add, args=(str(self.root), text, barrier)) for text in
                   ('When tests fail, inspect their output.', 'When layouts narrow, wrap the labels.')]
        for worker in workers:
            worker.start()
        try:
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=2)
                worker.close()
        self.assertEqual(len(lessons.entries(PID)), 2)
