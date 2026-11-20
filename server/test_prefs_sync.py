import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import prefs_sync


class PrefsSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(prefs_sync, 'FILE', Path(self.folder.name) / 'prefs.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)

    def test_default_session_folder_round_trip_and_resolution(self):
        home = Path(self.folder.name)
        project = home / 'project with spaces'
        project.mkdir()
        with mock.patch.object(prefs_sync.paths, 'HOME', home):
            self.assertEqual(prefs_sync.session_folder(), str(home))
            prefs_sync.save({'sessionFolder': '~/project with spaces'})
            self.assertEqual(prefs_sync.load()['prefs']['sessionFolder'], '~/project with spaces')
            self.assertEqual(prefs_sync.session_folder(), str(project))
            prefs_sync.save({'sessionFolder': str(project)})
            self.assertEqual(prefs_sync.session_folder(), str(project))
            project.rmdir()
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                prefs_sync.session_folder()
            prefs_sync.save({'sessionFolder': ''})
            self.assertEqual(prefs_sync.session_folder(), str(home))

    def test_invalid_default_session_folder_does_not_change_preferences(self):
        home = Path(self.folder.name)
        file = home / 'file'
        file.write_text('text')
        with mock.patch.object(prefs_sync.paths, 'HOME', home):
            for value in ('relative/path', '~other/folder', '~/missing', str(file), 'a' * 1025, '~/x\n', None, 42):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    prefs_sync.save({'sessionFolder': value})
        self.assertEqual(prefs_sync.load()['version'], 0)

    def test_bad_entries_name_the_key_and_do_not_save_the_batch(self):
        for key, value in (('drafts', {'%1': {'text': 5, 'at': 1}}),
                           ('repos', {'/project': [42]}),
                           ('waitingOrder', 'sideways')):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, rf'^Invalid preference: {key}\.$'):
                    prefs_sync.save({'theme': 'dark', key: value})
                self.assertEqual(prefs_sync.load(), {'prefs': {}, 'version': 0})
                self.assertFalse(prefs_sync.FILE.exists())
        original = prefs_sync.save({'theme': 'light'})
        contents = prefs_sync.FILE.read_bytes()
        with self.assertRaisesRegex(ValueError, r'^Invalid preference: drafts\.$'):
            prefs_sync.save({'theme': 'dark', 'drafts': {'%1': {}}})
        self.assertEqual(prefs_sync.load(), original)
        self.assertEqual(prefs_sync.FILE.read_bytes(), contents)

    def test_session_folder_keeps_its_validation_message(self):
        for value, message in (('relative/path', r'^Use an absolute folder path'),
                               (42, r'^Use an absolute folder path'),
                               (str(Path(self.folder.name) / 'missing'), r'^The default session folder is unavailable\.')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                prefs_sync.save({'theme': 'dark', 'sessionFolder': value})
        self.assertEqual(prefs_sync.load(), {'prefs': {}, 'version': 0})
        self.assertFalse(prefs_sync.FILE.exists())

    def test_empty_store_is_version_zero(self):
        self.assertEqual(prefs_sync.load(), {'prefs': {}, 'version': 0})

    def test_changed_keys_merge_and_bump_version(self):
        prefs_sync.save({'pinned': ['$1'], 'theme': 'dark'})
        stored = prefs_sync.save({'paneOrder': {'$1': ['%2', '%1']}})
        self.assertEqual(stored['version'], 2)
        self.assertEqual(stored['prefs'], {'pinned': ['$1'], 'theme': 'dark', 'paneOrder': {'$1': ['%2', '%1']}})
        self.assertEqual(prefs_sync.load(), stored)

    def test_color_scheme_syncs(self):
        # The page syncs 'scheme' with every first save; refusing it failed every prefs save from that device.
        self.assertEqual(prefs_sync.save({'scheme': 'claude', 'theme': 'dark'})['prefs'], {'scheme': 'claude', 'theme': 'dark'})

    def test_waiting_order_round_trip(self):
        for value in ('newest', 'oldest'):
            with self.subTest(value=value):
                stored = prefs_sync.save({'waitingOrder': value})
                self.assertEqual(stored['prefs']['waitingOrder'], value)
                self.assertEqual(prefs_sync.load(), stored)

    def test_invalid_waiting_order_does_not_save(self):
        for value in ('sideways', 42):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, r'^Invalid preference: waitingOrder\.$'):
                prefs_sync.save({'waitingOrder': value})
        self.assertEqual(prefs_sync.load()['version'], 0)

    def test_main_model_round_trip_and_rejects_unknown(self):
        for value in ('auto', 'claude', 'codex', 'none'):
            with self.subTest(value=value):
                self.assertEqual(prefs_sync.save({'mainModel': value})['prefs']['mainModel'], value)
        for value in ('gpt', 7):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, r'^Invalid preference: mainModel\.$'):
                prefs_sync.save({'mainModel': value})

    def test_sidebar_preferences_round_trip(self):
        values = {'colors': {'$6': 'red', '%12': 'purple'},
                  'groups': [{'id': 'project-1', 'name': ' Project '}],
                  'groupOf': {'$6': 'project-1'}, 'groupBy': 'custom', 'sort': 'name'}
        expected = dict(values, groups=[{'id': 'project-1', 'name': 'Project'}])
        self.assertEqual(prefs_sync.save(values)['prefs'], expected)
        self.assertEqual(prefs_sync.load()['prefs'], expected)
        for group_by in ('custom', 'folder', 'agent', 'none'):
            self.assertEqual(prefs_sync.save({'groupBy': group_by})['prefs']['groupBy'], group_by)
        for sort in ('smart', 'manual', 'name', 'recent', 'legacy-unknown'):
            self.assertEqual(prefs_sync.save({'sort': sort})['prefs']['sort'], sort)

    def test_sidebar_colors_drop_invalid_entries(self):
        colors = {'$1': 'red', '%2': 'orange', '$3': 'yellow', '%4': 'green', '$5': 'blue', '%6': 'purple',
                  '$7': 'pink', '$8': [], 'session': 'red', '%x': 'red', '$-1': 'red', '$1\n': 'red', 1: 'red'}
        self.assertEqual(prefs_sync.save({'colors': colors})['prefs']['colors'],
                         {'$1': 'red', '%2': 'orange', '$3': 'yellow', '%4': 'green', '$5': 'blue', '%6': 'purple'})

    def test_sidebar_groups_drop_invalid_items_and_keep_twenty(self):
        bad = [None, [], 'name', {}, {'id': 'Uppercase', 'name': 'No'}, {'id': 'bad_id', 'name': 'No'},
               {'id': '', 'name': 'No'}, {'id': 'a' * 41, 'name': 'No'}, {'id': 1, 'name': 'No'},
               {'id': 'valid', 'name': None}, {'id': 'valid', 'name': ' '}, {'id': 'valid', 'name': 'n' * 41}]
        good = [{'id': str(index).ljust(40, 'a'), 'name': '  ' + 'n' * 40 + '  '} for index in range(21)]
        self.assertEqual(prefs_sync.save({'groups': bad + good})['prefs']['groups'],
                         [{'id': item['id'], 'name': 'n' * 40} for item in good[:20]])

    def test_sidebar_group_assignments_drop_bad_ids(self):
        assignments = {'$6': 'project-1', '%12': 'project-1', 'session': 'project-1', '$7': '',
                       '$8': 'Uppercase', '$9': 'a' * 41, '$10': [], '$11': 'a' * 40}
        self.assertEqual(prefs_sync.save({'groupOf': assignments})['prefs']['groupOf'],
                         {'$6': 'project-1', '$11': 'a' * 40})

    def test_sidebar_keys_use_last_write_wins_and_can_clear(self):
        prefs_sync.save({'colors': {'$1': 'red'}, 'groups': [{'id': 'one', 'name': 'One'}],
                         'groupOf': {'$1': 'one'}, 'groupBy': 'folder', 'theme': 'dark'})
        stored = prefs_sync.save({'colors': {'%2': 'blue'}, 'groups': [], 'groupOf': {}, 'groupBy': 'none'})
        self.assertEqual(stored['prefs'], {'colors': {'%2': 'blue'}, 'groups': [], 'groupOf': {},
                                          'groupBy': 'none', 'theme': 'dark'})
        self.assertEqual(prefs_sync.save({'colors': {}})['prefs']['colors'], {})
        self.assertEqual(prefs_sync.load()['version'], 3)

    def test_sidebar_bad_types_and_group_mode_do_not_save(self):
        for change in ({'colors': []}, {'groups': {}}, {'groupOf': []}, {'groupBy': 'unknown'},
                       {'groupBy': []}, {'sort': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prefs_sync.save(change)
        self.assertEqual(prefs_sync.load()['version'], 0)

    def test_side_pins_round_trip(self):
        pins = [{'pane': '%12', 'note': 'Review the plan'}, {'pane': '%3'}]
        stored = prefs_sync.save({'sidePins': pins})
        self.assertEqual(stored['prefs']['sidePins'], pins)
        self.assertEqual(prefs_sync.load(), stored)
        limits = [{'pane': str(index).ljust(64, 'x'), 'note': 'n' * 60} for index in range(40)]
        self.assertEqual(prefs_sync.save({'sidePins': limits})['prefs']['sidePins'], limits)
        self.assertEqual(prefs_sync.save({'sidePins': []})['prefs']['sidePins'], [])

    def test_rejects_invalid_side_pins(self):
        bad = [None, {}, '%1', [None], ['%1'], [{}], [{'note': 'Missing pane'}],
               [{'pane': 1}], [{'pane': ''}], [{'pane': 'p' * 65}],
               [{'pane': '%1', 'note': None}], [{'pane': '%1', 'note': 1}],
               [{'pane': '%1', 'note': 'n' * 61}], [{'pane': '%1', 'extra': True}],
               [{'pane': '%1'}, {'pane': '%1', 'note': 'Duplicate'}],
               [{'pane': str(index)} for index in range(41)]]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r'^Invalid preference: sidePins\.$'):
                    prefs_sync.save({'sidePins': value})
        self.assertEqual(prefs_sync.load()['version'], 0)

    def test_seen_keeps_the_latest_mark_per_pane(self):
        prefs_sync.save({'seen': {'%1': 200, '%2': 100}})
        stored = prefs_sync.save({'seen': {'%1': 150, '%3': 300}})
        self.assertEqual(stored['prefs']['seen'], {'%2': 100, '%1': 200, '%3': 300})

    def test_drafts_keep_the_newest_edit_and_old_clears_expire(self):
        now = time.time()
        prefs_sync.save({'drafts': {'%1': {'text': 'phone', 'at': now - 10}, '%2': {'text': 'keep', 'at': now - 30}}})
        stored = prefs_sync.save({'drafts': {'%1': {'text': 'older desktop copy', 'at': now - 20}, '%2': {'text': '', 'at': now}}})
        self.assertEqual(stored['prefs']['drafts'], {'%1': {'text': 'phone', 'at': now - 10}, '%2': {'text': '', 'at': now}})
        merged = prefs_sync.merge_drafts(stored['prefs']['drafts'], {}, now=now + prefs_sync.CLEARED_KEEP + 1)
        self.assertEqual(merged, {'%1': {'text': 'phone', 'at': now - 10}})
        with self.assertRaises(ValueError):
            prefs_sync.save({'drafts': {'%1': {'text': 5, 'at': 1}}})

    def test_archived_sessions_and_desktop_layout_sync(self):
        layout = {'desktop': {'count': 2, 'panes': ['%1', '%4'], 'focus': 0, 'cols': {'2': [0.6]}, 'row': 0.5, 'inbox': True, 'sidebar': True}}
        archived_tabs = {'%1': {'thread': 'old-thread', 'dir': '/work'}, '%4': {'thread': None, 'dir': '/shell'}}
        stored = prefs_sync.save({'archived': {'$3': 'old-work'}, 'archivedTabs': archived_tabs, 'layout': layout})
        self.assertEqual(stored['prefs'], {'archived': {'$3': 'old-work'}, 'archivedTabs': archived_tabs, 'layout': layout})
        self.assertEqual(prefs_sync.load(), stored)
        with self.assertRaises(ValueError):
            prefs_sync.save({'archived': ['$3']})
        with self.assertRaises(ValueError):
            prefs_sync.save({'archivedTabs': ['%1']})

    def test_helper_tab_filter_syncs(self):
        shown = {'%2': {'thread': 'review-thread', 'dir': '/work'}}
        stored = prefs_sync.save({'hideHelpers': False, 'shownHelpers': shown})
        self.assertEqual(stored['prefs'], {'hideHelpers': False, 'shownHelpers': shown})
        self.assertEqual(prefs_sync.load(), stored)
        for bad in ({'hideHelpers': 'yes'}, {'shownHelpers': ['%2']}):
            with self.assertRaises(ValueError):
                prefs_sync.save(bad)

    def test_rejects_unknown_or_badly_typed_keys(self):
        for changes in ({}, {'selected': '%1'}, {'pinned': 'x'}, {'showPaths': 1}, []):
            with self.assertRaises(ValueError):
                prefs_sync.save(changes)
        self.assertEqual(prefs_sync.load()['version'], 0)


if __name__ == '__main__':
    unittest.main()
