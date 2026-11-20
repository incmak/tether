"""A resumed Claude session keeps writing to the folder where it began, not the one named after its working folder."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents
import paths

SESSION = '3c0c1e9c-b1f5-4a5f-8d24-a32911405d22'


class ClaudeTranscriptPath(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.projects = self.home / '.claude/projects'
        self.projects.mkdir(parents=True)
        for patcher in (patch.object(agents, 'HOME', self.home), patch.object(paths, 'CLAUDE', self.home / '.claude'),
                        patch.dict(agents._transcripts, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, project, session=SESSION):
        path = self.projects / project / (session + '.jsonl')
        path.parent.mkdir(exist_ok=True)
        path.write_text('{}\n')
        return path

    def test_file_in_the_working_folder_project_is_used(self):
        path = self.write('-srv-user-app')
        self.assertEqual(agents.claude_transcript('/srv/user/app', SESSION), path)

    def test_session_moved_from_the_mac_is_found_by_id(self):
        path = self.write('-Users-user-src-app')
        self.assertEqual(agents.claude_transcript('/srv/user/src/app/notes/x', SESSION), path)

    def test_session_resumed_in_a_subfolder_is_found_by_id(self):
        path = self.write('-srv-user-app')
        self.assertEqual(agents.claude_transcript('/srv/user/app/sub', SESSION), path)

    def test_new_session_without_a_file_uses_the_working_folder_project(self):
        self.write('-srv-user-other', session='11111111-2222-3333-4444-555555555555')
        self.assertEqual(agents.claude_transcript('/srv/user/app', SESSION),
                         self.projects / '-srv-user-app' / (SESSION + '.jsonl'))

    def test_moved_file_is_looked_up_again(self):
        first = self.write('-a')
        self.assertEqual(agents.claude_transcript('/b', SESSION), first)
        first.unlink()
        second = self.write('-c')
        self.assertEqual(agents.claude_transcript('/b', SESSION), second)


if __name__ == '__main__':
    unittest.main()
