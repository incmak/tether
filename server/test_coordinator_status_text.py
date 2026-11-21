"""Keep coordinator.message and the status card's line grammar in sync."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import coordinator


FIXTURES = Path(__file__).with_name('fixtures') / 'injected_prompts'
CLI = 'python3 /opt/tether/server/agent_coordinator_cli.py'
# These patterns mirror ui/src/statusText.ts. fullmatch anchors each pattern to one whole line.
HEADER = re.compile(
    r'\[Tether status\] ([1-9][0-9]*) tab (updates?)\. Tell the user what changed and what needs them, '
    r'in one or two lines per tab\. Reports are set to "(all|needs|off)"\. If you do not have the coordinator '
    r'rules, run `[^`\r\n\u2028\u2029]+ claim`\.'
)
ROW = re.compile(
    r'- `([^`\r\n\u2028\u2029]+)` \((%[0-9]+)\) '
    r'(finished and asks the user|finished|waits on the user|failed)(?:: ([^\r\n\u2028\u2029]*))?'
)


class CoordinatorStatusTextTests(unittest.TestCase):
    def assert_status_grammar(self, text):
        self.assertNotIn('\r', text)
        first, *lines = text.split('\n')
        head = HEADER.fullmatch(first)
        self.assertIsNotNone(head, first)
        count = int(head[1])
        self.assertEqual(head[2], 'update' if count == 1 else 'updates')
        self.assertLessEqual(count, len(lines))
        self.assertTrue(all(not line.strip() for line in lines[count:]))
        for index, line in enumerate(lines[:count]):
            if index == count - 1:
                trimmed = line.rstrip()
                line = trimmed + ' ' if trimmed.endswith(':') and line[len(trimmed):].startswith(' ') else trimmed
            row = ROW.fullmatch(line)
            self.assertIsNotNone(row, line)
            detail = row[4] or ''
            if index < count - 1:
                self.assertEqual(detail, detail.rstrip())

    def test_writer_cases_match_exact_text_and_grammar(self):
        cases = json.loads((FIXTURES / 'tether_status_written.json').read_text(encoding='utf-8'))['cases']
        for index, case in enumerate(cases):
            with self.subTest(case=index, mode=case['mode']), patch.object(coordinator, 'cli', return_value=CLI):
                text = coordinator.message(case['events'], case['mode'])
                self.assertEqual(text, case['text'])
                self.assert_status_grammar(text)

    def test_recorded_prompts_match_line_grammar(self):
        prompts = json.loads((FIXTURES / 'tether_status.json').read_text(encoding='utf-8'))['prompts']
        self.assertEqual(len(prompts), 56)
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=index):
                self.assert_status_grammar(prompt)
