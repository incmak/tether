"""prune.py: the clean-up list of stale lessons and notes (B211). Nothing goes without the person's tap."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import lessons
import paths
import projects
import prune

PID, OTHER = 'a' * 12, 'b' * 12
DAY = 86400


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in (('CONFIG', root / 'config'), ('TETHER', root / 'tether'), ('CACHE', root / 'cache')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.now = time.time()
        for pid, name in ((PID, 'tether'), (OTHER, 'briskbot')):
            projects.bundle(pid).mkdir(parents=True)
            projects.set_name(pid, name)

    def lesson(self, text, age, pid=PID, hits=1, reviewed=None):
        row = lessons.add(pid, text, 'avoid', 'user', 'claude', True)['lesson']
        file = lessons.path(pid)
        data = json.loads(file.read_text())
        for item in data['lessons']:
            if item['id'] == row['id']:
                item.update(created=self.now - age * DAY, last_hit=self.now - age * DAY, hits=hits, reviewed_at=reviewed)
        file.write_text(json.dumps(data))
        return row['id']

    def note(self, name, age, pid=PID, text='# Title of the note\nbody\n'):
        projects.write_note(pid, name, text)
        file = projects.bundle(pid) / 'notes' / name
        os.utime(file, (self.now - age * DAY, self.now - age * DAY))
        return file

    def remove(self, kind, pid, name, now=None, mark=None):
        now = self.now if now is None else now
        if mark is None:
            state = prune.state(now)
            mark = next((item['mark'] for item in state['lessons' if kind == 'lesson' else 'notes']
                         if (item['id'] if kind == 'lesson' else item['name']) == name and item['project'] == pid), 'none')
        return prune.remove(kind, pid, name, mark, now)

    def keys(self, kind='lessons'):
        return [item['id'] if kind == 'lessons' else item['name'] for item in prune.state(self.now)[kind]]


class StaleTests(Case):
    def test_only_old_lessons_that_never_repeated(self):
        old = self.lesson('When a check fails, read the first error.', 31)
        self.lesson('When a build is slow, profile the bundler first.', 29)
        self.lesson('When tabs overlap, measure the row height.', 90, hits=2)
        self.lesson('When a deploy starts, read the deploy log.', 90, reviewed=self.now - DAY)  # the person kept it
        self.assertEqual([row['id'] for row in prune.stale(self.now)['lessons']], [old])

    def test_only_notes_untouched_for_30_days_oldest_first(self):
        self.note('old.md', 45)
        self.note('older.md', 80, OTHER)
        self.note('fresh.md', 29)
        (projects.bundle(PID) / 'notes' / 'postmortems').mkdir()
        (projects.bundle(PID) / 'notes' / 'postmortems' / '2026-01-01.md').write_text('x')
        found = prune.stale(self.now)['notes']
        self.assertEqual([(note['name'], note['project_name'], note['title']) for note in found],
                         [('older.md', 'briskbot', 'Title of the note'), ('old.md', 'tether', 'Title of the note')])

    def test_the_list_shows_only_what_the_weekly_check_flagged(self):
        self.note('old.md', 45)
        self.assertEqual(self.keys('notes'), [])  # no check ran yet
        self.assertEqual(prune.check(self.now), {'lessons': 0, 'notes': 1})
        self.note('later.md', 45)
        self.assertEqual(self.keys('notes'), ['old.md'])
        state = prune.state(self.now)
        self.assertEqual((state['last_run'], state['next_run']), (self.now, self.now + 7 * DAY))
        self.assertEqual((state['stale_days'], state['note_days'], state['undo_days']), (30, 30, 7))

    def test_a_flagged_note_that_changed_leaves_the_list(self):
        file = self.note('old.md', 45)
        prune.check(self.now)
        os.utime(file, (self.now, self.now))
        self.assertEqual(self.keys('notes'), [])

    def test_the_check_removes_nothing(self):
        self.note('old.md', 45)
        lesson = self.lesson('When a check fails, read the first error.', 60)
        prune.check(self.now)
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())
        self.assertEqual([row['id'] for row in lessons.entries(PID)], [lesson])


class ActionTests(Case):
    def setUp(self):
        super().setUp()
        self.lesson_id = self.lesson('When a check fails, read the first error.', 60)
        self.global_id = self.lesson('When a tab is slow, read the server log.', 60)
        lessons.keep(PID, self.global_id)
        lessons.promote(PID, self.global_id)
        data = json.loads(lessons.global_path().read_text())
        data['lessons'][0].update(reviewed_at=None)
        lessons.global_path().write_text(json.dumps(data))
        self.note('old.md', 45)
        prune.check(self.now)

    def test_remove_and_undo_a_lesson(self):
        before = lessons.find(PID, self.lesson_id)
        token = self.remove('lesson', PID, self.lesson_id)
        self.assertEqual(lessons.entries(PID), [])
        state = prune.state(self.now)
        self.assertEqual(self.keys(), [self.global_id])
        self.assertEqual([(item['token'], item['kind'], item['text'], item['project_name'], item['until']) for item in state['removed']],
                         [(token, 'lesson', 'When a check fails, read the first error.', 'tether', self.now + 7 * DAY)])
        prune.restore(token, self.now + 6 * DAY)
        self.assertEqual(lessons.find(PID, self.lesson_id), before)
        state = prune.state(self.now + 6 * DAY)
        self.assertEqual(state['removed'], [])
        prune.check(self.now + 6 * DAY)
        self.assertNotIn(self.lesson_id, [row['id'] for row in prune.state(self.now + 6 * DAY)['lessons']])  # undo keeps it

    def test_remove_and_undo_a_global_lesson(self):
        token = self.remove('lesson', None, self.global_id)
        self.assertEqual(lessons.global_entries(), [])
        self.assertEqual(prune.state(self.now)['removed'][0]['project_name'], 'All projects')
        prune.restore(token, self.now)
        self.assertEqual([row['id'] for row in lessons.global_entries()], [self.global_id])

    def test_remove_and_undo_a_note(self):
        notes = projects.bundle(PID) / 'notes'
        text = (notes / 'old.md').read_text()
        token = self.remove('note', PID, 'old.md')
        self.assertFalse((notes / 'old.md').exists())
        self.assertEqual(len(list((notes / '.trash').iterdir())), 1)
        self.assertEqual(prune.state(self.now)['removed'][0]['text'], 'Title of the note')
        prune.restore(token, self.now + DAY)
        self.assertEqual((notes / 'old.md').read_text(), text)
        self.assertEqual(list((notes / '.trash').iterdir()), [])
        self.assertEqual(self.keys('notes'), [])

    def test_undo_never_replaces_a_newer_note(self):
        token = self.remove('note', PID, 'old.md')
        projects.write_note(PID, 'old.md', 'new text')
        with self.assertRaisesRegex(prune.PruneError, 'A note named old.md exists now'):
            prune.restore(token, self.now)
        self.assertEqual((projects.bundle(PID) / 'notes' / 'old.md').read_text(), 'new text')
        self.assertEqual(len(prune.state(self.now)['removed']), 1)  # still there to undo after a rename

    def test_undo_ends_after_7_days_and_the_note_file_stays_in_the_trash(self):
        lesson = self.remove('lesson', PID, self.lesson_id)
        note = self.remove('note', PID, 'old.md')
        late = self.now + 7 * DAY + 60
        for token in (lesson, note):
            with self.assertRaisesRegex(prune.PruneError, 'Undo works for 7 days'):
                prune.restore(token, late)
        self.assertEqual(prune.state(late)['removed'], [])
        self.assertEqual(json.loads(prune.path().read_text())['trash'], [])
        self.assertEqual(len(list((projects.bundle(PID) / 'notes' / '.trash').iterdir())), 1)  # Tether deletes no note file

    def test_keep_takes_the_item_off_the_list_until_it_is_stale_again(self):
        prune.keep('note', PID, 'old.md', self.now)
        prune.keep('lesson', PID, self.lesson_id, self.now)
        self.assertEqual((self.keys('notes'), self.keys()), ([], [self.global_id]))
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())
        self.assertEqual(lessons.find(PID, self.lesson_id)['status'], 'trial')  # keep here does not confirm the lesson
        prune.check(self.now + 29 * DAY)
        self.assertEqual(prune.state(self.now + 29 * DAY)['notes'], [])
        prune.check(self.now + 31 * DAY)
        self.assertEqual([note['name'] for note in prune.state(self.now + 31 * DAY)['notes']], ['old.md'])

    def test_only_flagged_items_can_go(self):
        fresh = self.lesson('When a build is slow, profile the bundler first.', 1)
        self.note('fresh.md', 1)
        for kind, name in (('lesson', fresh), ('note', 'fresh.md'), ('lesson', '0' * 8), ('note', 'none.md')):
            for action in (lambda *args: prune.remove(*args[:3], 'x', self.now), prune.keep):
                with self.assertRaisesRegex(prune.PruneError, 'no longer in the clean-up list'):
                    action(kind, PID, name, self.now)
        self.assertEqual(len(lessons.entries(PID)), 2)
        self.assertTrue((projects.bundle(PID) / 'notes' / 'fresh.md').is_file())

    def test_a_stale_button_does_not_remove_an_item_that_changed(self):
        state = prune.state(self.now)
        note_mark = state['notes'][0]['mark']
        lesson_mark = next(row['mark'] for row in state['lessons'] if row['id'] == self.lesson_id)
        file = projects.bundle(PID) / 'notes' / 'old.md'
        text = file.read_text()
        file.write_text(text + 'more')  # someone edits the note; the old page still shows Remove
        os.utime(file, (self.now - 45 * DAY, self.now - 45 * DAY))  # even with the old time, the text differs
        with self.assertRaisesRegex(prune.PruneError, 'changed after the list loaded'):
            prune.remove('note', PID, 'old.md', note_mark, self.now)
        self.assertTrue(file.is_file())
        lessons.reinforce(PID, self.lesson_id)  # the lesson repeated: it is no longer stale
        with self.assertRaisesRegex(prune.PruneError, 'no longer in the clean-up list'):
            prune.remove('lesson', PID, self.lesson_id, lesson_mark, self.now)
        self.assertIsNotNone(lessons.find(PID, self.lesson_id))
        for mark in (None, 5, '', 'wrong'):
            with self.assertRaises(prune.PruneError):
                prune.remove('lesson', None, self.global_id, mark, self.now)
        self.assertEqual(prune.state(self.now)['removed'], [])

    def test_a_note_kept_in_another_tab_is_not_removed_by_an_old_button(self):
        mark = prune.state(self.now)['notes'][0]['mark']
        prune.keep('note', PID, 'old.md', self.now)
        with self.assertRaisesRegex(prune.PruneError, 'no longer in the clean-up list'):
            prune.remove('note', PID, 'old.md', mark, self.now)
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())

    def test_a_full_undo_list_refuses_a_removal_and_drops_no_record(self):
        first = self.remove('lesson', PID, self.lesson_id)
        with mock.patch.object(prune, 'TRASH', 1):
            with self.assertRaisesRegex(prune.PruneError, '1 removals wait in Undo'):
                self.remove('note', PID, 'old.md')
            self.assertEqual([item['token'] for item in prune.state(self.now + 1)['removed']], [first])
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())
        prune.restore(first, self.now + 1)

    def test_a_lesson_that_repeats_just_before_the_write_stays(self):
        mark = next(row['mark'] for row in prune.state(self.now)['lessons'] if row['id'] == self.lesson_id)
        real = lessons.take

        def take(pid, identifier, before):  # the reinforce lands after every check that prune made on its own
            lessons.reinforce(pid, identifier)
            return real(pid, identifier, before)
        with mock.patch.object(lessons, 'take', take):
            with self.assertRaises(prune.PruneError):
                prune.remove('lesson', PID, self.lesson_id, mark, self.now)
        self.assertEqual(lessons.find(PID, self.lesson_id)['hits'], 2)
        self.assertEqual(prune.state(self.now)['removed'], [])
        self.assertEqual(json.loads(prune.path().read_text())['trash'], [])

    def test_take_checks_the_row_under_the_lesson_lock(self):
        seen = []
        self.assertIsNone(lessons.take(PID, '0' * 8, seen.append))
        with self.assertRaises(RuntimeError):
            lessons.take(PID, self.lesson_id, mock.Mock(side_effect=RuntimeError('no')))
        self.assertIsNotNone(lessons.find(PID, self.lesson_id))
        row = lessons.take(PID, self.lesson_id, seen.append)
        self.assertEqual((seen, lessons.find(PID, self.lesson_id)), ([row], None))

    def test_a_note_with_other_text_of_the_same_size_and_time_stays(self):
        mark = prune.state(self.now)['notes'][0]['mark']
        file = projects.bundle(PID) / 'notes' / 'old.md'
        stat = file.stat()
        file.write_text(file.read_text().replace('body', 'BODY'))
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(file.stat().st_size, stat.st_size)
        with self.assertRaisesRegex(prune.PruneError, 'changed after the list loaded'):
            prune.remove('note', PID, 'old.md', mark, self.now)
        self.assertIn('BODY', file.read_text())
        self.assertEqual(list((file.parent / '.trash').iterdir()), [])
        self.assertEqual(prune.state(self.now)['removed'], [])

    def test_a_note_replaced_during_the_move_keeps_both_texts(self):
        mark = prune.state(self.now)['notes'][0]['mark']
        notes = projects.bundle(PID) / 'notes'
        real = os.replace

        def replace(source, target):  # an agent edits the note, then writes it again right after the move
            if str(target).endswith('old.md') and '.trash' in str(target):
                Path(source).write_text('edited by an agent')
                real(source, target)
                (notes / 'old.md').write_text('written again')
            else:
                real(source, target)
        with mock.patch.object(prune.os, 'replace', replace):
            with self.assertRaisesRegex(prune.NoteInTrash, 'use Undo'):
                prune.remove('note', PID, 'old.md', mark, self.now)
        self.assertEqual((notes / 'old.md').read_text(), 'written again')
        self.assertEqual([file.read_text() for file in (notes / '.trash').iterdir()], ['edited by an agent'])
        removed = prune.state(self.now)['removed']
        self.assertEqual(len(removed), 1)  # the older text is in the trash, so its way back stays
        with self.assertRaisesRegex(prune.PruneError, 'A note named old.md exists now'):
            prune.restore(removed[0]['token'], self.now)
        (notes / 'old.md').rename(notes / 'newer.md')
        prune.restore(removed[0]['token'], self.now)
        self.assertEqual((notes / 'old.md').read_text(), 'edited by an agent')

    def test_a_note_that_cannot_be_read_after_the_move_goes_back(self):
        notes = projects.bundle(PID) / 'notes'
        text = (notes / 'old.md').read_text()
        mark = prune.state(self.now)['notes'][0]['mark']
        with mock.patch.object(prune, '_note_mark', side_effect=OSError('read error')):
            with self.assertRaisesRegex(prune.PruneError, 'could not read the note'):
                prune.remove('note', PID, 'old.md', mark, self.now)
        self.assertEqual((notes / 'old.md').read_text(), text)
        self.assertEqual((list((notes / '.trash').iterdir()), prune.state(self.now)['removed']), ([], []))

    def test_a_note_that_cannot_go_back_keeps_its_undo_record(self):
        notes = projects.bundle(PID) / 'notes'
        text = (notes / 'old.md').read_text()
        mark = prune.state(self.now)['notes'][0]['mark']
        with mock.patch.object(prune, '_note_mark', side_effect=OSError('read error')), \
                mock.patch.object(prune.os, 'link', side_effect=OSError('disk')):
            with self.assertRaisesRegex(prune.NoteInTrash, 'use Undo'):
                prune.remove('note', PID, 'old.md', mark, self.now)
        self.assertFalse((notes / 'old.md').exists())
        removed = prune.state(self.now)['removed']
        self.assertEqual(len(removed), 1)
        prune.restore(removed[0]['token'], self.now)
        self.assertEqual((notes / 'old.md').read_text(), text)

    def test_a_note_move_that_fails_leaves_the_note_and_no_undo_record(self):
        real = os.replace

        def replace(source, target):
            if '.trash' in str(target):
                raise OSError('disk')
            real(source, target)
        with mock.patch.object(prune.os, 'replace', replace):
            with self.assertRaises(OSError):
                self.remove('note', PID, 'old.md')
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())
        self.assertEqual(prune.state(self.now)['removed'], [])

    def test_the_undo_record_is_saved_before_the_note_moves(self):
        seen = []
        real = prune._move_note

        def move(pid, name, kept_as, mark):
            seen.append([item.get('file') for item in json.loads(prune.path().read_text())['trash']])
            return real(pid, name, kept_as, mark)
        with mock.patch.object(prune, '_move_note', move):
            self.remove('note', PID, 'old.md')
        self.assertEqual(len(seen[0]), 1)
        self.assertTrue((projects.bundle(PID) / 'notes' / '.trash' / seen[0][0]).is_file())

    def test_bad_requests(self):
        for kind, pid, name in (('file', PID, 'old.md'), ('note', None, 'old.md'), ('note', 'nope', 'old.md'),
                                ('note', PID, ''), ('lesson', PID, 7), ('note', '../' + PID, 'old.md')):
            with self.assertRaises(prune.PruneError):
                prune.remove(kind, pid, name, 'x', self.now)
        with self.assertRaises(prune.PruneError):
            prune.restore('nope', self.now)

    def test_a_removed_item_cannot_go_twice(self):
        self.remove('lesson', PID, self.lesson_id)
        with self.assertRaises(prune.PruneError):
            self.remove('lesson', PID, self.lesson_id)
        self.assertEqual(len(prune.state(self.now)['removed']), 1)

    def test_a_lesson_gone_elsewhere_leaves_no_undo_record(self):
        mark = prune.state(self.now)['lessons'][0]['mark']
        lessons.remove(PID, self.lesson_id)
        with self.assertRaisesRegex(prune.PruneError, 'no longer in the clean-up list'):
            prune.remove('lesson', PID, self.lesson_id, mark, self.now)
        self.assertEqual(prune.state(self.now)['removed'], [])

    def test_undo_refuses_a_full_list_and_keeps_the_record(self):
        token = self.remove('lesson', PID, self.lesson_id)
        for index in range(60):
            lessons.add(PID, f'When case {index} occurs, inspect the exact output.', 'avoid', 'user', 'claude', True)
        with self.assertRaisesRegex(prune.PruneError, 'This project has 60 lessons'):
            prune.restore(token, self.now)
        self.assertEqual(len(lessons.entries(PID)), 60)
        self.assertEqual(len(prune.state(self.now)['removed']), 1)

    def test_a_bad_state_file_is_never_overwritten(self):
        prune.path().write_text('{"version": 2}')
        for work in (lambda: prune.state(self.now), lambda: prune.check(self.now), lambda: self.remove('note', PID, 'old.md')):
            with self.assertRaisesRegex(prune.PruneError, 'invalid'):
                work()
        self.assertEqual(prune.path().read_text(), '{"version": 2}')
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())


class PrunerTests(Case):
    def test_weekly_check_and_one_low_notice(self):
        self.note('old.md', 45)
        self.lesson('When a check fails, read the first error.', 60)
        clock, sent = [self.now], []
        pruner = prune.Pruner(lambda payload, urgent: sent.append((payload, urgent)), lambda: {'low': 'sound'}, clock=lambda: clock[0])
        self.assertTrue(pruner.observe(wait=True))
        self.assertEqual(pruner.counts, {'lessons': 1, 'notes': 1})
        self.assertFalse(pruner.observe(wait=True))  # at most one look a minute
        clock[0] += 6 * DAY
        pruner.observe(wait=True)
        self.assertEqual(len(sent), 1)
        clock[0] += DAY + 120
        pruner.observe(wait=True)
        self.assertEqual(len(sent), 2)
        payload, urgent = sent[0]
        self.assertFalse(urgent)
        self.assertEqual((payload['priority'], payload['go']), ('low', 'cleanup'))
        self.assertIn('1 lesson and 1 note look stale', payload['body'])
        self.assertTrue((projects.bundle(PID) / 'notes' / 'old.md').is_file())

    def test_no_notice_when_nothing_is_stale_or_low_is_off(self):
        sent = []
        prune.Pruner(lambda *args: sent.append(args), lambda: {'low': 'sound'}, clock=lambda: self.now).observe(wait=True)
        self.note('old.md', 45)
        prune.path().unlink()
        prune.Pruner(lambda *args: sent.append(args), lambda: {'low': 'off'}, clock=lambda: self.now).observe(wait=True)
        self.assertEqual(sent, [])
        self.assertEqual(self.keys('notes'), ['old.md'])

    def test_a_failed_check_frees_the_pruner(self):
        pruner = prune.Pruner(clock=lambda: self.now)
        with mock.patch.object(prune, 'check', side_effect=OSError('disk')), mock.patch('builtins.print'):
            self.assertTrue(pruner.observe(wait=True))
        self.assertFalse(pruner.busy)


if __name__ == '__main__':
    unittest.main()
