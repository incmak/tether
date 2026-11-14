"""Rule migration contracts and isolated loader, fallback, and input guard tests."""
import importlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import detection_rules
import terminal_input as terminal
import terminal_replies

# Literal source text copied from the original Python definitions and calls.
# module, (rules file, JSON name), public attribute, original text, original flags
ORIGINALS = [
    ('terminal_input', ('common', 'WORKING'), 'WORKING', (r'Working \(|esc to interrupt|(?m:^\s*[✻✶✽✢·*●]\s+[A-Z][a-z]+(?:ing|ed)?…)'), re.I),
    ('terminal_input', ('common', 'APPROVAL_QUESTION'), 'APPROVAL_QUESTION', (r'^(?:Claude has written up a plan.*Would you like to proceed|Ready to code\?|Quick safety check|'
    r'Run this command\?|Would you like to \w+|Do you want to (?:proceed|allow|approve|make this edit|create)|'
    r'Do you trust|Trust this (?:folder|directory|project)|Allow access to|Allow this|'
    r'trust.{0,100}(?:folder|directory|project)|(?:folder|directory|project).{0,100}trust)'), re.I),
    ('terminal_input', ('common', 'DIALOG_FRAME'), 'DIALOG_FRAME', (r'^\s*(?:[─━═╌╍┄┅\-_]{3,}|Do you want to proceed\?)\s*$'), 0),
    ('terminal_input', ('common', 'NUMBERED_ITEM'), 'NUMBERED_ITEM', (r'^\s*[>❯›]?\s*\d+[.)]\s+\S'), 0),
    ('terminal_input', ('common', 'CURSOR_ITEM'), 'CURSOR_ITEM', (r'^\s*[>❯›]\s*\d+[.)]\s+\S'), 0),
    ('terminal_input', ('common', 'CHAT_LINE'), 'CHAT_LINE', (r'^\s*[●⏺✻✶✳✢✽·]\s|^\s*⎿'), 0),
    ('terminal_input', ('common', 'INPUT_HINT'), 'INPUT_HINT', (r'esc Close|↑/↓ Scroll|Enter to (?:select|confirm|continue)|↑/?↓ (?:to )?(?:navigate|select)|enter Select|Esc to cancel|press enter|press any key|'
                        r'\benter\s+\S+\s*[·•]\s*esc\b|'
                        r'Keyboard:|\(y/n\)|\[y/n\]|\(yes/no\)'), re.I),
    ('terminal_input', ('common', 'RULE_LINE'), 'RULE_LINE', (r'^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║-]*$'), 0),
    ('terminal_input', ('common', 'PICKER_CURSOR'), 'PICKER_CURSOR', (r'^\s*[❯›]\s+(\S.*)$'), 0),
    ('terminal_input', ('common', 'PICKER_MORE'), 'PICKER_MORE', (r'^\s*([↑↓])\s+(\d+)\s+more\s*$'), 0),
    ('terminal_input', ('common', 'PICKER_HINT'), 'PICKER_HINT', (r'^Enter to .+[·•].*\bEsc to \S'), re.I),
    ('terminal_input', ('claude', 'SETTINGS_TABS'), 'SETTINGS_TABS', (r'^\s*Settings\s+Status\b'), 0),
    ('terminal_input', ('claude', 'PANEL_CLOSE'), 'PANEL_CLOSE', (r'Esc to (?:cancel|close|exit|go back)|esc (?:Close|to close)'), re.I),
    ('terminal_input', ('antigravity', 'AGY_MODE_HINT'), 'AGY_MODE_HINT', (r'\bmode\b.*(?:\(shift\+tab to cycle\)|\.\.\.|…)\s*$'), re.I),
    ('terminal_input', ('antigravity', 'AGY_PANEL'), 'AGY_PANEL', (r'esc Close|↑/↓ Scroll|\(\d+–\d+ of \d+ lines\)|general\s+commands\s+shortcuts'), re.I),
    ('terminal_input', ('common', 'PICKER_HINT_START'), 'PICKER_HINT_START', (r'^\s*Enter\b'), re.I),
    ('terminal_input', ('common', 'MENU_HINT_START'), 'MENU_HINT_START', (r'^\s*enter\b'), re.I),
    ('terminal_input', ('common', 'PARTIAL_PICKER_HINT'), 'PARTIAL_PICKER_HINT', (r'\bEnter\s+to\b'), re.I),
    ('terminal_input', ('claude', 'PLAN_MENU_OPTION'), 'PLAN_MENU_OPTION', (r'\bkeep planning\b|tell Claude what to change'), re.I),
    ('terminal_input', ('antigravity', 'AGY_BUSY'), 'AGY_BUSY', (r'esc to cancel|Loading\.\.\.|Working'), 0),
    ('needs', ('common', 'GENERIC'), 'GENERIC', (r'^(?:Do you want to proceed\?|Would you like to proceed\?|Permission rule .*|Requesting permission for:?)$'), re.I),
    ('model_picker', ('common', 'MODEL_NUMBERED'), 'NUMBERED', (r'^\s*(?P<mark>[❯›>↓↑]?)\s*(?P<number>\d+)[.)]\s+(?P<body>\S.*)$'), 0),
    ('model_picker', ('common', 'MODEL_LEVEL_IN_NAME'), 'LEVEL_IN_NAME', (r'\((?:minimal|low|medium|high|xhigh)\)\s*$'), re.I),
    ('model_picker', ('common', 'MODEL_CONFIRMATION'), 'CONFIRMATION', (r'\b(set model|kept model|model (set|changed)|changed model|model to|effort (set|changed))\b'), re.I),
    ('model_picker', ('claude', 'MODEL_MORE'), 'MORE', (r'…\s*\+(\d+)\s+models'), 0),
    ('model_picker', ('claude', 'MODEL_EFFORT'), 'MODEL_EFFORT', (r'(?P<level>extra\s+high|[A-Za-z]+)\s+effort(?:\s+\(default\))?\s+←/→'), re.I),
    ('model_picker', ('antigravity', 'MODEL_CURRENT'), 'MODEL_CURRENT', (r'\(curr'), 0),
    ('permissions_dialog', ('claude', 'PERMISSIONS_TAB_ROW'), 'TAB_ROW', (r'^\s*Permissions\s{2,}(Recently blocked|Allow)\b'), 0),
    ('permissions_dialog', ('claude', 'PERMISSIONS_HINT'), 'HINT', (r'\bEsc\b.*(?:cancel|close|appl(?:y|ies))|←/→.*(?:switch|tabs?)'), re.I),
    ('terminal_replies', ('codex', 'REPLIES_SPINNER'), 'SPINNER', (r'\besc to interrupt\b'), re.I),
    ('terminal_replies', ('codex', 'REPLIES_PICKER'), 'PICKER', (r'enter (?:to )?(?:select|confirm|continue)|esc (?:back|to cancel|to go back)|↑/?↓|to navigate|press enter'), re.I),
    ('terminal_replies', ('codex', 'REPLIES_FOOTER'), 'FOOTER', (r'^\s*\S+\s+(?:minimal|low|medium|high|xhigh|extra high)\s+·\s+Context'), re.I),
    ('terminal_replies', ('codex', 'REPLIES_BOX'), 'BOX', (r'^[\s─━│┃┌┐└┘├┤┬┴┼╭╮╰╯═║\-_|+]*$'), 0),
    ('terminal_replies', ('claude', 'BANNER_START'), 'CLAUDE_BANNER_START', (r'Claude Code v\d'), 0),
    ('terminal_replies', ('claude', 'BANNER_END'), 'CLAUDE_BANNER_END', (r'^\s*(?:─{3,}|[❯>!]|⏺|●)'), 0),
    ('terminal_replies', ('codex', 'REPLIES_CONTEXT'), 'CONTEXT_LINE', (r'.*·\s*Context\b'), 0),
    ('terminal_replies', ('antigravity', 'BANNER_END'), 'AGY_BANNER_END', (r'^\s*─{3,}'), 0),
    ('prompt_actions', ('codex', 'REPORT_RULE'), 'RULE', (r'^\s*[─━═-]{3,}\s*$'), 0),
    ('side_question', ('claude', 'SIDE_RULE'), 'RULE', (r'^▔{20,}\s*$'), 0),
    ('side_question', ('claude', 'SIDE_QUESTION'), 'QUESTION', (r'^\s*/btw\s+(\S.*)$'), 0),
    ('side_question', ('claude', 'SIDE_FOOTER'), 'FOOTER', (r'(?:^|· )Esc to close$'), 0),
    ('side_question', ('claude', 'SIDE_COPY'), 'COPY', (r'\bc to copy\b|\bCopied to clipboard\b'), 0),
    ('side_question', ('claude', 'SIDE_FORK'), 'FORK', (r'\bf to fork\b'), 0),
]


class ShippedRulesTests(unittest.TestCase):
    def test_original_patterns_and_flags_are_unchanged(self):
        for module, key, attribute, text, flags in ORIGINALS:
            with self.subTest(pattern=key):
                compiled = getattr(importlib.import_module(module), attribute)
                self.assertEqual(compiled.pattern, text, 'pattern changed: move it deliberately')
                self.assertEqual(compiled.flags, re.compile(text, flags).flags,
                                 'pattern changed: move it deliberately')

    def test_table_is_complete(self):
        loaded = detection_rules.load_all()
        expected = [key for _, key, _, _, _ in ORIGINALS]
        actual = {(file, name) for file, entries in loaded.items() for name in entries}
        self.assertEqual(set(expected), actual)
        self.assertEqual(len(expected), len(actual))

    def test_shipped_files_have_versions_comments_and_no_runtime_errors(self):
        for module in {row[0] for row in ORIGINALS}:
            importlib.import_module(module)
        for file in detection_rules.FILES:
            raw = (detection_rules.DIRECTORY / (file + '.json')).read_text(encoding='utf-8')
            data = json.loads(raw)
            self.assertIs(type(data['version']), int)
            self.assertEqual(data['version'], detection_rules.VERSION)
            self.assertTrue(data['comment'].strip())
            self.assertNotRegex(raw, r'\\u[0-9a-fA-F]{4}')
            for entry in data['patterns'].values():
                self.assertTrue(entry['comment'].strip())
        self.assertEqual(detection_rules._RULES.errors, [])

    def test_trust_question_stays_in_code(self):
        for file, patterns in detection_rules.load_all().items():
            with self.subTest(file=file):
                self.assertNotIn('TRUST_QUESTION', patterns)
        self.assertIsNone(terminal.approval("Allow this\n  Yes, proceed\n  No, exit\n"))

    def test_agent_samples(self):
        for pattern, text in [(terminal.AGY_PANEL, '(1–5 of 9 lines)'),
                              (terminal.SETTINGS_TABS, '  Settings  Status'),
                              (terminal_replies.SPINNER, 'esc to interrupt')]:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNotNone(pattern.search(text))
        self.assertIsNone(terminal.AGY_BUSY.search('working'))
        self.assertIsNotNone(terminal.AGY_BUSY.search('Working'))

    def test_numbered_and_trust_approvals(self):
        for screen in ('Do you want to proceed?\n❯ 1. Yes\n  2. No',
                       'Do you trust this folder?\n❯ Yes\n  No'):
            with self.subTest(screen=screen):
                pending = terminal.approval(screen)
                self.assertIsNotNone(pending)
                self.assertTrue(pending['actionable'])
                self.assertEqual([option['text'] for option in pending['options']], ['Yes', 'No'])


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / 'shipped'
        self.snapshot = self.root / 'last-good'
        self.directory.mkdir()
        self.logs = []
        for name in detection_rules.FILES:
            self.write(name)

    def data(self, regex=r'(?P<label>good)', **entry):
        return {'version': 1, 'comment': 'Test rules.',
                'patterns': {'TEST': {'regex': regex, 'comment': 'Find a test label.', **entry}}}

    def write(self, name='common', data=None, directory=None):
        directory = self.directory if directory is None else directory
        directory.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(self.data() if data is None else data, ensure_ascii=False).encode('utf-8')
        (directory / (name + '.json')).write_bytes(raw)
        return raw

    def rules(self, snapshot=None):
        return detection_rules.Rules(self.directory, snapshot=snapshot, log=self.logs.append)

    def seed_snapshot(self):
        for name in detection_rules.FILES:
            self.write(name, directory=self.snapshot)

    def failures(self):
        good = self.data()
        entry = good['patterns']['TEST']
        cases = [
            ('missing file', None, False),
            ('bad JSON', b'{', False),
            ('bad UTF-8', b'\xff', False),
            ('top-level array', b'[]', False),
            ('duplicate root key', b'{"version":1,"version":1}', False),
            ('duplicate pattern key', b'{"version":1,"patterns":{"TEST":{},"TEST":{}}}', False),
            ('duplicate entry key', b'{"version":1,"patterns":{"TEST":{"regex":"a","regex":"b"}}}', True),
            ('duplicate deeply nested key', b'{"version":1,"patterns":{"TEST":{"flags":[{"x":1,"x":2}]}}}', True),
        ]
        for version in (2, True, 1.0, '1', None):
            cases.append((f'wrong version {version!r}', json.dumps({**good, 'version': version}).encode(), False))
        for patterns in (None, {}, [], 'TEST'):
            cases.append((f'bad patterns {patterns!r}', json.dumps({**good, 'patterns': patterns}).encode(), False))
        for label, value in [('entry array', []), ('entry string', 'bad'), ('missing regex', {'comment': 'Test.'}),
                             ('missing comment', {'regex': 'good'}),
                             ('bad regex', {**entry, 'regex': '['}),
                             ('recursive regex', {**entry, 'regex': '(' * 1000}),
                             ('overflow regex', {**entry, 'regex': 'a{999999999999999999999}'}),
                             ('unknown flag', {**entry, 'flags': ['VERBOSE']}),
                             ('flags string', {**entry, 'flags': 'IGNORECASE'}),
                             ('flags null', {**entry, 'flags': None}),
                             ('non-string flag', {**entry, 'flags': [True]}),
                             ('non-hashable flag', {**entry, 'flags': [[]]}),
                             ('unknown entry key', {**entry, 'extra': 1})]:
            cases.append((label, json.dumps({**good, 'patterns': {'TEST': value}}).encode(), True))
        for field, values in [('regex', (None, 1, [])), ('comment', (None, 1, '', ' \n\t'))]:
            for value in values:
                cases.append((f'bad {field} {value!r}',
                              json.dumps({**good, 'patterns': {'TEST': {**entry, field: value}}}).encode(), True))
        return cases

    def install_failure(self, raw, file='common'):
        path = self.directory / (file + '.json')
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)
        return path

    def test_strict_failures_name_file_and_pattern(self):
        for label, raw, pattern_context in self.failures():
            with self.subTest(failure=label):
                path = self.install_failure(raw)
                with self.assertRaises(detection_rules.RulesError) as caught:
                    detection_rules.load_file('common', self.directory)
                self.assertIn(str(path), str(caught.exception))
                if pattern_context:
                    self.assertIn('TEST', str(caught.exception))

    def test_runtime_failures_do_not_raise_and_disable_agents(self):
        for label, raw, _ in self.failures():
            with self.subTest(failure=label):
                path = self.install_failure(raw)
                self.logs.clear()
                rules = self.rules()
                self.assertTrue(rules.errors)
                self.assertTrue(self.logs)
                self.assertTrue(all(str(path) in message for message in self.logs))
                for agent in ('claude', 'codex', 'antigravity'):
                    self.assertFalse(rules.available(agent))
                pattern = rules.pattern('common', 'TEST')
                self.assertIs(pattern, detection_rules.NEVER)
                for text in ('', 'good', 'anything\nelse'):
                    self.assertIsNone(pattern.search(text))
                    self.assertIsNone(pattern.match(text))
                    self.assertIsNone(pattern.fullmatch(text))
                    self.assertEqual(list(pattern.finditer(text)), [])

    def test_unreadable_file_is_strict_and_runtime_safe(self):
        original = Path.read_bytes
        path = self.directory / 'common.json'
        def read(candidate):
            if candidate == path:
                raise PermissionError('Test denied access.')
            return original(candidate)
        with patch.object(Path, 'read_bytes', read):
            with self.assertRaises(detection_rules.RulesError) as caught:
                detection_rules.load_file('common', self.directory)
            self.assertIn(str(path), str(caught.exception))
            rules = self.rules()
        self.assertFalse(rules.available('codex'))
        self.assertIs(rules.pattern('common', 'TEST'), detection_rules.NEVER)
        self.assertTrue(self.logs)

    def test_strict_load_all_raises_first_error(self):
        self.install_failure(b'{')
        self.install_failure(b'{', 'claude')
        with self.assertRaises(detection_rules.RulesError) as caught:
            detection_rules.load_all(self.directory)
        self.assertIn(str(self.directory / 'common.json'), str(caught.exception))

    def test_optional_flags_and_all_allowed_flags(self):
        for flags in (None, [], ['IGNORECASE'], ['MULTILINE'], ['DOTALL'],
                      ['IGNORECASE', 'MULTILINE', 'DOTALL']):
            with self.subTest(flags=flags):
                self.write(data=self.data(r'^a.b$', **({} if flags is None else {'flags': flags})))
                pattern = detection_rules.load_file('common', self.directory)['TEST']
                combined = 0
                for flag in flags or []:
                    combined |= getattr(re, flag)
                self.assertEqual(pattern.flags, re.compile(r'^a.b$', combined).flags)

    def test_load_and_compile_once_per_file_without_reading_snapshot(self):
        with patch.object(detection_rules, '_load_file', wraps=detection_rules._load_file) as load:
            with patch.object(detection_rules.re, 'compile', wraps=re.compile) as compile_pattern:
                rules = self.rules(snapshot=self.snapshot)
                rules.pattern('common', 'TEST')
                rules.pattern('common', 'TEST')
                self.assertEqual(compile_pattern.call_count, len(detection_rules.FILES))
            self.assertEqual(load.call_count, len(detection_rules.FILES))
            self.assertEqual([call.args for call in load.call_args_list],
                             [(name, self.directory) for name in detection_rules.FILES])

    def test_all_failure_kinds_fall_back_to_good_snapshot(self):
        self.seed_snapshot()
        for label, raw, _ in self.failures():
            with self.subTest(failure=label):
                self.install_failure(raw)
                self.logs.clear()
                rules = self.rules(snapshot=self.snapshot)
                self.assertTrue(rules.available('claude'))
                self.assertEqual(rules.pattern('common', 'TEST').pattern, r'(?P<label>good)')
                self.assertTrue(any('last good' in message for message in self.logs))
                self.assertTrue(rules.errors)

    def test_wrong_version_snapshot_is_rejected(self):
        self.seed_snapshot()
        self.install_failure(b'{')
        self.write(data={**self.data(), 'version': 2}, directory=self.snapshot)
        rules = self.rules(snapshot=self.snapshot)
        self.assertFalse(rules.available('claude'))
        self.assertIs(rules.pattern('common', 'TEST'), detection_rules.NEVER)
        self.assertTrue(any(str(self.snapshot / 'common.json') in message for message in rules.errors))

    def test_missing_snapshot_is_logged_once_per_file(self):
        rules = self.rules(snapshot=self.snapshot)
        for _ in range(2):
            self.assertIs(rules.pattern('claude', 'MISSING'), detection_rules.NEVER)
        snapshot_errors = [error for error in rules.errors if str(self.snapshot / 'claude.json') in error]
        self.assertEqual(len(snapshot_errors), 1)

    def test_missing_name_falls_back_and_snapshot_is_loaded_once(self):
        self.seed_snapshot()
        self.write(data={'version': 1, 'patterns': {'OTHER': {'regex': 'new', 'comment': 'Find new text.'}}})
        rules = self.rules(snapshot=self.snapshot)
        with patch.object(detection_rules, 'load_file', wraps=detection_rules.load_file) as load:
            for _ in range(2):
                self.assertEqual(rules.pattern('common', 'TEST').pattern, r'(?P<label>good)')
            load.assert_called_once_with('common', self.snapshot)
        self.assertTrue(rules.available('codex'))
        self.assertTrue(any('last good' in message for message in self.logs))

    def test_group_and_named_group_contracts_fall_back(self):
        self.seed_snapshot()
        for regex, constraints in [('good', {'groups': 1}), ('(good)', {'names': ('label',)}),
                                   ('(good)', {'groups': 1, 'names': ('label',)})]:
            with self.subTest(regex=regex, constraints=constraints):
                self.write(data=self.data(regex))
                rules = self.rules(snapshot=self.snapshot)
                self.assertEqual(rules.pattern('common', 'TEST', **constraints).pattern, r'(?P<label>good)')
                self.assertTrue(rules.available('codex'))

    def test_group_contracts_fail_closed_without_snapshot(self):
        for constraints in ({'groups': 2}, {'names': ('missing',)}):
            with self.subTest(constraints=constraints):
                rules = self.rules()
                self.assertIs(rules.pattern('common', 'TEST', **constraints), detection_rules.NEVER)
                self.assertFalse(rules.available('antigravity'))
                self.assertTrue(rules.errors)

    def test_snapshot_with_missing_groups_is_rejected(self):
        self.seed_snapshot()
        self.write(data=self.data('good'), directory=self.snapshot)
        rules = self.rules(snapshot=self.snapshot)
        self.assertIs(rules.pattern('common', 'TEST', groups=2), detection_rules.NEVER)
        self.assertFalse(rules.available('codex'))

    def test_sufficient_groups_are_accepted_without_snapshot(self):
        rules = self.rules(snapshot=self.snapshot)
        pattern = rules.pattern('common', 'TEST', groups=1, names=('label',))
        self.assertEqual(pattern.search('good')['label'], 'good')
        self.assertEqual(rules.errors, [])
        self.assertTrue(rules.available('claude'))

    def test_bad_agent_file_is_isolated(self):
        self.install_failure(b'{', 'claude')
        rules = self.rules()
        self.assertFalse(rules.available('claude'))
        self.assertTrue(rules.available('codex'))
        self.assertTrue(rules.available('antigravity'))

    def test_missing_pattern_only_disables_its_file(self):
        rules = self.rules()
        self.assertIs(rules.pattern('claude', 'MISSING'), detection_rules.NEVER)
        self.assertFalse(rules.available('claude'))
        self.assertTrue(rules.available('codex'))
        self.assertTrue(rules.available('antigravity'))

    def test_bad_common_file_disables_all_agents(self):
        self.install_failure(b'{')
        rules = self.rules()
        for agent in ('claude', 'codex', 'antigravity'):
            self.assertFalse(rules.available(agent))

    def test_save_writes_exact_bytes_atomically_and_skips_unchanged(self):
        path = self.directory / 'common.json'
        path.write_bytes(json.dumps(self.data(), ensure_ascii=False, indent=4).encode() + b'\n\n')
        rules = self.rules(snapshot=self.snapshot)
        with patch.object(detection_rules.os, 'replace', wraps=detection_rules.os.replace) as replace:
            rules.save_last_good()
            self.assertEqual(replace.call_count, len(detection_rules.FILES))
            for call in replace.call_args_list:
                self.assertEqual(call.args[0].parent, self.snapshot)
                self.assertEqual(call.args[1].parent, self.snapshot)
            replace.reset_mock()
            rules.save_last_good()
            replace.assert_not_called()
        for name in detection_rules.FILES:
            self.assertEqual((self.directory / (name + '.json')).read_bytes(),
                             (self.snapshot / (name + '.json')).read_bytes())
        self.assertEqual(set(path.suffix for path in self.snapshot.iterdir()), {'.json'})

    def test_save_skips_failed_file(self):
        self.seed_snapshot()
        original = (self.snapshot / 'claude.json').read_bytes()
        self.install_failure(b'{', 'claude')
        self.write('codex', self.data('new'))
        rules = self.rules(snapshot=self.snapshot)
        rules.save_last_good()
        self.assertEqual((self.snapshot / 'claude.json').read_bytes(), original)
        self.assertEqual((self.snapshot / 'codex.json').read_bytes(), (self.directory / 'codex.json').read_bytes())

    def test_save_preserves_snapshot_after_losing_required_name_or_groups(self):
        self.seed_snapshot()
        original = (self.snapshot / 'common.json').read_bytes()
        for data, constraints in [({'version': 1, 'patterns': {'OTHER': {'regex': 'new', 'comment': 'Test.'}}}, {}),
                                  (self.data('good'), {'groups': 1}),
                                  (self.data('(good)'), {'names': ('label',)})]:
            with self.subTest(data=data, constraints=constraints):
                self.write(data=data)
                rules = self.rules(snapshot=self.snapshot)
                self.assertIsNot(rules.pattern('common', 'TEST', **constraints), detection_rules.NEVER)
                rules.save_last_good()
                self.assertEqual((self.snapshot / 'common.json').read_bytes(), original)

    def test_save_without_snapshot_does_nothing(self):
        rules = self.rules()
        with patch.object(detection_rules.os, 'replace') as replace, patch.object(Path, 'mkdir') as mkdir:
            rules.save_last_good()
        replace.assert_not_called()
        mkdir.assert_not_called()

    def test_save_logs_os_errors_and_cleans_temporary_file(self):
        rules = self.rules(snapshot=self.snapshot)
        with patch.object(detection_rules.os, 'replace', side_effect=OSError('Test replacement failure.')):
            rules.save_last_good()
        self.assertEqual(len(self.logs), len(detection_rules.FILES))
        self.assertEqual(list(self.snapshot.iterdir()), [])
        self.assertTrue(all(str(self.snapshot) in message for message in self.logs))

    def test_default_log_is_one_line_on_stderr(self):
        self.install_failure(b'{')
        with patch.object(detection_rules.sys, 'stderr') as stderr:
            rules = detection_rules.Rules(self.directory)
        self.assertTrue(rules.errors)
        self.assertTrue(stderr.flush.called)
        messages = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertEqual(len(messages.splitlines()), 1)
        self.assertIn(str(self.directory / 'common.json'), messages)


class InputGuardTests(unittest.TestCase):
    def entry(self, agent):
        return {'id': '%test', 'agent': agent, 'owner_pid': 42, 'process_start': 123,
                'writable': True, 'pending_conversation': True}

    def test_bad_rules_return_unknown_without_tmux_for_all_agents(self):
        for agent in ('claude', 'codex', 'antigravity'):
            with self.subTest(agent=agent), patch.object(detection_rules, 'available', return_value=False):
                run = Mock()
                self.assertEqual(terminal.live_status(self.entry(agent), run), {'state': 'Unknown', 'approval': None})
                run.assert_not_called()

    def test_bad_rules_reject_verify_before_process_or_tmux_access(self):
        for agent in ('claude', 'codex', 'antigravity'):
            with self.subTest(agent=agent), patch.object(detection_rules, 'available', return_value=False):
                with patch.object(terminal.procinfo, 'comm') as comm:
                    run = Mock()
                    with self.assertRaisesRegex(RuntimeError, 'Tether could not load its prompt rules for this agent. Check the server log.'):
                        terminal.verify(self.entry(agent), run)
                    run.assert_not_called()
                    comm.assert_not_called()

    def test_unwritable_entry_still_returns_unavailable(self):
        with patch.object(detection_rules, 'available', return_value=False) as available:
            run = Mock()
            self.assertEqual(terminal.live_status({'writable': False}, run)['state'], 'Unavailable')
            run.assert_not_called()
            available.assert_not_called()

    def test_good_rules_keep_live_capture_and_status_for_all_agents(self):
        screen = 'Do you want to proceed?\n❯ 1. Yes\n  2. No'
        expected = terminal.approval(screen)
        for agent in ('claude', 'codex', 'antigravity'):
            with self.subTest(agent=agent), patch.object(detection_rules, 'available', return_value=True):
                run = Mock(return_value=Mock(stdout=screen))
                with patch('ask_bridge.overlay', side_effect=lambda entry, status: status), \
                        patch.object(terminal, 'capture_notices'), patch.object(terminal, 'capture_quota_notices'):
                    result = terminal.live_status(self.entry(agent), run)
                self.assertEqual(result['state'], 'Approval')
                self.assertEqual(result['approval']['id'], expected['id'])
                run.assert_called_once()
                self.assertIn('capture-pane', run.call_args.args[0])

    def test_good_rules_keep_verify_checks_for_all_agents(self):
        for agent, command in [('claude', 'claude'), ('codex', 'codex'), ('antigravity', 'agy')]:
            with self.subTest(agent=agent), patch.object(detection_rules, 'available', return_value=True):
                with patch.object(terminal.procinfo, 'comm', return_value=command), \
                        patch.object(terminal, 'process_start', return_value=123):
                    run = Mock(return_value=Mock(stdout=f'42\t{command}\t0'))
                    terminal.verify(self.entry(agent), run)
                    run.assert_called_once()
                    self.assertIn('display-message', run.call_args.args[0])
                    run.return_value.stdout = f'42\t{command}\t1'
                    with self.assertRaisesRegex(RuntimeError, 'Return to the agent prompt'):
                        terminal.verify(self.entry(agent), run)


if __name__ == '__main__':
    unittest.main()
