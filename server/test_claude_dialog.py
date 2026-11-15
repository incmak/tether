"""Claude slash-command dialogs read as data. Fixtures are styled frames of Claude Code 2.1.325 (names and paths replaced)."""
import re
import subprocess
import unittest
from pathlib import Path

import claude_dialog
import terminal_input

FIXTURES = Path(__file__).parent / 'fixtures'


def frame(name):
    return (FIXTURES / f'claude_dialog_{name}.ansi').read_text()


def dialog(name):
    found = claude_dialog.parse(frame(name))
    assert found is not None, name
    return found


def block(found, kind):
    return next(item for item in found['blocks'] if item['type'] == kind)


def items(found):
    return claude_dialog._items(found)


def keys(found):
    return [key['key'] or key['label'] for hint in found['hints'] for key in hint['keys']]


class Parse(unittest.TestCase):
    def test_list_with_groups_marks_and_values(self):
        found = dialog('mcp')
        self.assertEqual(found['title'], 'Manage MCP servers')
        rows = block(found, 'list')['items']
        self.assertEqual(rows[0], {'heading': 'User MCPs (/srv/u/.claude.json)'})
        self.assertEqual(rows[1], {'text': 'cloudflare', 'value': '4 tools', 'mark': '✔', 'detail': '', 'selected': True})
        self.assertEqual(rows[2]['mark'], '⚠')
        self.assertEqual(len(items(found)), 9)
        self.assertEqual(sum(item['selected'] for item in items(found)), 1)
        self.assertEqual(keys(found), ['Up', 'Down', 'Enter', 'Escape'])
        self.assertEqual([hint['effect'] for hint in found['hints']], ['navigate', 'confirm', 'cancel'])

    def test_narrow_pane_wraps_the_footer(self):
        found = dialog('mcp_narrow')
        self.assertEqual(keys(found), ['Up', 'Down', 'Enter', 'Escape'])
        self.assertEqual(len(items(found)), 9)
        self.assertNotIn('cancel', found['blocks'][-1].get('lines', [''])[0])

    def test_second_cursor_row_is_an_item_and_not_the_selection(self):
        found = dialog('memory')
        self.assertEqual([(item['text'], item['selected']) for item in items(found)],
                         [('Auto-memory', False), ('User instructions', True), ('Project instructions', False), ('Open auto-memory folder', False)])
        self.assertEqual(items(found)[1]['value'], 'Saved in ~/.claude/CLAUDE.md')
        self.assertEqual(found['blocks'][-1]['lines'], ['Learn more: https://code.claude.com/docs/en/memory'])

    def test_title_note_search_box_and_detail_rows(self):
        found = dialog('resume')
        self.assertEqual((found['title'], found['note']), ('Resume session', '(1 of 49)'))
        self.assertEqual(block(found, 'search'), {'type': 'search', 'placeholder': 'Search…', 'value': '', 'focused': False})
        rows = block(found, 'list')
        self.assertEqual(rows['items'][0], {'heading': 'projects'})
        self.assertEqual(rows['items'][1]['text'], 'Session title 1')
        self.assertRegex(rows['items'][1]['detail'], r'ago · HEAD · ')
        self.assertEqual(rows['more_below'], -1)  # "↓" in the cursor column: more rows, count not shown
        self.assertEqual(keys(found), ['C-a', 'C-b', 'Space', 'C-r', 'Type', 'Escape'])

    def test_tabs_and_facts(self):
        found = dialog('status')
        self.assertEqual((found['title'], found['tabs'], found['tab'], found['tab_focus']),
                         ('Settings', ['Status', 'Config', 'Usage', 'Stats'], 'Status', True))
        facts = {row['label']: row['value'] for row in block(found, 'facts')['rows']}
        self.assertEqual(facts['Version'], '2.1.325')
        self.assertEqual(facts['Email'], 'person@example.com')

    def test_narrow_facts_join_wrapped_values(self):
        facts = {row['label']: row['value'] for row in block(dialog('status_narrow'), 'facts')['rows']}
        self.assertRegex(facts['Session ID'], r'^[0-9a-f-]{36}$')  # cut at the pane edge: joined with no space
        self.assertEqual(facts['MCP servers'], '8 connected, 1 need auth · /mcp')

    def test_config_focus_follows_the_cursor_colour(self):
        search, rows = dialog('config'), dialog('config_list')
        self.assertEqual((search['focus'], search['tab'], search['tab_focus']), ('search', 'Config', False))
        self.assertEqual(keys(search), ['Type', 'Enter', 'Down', 'Up', 'Escape'])
        self.assertEqual(rows['focus'], 'list')
        self.assertEqual(keys(rows), ['Enter', 'Space', '/', 'Escape'])
        self.assertEqual(items(rows)[0], {'text': 'Auto-compact', 'value': 'true', 'mark': '', 'detail': '', 'selected': True})
        self.assertEqual(block(rows, 'list')['more_below'], 16)

    def test_usage_meters(self):
        for name in ('usage', 'usage_narrow'):
            found = dialog(name)
            meters = [item for item in found['blocks'] if item['type'] == 'meter']
            self.assertEqual(len(meters), 3, name)
            self.assertTrue(all(0 <= meter['percent'] <= 100 and meter['text'].endswith('% used') for meter in meters))
            self.assertIn('Total cost', [row['label'] for row in block(found, 'facts')['rows']])

    def test_stats_activity_chart_is_data(self):
        found = dialog('stats')
        self.assertEqual(block(found, 'tabs'), {'type': 'tabs', 'tabs': ['Overview', 'Models'], 'tab': 'Overview'})
        chart = block(found, 'heatmap')
        self.assertEqual(len(chart['rows']), 7)
        self.assertEqual([row['label'] for row in chart['rows']], ['', 'Mon', '', 'Wed', '', 'Fri', ''])
        self.assertTrue(all(set(row['cells']) <= set('01234') and len(row['cells']) >= 50 for row in chart['rows']))
        self.assertEqual(chart['months'][0], [0, 'Nov'])
        self.assertEqual(chart['months'][1][0], 4)
        self.assertFalse(any(item['type'] == 'text' and item['mono'] for item in found['blocks']))  # no terminal drawing left
        self.assertFalse(any('Less' in line for item in found['blocks'] if item['type'] == 'text' for line in item['lines']))
        self.assertIn('Favorite model', [row['label'] for row in block(found, 'facts')['rows']])
        self.assertEqual(block(found, 'options'), {'type': 'options', 'options': ['All time', 'Last 7 days', 'Last 30 days'], 'value': 'All time'})

    def test_stats_footer_names_no_esc(self):
        # Before the body has the focus the footer is "↓ stats"; then "↑ tabs · r to cycle dates · ctrl+s to copy".
        self.assertEqual([(hint['text'], hint.get('go')) for hint in dialog('stats')['hints']], [('↓ stats', True)])
        found = dialog('stats_body')
        self.assertEqual(keys(found), ['Up', 'r', 'C-s'])
        self.assertEqual(found['hints'][0]['effect'], 'tabs')
        self.assertEqual(claude_dialog.allowed(found), {'Up', 'r', 'C-s', 'Escape'})
        self.assertIsNone(claude_dialog.hints('↓ 16 more'))  # a list's overflow row is no key hint
        self.assertIsNone(claude_dialog.hints('← opens agents true'))

    def test_clipped_setting_row_is_no_footer(self):
        # A /config list cut after "← opens agents   true": a setting label, not a focus hint.
        lines = frame('config_list').rstrip().split('\n')
        lines = lines[:next(index for index, line in enumerate(lines) if 'Auto-compact' in line) + 1] + ['     ↓ agents']
        found = claude_dialog.parse('\n'.join(lines))
        self.assertEqual(found['hints'], [])  # not grey: body text
        self.assertNotIn('Down', claude_dialog.allowed(found) - {'Up', 'Down'})

    def test_labels_with_a_dot_are_no_choice(self):
        for name in ('usage_narrow', 'usage', 'plugin', 'plugin_narrow'):
            self.assertFalse([item for item in dialog(name)['blocks'] if item['type'] == 'options'], name)

    def test_stats_date_range_shows_while_it_loads(self):
        self.assertEqual(block(dialog('stats_cycling'), 'options')['value'], 'Last 7 days')  # a spinner follows the row

    def test_stats_models_view(self):
        found = dialog('stats_models')
        self.assertEqual(block(found, 'tabs')['tab'], 'Models')
        entries = block(found, 'entries')['items']
        self.assertEqual([entry['title'].split(' (')[0] for entry in entries], ['Opus 5.5', 'Sonnet 5.5', 'Fable 5.2', 'Opus 4.8'])  # left column, then right
        self.assertTrue(all(len(entry['lines']) == 2 and entry['lines'][0].startswith('In: ') for entry in entries))
        self.assertIn('Down', keys(found))  # "(↑/↓ to scroll)" beside the model count

    def test_narrow_stats_joins_the_wrapped_chart_and_tab_row(self):
        found = dialog('stats_narrow')
        self.assertEqual((found['title'], found['tab']), ('Settings', 'Stats'))
        chart = block(found, 'heatmap')
        self.assertEqual(len({len(row['cells']) for row in chart['rows']} - {len(chart['rows'][0]['cells']) - 1}), 1)
        self.assertEqual(chart['months'][-1], [36, 'Nov'])

    def test_effort_scale(self):
        for name in ('effort', 'effort_narrow'):
            found = dialog(name)
            self.assertEqual(block(found, 'scale'), {'type': 'scale', 'options': ['low', 'medium', 'high', 'xhigh', 'max'],
                                                     'value': 'medium', 'ends': ['Faster', 'Smarter']}, name)
            self.assertEqual(sorted(keys(found)), sorted(['Left', 'Right', 'Enter', 's', 'Escape', 'Tab']), name)
            self.assertEqual(found['hints'][-1]['effect'], 'toggle Ultracode', name)

    def test_toggle_row_is_a_fact(self):
        found = dialog('privacy')
        self.assertEqual(block(found, 'facts')['rows'], [{'label': 'Help improve our AI models', 'value': 'false'}])
        self.assertEqual(keys(found), ['Enter', 'Tab', 'Space', 'Escape'])

    def test_help_shortcut_table_is_a_list(self):
        for name in ('help', 'help_narrow'):
            found = dialog(name)
            self.assertEqual((found['tabs'], found['tab']), (['General', 'Commands', 'Custom commands'], 'General'))
            self.assertEqual(block(found, 'heading')['text'], 'Shortcuts')
            items = {item['keys']: item['effect'] for item in block(found, 'shortcuts')['items']}
            self.assertEqual(len(items), 17, name)
            self.assertEqual(items['!'], 'shell mode')
            self.assertEqual(items['ctrl + shift + _'], 'undo')  # "shift +" inside a key is no new entry
            self.assertEqual(items['backslash (\\) + return (⏎)'], 'newline')  # wraps onto the next row
            self.assertEqual(items['/keybindings'], 'customize')
            self.assertFalse(any(item['type'] == 'text' and item['mono'] for item in found['blocks']), name)
        self.assertEqual(dialog('help_commands')['tab'], 'Custom commands')

    def test_unknown_key_name_keeps_the_table_as_text(self):
        rows = [('   ctrl + a to start     ctrl + c to quit', []), ('   enter to accept       ctrl + d to end', []), ('   ctrl + b to stop      ctrl + e to go', [])]
        self.assertIsNone(claude_dialog._shortcuts(rows, 0))  # "enter to accept" would merge into "start"

    def test_clipped_shortcut_table_stays_text(self):
        lines = frame('help').rstrip().split('\n')
        cut = next(index for index, line in enumerate(lines) if 'backslash' in line)
        found = claude_dialog.parse('\n'.join(lines[:cut + 1]))
        self.assertFalse(any(item['type'] == 'shortcuts' for item in found['blocks']))  # "… return (⏎) for" has no effect yet
        self.assertTrue(any(item['type'] == 'text' and item['mono'] for item in found['blocks']))

    def test_body_text_that_looks_like_a_hint_is_no_footer(self):
        # A short pane clips /help: its last rows are then part of the shortcut table.
        lines = frame('help').rstrip().split('\n')
        cut = next(index for index, line in enumerate(lines) if 'backslash' in line)
        found = claude_dialog.parse('\n'.join(lines[:cut + 1]))
        self.assertEqual(found['hints'], [])
        self.assertEqual(claude_dialog.allowed(found), {'Escape', 'Left', 'Right'})

    def test_plugin_marks_and_wrapped_tab_row(self):
        for name in ('plugin', 'plugin_narrow'):
            found = dialog(name)
            self.assertEqual(found['tabs'], ['Discover', 'Installed', 'Marketplaces', 'Errors', 'Stats'], name)
            self.assertEqual(items(found)[0]['mark'], '◯')
            self.assertEqual(block(found, 'list')['more_below'], 6101)
            self.assertEqual(keys(found)[:4], ['Type', 'Space', 'Enter', 'Escape'])
        self.assertEqual(keys(dialog('plugin'))[4:], ['C-s'])  # "(ctrl+s to filter by marketplace)"

    def test_hints_in_the_subtitle(self):
        found = dialog('skills')
        self.assertEqual(found['blocks'][0]['lines'], ['102 skills'])
        self.assertEqual(keys(found), ['Enter', 'Space', '/', 't', 'Escape'])

    def test_every_fixture_has_no_private_keys(self):
        for path in FIXTURES.glob('claude_dialog_*.ansi'):
            found = claude_dialog.parse(path.read_text())
            self.assertIsNotNone(found, path.name)
            for item in found['blocks']:
                self.assertFalse([key for key in item if key.startswith('_')], path.name)

    def test_closed_dialog_and_other_screens_are_not_a_dialog(self):
        closed = frame('mcp') + '\n────────────────────\n❯ \n────────────────────\n  ⏵⏵ auto mode on\n'
        self.assertIsNone(claude_dialog.parse(closed))
        self.assertIsNone(claude_dialog.parse('● Done.\n\n────────\n❯ \n────────\n'))
        self.assertIsNone(claude_dialog.parse(''))
        for path in FIXTURES.glob('claude_btw_*'):
            self.assertIsNone(claude_dialog.parse(path.read_text()), path.name)  # /btw has its own card


class Hints(unittest.TestCase):
    def test_keys(self):
        found = claude_dialog.hints('←/→/tab to switch · ↓ to return · Ctrl+A to show all · s for this session only · / to search')
        self.assertEqual([[key['key'] for key in hint['keys']] for hint in found], [['Left', 'Right', 'Tab'], ['Down'], ['C-a'], ['s'], ['/']])
        self.assertEqual(found[3]['effect'], 'this session only')

    def test_text_that_is_not_a_hint(self):
        self.assertIsNone(claude_dialog.hints('https://code.claude.com/docs/en/mcp for help'))
        self.assertIsNone(claude_dialog.hints('Usage credits are off · /usage-credits to turn them on'))
        self.assertIsNone(claude_dialog.hints('Resets 11:29am (Asia/Kolkata)'))
        self.assertIsNone(claude_dialog.hints('S to save'))  # a capital letter is another key

    def test_allowed_keys_come_from_the_dialog(self):
        self.assertEqual(claude_dialog.allowed(dialog('mcp')), {'Up', 'Down', 'Enter', 'Escape'})
        self.assertEqual(claude_dialog.allowed(dialog('status')), {'Escape', 'Left', 'Right'})
        self.assertEqual(claude_dialog.allowed(dialog('config')), {'Enter', 'Down', 'Up', 'Escape'})
        self.assertNotIn('Enter', claude_dialog.allowed(dialog('tasks')))


class Terminal:
    """A pane that shows frames; each key moves to the frame the test maps it to."""
    def __init__(self, frames, moves=None, visible=None, lag=0):
        self.frames, self.moves, self.at, self.sent, self.visible = frames, moves or {}, 0, [], visible
        self.lag, self.wait, self.shown = lag, 0, 0  # lag: captures after a key that still show the old frame

    def __call__(self, command, **_):
        if command[1] == 'capture-pane':
            if self.wait:
                self.wait -= 1
            else:
                self.shown = self.at
            text = self.frames[self.shown]
            if self.visible and '-S' not in command:
                text = '\n'.join(text.splitlines()[-self.visible:])
            return subprocess.CompletedProcess(command, 0, stdout=text)
        if '-l' in command:
            assert command[-2] == '--' and len(command[-1]) <= 2, command  # text can never be read as a tmux option
        sent = [part for part in command[2:] if part not in ('-t', '%1', '--')]
        self.sent.append(sent)
        if '-l' not in sent:
            for key in sent:
                self.at = self.moves.get((self.at, key), self.at)
            self.wait = self.lag
        return subprocess.CompletedProcess(command, 0, stdout='')


ENTRY = {'id': '%1', 'agent': 'claude'}


def moved(source, selected):
    """The mcp frame with the cursor on another server row, drawn as Claude Code draws it."""
    lines = source.split('\n')
    rows = [index for index, line in enumerate(lines) if re.search(r'm[✔⚠]\x1b\[39m ', line)]
    cursor = next(index for index in rows if '❯' in lines[index])
    lines[cursor] = lines[cursor].replace('   \x1b[38;5;153m❯\x1b[39m ', '     ', 1)
    lines[rows[selected]] = lines[rows[selected]].replace('     ', '   \x1b[38;5;153m❯\x1b[39m ', 1)
    return '\n'.join(lines)


class Act(unittest.TestCase):
    def act(self, terminal, mode=None, **body):
        # The card echoes the mode of the dialog it showed; these tests act on the frame the terminal shows first.
        if mode is None and body.get('action') in ('key', 'filter'):
            found = claude_dialog.parse(terminal.frames[terminal.shown])
            mode = found['mode'] if found else 'none'
        return claude_dialog.act(ENTRY, {**body, 'mode': mode}, run=terminal, sleep=lambda _: None)

    def test_key_goes_only_to_the_screen_the_person_saw(self):
        # Enter selects in the search box of /config and changes a setting in its list. Both have the title "Settings".
        seen = claude_dialog.parse(frame('config'))['mode']
        self.assertNotEqual(seen, claude_dialog.parse(frame('config_list'))['mode'])
        terminal = Terminal([frame('config_list')])
        with self.assertRaises(claude_dialog.Changed):
            self.act(terminal, mode=seen, title='Settings', action='key', key='Enter')
        with self.assertRaises(claude_dialog.Changed):
            claude_dialog.act(ENTRY, {'title': 'Settings', 'action': 'key', 'key': 'Enter'}, run=terminal, sleep=lambda _: None)
        self.assertEqual(terminal.sent, [])
        # Enter acts on the marked row: a cursor that moved in the terminal makes the card's Enter stale too.
        self.assertNotEqual(claude_dialog.parse(frame('mcp'))['mode'], claude_dialog.parse(moved(frame('mcp'), 3))['mode'])
        self.assertEqual(claude_dialog.parse(frame('mcp'))['mode'], claude_dialog.parse(frame('mcp'))['mode'])

    def test_key_must_be_offered(self):
        terminal = Terminal([frame('mcp')])
        self.act(terminal, title='Manage MCP servers', action='key', key='Enter')
        self.assertEqual(terminal.sent, [['Enter']])
        with self.assertRaisesRegex(ValueError, 'does not offer'):
            self.act(terminal, title='Manage MCP servers', action='key', key='x')
        with self.assertRaisesRegex(ValueError, 'does not offer'):
            self.act(Terminal([frame('tasks')]), title='Background', action='key', key='Enter')

    def test_changed_dialog_refuses(self):
        terminal = Terminal([frame('mcp')])
        with self.assertRaises(claude_dialog.Changed):
            self.act(terminal, title='Memory', action='key', key='Enter')
        with self.assertRaises(claude_dialog.Changed):
            self.act(Terminal(['● Done.\n❯ \n']), title='Memory', action='key', key='Escape')
        self.assertEqual(terminal.sent, [])

    def test_row_moves_the_cursor_and_checks_each_step(self):
        frames = [moved(frame('mcp'), index) if index else frame('mcp') for index in range(4)]
        for index in range(4):
            self.assertEqual([item['selected'] for item in items(claude_dialog.parse(frames[index]))].index(True), index)
        moves = {(at, 'Down'): at + 1 for at in range(3)} | {(at, 'Up'): at - 1 for at in range(1, 4)}
        terminal = Terminal(frames, moves)
        self.act(terminal, title='Manage MCP servers', action='row', row=3, label='claude.ai Claude Docs')
        self.assertEqual(terminal.sent, [['Down']] * 3)
        self.act(terminal, title='Manage MCP servers', action='row', row=1, label='posthog')
        self.assertEqual(terminal.sent[3:], [['Up']] * 2)
        with self.assertRaises(claude_dialog.Changed):
            self.act(terminal, title='Manage MCP servers', action='row', row=1, label='not there')

    def test_row_fails_when_the_cursor_does_not_move(self):
        terminal = Terminal([frame('mcp')])
        with self.assertRaisesRegex(RuntimeError, 'Could not move'):
            self.act(terminal, title='Manage MCP servers', action='row', row=2, label='scrut')
        self.assertEqual(len(terminal.sent), 2)  # two keys with no effect end it; SEND_LOCK is held for all of it

    def test_row_in_a_list_whose_search_box_has_the_focus(self):
        terminal = Terminal([frame('config'), frame('config_list')], {(0, 'Down'): 1})
        self.act(terminal, title='Settings', action='row', row=0, label='Auto-compact')
        self.assertEqual(terminal.sent, [['Down']])

    def test_tab_with_focus_uses_arrows(self):
        terminal = Terminal([frame('status'), frame('config'), frame('usage')], {(0, 'Right'): 1, (1, 'Up'): 1})
        with self.assertRaisesRegex(RuntimeError, 'Could not open'):
            self.act(terminal, title='Settings', action='tab', tab='Usage')  # Config keeps the focus in this fake
        self.assertEqual(terminal.sent[0], ['Right'])
        self.assertEqual(terminal.sent[1], ['Up'])  # "↑ to tabs"
        terminal = Terminal([frame('usage'), frame('status')], {(0, 'Left'): 1})
        with self.assertRaises(RuntimeError):
            self.act(terminal, title='Settings', action='tab', tab='Config')
        terminal = Terminal([frame('status'), frame('usage')], {(0, 'Right'): 1})
        self.act(terminal, title='Settings', action='tab', tab='Usage')
        self.assertEqual(terminal.sent, [['Right']])
        with self.assertRaisesRegex(ValueError, 'no such tab'):
            self.act(terminal, title='Settings', action='tab', tab='Billing')

    def test_slow_redraw_does_not_press_a_key_twice(self):
        # Right on the last tab wraps to the first one: a second Right on a stale screen would open the wrong tab.
        frames = [frame('status'), frame('usage'), frame('stats')]
        terminal = Terminal(frames, {(0, 'Right'): 1, (1, 'Right'): 2, (2, 'Right'): 0}, lag=3)
        self.act(terminal, title='Settings', action='tab', tab='Stats')
        self.assertEqual(terminal.sent, [['Right'], ['Right']])
        rows = [moved(frame('mcp'), index) if index else frame('mcp') for index in range(3)]
        terminal = Terminal(rows, {(0, 'Down'): 1, (1, 'Down'): 2, (2, 'Down'): 2}, lag=3)
        self.act(terminal, title='Manage MCP servers', action='row', row=2, label='scrut')
        self.assertEqual(terminal.sent, [['Down'], ['Down']])

    def test_tab_from_a_list_goes_through_the_search_box(self):
        terminal = Terminal([frame('config_list'), frame('config'), frame('status')], {(0, '/'): 1, (1, 'Up'): 2})
        self.act(terminal, title='Settings', action='tab', tab='Status')
        self.assertEqual(terminal.sent, [['/'], ['Up']])

    def test_tab_key_cycles_forward_and_is_not_mixed_with_arrows(self):
        # /plugin: Tab from a list goes to the next tab. On "Errors" the tab row has the focus; Left there goes back.
        plugin = frame('plugin')
        names = ['Discover', 'Installed', 'Marketplaces', 'Errors', 'Stats']

        def tab(name, focus=False):
            cells = [('\x1b[1m\x1b[48;5;153m ' if focus else '\x1b[1;7m ') + item + ' \x1b[0m' if item == name else ' ' + item + ' ' for item in names]
            row = '\x1b[39m   \x1b[1m\x1b[38;5;153mPlugins\x1b[0m ' + ' '.join(cells)
            return '\n'.join(row if 'Plugins\x1b[0m' in line else line for line in plugin.split('\n'))
        frames = [tab('Marketplaces'), tab('Errors', True), tab('Stats', True), plugin]
        self.assertEqual([claude_dialog.parse(text)['tab'] for text in frames], ['Marketplaces', 'Errors', 'Stats', 'Discover'])
        terminal = Terminal(frames, {(0, 'Tab'): 1, (1, 'Tab'): 2, (2, 'Tab'): 3, (1, 'Left'): 0})
        self.act(terminal, title='Plugins', action='tab', tab='Discover')
        self.assertEqual(terminal.sent, [['Tab']] * 3)

    def test_tab_key_is_not_pressed_where_it_changes_a_value(self):
        terminal = Terminal([frame('config_list').replace('/ to search · ', '')])
        with self.assertRaisesRegex(RuntimeError, 'Could not open'):
            self.act(terminal, title='Settings', action='tab', tab='Status')
        self.assertEqual(terminal.sent, [])

    def test_stats_view_and_date_range(self):
        # "↓ stats": the body has no focus yet, so Down goes first. Tab changes the view, "r" the date range.
        frames = [frame('stats'), frame('stats_body'), frame('stats_models')]
        terminal = Terminal(frames, {(0, 'Down'): 1, (1, 'Tab'): 2, (2, 'Tab'): 1})
        self.act(terminal, title='Settings', action='subtab', tab='Models')
        self.assertEqual(terminal.sent, [['Down'], ['Tab']])
        self.act(terminal, title='Settings', action='subtab', tab='Overview')
        self.assertEqual(terminal.sent[2:], [['Tab']])
        self.act(terminal, title='Settings', action='option', value='All time')
        self.assertEqual(len(terminal.sent), 3)  # already current

    def test_date_range_waits_for_each_load(self):
        def ranged(value):
            text = frame('stats_body').replace('\x1b[1m\x1b[38;5;174mAll time\x1b[0m\x1b[38;5;246m · Last 7 days · Last 30 days\x1b[39m', '@')
            assert '@' in text
            parts = ['\x1b[1m\x1b[38;5;174m' + name + '\x1b[0m' if name == value else '\x1b[38;5;246m' + name + '\x1b[39m' for name in ('All time', 'Last 7 days', 'Last 30 days')]
            return text.replace('@', '\x1b[38;5;246m · \x1b[39m'.join(parts))
        frames = [ranged('All time'), ranged('Last 7 days'), ranged('Last 30 days')]
        self.assertEqual([block(claude_dialog.parse(text), 'options')['value'] for text in frames], ['All time', 'Last 7 days', 'Last 30 days'])
        terminal = Terminal(frames, {(0, 'r'): 1, (1, 'r'): 2, (2, 'r'): 0}, lag=6)  # each range shows late
        self.act(terminal, title='Settings', action='option', value='Last 30 days')
        self.assertEqual(terminal.sent, [['r'], ['r']])
        self.act(terminal, title='Settings', action='option', value='Last 30 days')  # a second tap must not go on
        self.assertEqual(len(terminal.sent), 2)
        stuck = Terminal(frames)
        with self.assertRaisesRegex(RuntimeError, 'still loads'):
            self.act(stuck, title='Settings', action='option', value='Last 7 days')
        self.assertEqual(stuck.sent, [['r']])
        with self.assertRaisesRegex(ValueError, 'no such choice'):
            self.act(terminal, title='Settings', action='option', value='Last year')
        with self.assertRaisesRegex(ValueError, 'no such choice'):
            self.act(Terminal([frame('mcp')]), title='Manage MCP servers', action='subtab', tab='Models')

    def test_a_spinner_is_not_the_result_of_a_key(self):
        # /stats redraws a spinner while it loads. That change must not count as "Tab worked": a second Tab would undo it.
        loading = frame('stats_body').replace('Favorite model', 'Favorite  model')
        self.assertEqual(block(claude_dialog.parse(loading), 'tabs')['tab'], 'Overview')
        terminal = Terminal([frame('stats_body'), loading], {(0, 'Tab'): 1})
        with self.assertRaisesRegex(RuntimeError, 'Could not change'):
            self.act(terminal, title='Settings', action='subtab', tab='Models')  # the text changed, the view did not
        self.assertEqual(terminal.sent, [['Tab'], ['Tab']])

    def test_tab_row_from_the_stats_body(self):
        terminal = Terminal([frame('stats_models'), frame('stats'), frame('usage')], {(0, 'Up'): 1, (1, 'Left'): 2})
        self.act(terminal, title='Settings', action='tab', tab='Usage')
        self.assertEqual(terminal.sent, [['Up'], ['Left']])  # "↑ tabs", though "↑/↓ to scroll" names ↑ too

    def test_scale(self):
        high = frame('effort').replace('\x1b[1m\x1b[38;5;114mmedium\x1b[0m', '\x1b[38;5;246mmedium\x1b[39m').replace(
            '\x1b[38;5;246mhigh\x1b[39m', '\x1b[1m\x1b[38;5;114mhigh\x1b[0m')
        self.assertEqual(block(claude_dialog.parse(high), 'scale')['value'], 'high')
        terminal = Terminal([frame('effort'), high], {(0, 'Right'): 1, (1, 'Left'): 0})
        self.act(terminal, title='Effort', action='scale', value='high')
        self.act(terminal, title='Effort', action='scale', value='medium')
        self.assertEqual(terminal.sent, [['Right'], ['Left']])
        with self.assertRaisesRegex(ValueError, 'no such level'):
            self.act(terminal, title='Effort', action='scale', value='turbo')

    def test_filter_types_only_into_a_search_box(self):
        terminal = Terminal([frame('resume')])
        self.act(terminal, title='Resume session', action='filter', text='voice mode')
        self.assertEqual(terminal.sent, [['-l', char] for char in 'voice mode'])  # one call per character
        terminal = Terminal([frame('config_list'), frame('config')], {(0, '/'): 1})
        self.act(terminal, title='Settings', action='filter', text='compact')
        self.assertEqual(terminal.sent, [['/'], *(['-l', char] for char in 'compact')])
        terminal = Terminal([frame('resume')])
        self.act(terminal, title='Resume session', action='filter', text='a;b')
        self.assertEqual(terminal.sent[1], ['-l', '\\;'])  # tmux reads a lone ";" as its command separator
        with self.assertRaisesRegex(ValueError, 'no search box'):
            self.act(Terminal([frame('mcp')]), title='Manage MCP servers', action='filter', text='x')
        terminal = Terminal([frame('resume')])
        self.act(terminal, title='Resume session', action='filter', text='-R')  # "-R" as an option would reset the terminal
        self.assertEqual(terminal.sent, [['-l', '-'], ['-l', 'R']])
        with self.assertRaisesRegex(ValueError, 'control characters'):
            self.act(Terminal([frame('resume')]), title='Resume session', action='filter', text='a\x1bb')

    def test_filter_replaces_the_old_text(self):
        typed = frame('resume').replace('\x1b[38;5;246m⌕ Search…\x1b[39m', '⌕ voice')
        self.assertEqual(block(claude_dialog.parse(typed), 'search')['value'], 'voice')
        terminal = Terminal([typed, frame('resume')], {(0, 'BSpace'): 1})
        self.act(terminal, title='Resume session', action='filter', text='ab')
        self.assertEqual(terminal.sent, [['BSpace'] * 5, ['-l', 'a'], ['-l', 'b']])

    def test_filter_does_not_type_when_the_dialog_closed_after_the_old_text_went(self):
        typed = frame('resume').replace('\x1b[38;5;246m⌕ Search…\x1b[39m', '⌕ voice')
        terminal = Terminal([typed, '● Done.\n❯ \n'], {(0, 'BSpace'): 1})
        with self.assertRaises(claude_dialog.Changed):
            self.act(terminal, title='Resume session', action='filter', text='rm -rf')
        self.assertEqual(terminal.sent, [['BSpace'] * 5])

    def test_unknown_action(self):
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.act(Terminal([frame('mcp')]), title='Manage MCP servers', action='paste')


class Tall(unittest.TestCase):
    def test_top_rows_come_from_the_scrollback(self):
        whole = frame('resume_tall')
        terminal = Terminal([whole], visible=20)
        visible = '\n'.join(whole.splitlines()[-20:])
        self.assertIsNone(claude_dialog.parse(visible))
        self.assertTrue(claude_dialog.tall(visible))
        found = claude_dialog.read(ENTRY, visible, terminal)
        self.assertEqual(found['title'], 'Resume session')

    def test_chat_is_not_the_lower_part_of_a_dialog(self):
        self.assertFalse(claude_dialog.tall('● Done.\n\n────────\n❯ \n────────\n  Esc to cancel\n'))
        self.assertFalse(claude_dialog.tall(frame('mcp')))
        self.assertFalse(claude_dialog.tall('   some indented text\n   and more\n'))


class Attention(unittest.TestCase):
    def setUp(self):
        terminal_input._attention_seen.clear()

    def attention(self, name, **more):
        styled = frame(name)
        screen = terminal_input.prompt_actions.plain(styled)
        return terminal_input.attention({'id': '%9', 'agent': 'claude'}, screen, False, now=100, styled_screen=styled,
                                        dialog=claude_dialog.parse(styled), **more)

    def test_dialog_shows_at_once_and_asks_for_no_answer(self):
        found = self.attention('mcp')
        self.assertEqual((found['reason'], found['requiresResponse']), ('dialog', False))
        self.assertEqual(found['dialog']['title'], 'Manage MCP servers')
        self.assertTrue(found['text'].startswith('Manage MCP servers'))

    def test_id_stays_when_the_cursor_or_tab_moves(self):
        first = self.attention('status')['id']
        self.assertEqual(self.attention('usage')['id'], first)
        self.assertEqual(self.attention('config')['id'], first)
        self.assertNotEqual(self.attention('mcp')['id'], first)

    def test_settings_dialog_is_a_dialog_and_no_longer_a_text_panel(self):
        self.assertEqual(self.attention('usage')['reason'], 'dialog')

    def test_each_command_of_the_backlog_item_gets_a_card(self):
        for name in ('help', 'mcp', 'memory', 'resume', 'tasks', 'privacy', 'effort', 'plugin', 'status', 'config', 'usage', 'stats', 'stats_body', 'stats_models', 'skills'):
            terminal_input._attention_seen.clear()
            self.assertEqual(self.attention(name)['reason'], 'dialog', name)


if __name__ == '__main__':
    unittest.main()
