import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ui_usage


def event(k, label, region='Tether', kind='button', depth=0, steps=1, visit='v1', device='wide', gen='0'):
    return {'k': k, 'l': label, 'r': region, 'c': kind, 'd': depth, 'n': steps, 'j': visit, 'w': device, 'g': gen}


class Handler:
    def __init__(self, path, body=b''):
        self.path = path
        self.headers = {'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)
        self.status = None
        self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(mock.patch.object(ui_usage.paths, 'CACHE', Path(folder.name)))
        words = Path(folder.name) / 'ui-words.txt'
        words.write_text('settings home archive export theme shell close open file folder terminal in browser restore tabs tether more new tab rail bar search\n')
        self.enterContext(mock.patch.object(ui_usage, 'words_file', lambda: words))
        self.enterContext(mock.patch.dict(ui_usage._state, checked=float('-inf')))
        self.prefs = {}
        self.enterContext(mock.patch.object(ui_usage.prefs_sync, 'load', side_effect=lambda: {'prefs': self.prefs}))


class RecordTest(Base):
    def test_keeps_valid_events_and_drops_the_rest(self):
        kept = ui_usage.record([event('click', 'Settings'), {'k': 'type', 'l': 'x', 'j': 'v', 'w': 'wide'},
                                event('click', '', visit='v1'), event('seen', 'Home', device='tv'), 'text'], now=100)
        self.assertEqual(kept, 1)
        stored = [json.loads(line) for line in ui_usage.file().read_text().splitlines()]
        self.assertEqual(stored, [{'at': 100, 'k': 'click', 'r': 'Tether', 'l': 'Settings', 'c': 'button', 'd': 0, 'n': 1, 'j': 'v1', 'w': 'wide'}])
        self.assertEqual(ui_usage.file().stat().st_mode & 0o777, 0o600)

    def test_words_outside_the_ui_become_an_ellipsis(self):
        ui_usage.record([event('click', 'Restore Secret acquisition', region='Tabs: Ünïcode name'),
                         event('open', 'notes-2026.md', kind='', depth=1), event('click', 'Close tab 12')], now=1)
        stored = [json.loads(line) for line in ui_usage.file().read_text().splitlines()]
        self.assertEqual([(item['r'], item['l']) for item in stored],
                         [('Tabs: …', 'Restore …'), ('Tether', '…'), ('Tether', 'Close tab #')])

    def test_no_word_list_keeps_nothing(self):
        with mock.patch.object(ui_usage, 'words_file', lambda: Path('/nonexistent/ui-words.txt')):
            self.assertEqual(ui_usage.record([event('click', 'Settings')], now=1), 0)
        self.assertFalse(ui_usage.file().exists())

    def test_time_comes_from_the_page_offset(self):
        ui_usage.record([dict(event('click', 'Settings'), t=5_000), dict(event('click', 'Home'), t='x')], now=100, sent=9_000)
        stored = [json.loads(line)['at'] for line in ui_usage.file().read_text().splitlines()]
        self.assertEqual(stored, [96, 100])

    def test_caps_text_and_numbers(self):
        ui_usage.record([event('click', 'Settings ' * 50, depth=50, steps=-3)], now=1)
        stored = json.loads(ui_usage.file().read_text())
        self.assertLessEqual(len(stored['l']), ui_usage.TEXT)
        self.assertTrue(stored['l'].startswith('Settings Settings'))
        self.assertEqual((stored['d'], stored['n']), (9, 0))

    def test_trims_old_events_when_the_file_is_too_big(self):
        with mock.patch.object(ui_usage, 'MAX_FILE', 2000):
            ui_usage.record([event('click', 'Old')] * 10, now=0)
            ui_usage.record([event('click', 'New')] * 20, now=ui_usage.KEEP_DAYS * 86400 + 10)
        labels = {json.loads(line)['l'] for line in ui_usage.file().read_text().splitlines()}
        self.assertEqual(labels, {'New'})
        self.assertLessEqual(ui_usage.file().stat().st_size, 2000)

    def test_old_events_go_after_90_days_in_a_small_file(self):
        ui_usage.record([event('click', 'Settings')], now=0)
        ui_usage.record([event('click', 'Home')], now=ui_usage.KEEP_DAYS * 86400 + 10)
        self.assertEqual([json.loads(line)['l'] for line in ui_usage.file().read_text().splitlines()], ['Home'])
        ui_usage.expire(now=2 * ui_usage.KEEP_DAYS * 86400 + 20)
        self.assertEqual(ui_usage.file().read_text(), '')

    def test_a_batch_from_before_the_clear_is_dropped_even_when_late(self):
        old = ui_usage.generation()
        ui_usage.clear(now=20)
        # Captured and sent at 19 with the old generation, or with none; it arrives at 21.
        self.assertEqual(ui_usage.record([dict(event('click', 'Home', gen=old), t=19_000)], now=21, sent=19_000), 0)
        self.assertEqual(ui_usage.record([dict(event('click', 'Home', gen=None), t=19_000)], now=21, sent=19_000), 0)
        self.assertEqual(ui_usage.record([dict(event('click', 'Theme', gen=ui_usage.generation()), t=21_000)], now=22, sent=21_000), 1)
        self.assertEqual([json.loads(line)['l'] for line in ui_usage.file().read_text().splitlines()], ['Theme'])

    def test_an_old_event_after_newer_ones_still_expires(self):
        cutoff = ui_usage.KEEP_DAYS * 86400
        ui_usage.record([dict(event('click', 'Home'), t=100_000_000)], now=100_000, sent=100_000_000)
        ui_usage.record([dict(event('click', 'Theme'), t=1)], now=100_000 + 10, sent=86_400_000)  # a day late: at 13610
        ui_usage.expire(now=13_700 + cutoff)
        self.assertEqual([json.loads(line)['l'] for line in ui_usage.file().read_text().splitlines()], ['Home'])

    def test_clear_answers_the_new_generation(self):
        ui_usage.record([event('click', 'Settings')], now=10)
        handler = Handler(ui_usage.CLEAR)
        ui_usage.post(handler)
        self.assertEqual(handler.body, {'cleared': True, 'gen': ui_usage.generation()})
        self.assertNotEqual(ui_usage.generation(), '0')


class HttpTest(Base):
    def test_post_records_and_off_records_nothing(self):
        body = json.dumps({'events': [event('click', 'Settings')]}).encode()
        handler = Handler(ui_usage.PATH, body)
        ui_usage.post(handler)
        self.assertEqual((handler.status, handler.body), (200, {'kept': 1, 'enabled': True, 'gen': '0'}))
        self.prefs['uiUsage'] = False
        handler = Handler(ui_usage.PATH, body)
        ui_usage.post(handler)
        self.assertEqual(handler.body, {'kept': 0, 'enabled': False, 'gen': '0'})
        self.assertEqual(len(ui_usage.file().read_text().splitlines()), 1)

    def test_post_rejects_bad_bodies(self):
        for body in (b'', b'[]', b'{"events": 3}', b'not json', b'x' * (ui_usage.MAX_BODY + 1)):
            handler = Handler(ui_usage.PATH, body)
            ui_usage.post(handler)
            self.assertEqual(handler.status, 400, body[:20])

    def test_clear_removes_the_log(self):
        ui_usage.record([event('click', 'Settings')])
        handler = Handler(ui_usage.CLEAR)
        ui_usage.post(handler)
        self.assertEqual(handler.status, 200)
        self.assertFalse(ui_usage.file().exists())

    def test_get_checks_the_query(self):
        for path, status in (('?days=30', 200), ('?days=7&device=phone', 200), ('?days=5', 400), ('?device=tv', 400)):
            handler = Handler(ui_usage.PATH + path)
            ui_usage.get(handler)
            self.assertEqual(handler.status, status, path)


class ReportTest(Base):
    def visits(self, count, events, device='wide'):
        sent = []
        for index in range(count):
            sent += [dict(item, j=f'v{index}', w=device) for item in events(index)]
        ui_usage.record(sent, now=1000)

    def test_lists_rare_page_controls_and_popular_menu_controls(self):
        # Ten visits. "Archive" sits on the page and is never used. "Settings" is used on every visit.
        # "Theme" is in a menu and used in half of the visits; "Export" is in the same menu and never used.
        self.visits(10, lambda index: [
            event('seen', 'Archive'), event('seen', 'Settings'), event('click', 'Settings'),
            event('seen', 'Export', region='More', depth=1),
            *([event('click', 'Theme', region='More', kind='menuitem', depth=1, steps=2)] if index % 2 else []),
            event('open', 'More', region='Tether', kind='', depth=1, steps=1),
        ])
        result = ui_usage.report(now=2000)
        self.assertEqual(result['visits'], 10)
        self.assertTrue(result['ready'])
        self.assertEqual([row['label'] for row in result['top']], ['Settings', 'Theme'])
        self.assertEqual([row['label'] for row in result['demote']], ['Archive'])
        self.assertEqual([row['label'] for row in result['promote']], ['Theme'])
        self.assertEqual([row['label'] for row in result['unused']], ['Export'])
        self.assertEqual(result['views'], [{'view': 'More', 'visits': 10, 'steps': 1, 'most': 1, 'keyboard': 0, 'from': 'Tether'}])
        self.assertNotIn('_clickedIn', result['top'][0])
        json.dumps(result)

    def test_deep_menu_control_is_listed_even_when_rare(self):
        self.visits(20, lambda index: [event('seen', 'Home')] + ([event('click', 'Shell', region='New tab', depth=2, steps=4)] if index < 3 else []))
        self.assertEqual([row['label'] for row in ui_usage.report(now=2000)['promote']], ['Shell'])

    def test_keyboard_opens_count_apart(self):
        self.visits(2, lambda index: [event('open', 'Search', steps=0), event('open', 'Search', steps=3)])
        view = ui_usage.report(now=2000)['views'][0]
        self.assertEqual((view['visits'], view['keyboard'], view['steps']), (4, 2, 3))

    def test_labels_with_an_item_name_count_as_one_control(self):
        self.visits(1, lambda index: [event('click', f'Close {name}', region='Tabs') for name in ('alpha', 'beta two', 'gamma')]
                    + [event('click', 'Close')])
        labels = sorted((row['label'], row['clicks']) for row in ui_usage.report(now=2000)['top'])
        self.assertEqual(labels, [('Close', 1), ('Close …', 3)])

    def test_controls_that_share_a_first_word_stay_apart(self):
        self.visits(1, lambda index: [event('click', 'Open file'), event('click', 'Open folder'), event('click', 'Open terminal'),
                                      event('click', 'Open in browser')])
        self.assertEqual(sorted(row['label'] for row in ui_usage.report(now=2000)['top']),
                         ['Open file', 'Open folder', 'Open in browser', 'Open terminal'])

    def test_filters_by_device_and_days(self):
        self.visits(1, lambda index: [event('click', 'Rail')], device='wide')
        self.visits(1, lambda index: [event('click', 'Tab bar')], device='phone')
        self.assertEqual([row['label'] for row in ui_usage.report(device='phone', now=2000)['top']], ['Tab bar'])
        self.assertEqual(ui_usage.report(days=7, now=1000 + 8 * 86400)['clicks'], 0)

    def test_not_ready_with_few_visits(self):
        self.visits(2, lambda index: [event('seen', 'Archive')])
        result = ui_usage.report(now=2000)
        self.assertFalse(result['ready'])
        self.assertEqual(result['demote'], [])

    def test_empty_log(self):
        result = ui_usage.report(now=2000)
        self.assertEqual((result['visits'], result['clicks'], result['top'], result['views']), (0, 0, [], []))


if __name__ == '__main__':
    unittest.main()
