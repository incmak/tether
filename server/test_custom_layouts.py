import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import custom_layouts as cl

GOOD = {
    'name': 'Cockpit',
    'panels': ['waiting', 'chat', 'agents', 'status'],
    'grid': {'columns': [{'main': 'waiting', 'width': 250}, {'main': 'chat'}, {'main': 'status', 'width': 220}]},
    'style': {'radius': 4, 'gap': 6},
    'theme': {'bg': '#10151b', 'surface': '#17202a', 'text': '#e3e9ef', 'muted': '#8fa1b3', 'line': '#26323f', 'accent': '#3f8fcf', 'attention': '#e0a23a'},
}


def changed(**parts):
    data = json.loads(json.dumps(GOOD))
    data.update(parts)
    return data


class Validate(unittest.TestCase):
    def test_a_good_layout_comes_back_clean(self):
        layout = cl.validate(GOOD, 'cockpit')
        self.assertEqual(layout['id'], 'cockpit')
        self.assertEqual(layout['style'], {'radius': 4, 'gap': 6, 'surface': 'flat'})
        self.assertEqual(layout['grid']['columns'][1], {'main': 'chat'})
        self.assertEqual(layout['theme']['bg'], '#10151b')

    def test_bad_layouts_are_refused_with_a_reason(self):
        cases = [
            (changed(script='alert(1)'), 'unknown key'),
            (changed(panels=['chat', 'terminal']), 'panels must be'),
            (changed(panels=['waiting', 'status', 'agents']), 'must include chat'),
            (changed(style={'radius': 99}), 'style.radius'),
            (changed(style={'radius': True}), 'style.radius'),
            (changed(style={'surface': 'url(x)'}), 'style.surface'),
            (changed(theme=dict(GOOD['theme'], bg='red')), 'hex colour'),
            (changed(theme=dict(GOOD['theme'], accent='url(https://example.com/x)')), 'hex colour'),
            (changed(theme=dict(GOOD['theme'], css='body{}')), 'unknown key'),
            (changed(theme={'bg': '#000000'}), 'theme needs'),
            (changed(theme=dict(GOOD['theme'], text='#20262c')), 'contrast'),
            (changed(grid={'areas': ['wait main status']}), 'unknown key'),
            (changed(grid={'columns': [{'main': 'chat'}, {'main': 'chat'}]}), 'placed twice'),
            (changed(grid={'columns': [{'main': 'waiting'}, {'main': 'chat'}]}), 'status is in panels'),
            (changed(grid={'columns': [{'top': 'status', 'main': 'chat'}, {'main': 'waiting'}]}), 'can only be waiting'),
            (changed(grid={'columns': [{'main': 'chat'}, {'main': 'waiting', 'width': 900}, {'main': 'status'}]}), 'width'),
            (changed(panels=['chat'], grid={'columns': [{'main': 'chat'}], 'chatWidth': 100}), 'chatWidth'),
            (changed(id='classic'), 'built-in'),
            (changed(id='Bad Id'), 'id must'),
            (changed(name='x' * 41), 'name must'),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(cl.Bad) as caught:
                    cl.validate(data, 'cockpit')
                self.assertIn(reason, str(caught.exception))

    def test_activity_only_leads(self):
        data = changed(panels=['activity', 'chat'], grid={'columns': [{'main': 'chat'}, {'main': 'activity'}]})
        with self.assertRaises(cl.Bad):
            cl.validate(data, 'x')
        data['grid']['columns'].reverse()
        self.assertEqual(cl.validate(data, 'x')['grid']['columns'][0], {'main': 'activity'})


class Scan(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        cl._last_good.clear()

    def write(self, name, data):
        with open(os.path.join(self.folder, name), 'w') as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def test_missing_folder_is_empty(self):
        result = cl.scan(os.path.join(self.folder, 'none'))
        self.assertEqual((result['layouts'], result['errors']), ([], []))

    def test_a_bad_file_keeps_its_last_good_layout(self):
        self.write('Cockpit.json', GOOD)
        self.assertEqual(cl.scan(self.folder)['layouts'][0]['id'], 'cockpit')
        self.write('Cockpit.json', '{not json')
        result = cl.scan(self.folder)
        self.assertEqual(result['layouts'][0]['name'], 'Cockpit')
        self.assertIn('not valid JSON', result['layouts'][0]['stale'])
        self.assertEqual(result['errors'][0]['file'], 'Cockpit.json')

    def test_large_files_links_and_duplicate_ids_are_refused(self):
        self.write('big.json', ' ' * (cl.MAX_BYTES + 1))
        self.write('a.json', changed(id='same'))
        self.write('b.json', changed(id='same'))
        os.symlink(os.path.join(self.folder, 'a.json'), os.path.join(self.folder, 'link.json'))
        result = cl.scan(self.folder)
        self.assertEqual([layout['id'] for layout in result['layouts']], ['same'])
        reasons = {error['file']: error['reason'] for error in result['errors']}
        self.assertIn('16 KB', reasons['big.json'])
        self.assertIn('already uses', reasons['b.json'])
        self.assertIn('plain files', reasons['link.json'])

    def test_folder_label_hides_the_home_path(self):
        self.assertEqual(cl.folder_label(cl.paths.HOME / '.config/x'), '~/.config/x')


class VersionTwo(unittest.TestCase):
    def test_optional_keys_default_without_changing_old_files(self):
        clean = cl.validate(GOOD, 'cockpit')
        self.assertEqual((clean['variants'], clean['phone'], clean['manifest']), ({}, 'theme', 1))
        self.assertNotIn('variants', GOOD)

    def test_every_manifest_variant_option_is_accepted(self):
        for key, spec in cl.MANIFEST['variants'].items():
            for value in spec['options']:
                with self.subTest(key=key, value=value):
                    self.assertEqual(cl.validate(changed(variants={key: value}, phone='keep', manifest=2), 'cockpit')['variants'], {key: value})

    def test_unknown_keys_values_and_non_whole_versions_are_refused(self):
        for parts in ({'variants': {'nope': 'list'}}, {'variants': {'sessions': 'nope'}},
                      {'variants': []}, {'variants': None}, {'phone': 'nope'}, {'phone': None},
                      {'manifest': True}, {'manifest': 1.5}, {'manifest': '1'}, {'manifest': 0}):
            with self.subTest(parts=parts), self.assertRaises(cl.Bad):
                cl.validate(changed(**parts), 'cockpit')


class SaveDelete(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.folder = Path(folder) / 'layouts'
        self.enterContext(patch.dict(os.environ, {'TETHER_LAYOUTS': str(self.folder)}))
        # Read the real env override without reloading paths for unrelated tests.
        import runpy
        resolved = runpy.run_path(str(Path(cl.paths.__file__)))['LAYOUTS']
        self.enterContext(patch.object(cl.paths, 'LAYOUTS', resolved))
        cl._last_good.clear()

    def test_save_scan_overwrite_and_delete(self):
        result = cl.save(changed(id='cockpit'))
        self.assertEqual(result['file'], 'cockpit.json')
        self.assertEqual(json.loads((self.folder / result['file']).read_text()), result['layout'])
        self.assertEqual(cl.scan()['layouts'][0]['name'], 'Cockpit')
        with self.assertRaises(cl.Conflict):
            cl.save(changed(id='cockpit', name='Replacement'))
        self.assertEqual(cl.save(changed(id='cockpit', name='Replacement'), True)['layout']['name'], 'Replacement')
        self.assertEqual(cl.delete('cockpit'), {'ok': True})
        self.assertEqual(cl.scan()['layouts'], [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_bad_ids_reserved_ids_and_overwrite_types_are_refused(self):
        for value in ('../outside', '/absolute', 'classic', 'mine', '', None, 'a.json'):
            with self.subTest(value=value), self.assertRaises(cl.Bad):
                cl.delete(value)
            with self.subTest(save=value), self.assertRaises(cl.Bad):
                cl.save(changed(id=value))
        with self.assertRaises(cl.Bad):
            cl.save(changed(id='cockpit'), 'yes')
        with self.assertRaises(cl.Missing):
            cl.delete('missing')
        self.assertFalse(self.folder.exists())

    def test_limits_and_failed_replace_leave_existing_data_intact(self):
        cl.save(changed(id='cockpit'))
        target = self.folder / 'cockpit.json'
        original = target.read_bytes()
        with patch.object(cl, 'MAX_FILES', 1):
            with self.assertRaisesRegex(cl.Bad, 'already has'):
                cl.save(changed(id='other'))
            cl.save(changed(id='cockpit'), True)
        with patch.object(cl, 'MAX_BYTES', 10):
            with self.assertRaisesRegex(cl.Bad, '16 KB'):
                cl.save(changed(id='cockpit'), True)
        with patch.object(cl.os, 'replace', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                cl.save(changed(id='cockpit', name='Replacement'), True)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual([item.name for item in self.folder.iterdir()], ['cockpit.json'])

    def test_existing_id_in_an_older_filename_requires_overwrite(self):
        self.folder.mkdir()
        previous = self.folder / 'Old cockpit.json'
        previous.write_text(json.dumps(changed(id='cockpit')))
        with self.assertRaises(cl.Conflict):
            cl.save(changed(id='cockpit'))
        with patch.object(cl, 'MAX_FILES', 1):
            result = cl.save(changed(id='cockpit', name='Replacement'), True)
        self.assertEqual(result['file'], 'cockpit.json')
        self.assertFalse(previous.exists())
        self.assertEqual([item['name'] for item in cl.scan()['layouts']], ['Replacement'])

    def test_symlinks_are_never_written_or_deleted(self):
        self.folder.mkdir()
        outside = self.folder.parent / 'outside.json'
        outside.write_text('leave this')
        (self.folder / 'cockpit.json').symlink_to(outside)
        with self.assertRaises(cl.Bad):
            cl.save(changed(id='cockpit'), True)
        with self.assertRaises(cl.Bad):
            cl.delete('cockpit')
        self.assertEqual(outside.read_text(), 'leave this')


if __name__ == '__main__':
    unittest.main()
