"""Feedback storage uses temporary projects, including cross-process writers."""
import json
import multiprocessing
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import lessons
import paths
from own_time import own_time

PID = 'abcdef012345'


def process_add(root, text, barrier):
    paths.CONFIG, paths.TETHER = Path(root) / 'config', Path(root) / 'tether'
    barrier.wait(timeout=30)  # spawned processes start slowly on a loaded machine
    lessons.add(PID, text, 'avoid', 'user', 'codex', force_new=True)


class Lessons(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('CONFIG', 'TETHER'):
            self.enterContext(patch.object(paths, name, self.root / name.lower()))
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)

    def add(self, text='When a check fails, inspect its output.', **options):
        return lessons.add(PID, text, options.get('kind', 'avoid'), options.get('source', 'user'),
                           options.get('agent', 'codex'), options.get('force_new', False),
                           quote=options.get('quote', ''), tab=options.get('tab', ''), pane=options.get('pane', ''))

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
        with own_time(lessons, time=Mock(return_value=first['last_hit'] + 100)):
            second = self.add(force_new=True)['lesson']
        self.assertEqual(lessons.entries(PID)[0]['id'], second['id'])
        lessons.reinforce(PID, first['id'])
        self.assertEqual(lessons.entries(PID)[0]['id'], first['id'])

    def test_prompt_limits_global_first_and_promotion(self):
        row = self.add(source='reviewer')['lesson']
        lessons.keep(PID, row['id'])
        lessons.promote(PID, row['id'])
        self.assertEqual(lessons.entries(PID), [])
        self.assertEqual(lessons.global_entries()[0]['source'], 'reviewer')
        for index in range(12):
            self.add('When case ' + str(index) + ' appears, ' + 'x' * 130, force_new=True)
        prompt = 'The check fails: inspect the output of each case that appears.'
        selected = lessons.fit(lessons.for_prompt(PID, prompt)['rows'])
        self.assertEqual(selected[0]['id'], row['id'])
        self.assertLessEqual(len(selected), 8)
        self.assertLessEqual(sum(len(r['text']) for r in selected), 1200)
        self.assertTrue(lessons.remove_global(row['id']))
        self.assertFalse(lessons.remove_global(row['id']))
        for row in lessons.entries(PID):
            lessons.edit(PID, row['id'], 'When errors occur, read the output.')
        found = lessons.for_prompt(PID, 'Errors occur.')
        self.assertEqual((len(found['rows']), len(lessons.fit(found['rows'])), len(found['usable'])), (12, 8, 12))
        self.assertEqual(lessons.for_prompt(PID)['rows'], [], 'no prompt, no lesson')

    def test_global_cap_refuses_user_and_evicts_agent(self):
        for index in range(30):
            row = self.add(f'When global case {index} occurs, check it.', force_new=True)['lesson']
            lessons.keep(PID, row['id'])
            lessons.promote(PID, row['id'])
        row = self.add('When another case occurs, check it.', force_new=True)['lesson']
        lessons.keep(PID, row['id'])
        with self.assertRaisesRegex(ValueError, '30 lessons'):
            lessons.promote(PID, row['id'])
        self.assertEqual(len(lessons.entries(PID)), 1)
        lessons.remove_global(lessons.global_entries()[0]['id'])
        agent = self.add(source='agent', force_new=True)['lesson']
        lessons.keep(PID, agent['id'])
        lessons.promote(PID, agent['id'])
        lessons.keep(PID, row['id'])
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
                worker.join(timeout=60)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=2)
                worker.close()
        self.assertEqual(len(lessons.entries(PID)), 2)


class Matching(unittest.TestCase):
    """Which lessons a prompt gets (B74). The texts are made up; the shapes are from real lessons and prompts."""
    CI = 'When checking CI for a repo, pass -R owner/repo to gh and read the run logs before saying CI did not run.'
    BAR = 'When adding a new action to the phone UI, do not add it to the bottom tab bar; put it in the menu.'
    LINK = 'When sending deep links from outside apps (Teams, mail), link inside the PWA scope and confirm on the phone.'

    def match(self, lesson, prompt, names=(), weak=frozenset()):
        return lessons.score(lesson, lessons.spans(prompt, names), weak) >= lessons.MATCH - 1e-9

    def test_stems_and_parts(self):
        for word, other in (('tabs', 'tab'), ('grouping', 'group'), ('swiped', 'swipe'), ('repos', 'repo'),
                            ('dependencies', 'dependency'), ('stopped', 'stop'), ('checks', 'checking')):
            self.assertEqual(lessons._stem(word), lessons._stem(other), word)
        self.assertEqual(lessons._stem('separately'), lessons._stem('separate'))
        self.assertEqual(lessons._stem('mobile'), lessons._stem('phones'), 'one SAME line')
        self.assertEqual(lessons._parts(self.LINK)[0], 'When sending deep links from outside apps (Teams, mail)')
        self.assertEqual(lessons._parts('When there is no comma')[1], '')
        self.assertEqual(lessons._stems('When the CI is red'), ['', '', 'ci', '', 'red'])

    def test_one_distinctive_situation_word_is_enough(self):
        self.assertTrue(self.match(self.CI, 'ci is red again'))
        self.assertTrue(self.match(self.CI, 'Why did CI not run on my last push?'))
        self.assertTrue(self.match(self.LINK, 'the link in the Teams message opens the browser'))
        self.assertTrue(self.match(self.LINK, 'Mail me a deep-link to this tab.'))

    def test_common_words_alone_do_not_match(self):
        self.assertFalse(self.match(self.CI, 'check the run and read the file'), 'common advice words give nothing')
        self.assertFalse(self.match(self.BAR, 'add a test'))
        self.assertFalse(self.match(self.LINK, 'open the link'))
        self.assertFalse(self.match(self.CI, ''))
        self.assertFalse(self.match(self.CI, 'yes, continue'))

    def test_common_words_together_or_two_advice_words_match(self):
        self.assertTrue(self.match(self.BAR, 'add an action for undo'))
        self.assertTrue(self.match(self.BAR, 'a new screen for mobile'), 'mobile means phone')
        self.assertTrue(self.match(self.CI, 'the pipeline is red'), 'pipeline means CI')
        self.assertTrue(self.match(self.BAR, 'put Undo in the bottom bar'))
        self.assertTrue(self.match(self.CI, 'what does gh say for the owner?'))
        self.assertFalse(self.match(self.BAR, 'the bar is too wide'))

    def test_a_long_prompt_needs_the_words_close_together(self):
        filler = ' '.join(['lorem'] * 60)
        self.assertFalse(self.match(self.BAR, f'add it. {filler} a new one. {filler} the action. {filler} on a phone.'))
        self.assertFalse(self.match(self.CI, f'check it. {filler} in the repo'))
        self.assertTrue(self.match(self.BAR, f'{filler} {filler} add a new action on the phone {filler}'))
        self.assertTrue(self.match(self.CI, f'{filler} {filler} {filler} and then the CI must pass'))
        self.assertTrue(self.match(self.CI, ' '.join(['lorem'] * 9000) + ' ci') is False, 'only the first 8,000 words')

    def test_names_count_in_every_part_of_the_prompt(self):
        self.assertTrue(self.match(self.CI, 'yes, continue', names=['Fix CI on main', 'tether']))
        self.assertTrue(self.match(self.BAR, 'add it', names=['phone-polish']))
        self.assertFalse(self.match(self.BAR, 'add it', names=['desktop-polish']))

    def test_the_project_name_and_an_image_mark_say_little(self):
        lesson = 'When Gizmo groups tabs into stacks, keep fewer stacks than tabs.'
        self.assertTrue(self.match(lesson, 'gizmo is slow today'))
        self.assertFalse(self.match(lesson, 'gizmo is slow today', weak={lessons._stem('gizmo')}))
        self.assertTrue(self.match(lesson, 'the stacks look wrong', weak={lessons._stem('gizmo')}))
        image = 'When a big image loads on a slow link, save a small WebP first.'
        self.assertFalse(self.match(image, '[Image #1] why is this cut off?'))
        self.assertFalse(self.match(image, '/cache/uploads/ab12-image.png /cache/Shot_1.JPG why is this cut off?'))
        self.assertTrue(self.match(image, 'the image appears late'))


class LessonReview(unittest.TestCase):
    """Review reads and writes only the temporary project/config roots from Lessons."""

    setUp = Lessons.setUp
    add = Lessons.add

    def row(self, **options):
        return self.add(force_new=True, **options)['lesson']

    def test_old_rows_migrate_on_next_save_in_both_scopes(self):
        for file in (lessons.path(PID), lessons.global_path()):
            rows = []
            for hits in (1, 2):
                row = self.row()
                rows.append({key: value for key, value in row.items()
                             if key not in ('status', 'quote', 'tab', 'first_pane', 'disputes', 'reviewed_at')})
                rows[-1]['hits'] = hits
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({'version': 1, 'lessons': rows}))
            loaded = lessons._read(file)
            self.assertEqual([row['status'] for row in loaded], ['trial', 'confirmed'])
            for row in loaded:
                self.assertEqual((row['quote'], row['tab'], row['first_pane'], row['disputes'], row['reviewed_at']),
                                 ('', '', '', [], None))
            if file == lessons.global_path():
                lessons.keep_global(rows[0]['id'])
            else:
                lessons.reinforce(PID, rows[0]['id'])
            self.assertTrue(all('status' in row and 'disputes' in row
                                for row in json.loads(file.read_text())['lessons']))

    def test_new_fields_are_strict_and_input_is_cleaned(self):
        row = self.row(quote=' User\nsaid\x00\u202e this ' + 'x' * 200, tab=' Review\n' + 't' * 80, pane='%7')
        self.assertEqual(len(row['quote']), 160)
        self.assertTrue(row['quote'].startswith('User said this '))
        self.assertEqual(len(row['tab']), 60)
        self.assertEqual(row['first_pane'], '%7')
        file = lessons.path(PID)
        for key, value in (('status', None), ('status', 'bad'), ('status', []),
                           ('quote', None), ('quote', 'x' * 161), ('tab', 1), ('tab', 'x' * 61),
                           ('first_pane', None), ('first_pane', '%x'), ('first_pane', '7'),
                           ('reviewed_at', True), ('reviewed_at', '1'), ('reviewed_at', float('nan')),
                           ('reviewed_at', 10 ** 500),
                           ('disputes', None), ('disputes', [{}]),
                           ('disputes', [{'pane': '', 'reason': 'x', 'at': 1}]),
                           ('disputes', [{'pane': '%1', 'reason': None, 'at': 1}]),
                           ('disputes', [{'pane': '%1', 'reason': 'x' * 201, 'at': 1}]),
                           ('disputes', [{'pane': '%1', 'reason': 'x', 'at': True}]),
                           ('disputes', [{'pane': '%1', 'reason': 'x', 'at': 1}] * 21)):
            with self.subTest(key=key, value=value):
                file.write_text(json.dumps({'version': 1, 'lessons': [dict(row, **{key: value})]}))
                with self.assertRaises(ValueError):
                    lessons.entries(PID)
        file.write_text(json.dumps({'version': 1, 'lessons': [row]}))
        for options in ({'quote': None}, {'tab': 7}, {'pane': '%x'}):
            with self.assertRaises(ValueError):
                self.row(**options)

    def test_independent_hits_and_time_confirm_but_same_pane_does_not(self):
        with own_time(lessons, time=Mock(return_value=100)):
            row = self.row(pane='%1')
            for pane in ('%1', '', '%1'):
                repeated = lessons.reinforce(PID, row['id'], pane)
                self.assertEqual(repeated['status'], 'trial')
            self.assertEqual(repeated['hits'], 4)
            self.assertEqual(lessons.reinforce(PID, row['id'], '%2')['status'], 'confirmed')
            no_pane = self.row()
            self.assertEqual(lessons.reinforce(PID, no_pane['id'], '%2')['status'], 'trial')
        with own_time(lessons, time=Mock(return_value=100 + lessons.DAY - 1)):
            self.assertEqual(lessons.reinforce(PID, no_pane['id'])['status'], 'trial')
        with own_time(lessons, time=Mock(return_value=100 + lessons.DAY)):
            self.assertEqual(lessons.reinforce(PID, no_pane['id'])['status'], 'confirmed')

    def test_dispute_once_per_pane_keep_and_edit_clear_disputes(self):
        row = self.row()
        disputed = lessons.dispute(PID, row['id'], '%1', ' Tests\n show\x00 this. ' + 'x' * 250)
        self.assertTrue(lessons.disputed(disputed))
        self.assertTrue(lessons.entries(PID)[0]['disputed'])
        self.assertEqual(len(disputed['disputes'][0]['reason']), 200)
        before = lessons.path(PID).read_bytes()
        with self.assertRaisesRegex(ValueError, 'This tab already disputed this lesson.'):
            lessons.dispute(PID, row['id'], '%1', 'Another reason')
        self.assertEqual(lessons.path(PID).read_bytes(), before)
        for index in range(2, 21):
            lessons.dispute(PID, row['id'], f'%{index}', 'Tests show this.')
        with self.assertRaisesRegex(ValueError, '20 disputes'):
            lessons.dispute(PID, row['id'], '%21', 'Tests show this.')
        with own_time(lessons, time=Mock(return_value=1234)):
            kept = lessons.keep(PID, row['id'])
            self.assertEqual((kept['status'], kept['disputes'], kept['reviewed_at']), ('confirmed', [], 1234))
            lessons.dispute(PID, row['id'], '%1', 'Evidence')
            edited = lessons.edit(PID, row['id'], 'When checks fail, read the test details.')
            self.assertEqual((edited['status'], edited['disputes'], edited['reviewed_at']), ('confirmed', [], 1234))
        lessons.promote(PID, row['id'])
        self.assertEqual(lessons.keep_global(row['id'])['status'], 'confirmed')
        self.assertIsNone(lessons.keep_global('00000000'))
        self.assertIsNone(lessons.keep(PID, row['id']))
        self.assertIsNone(lessons.dispute(PID, '00000000', '%1', 'Evidence'))
        self.assertTrue(lessons.dispute(PID, row['id'], '%1', 'Evidence')['disputes'])
        self.assertTrue(lessons.for_prompt(PID, 'The checks fail: read the test details.')['rows'][0]['disputed'])
        self.assertEqual(lessons.keep_global(row['id'])['disputes'], [])

    def test_promotion_refuses_trial_and_disputed_without_writes(self):
        row = self.row()
        for confirm in (False, True):
            if confirm:
                lessons.keep(PID, row['id'])
                lessons.dispute(PID, row['id'], '%2', 'Tests disagree.')
            before = lessons.path(PID).read_bytes()
            with patch.object(lessons, '_write') as write:
                with self.assertRaisesRegex(ValueError, 'Only a confirmed lesson can be used in all projects.'):
                    lessons.promote(PID, row['id'])
                write.assert_not_called()
            self.assertEqual(lessons.path(PID).read_bytes(), before)
            self.assertFalse(lessons.global_path().exists())

    def test_usable_lessons_put_confirmed_first_and_leave_out_disputed_trials(self):
        confirmed = []
        for index in range(3):
            confirmed.append(self.row(text=f'When confirmed case {index} occurs, inspect it.'))
            lessons.keep(PID, confirmed[-1]['id'])
        trials = [self.row(text=f'When trial case {index} occurs, inspect it.') for index in range(3)]
        usable = lessons.usable(PID)
        self.assertEqual([row['status'] for row in usable], ['confirmed'] * 3 + ['trial'] * 3)
        lessons.dispute(PID, trials[0]['id'], '%1', 'Evidence differs.')
        lessons.dispute(PID, confirmed[0]['id'], '%1', 'Evidence differs.')
        usable = lessons.usable(PID)
        self.assertEqual({row['id'] for row in usable}, {row['id'] for row in confirmed + trials[1:]})
        self.assertEqual((usable[-1]['id'], usable[-1]['disputed']), (confirmed[0]['id'], True), 'a disputed one is last')
        found = lessons.for_prompt(PID, 'A trial case occurs.')
        self.assertEqual({row['id'] for row in found['rows']}, {row['id'] for row in confirmed + trials[1:]})
        self.assertEqual(found['usable'], usable)

    def test_review_due_reasons_gap_snooze_done_and_empty(self):
        start = 1000000
        first = lessons.review(now=start)
        self.assertEqual((first['due'], first['last_review'], first['snoozed_until']), (False, start, 0))
        self.assertEqual(lessons.review_path().stat().st_mode & 0o777, 0o600)
        self.assertFalse(lessons.review(now=start + 100 * lessons.DAY)['due'])
        with own_time(lessons, time=Mock(return_value=start + 1)):
            rows = [self.row() for _ in range(5)]
        self.assertFalse(lessons.review(now=start + 3 * lessons.DAY - 1)['due'])
        due = lessons.review(now=start + 3 * lessons.DAY)
        self.assertEqual((due['due'], due['reason'], due['new']), (True, 'new', 5))
        lessons.review_snooze(now=start + 3 * lessons.DAY)
        self.assertFalse(lessons.review(now=start + 6 * lessons.DAY - 1)['due'])
        self.assertTrue(lessons.review(now=start + 6 * lessons.DAY)['due'])
        lessons.review_done(now=start + 6 * lessons.DAY)
        self.assertEqual(lessons.review()['snoozed_until'], 0)
        self.assertFalse(lessons.review(now=start + 20 * lessons.DAY - 1)['due'])
        self.assertEqual(lessons.review(now=start + 20 * lessons.DAY)['reason'], 'time')
        lessons.dispute(PID, rows[0]['id'], '%1', 'Evidence differs.')
        self.assertEqual(lessons.review(now=start + 6 * lessons.DAY)['reason'], 'disputed')
        lessons.review_snooze(now=start + 6 * lessons.DAY)
        self.assertFalse(lessons.review(now=start + 6 * lessons.DAY)['due'])
        self.assertEqual(len(lessons.entries(PID)), 5)

    def test_review_groups_flags_scopes_names_and_skipped_projects(self):
        now = 100 * lessons.DAY
        lessons.review_done(now=now - 10)
        other_pid = '012345abcdef'
        (paths.TETHER / 'projects' / other_pid).mkdir()
        other = lessons.add(other_pid, 'When a check fails, inspect its output.', 'keep', 'user', '')['lesson']
        (lessons.path(PID).parent / 'project.json').write_text(json.dumps({'name': 'Test project'}))
        with own_time(lessons, time=Mock(return_value=now - 31 * lessons.DAY)):
            stale = self.row()
            stuck = self.row()
            dispute = self.row()
            older = self.row()
        lessons.keep(PID, older['id'])
        lessons.reinforce(PID, stuck['id'])
        lessons.reinforce(PID, stuck['id'])
        lessons.dispute(PID, dispute['id'], '%1', 'Evidence differs.')
        with own_time(lessons, time=Mock(return_value=now)):
            new = self.row()
            global_row = self.row()
        lessons.keep(PID, global_row['id'])
        lessons.promote(PID, global_row['id'])
        broken_pid = '111111111111'
        folder = paths.TETHER / 'projects' / broken_pid
        folder.mkdir()
        (folder / 'lessons.json').write_text('bad json')
        (folder.parent / 'not-a-project').mkdir()
        result = lessons.review(now=now)
        self.assertEqual(result['skipped'], [broken_pid])
        self.assertEqual([group['key'] for group in result['groups']], ['disputed', 'stuck', 'stale', 'new', 'other'])
        by_id = {row['id']: (group['key'], row) for group in result['groups'] for row in group['lessons']}
        for row, key in ((stale, 'stale'), (stuck, 'stuck'), (dispute, 'disputed'), (older, 'other'), (new, 'new')):
            self.assertEqual(by_id[row['id']][0], key)
        project_row = by_id[new['id']][1]
        self.assertEqual((project_row['project'], project_row['project_name']), (PID, 'Test project'))
        self.assertIn({'type': 'repeat', 'with': older['id']}, project_row['flags'])
        self.assertNotIn({'type': 'conflict', 'with': other['id']}, project_row['flags'])
        self.assertEqual(by_id[other['id']][1]['project_name'], other_pid)
        global_entry = by_id[global_row['id']][1]
        self.assertEqual((global_entry['project'], global_entry['project_name']), (None, None))
        self.assertIn({'type': 'conflict', 'with': other['id']}, global_entry['flags'])
        self.assertEqual(result['total'], 7)

    def test_review_state_rejects_corruption_and_atomic_failure_preserves_it(self):
        lessons.review(now=100)
        file = lessons.review_path()
        before = file.read_bytes()
        with patch.object(lessons.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                lessons.review_done(now=200)
        self.assertEqual(file.read_bytes(), before)
        self.assertFalse(list(file.parent.glob('.lessons-*')))
        for data in ([], {}, {'version': 1, 'last_review': True, 'snoozed_until': 0},
                     {'version': 1, 'last_review': 0, 'snoozed_until': float('inf')}):
            file.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                lessons.review(now=300)
