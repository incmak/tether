"""advisor_log.py: concurrent writers, damaged JSONL lines and the full text of each ask."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import advisor_log
import helper_tabs

HERE = Path(__file__).resolve().parent
# One child process: wait for the start file, then append COUNT entries to FOLDER.
WRITER = '''
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import advisor_log
folder, start, name, count = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], int(sys.argv[5])
while not start.exists():
    time.sleep(0.001)
for index in range(count):
    advisor_log.append(folder, advisor_log.make(f'{name} {index}', {'status': 'decided', 'answer': 'yes'}, 'claude', 'fable'))
'''


def writer(folder, start, name, count):
    return subprocess.Popen([sys.executable, '-c', WRITER, str(HERE), str(folder), str(start), name, str(count)])


class AdvisorLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / 'project'
        self.folder.mkdir(mode=0o700)

    def entry(self, question='Which cache?', answer='Use the file cache.', now=1_700_000_000, **result):
        return advisor_log.make(question, dict({'status': 'decided', 'answer': answer}, **result), 'claude', 'fable', now=now)

    def jsonl(self):
        return [json.loads(text) for text in (self.folder / 'advisor-log.jsonl').read_text().splitlines()]

    def test_concurrent_asks_keep_every_entry_in_both_files(self):
        start = self.folder.parent / 'start'
        children = [writer(self.folder, start, f'w{number}', 25) for number in range(4)]
        start.touch()
        for child in children:
            self.assertEqual(child.wait(timeout=60), 0)
        questions = sorted(entry['question'] for entry in self.jsonl())
        self.assertEqual(questions, sorted(f'w{number} {index}' for number in range(4) for index in range(25)))
        self.assertEqual(len((self.folder / 'advisor-log.md').read_text().splitlines()), 100)
        self.assertEqual(len(advisor_log.listing(self.folder)), 100)

    def test_append_waits_while_another_writer_holds_the_lock(self):
        lock = os.open(self.folder / '.advisor-log.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = self.folder.parent / 'start'
            start.touch()
            child = writer(self.folder, start, 'late', 1)
            time.sleep(0.5)
            self.assertIsNone(child.poll())
            self.assertFalse((self.folder / 'advisor-log.jsonl').exists())
        finally:
            os.close(lock)
        self.assertEqual(child.wait(timeout=30), 0)
        self.assertEqual([entry['question'] for entry in self.jsonl()], ['late 0'])
        self.assertEqual((self.folder / '.advisor-log.lock').stat().st_mode & 0o777, 0o600)

    def test_bad_lines_are_skipped_and_good_entries_still_load(self):
        good = self.entry()
        bad = [dict(good, id='aaaaaaaaaaaa', time=10 ** 100), dict(good, id='bbbbbbbbbbbb', time=-10 ** 100),
               dict(good, id='cccccccccccc', time=True), dict(good, id='dddddddddddd', time=1.5)]
        lines = [json.dumps(item) for item in bad] + ['[' * 20000 + ']' * 20000, '{"a":' * 20000 + '1' + '}' * 20000,
                                                       '{"time": NaN}', json.dumps(good)]
        (self.folder / 'advisor-log.jsonl').write_text('\n'.join(lines) + '\n')
        self.assertEqual([row['id'] for row in advisor_log.listing(self.folder)], [good['id']])
        self.assertEqual(advisor_log.find(self.folder, good['id']), good)
        self.assertIsNone(advisor_log.find(self.folder, 'aaaaaaaaaaaa'))
        later = self.entry('Next?', now=1_700_000_100)
        advisor_log.append(self.folder, later)
        self.assertEqual([entry['id'] for entry in self.jsonl()], [good['id'], later['id']])

    def test_full_text_is_kept_up_to_what_the_cli_can_produce(self):
        # The longest fields the CLI makes: one reply per advisor (helper_tabs.MAX_REPLY), evidence from two replies.
        self.assertGreaterEqual(advisor_log.FULL, 2 * helper_tabs.MAX_REPLY + 2)
        reply = 'a' * helper_tabs.MAX_REPLY
        entry = self.entry('q' * 131072, reply, evidence='e' * helper_tabs.MAX_REPLY + '\n\n' + 'f' * helper_tabs.MAX_REPLY,
                           advisors=[{'role': 'main', 'agent': 'claude', 'model': 'fable', 'answer': reply, 'confidence': 'high'}])
        advisor_log.append(self.folder, entry)
        stored = advisor_log.find(self.folder, entry['id'])
        self.assertEqual((len(stored['question']), stored['answer'], stored['advisors'][0]['answer']), (131072, reply, reply))
        self.assertEqual(len(stored['evidence']), 2 * helper_tabs.MAX_REPLY + 2)
        self.assertEqual(stored['cut'], [])
        # The list stays short: previews only.
        row = advisor_log.listing(self.folder)[0]
        self.assertEqual((len(row['question']), len(row['answer'])), (advisor_log.PREVIEW_QUESTION, advisor_log.PREVIEW_ANSWER))

    def test_text_over_the_cap_is_cut_and_marked(self):
        entry = self.entry(answer='b' * (advisor_log.FULL + 10))
        self.assertEqual(len(entry['answer']), advisor_log.FULL)
        self.assertTrue(entry['answer'].endswith('…'))
        self.assertEqual(entry['cut'], ['answer'])


if __name__ == '__main__':
    unittest.main()
