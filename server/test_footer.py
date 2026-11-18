"""footer.parse reads one styled capture of Claude's bottom area; no tmux needed."""
import json
from pathlib import Path
import tempfile
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import footer
import server

DIM, UNDIM = '\x1b[2m', '\x1b[22m'


def screen(prompt, above='', below=''):
    rule = '─' * 80
    return '\n'.join(x for x in [above, rule, prompt, rule, below] if x != '')


class FooterTests(unittest.TestCase):
    def test_wrapped_paste_notice_at_phone_and_desktop_widths(self):
        for width, hint in ((30, ['paste again to', 'expand']),
                            (44, ['paste again to', 'expand']),
                            (80, ['paste again to', 'expand']),
                            (80, ['paste again to expand'])):
            for context in ([], ['5% until auto-compact']):
                with self.subTest(width=width, context=context):
                    rows = [text.rjust(width - 8) for text in context]
                    rows += [text.rjust(width - 9) for text in hint]
                    capture = '\n'.join(rows + ['─' * width, '❯ [Pasted text #1 +20 lines]', '─' * width])
                    self.assertEqual(footer.parse(capture)['notices'], context)

    def test_paste_hint_never_becomes_status_or_notice(self):
        status = 'Haiku 5.5 | ctx 0% (0k/1000k) | claude-1000'
        for hint in ('paste again to expand', 'PASTE AGAIN TO EXPAND'):
            for above, below in (('', '  ' + status + '\n  ' + hint),
                                 (' ' * 40 + hint, '  ' + status),
                                 ('', '  ' + hint + '\n  ' + status),
                                 ('', '  ' + hint)):
                with self.subTest(above=above, below=below):
                    result = footer.parse(screen('❯', above, below))
                    self.assertEqual(result['notices'], [])
                    self.assertEqual(result['status'], status if status in below else '')

    def test_short_indented_notice_continuations(self):
        for width in (30, 44, 80):
            with self.subTest(width=width):
                # The continuation has fewer than 12 spaces in a 30-column pane.
                rows = ['✔ Update installed ·', '/restart to apply']
                capture = '\n'.join(text.rjust(width - 2) for text in rows)
                result = footer.parse(screen('❯', capture).replace('─' * 80, '─' * width))
                self.assertEqual(result['notices'], ['✔ Update installed · /restart to apply'])
                self.assertEqual(result['update'], '✔ Update installed · /restart to apply')

    def test_notice_boundaries_and_wrapped_effort(self):
        rows = ['5% until auto-compact', 'paste again to', 'expand', '◐ medium ·', '/effort']
        result = footer.parse(screen('❯', '\n'.join(' ' * 15 + text for text in rows)))
        self.assertEqual(result['notices'], ['5% until auto-compact'])
        self.assertEqual(result['effort'], 'medium')
        for first, second in (('All done.', 'paste again to expand'),
                              ('First phrase ·', 'another notice'),
                              ('some hint', '5% until auto-compact'),
                              ('some hint', '✔ Update installed · /restart'),
                              ('some hint', '◎ /goal active (10h)'),
                              ('some hint', '⚠ Context low'),
                              ('new task? /clear to save 150.8k tokens', 'paste again to expand')):
            with self.subTest(first=first, second=second):
                result = footer.parse(screen('❯', ' ' * 15 + first + '\n' + ' ' * 15 + second))
                self.assertEqual(result['notices'], [first] if second.startswith('◎') or second == 'paste again to expand' else [first, second])

    def test_wrapped_context_and_clear_notices(self):
        for width in (30, 44, 80):
            with self.subTest(width=width):
                rows = ['5% until', 'auto-compact', 'new task? /clear to', 'save 150.8k tokens']
                capture = '\n'.join(text.rjust(width - 2) for text in rows)
                result = footer.parse(screen('❯', capture).replace('─' * 80, '─' * width))
                self.assertEqual(result['notices'], ['5% until auto-compact', 'new task? /clear to save 150.8k tokens'])

    def test_join_precedes_splitting_notices_on_the_same_row(self):
        rows = ['5% until auto-compact   paste again to', 'expand']
        result = footer.parse(screen('❯', '\n'.join(' ' * 15 + text for text in rows)))
        self.assertEqual(result['notices'], ['5% until auto-compact'])

    def test_notice_style_changes_prevent_joining(self):
        for style in ('\x1b[22m', '\x1b[38;5;2m', '\x1b[38;2;2;0;2m'):
            with self.subTest(style=style):
                rows = '\x1b[2m' + ' ' * 15 + 'some hint\n' + style + ' ' * 15 + 'another notice\x1b[0m'
                self.assertEqual(footer.parse(screen('❯', rows))['notices'], ['some hint', 'another notice'])
        rows = '\x1b[2;38;5;2m' + ' ' * 15 + 'paste again to\n' + ' ' * 15 + 'expand\x1b[0m'
        self.assertEqual(footer.parse(screen('❯', rows))['notices'], [])

    def test_question_picker_is_not_the_footer(self):
        # AskUserQuestion: the cursor row starts with ❯ like the input box. The model pill showed "4. Chat about this".
        screen = '\n'.join(['● Merge now?', '', '  1. Merge now', '  2. Wait', '❯ 3. 344', '', '─' * 40, '  4. Chat about this', '',
                             'Enter to select · ↑/↓ to navigate · ctrl+g to edit in Vim · Esc to cancel'])
        result = footer.parse(screen)
        self.assertEqual((result['status'], result['notices'], result['suggestion']), ('', [], ''))

    def test_typed_numbered_list_is_still_the_input(self):
        screen = '\n'.join(['─' * 40, '❯ 1. fix the login', '─' * 40, '  claude-opus · 10% context'])
        self.assertEqual(footer.parse(screen)['status'], 'claude-opus · 10% context')

    def test_suggestion_effort_and_mode(self):
        r = footer.parse(screen('❯ ' + DIM + 'send it to saidee on chat' + UNDIM, ' ' * 60 + '◐ medium · /effort',
                                '  Opus 5.5 | ctx 16% | 7d 60% | example-project\n  ⏵⏵ auto mode on (shift+tab to cycle) · 2 shells'))
        self.assertEqual(r['suggestion'], 'send it to saidee on chat')
        self.assertEqual(r['effort'], 'medium')
        self.assertTrue(r['status'].startswith('Opus 5.5'))
        self.assertEqual(r['mode'], '⏵⏵ auto mode on')
        self.assertEqual(r['tasks'], ['2 shells'])
        self.assertEqual(r['update'], '')

    def test_typed_text_and_placeholders_are_not_suggestions(self):
        self.assertEqual(footer.parse(screen('❯ hello'))['suggestion'], '')
        self.assertEqual(footer.parse(screen('❯ ' + DIM + 'Try "edit <filepath> to..."' + UNDIM))['suggestion'], '')
        self.assertEqual(footer.parse(screen('❯ ' + DIM + 'Press up to edit queued messages' + UNDIM))['suggestion'], '')

    def test_update_notice_and_agents(self):
        r = footer.parse(screen('❯ ', ' ' * 50 + '4% until auto-compact   Update installed · /restart to apply',
                                '  Opus 5.5 | ctx 5%\n  ⏵⏵ auto mode on\n  ● main\n  ◯ general-purpose  Find the bug  47m 11s · ↓ 142.0k tokens'))
        self.assertEqual(r['notices'], ['4% until auto-compact', 'Update installed · /restart to apply'])
        self.assertEqual(r['update'], 'Update installed · /restart to apply')
        self.assertEqual([a['name'] for a in r['agents']], ['main', 'general-purpose'])
        self.assertEqual(r['agents'][1]['task'], 'Find the bug')
        self.assertTrue(r['agents'][1]['detail'].startswith('47m'))

    def test_narrow_pane_agent_row_is_not_a_notice(self):
        r = footer.parse(screen('❯ ', '', '  ⏵⏵ auto mode on (shift+tab to\n  ● main\n  ◯ general-purpose 1m 59s · ↓\n  ◯ general-purpose  1m 59s · ↓'))
        self.assertEqual(r['notices'], [])
        self.assertEqual([a['name'] for a in r['agents']], ['main', 'general-purpose', 'general-purpose'])
        self.assertEqual(r['agents'][1]['task'], '1m 59s · ↓')

    def test_goal_is_split_from_update(self):
        r = footer.parse(screen('❯ ', ' ' * 30 + '◎ /goal active (10h) · ✔ Update installed · /restart to apply',
                                '  Opus 5.5 | ctx 5%\n  ⏵⏵ auto mode on (shift+tab to cycle) · ⧉ 2 · 1 monitor'))
        self.assertEqual(r['goal'], '10h')
        self.assertEqual(r['update'], '✔ Update installed · /restart to apply')
        self.assertEqual(r['tasks'], ['1 monitor'])
        self.assertEqual(r['ide'], '2')

    def test_spinner_running_tool_and_heads_up(self):
        above = '\n'.join(['● Running the tests · 2m 55s', '  ⎿  $ npm test (2m 54s)', '',
                           '✻ Scampering… (7m 31s · ↓ 23.0k tokens)', '  ⎿  Tip: something', '',
                           '✦ Heads up · The gate skips database tests,', '  so a pass proves little.',
                           '  1: Learn more   2: Knew this already   0: Dismiss'])
        r = footer.parse(screen('❯ ', above))
        self.assertEqual(r['spinner'], {'verb': 'Scampering…', 'detail': '7m 31s · ↓ 23.0k tokens'})
        self.assertEqual(r['running']['title'], 'Running the tests')
        self.assertTrue(r['running']['command'].startswith('npm test'))
        self.assertEqual(r['heads_up']['text'], 'Heads up · The gate skips database tests, so a pass proves little.')
        self.assertEqual([o['key'] for o in r['heads_up']['options']], ['1', '2', '0'])

    def test_running_tool_while_its_dot_blinks_off(self):
        above = '\n'.join(['  Merging lanes and running checks · 39s', '  ⎿  $ set -e; git merge lane/x', '',
                           '✻ Newspapering… (53s · ↓ 1.2k tokens)'])
        r = footer.parse(screen('❯ ', above))
        self.assertEqual(r['running']['title'], 'Merging lanes and running checks')
        self.assertEqual(r['running']['command'], 'set -e; git merge lane/x')
        wrapped = '\n'.join(['● Reading the footer · 2s', '  ⎿  $', '     CJ=/tmp/x; curl -s', '     (ctrl+b ctrl+b (twice) to run in background)', '',
                             '✻ Newspapering… (53s · ↓ 1.2k tokens)'])
        self.assertEqual(footer.parse(screen('❯ ', wrapped))['running']['command'], 'CJ=/tmp/x; curl -s')

    def test_running_tool_with_a_wrapped_title(self):
        # A narrow pane (a phone-sized terminal) wraps the title. The second line alone is not the title.
        for dot in ('● ', '  '):
            above = '\n'.join(['', dot + 'Testing wrapper stops a run with no', '  output for 1 min · 19s', '  ⎿  $ python3 -m unittest test_x',
                               '', '✻ Deciphering… (1m 20s · ↓ 2k tokens)'])
            r = footer.parse(screen('❯ ', above))
            self.assertEqual(r['running'], {'title': 'Testing wrapper stops a run with no output for 1 min', 'elapsed': '19s', 'command': 'python3 -m unittest test_x'})

    def test_running_tool_call_shape(self):
        # Claude Code also shows a running call as "Bash(command)" with the time on its own line under the output.
        for dot in ('● ', '  '):
            above = '\n'.join(['', dot + 'Bash(echo "line one of output"; timeout 7 tail', '      -f /dev/null; tmux capture-pane', '      | head -40)',
                               '  ⎿  line one of output', '     … +10 lines (ctrl+o to expand)', '     (1m 43s · timeout 8m 20s)',
                               '     (ctrl+b ctrl+b (twice) to run in background)', '', '✻ Razzle-dazzling… (4m 15s · ↓ 9.7k tokens)'])
            r = footer.parse(screen('❯ ', above))
            self.assertEqual(r['running'], {'title': 'Bash', 'elapsed': '1m 43s', 'command': 'echo "line one of output"; timeout 7 tail -f /dev/null; tmux capture-pane | head -40'})
        short = '\n'.join(['', '  Bash(sleep 5)', '  ⎿  Running…', '     (6s)', '', '✻ Razzle-dazzling… (4m 15s · ↓ 9.7k tokens)'])
        self.assertEqual(footer.parse(screen('❯ ', short))['running'], {'title': 'Bash', 'elapsed': '6s', 'command': 'sleep 5'})
        finished = '\n'.join(['', '● Bash(ls)', '  ⎿  a.txt', '', '✻ Razzle-dazzling… (4m 15s · ↓ 9.7k tokens)'])
        self.assertIsNone(footer.parse(screen('❯ ', finished))['running'])

    def test_permission_denied_note_before_the_command(self):
        above = '\n'.join(['● Testing wrapper stops a run with no output for 1 min · 19s', '  ⎿  1 action not run · permission denied',
                           '     Reason: Runs a script outside the repo', '  ⎿  $ python3 x', '', '✻ Deciphering… (1m 20s · ↓ 2k tokens)'])
        r = footer.parse(screen('❯ ', above))
        self.assertEqual(r['running']['command'], 'python3 x')
        self.assertEqual(r['denied'], {'text': '1 action not run · permission denied', 'reason': 'Runs a script outside the repo'})
        self.assertIsNone(footer.parse(screen('❯ ', above.replace('✻ Deciphering… (1m 20s · ↓ 2k tokens)', '✻ Baked for 2m')))['denied'])

    def test_heads_up_card_states(self):
        card = lambda *rows: footer.parse(screen('❯ ', '\n'.join(['● Done.', ''] + list(rows))))['heads_up']
        making = card('✦ Making your page (a minute or two) · Claude keeps working…')
        self.assertEqual((making['state'], making['options']), ('making', []))
        ready = card('✦ Learning page ready', '  https://claude.ai/artifact/abc123', '  1: Helpful   2: Not helpful   3: Didn’t read')
        self.assertEqual((ready['state'], ready['url'], [o['key'] for o in ready['options']]), ('ready', 'https://claude.ai/artifact/abc123', ['1', '2', '3']))
        failed = card('✦ Couldn’t write that page · 0: OK')
        self.assertEqual((failed['state'], failed['text'], failed['options']), ('failed', 'Couldn’t write that page', [{'key': '0', 'label': 'OK'}]))
        # Taller than the screen: the key line is cut off, so the keys come from the state.
        explained = card('✦ Heads up · The test skipped the editor,', '  so it may not match.', '', '  The PDF test skipped the editor',
                         '  - You asked for a test on real PDFs. The result', '    was 0 of 84.', '  - A real path has four steps.', '↓ 3 more')
        self.assertEqual(explained['state'], 'explained')
        self.assertEqual(explained['text'], 'Heads up · The test skipped the editor, so it may not match.')
        self.assertEqual(explained['detail'], ['The PDF test skipped the editor', '- You asked for a test on real PDFs. The result was 0 of 84.', '- A real path has four steps.'])
        self.assertEqual([o['label'] for o in explained['options']], ['Understood', 'Chat in main session', 'Dismiss'])
        self.assertEqual(explained['more'], 3)

    def test_explained_card_gets_the_full_text_and_the_chat_note(self):
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            (store / 'cc-plugin-you-should-know_builtin-x.json').write_text(json.dumps({'sessions': [
                {'id': 'other', 'view': {'kind': 'explained', 'line': 'Nope.', 'text': 'x'}},
                {'id': 'thread-1', 'tag': 'Heads up', 'view': {'kind': 'explained', 'line': 'The test skipped the editor.', 'text': '**Title**\n\n* one\n* two'}}]}))
            card = {'text': 'Heads up · The test skipped the editor.', 'state': 'explained', 'detail': ['Title'], 'options': []}
            full = footer.card_note(card, 'thread-1', store)
            self.assertEqual(full['full'], '**Title**\n\n* one\n* two')
            self.assertEqual(full['note'], 'Here is a note offered by a side agent:\n\n> Heads up · The test skipped the editor.\n>\n> **Title**\n>\n> * one\n> * two\n\n')
            self.assertNotIn('full', footer.card_note(card, 'other', store))
            self.assertNotIn('full', footer.card_note(dict(card, state='offer'), 'thread-1', store))

    def test_scrolled_explained_card_without_headline(self):
        rows = ['· Concocting… (19s · ↓ 1.3k tokens)',
                '  - Switching signs example-host in to a different                      [-]',
                '    account for every session.',
                '  - Switch only when no job runs.', '',
                '  1: Understood   2: Chat in main session   0: Dismiss', '↑ 5 more']
        card = footer.parse(screen('❯ ', '\n'.join(rows)))['heads_up']
        self.assertEqual(card['detail'], ['- Switching signs example-host in to a different account for every session.', '- Switch only when no job runs.'])
        self.assertEqual((card['more'], card['clipped'], [o['key'] for o in card['options']]), (5, True, ['1', '2', '0']))
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'cc-plugin-you-should-know_builtin-x.json').write_text(json.dumps({'sessions': [
                {'id': 't', 'tag': 'Heads up', 'view': {'kind': 'explained', 'line': 'Switching can stop jobs.', 'text': '* a'}}]}))
            full = footer.card_note(card, 't', Path(folder))
            self.assertEqual((full['text'], full['full']), ('Heads up · Switching can stop jobs.', '* a'))
        self.assertIsNone(footer.parse(screen('❯ ', '\n'.join(['● Reply.', '  1: Yes   2: No', '↑ 5 more'])))['heads_up'])

    def test_scrolled_card_in_a_narrow_pane_drops_dismiss(self):
        # A 50-column pane has no room for "0: Dismiss"; the card scrolls both ways.
        rows = ['✻ Brewed for 1m 0s · done 2:36 PM', '    a Monitor or /loop).                      [-]', '',
                '  1: Understood   2: Chat in main session', '↑ 23 more · ↓ 1 more']
        card = footer.parse(screen('❯ ', '\n'.join(rows)))['heads_up']
        self.assertEqual((card['more'], [o['key'] for o in card['options']]), (23, ['1', '2', '0']))

    def test_scrolled_card_with_a_notice_under_it(self):
        rows = ['  - Check one build.', '', '  1: Understood   2: Chat in main session   0: Dismiss', '↑ 17 more',
                ' ' * 57 + '2% until auto-compact']
        r = footer.parse(screen('❯ ', '\n'.join(rows)))
        self.assertEqual((r['heads_up']['more'], r['heads_up']['detail']), (17, ['- Check one build.']))
        self.assertIn('2% until auto-compact', r['notices'])

    def test_an_old_card_above_newer_output_is_not_shown(self):
        r = footer.parse(screen('❯ ', '\n'.join(['✦ Making your page (a minute or two) · Claude keeps working…', '', '● Later reply.'])))
        self.assertIsNone(r['heads_up'])

    def test_spinner_with_several_words(self):
        r = footer.parse(screen('❯ ', '● Compacting conversation… (8s · ↓ 3 tokens)'))
        self.assertEqual(r['spinner'], {'verb': 'Compacting conversation…', 'detail': '8s · ↓ 3 tokens'})

    def test_idle_screen_has_no_live_parts(self):
        r = footer.parse(screen('❯ ', '● Running the tests · 2m 55s\n  ⎿  $ npm test'))
        self.assertIsNone(r['spinner']); self.assertIsNone(r['running']); self.assertIsNone(r['heads_up'])

    def test_no_prompt(self):
        self.assertEqual(footer.parse('plain shell $')['suggestion'], '')



class HeadsUpRegressionTests(unittest.TestCase):
    """Small, public-text copies of the captured and synthetic footer shapes."""

    def test_named_input_border_requires_a_bottom_rule(self):
        for border in (' Feature tethe… ─', ' skills investigat… ─', '── app-for-tmux ─'):
            with self.subTest(border=border):
                capture = '\n'.join([border, '❯', '─' * 17, '  Opus 5.5 | c…', '  ⏵⏵ auto mode'])
                self.assertEqual(footer.parse(capture)['status'], 'Opus 5.5 | c…')
                self.assertEqual(footer.parse(capture.replace('─' * 17, 'plain reply'))['status'], '')
        self.assertEqual(footer.parse('A transcript mentions ─ in a sentence\n❯ sent text\nplain reply')['status'], '')
        picker = screen('❯', below='  Old model') + '\n' + screen('❯ 1. Pick', below='Enter to select · ↑/↓ to navigate')
        self.assertIs(footer.parse(picker)['observed'], False)

    def test_observed_distinguishes_missing_input_from_empty_footer(self):
        result = footer.parse('✦ Heads up · Check one build.\n  1: Understood\n↑ 17 more')
        self.assertIs(result.get('observed'), False)
        self.assertIsNone(result['heads_up'])
        self.assertEqual(result['notices'], [])
        self.assertIs(footer.parse(screen('❯'))['observed'], True)

    def test_adjacent_actions_and_wrapped_labels_fill_all_state_keys(self):
        for headline in ('✦ Heads up · Check one build.\n\n  - Check one build.\n', '  - Check one build.\n'):
            for actions in ('  1: Understood   2: Chat in main session\n  0: Dismiss',
                            '  1: Unde\n  rstood', '  1: Understood',
                            '  1: Understood   2: Chat in main\n  session\n  0: Dismiss'):
                with self.subTest(headline=headline, actions=actions):
                    card = footer.parse(screen('❯', headline + '\n' + actions + '\n↑ 108 more · ↓ 6…'))['heads_up']
                    self.assertIsNotNone(card)
                    self.assertEqual(card['state'], 'explained')
                    self.assertEqual([(item['key'], item['label']) for item in card['options']], footer.CARD_KEYS['explained'])
                    self.assertEqual(card['more'], 108)
        for state, headline in (('offer', '✦ Heads up · Check one build.'), ('ready', '✦ Learning page ready')):
            actions = '\n'.join(f'  {key}: {label}' for key, label in footer.CARD_KEYS[state][:2])
            card = footer.parse(screen('❯', headline + '\n' + actions))['heads_up']
            self.assertEqual(card['state'], state)
            self.assertEqual([(item['key'], item['label']) for item in card['options']], footer.CARD_KEYS[state])
        self.assertIsNone(footer.parse(screen('❯', '  - Reply.\n  1: Yes\n  2: No\n↑ 5 more'))['heads_up'])

    def test_visible_headline_outside_40_lines_keeps_card_and_bounded_tools(self):
        above = ['● Running tests · 2s', '  ⎿  $ npm test', '✻ Working… (2s · ↓ 3 tokens)',
                 '✦ Heads up · Check one build.', '']
        above += [f'  - Detail {index:02d}.' for index in range(42)] + ['↓ 17 more']
        result = footer.parse(screen('❯', '\n'.join(above)))
        self.assertIsNotNone(result['heads_up'])
        self.assertEqual(result['heads_up']['text'], 'Heads up · Check one build.')
        self.assertEqual((result['heads_up']['state'], result['heads_up']['more']), ('explained', 17))
        self.assertIsNone(result['spinner'])
        self.assertIsNone(result['running'])

    def test_short_indented_notices_keep_full_and_clipped_cards(self):
        for headline in ('✦ Heads up · Check one build.\n\n', ''):
            for indent in (4, 7, 10, 12, 57):
                with self.subTest(headline=headline, indent=indent):
                    rows = headline + '  - Check one build.\n\n  1: Understood\n↑ 17 more\n'
                    notice = 'new task? /clear to save 150.8k tokens'
                    result = footer.parse(screen('❯', rows + ' ' * indent + notice))
                    self.assertIsNotNone(result['heads_up'])
                    self.assertEqual(result['notices'], [notice])
        rows = '✦ Heads up · Check one build.\n  1: Learn more\n    Unrelated output'
        self.assertIsNone(footer.parse(screen('❯', rows))['heads_up'])

    def stored_fragment(self, kind='explained', thread='thread-1', capture=None):
        if capture is None:
            capture = screen('❯', '    detail.  [-]\n\n↑ 108 more · ↓ 6…')
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            path = store / 'cc-plugin-you-should-know_builtin-test.json'
            content = json.dumps({'sessions': [{'id': thread, 'tag': 'Heads up',
                'view': {'kind': kind, 'line': 'Check one build.', 'text': '* Check the build.'}}]})
            path.write_text(content)
            handler = server.Handler.__new__(server.Handler)
            handler.path = '/mobile/api/footer?session=%25fixture'
            handler.deny = lambda: False
            replies = []
            handler.respond = lambda status, data: replies.append((status, data))
            with patch.object(server, 'choose', return_value={'agent': 'claude', 'id': '%fixture', 'thread': 'thread-1'}), \
                    patch.object(server, 'capture', return_value=capture) as read, \
                    patch.object(server.claude_hooks, 'overlay', side_effect=lambda config, result, **kwargs: result), \
                    patch.object(footer, 'CARD_STORE', store):
                handler.do_GET()
            read.assert_called_once()
            self.assertEqual(path.read_text(), content, 'Store recovery must stay read-only')
            self.assertEqual(replies[0][0], 200)
            return replies[0][1]['heads_up']

    def test_current_explained_store_restores_only_a_visible_fragment(self):
        for fragment in ('    detail.  [-]\n\n↑ 108 more · ↓ 6…', '  1: U…'):
            with self.subTest(fragment=fragment):
                card = self.stored_fragment(capture=screen('❯', fragment))
                self.assertIsNotNone(card)
                self.assertEqual((card['state'], card['text'], card['full']), ('explained', 'Heads up · Check one build.', '* Check the build.'))
                self.assertEqual([item['key'] for item in card['options']], ['1', '2', '0'])
        self.assertIsNone(self.stored_fragment(capture=screen('❯', '● Done.')))
        self.assertIsNone(self.stored_fragment(capture=screen('❯', '  1: U…\n● Later reply.')))
        self.assertIsNone(self.stored_fragment(capture=screen('❯', '  1: Undo the change')))
        self.assertIsNone(self.stored_fragment(capture='  1: U…\n↑ 17 more'))

    def test_dismissed_or_other_kind_store_entry_never_restores(self):
        for kind in ('dismissed', 'offer', 'making', 'ready'):
            with self.subTest(kind=kind):
                self.assertIsNone(self.stored_fragment(kind=kind))

    def test_other_thread_store_entry_never_restores(self):
        self.assertIsNone(self.stored_fragment(thread='other-thread'))

    def test_offer_at_80_columns_still_parses(self):
        capture = screen('❯', '✦ Heads up · Check one build.\n  1: Learn more   2: Knew this already   0: Dismiss')
        card = footer.parse(capture)['heads_up']
        self.assertEqual((card['state'], card['text']), ('offer', 'Heads up · Check one build.'))
        self.assertEqual([item['key'] for item in card['options']], ['1', '2', '0'])

    def test_named_border_full_footer_preserves_all_fields(self):
        rows = '\n'.join(['✦ Heads up · Check one build.', '  1: Learn more   2: Knew this already   0: Dismiss',
                          '            ◎ /goal active (10h)', '            2% until auto-compact'])
        capture = screen('❯', rows, '  Opus 5.5 | ctx 16%\n  ⏵⏵ auto mode on · 1 monitor\n  ● main\n  ◯ helper  Check build  1m')
        result = footer.parse(capture.replace('─' * 80, ' Feature investigation for the selected session ─', 1))
        for key in ('goal', 'notices', 'status', 'agents', 'heads_up', 'tasks', 'mode'):
            with self.subTest(key=key):
                self.assertEqual(result[key], footer.parse(capture)[key])
        self.assertEqual(result['goal'], '10h')
        self.assertEqual(result['notices'], ['2% until auto-compact'])
        self.assertEqual(len(result['agents']), 2)
        self.assertIs(result['observed'], True)


class CodexFooterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.fixture = Path(__file__).parent / 'fixtures'
        self.patches = [patch.object(server, 'RATES_FILE', self.folder / 'rates.json'),
                        patch.object(server, 'recent_rates', return_value=([], None)),
                        patch.object(server, 'capture', return_value='')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def rollout(self):
        state = server.codex_state()
        for index, line in enumerate((self.fixture / 'codex_footer.jsonl').read_text().splitlines()):
            server.codex_consume(state, index, json.loads(line))
        return state

    def report(self, state):
        return footer.codex(server.codex_metrics({'id': '%fixture'}, state['usage'], state['model'], state['effort'], state['credits'],
                                                 tier=state.get('tier', '')))

    def test_rollout_exact_context_rates_effort_and_credits(self):
        result = self.report(self.rollout())
        self.assertEqual((result['model'], result['effort']), ('gpt-6.1-sol', 'high'))
        self.assertAlmostEqual(result['context']['percent'], 128537 / 475000 * 100)
        self.assertEqual(result['context']['total_token_usage']['total_tokens'], 3307878)
        self.assertEqual(result['credits']['balance'], '8920.3102425000')
        self.assertEqual([(w['label'], w['percent'], w['resets_at']) for w in result['windows']],
                         [('5h', 12.5, 2000000000), ('168h', 55, 2000100000)])

    def test_rollout_precedes_rounded_screen(self):
        with patch.object(server, 'capture', return_value='  gpt-other low · Context 99% left'):
            result = self.report(self.rollout())
        self.assertEqual(result['model'], 'gpt-6.1-sol')
        self.assertEqual(result['effort'], 'high')
        self.assertAlmostEqual(result['context']['percent'], 128537 / 475000 * 100)

    def test_screen_fallback_without_invented_tokens_or_reset(self):
        with patch.object(server, 'capture', return_value=(self.fixture / 'codex_footer.txt').read_text()):
            result = self.report(server.codex_state())
        self.assertEqual(result['context'], {'percent': 27})
        self.assertEqual(result['effort'], 'high')
        self.assertEqual([w['percent'] for w in result['windows']], [13, 55])
        self.assertTrue(all(w['resets_at'] is None for w in result['windows']))
        self.assertNotIn('credits', result)

    def test_screen_formats_and_missing_effort(self):
        self.assertEqual(footer.codex_screen('  GPT · Context 97% left')['context']['percent'], 3)
        result = footer.codex_screen('\x1b[2m  gpt-5.2-codex xhigh · 87% context left · ~/x\x1b[0m')
        self.assertEqual((result['model'], result['effort'], result['context']['percent']), ('gpt-5.2-codex', 'xhigh', 13))
        self.assertEqual(footer.codex_screen('  gpt-5 low · Context 18.5% used')['context']['percent'], 18.5)
        self.assertNotIn('effort', footer.codex_screen('  GPT · Context 97% left'))
        self.assertEqual(footer.codex_screen('The example uses Context 50% left.\n› '), {})
        self.assertEqual(footer.codex_screen('  87% context left · ? for shortcuts'), {'context': {'percent': 13}, 'windows': []})
        self.assertEqual(footer.codex_screen('  GPT high · ~/x · Credits 0')['credits'], {'balance': '0'})
        self.assertNotIn('context', footer.codex_screen('  GPT high · 5h 87% left'))

    def test_clear_uses_screen_without_mutating_rollout_context(self):
        state = self.rollout()
        with patch.object(server, 'capture', return_value='OpenAI Codex\n› \n  GPT high · Context 100% left'):
            result = self.report(state)
        self.assertEqual(result['context'], {'percent': 0})
        self.assertEqual(state['usage'][0]['tokens'], 128537)

    def test_rate_only_event_keeps_context_and_handles_weekly_primary(self):
        state = self.rollout()
        server.codex_consume(state, 3, {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None,
            'rate_limits': {'primary': {'used_percent': 0, 'window_minutes': 10080, 'resets_at': 2000100000},
                            'secondary': None, 'credits': {'balance': '0', 'unlimited': False}}}})
        result = self.report(state)
        self.assertEqual(result['context']['tokens'], 128537)
        self.assertEqual(result['windows'], [{'label': '168h', 'percent': 0, 'resets_at': 2000100000}])
        self.assertEqual(result['credits']['balance'], '0')

    def test_missing_values_stay_hidden(self):
        result = self.report(server.codex_state())
        self.assertEqual(result, {'status': '', 'windows': []})
        state = server.codex_state()
        server.codex_consume(state, 0, {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {},
            'rate_limits': {'primary': {'used_percent': None, 'window_minutes': 300}, 'credits': {'unlimited': True}}}})
        result = self.report(state)
        self.assertEqual(result['windows'], [])
        self.assertTrue(result['credits']['unlimited'])

    def test_chat_payload_includes_footer_and_unknown_session_falls_back(self):
        path = self.folder / 'rollout.jsonl'
        path.write_text((self.fixture / 'codex_footer.jsonl').read_text())
        config = {'agent': 'codex', 'id': '%fixture', 'thread': 'fixture', 'transcript': str(path), 'writable': True}
        with patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}):
            data = server.chat_payload(config)
        self.assertEqual(data['footer']['credits']['balance'], '8920.3102425000')
        self.assertEqual(data['footer']['context'], data['usage']['context'])
        config['transcript'] = str(self.folder / 'missing')
        # Clear the account cache so this also checks the screen-only case.
        server.RATES_FILE.unlink()
        with patch.object(server, 'capture', return_value='  GPT medium · Context 98% left'):
            result = server.snapshot(config)['footer']
        self.assertEqual((result['model'], result['context']['percent']), ('GPT', 2))


class ModeTests(unittest.TestCase):
    def run_cycle(self, cycle, start, target):
        state = {'at': start, 'presses': 0}
        def press():
            state['presses'] += 1
            state['at'] = (state['at'] + 1) % len(cycle)
        read = lambda: {'mode': cycle[state['at']]}
        return footer.set_mode(read, press, target, sleep=lambda _: None), state['presses']

    def test_reaches_target(self):
        cycle = [None, '⏵⏵ accept edits on', '⏸ plan mode on', '⏵⏵ auto mode on']
        self.assertEqual(self.run_cycle(cycle, 3, 'plan'), ({'mode': 'plan'}, 3))
        self.assertEqual(self.run_cycle(cycle, 0, 'default'), ({'mode': 'default'}, 0))

    def test_missing_mode_stops_after_one_round(self):
        cycle = [None, '⏵⏵ accept edits on', '⏸ plan mode on']
        with self.assertRaisesRegex(RuntimeError, 'not offered'):
            self.run_cycle(cycle, 0, 'auto')

class PermissionDialogTests(unittest.TestCase):
    def test_a_permission_dialog_is_not_read_as_the_footer(self):
        # Rebuilt from a phone screenshot (pane %2): the dialog replaces the input box and indents its cursor row,
        # so the newest ❯ line was a sent message in the history. Its title became the model pill and every row a notice.
        rule, dash = '\u2500' * 60, '\u254c' * 60
        screen = ('\u276f Step 2 of 2 first: add the line to ~/.zshenv\n\n\u25cf Reading the file.\n\n' + rule + '\n Read file\n\n'
                  '  Read(\x1b]8;id=n13p79;file:///home/alex/.zshenv\x1b\\/home/alex/.zshenv\x1b]8;;\x1b\\)\n' + dash + '\n'
                  ' Do you want to proceed?\n \u276f 1. Yes\n   2. Yes, allow reading from /home/alex during this session\n   3. No\n\n'
                  ' Esc to cancel \u00b7 Tab to amend\n' + rule + '\n')
        result = footer.parse(screen)
        self.assertEqual((result['status'], result['notices'], result['mode'], result['suggestion']), ('', [], '', ''))

    def test_the_permission_card_shows_only_the_request(self):
        import terminal_input
        rule, dash = '\u2500' * 60, '\u254c' * 60
        screen = ('\u276f Step 2 of 2 first\n\n\u25cf Reading the file.\n\n' + rule + '\n Read file\n\n  Read(/home/alex/.zshenv)\n' + dash + '\n'
                  ' Do you want to proceed?\n \u276f 1. Yes\n   2. Yes, allow reading from /home/alex during this session\n   3. No\n\n'
                  ' Esc to cancel \u00b7 Tab to amend\n' + rule + '\n')
        card = terminal_input.approval(screen)
        self.assertEqual(card['text'], 'Read file\n\n  Read(/home/alex/.zshenv)')
        self.assertEqual([option['id'] for option in card['options']], [1, 2, 3])

    def test_the_input_box_below_a_numbered_history_line_still_parses(self):
        rule = '\u2500' * 40
        screen = ('\u276f 1. Do what is compliant\n   2. okay\n\n\u25cf Done.\n' + rule + '\n\u276f \n' + rule +
                  '\n  Opus 5.5 | ctx 9%\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n')
        result = footer.parse(screen)
        self.assertEqual((result['status'], result['mode'], result['notices']), ('Opus 5.5 | ctx 9%', '\u23f5\u23f5 auto mode on', []))

    def test_styled_capture_drops_terminal_links(self):
        import terminal_input
        styled = 'Read(\x1b]8;id=n1;file:///home/alex/.zshenv\x1b\\.zshenv\x1b]8;;\x1b\\) \x1b[1mbold\x1b[0m'
        run = lambda argv, **kwargs: __import__('types').SimpleNamespace(stdout=styled)
        self.assertEqual(terminal_input.capture({'id': '%1'}, run, styled=True), 'Read(.zshenv) \x1b[1mbold\x1b[0m')
        self.assertEqual(terminal_input.capture({'id': '%1'}, run), styled)


if __name__ == '__main__':
    unittest.main()
