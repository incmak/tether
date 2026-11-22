"""Friday draft lifecycle and HTTP tests with a controlled clock and a fake tmux."""
import unittest_isolated  # noqa: F401
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import runpy
import shlex
from subprocess import CalledProcessError, CompletedProcess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import friday_drafts as drafts
import friday_notes as notes
import paths
import restart_tab
import server
from test_friday_notes import NOW, draft, revision

THREAD = '11111111-1111-4111-8111-111111111111'
TARGET = '22222222-2222-4222-8222-222222222222'
REUSED = '33333333-3333-4333-8333-333333333333'
ROOT = Path(__file__).resolve().parents[1]
TRUST_SCREEN = (Path(__file__).parent / 'fixtures/claude_2.1.330_trust/home_folder.txt').read_text()
PREFIX_INSTRUCTION = 'Start the prompt with a word. Do not start it with / ! # @ ? or $.'


def agent(pane, thread, name, *, kind='claude', cwd='/project'):
    return dict(id=pane, pane_name=name, agent=kind, thread=thread, tmux_session='$3',
                pane_pid=int(pane[1:]) + 100, pane_start='start', label='Project', cwd=cwd, writable=True,
                codex_binding='hook', registry='/registry', process_start='start', owner_pid=901)


def reply(prompt='Write the checks.', *, skill=None, summary='Add checks.'):
    return '```tether-draft\n' + json.dumps(dict(skill=skill, prompt=prompt, summary=summary)) + '\n```'


class DraftTests(unittest.TestCase):
    def assertStartTimeout(self, error):
        # The generic start failure names the trust step and the checked Claude Code version (AGENTS.md rule 5).
        self.assertTrue(error.startswith(drafts.NO_START + ' If Claude asks to trust '), error)
        self.assertTrue(error.endswith(f'Tether checked this start on Claude Code {drafts.CHECKED_CLAUDE}.'), error)

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.folder = self.root / 'notes'
        self.path = self.folder / 'notes.json'
        self.enterContext(patch.dict(os.environ, {'TETHER_FRIDAY_NOTES': str(self.folder)}))
        self.enterContext(patch.object(paths, 'HOME', self.root / 'home'))
        self.enterContext(patch.object(paths, 'CLAUDE', paths.HOME / '.claude'))
        self.enterContext(patch.object(paths, 'CODEX', paths.HOME / '.codex'))
        self.claude_json = self.root / '.claude.json'
        self.enterContext(patch.object(paths, 'CLAUDE_JSON', self.claude_json))
        self.fallback = self.root / 'friday-drafts'
        self.enterContext(patch.dict(os.environ, {'TETHER_FRIDAY_DRAFTS': str(self.fallback)}))
        self.project = self.root / 'project'
        self.project.mkdir()
        self.target = agent('%42', TARGET, 'Work Chat', cwd=str(self.project))
        self.panes = {'%42': self.target}
        self.enterContext(patch.object(drafts.terminal_input, 'shell_prompt', side_effect=self.shell_prompt))
        self.enterContext(patch.object(drafts.helper_close.groups, 'closing', return_value=nullcontext()))
        self.enterContext(patch.object(drafts.helper_close.auto_resume, 'forget_pane'))
        self.enterContext(patch.object(drafts.helper_close, 'release_reviews'))
        self.kills, self.jobs, self.commands = [], [], []
        self.now, self.elapsed = NOW, 0
        self.launch_buffer = ''
        self.launch_pasted = False
        self.register = True
        self.screen = ''
        self.saved_reply = Mock(return_value=reply())
        self.reader = Mock(return_value=self.saved_reply)
        self.send = Mock()
        self.status = Mock(return_value={'state': 'Idle'})
        self.run = Mock(side_effect=self.tmux)
        self.runtime = drafts.Runtime(entries=lambda: list(self.panes.values()), folder=lambda entry: entry['cwd'],
                                      status=self.status, send=self.send, reply_reader=self.reader,
                                      lock=threading.Lock(), invalidate=Mock(), run=self.run,
                                      clock=lambda: self.now, monotonic=lambda: self.elapsed,
                                      wait=self.advance, spawn=self.jobs.append)
        self.enterContext(patch.object(server, 'friday_draft_runtime', return_value=self.runtime))
        self.enterContext(patch.object(drafts, '_last_sweep', float('-inf')))
        self.enterContext(patch.object(notes.time, 'time', side_effect=lambda: self.now))

    def advance(self, seconds):
        self.elapsed += seconds
        self.now = NOW + self.elapsed

    def shell_prompt(self, entry, run):
        return '$ ' + self.launch_buffer if self.launch_pasted else '$'

    def tmux(self, args, **kwargs):
        command = args[args.index(paths.TMUX) + 1:]
        self.commands.append(command)
        action = command[0]
        if action in ('new-session', 'new-window'):
            cwd = command[command.index('-c') + 1]
            self.panes['%12'] = dict(id='%12', agent='shell', cwd=cwd, tmux_session='$21', pane_pid=112,
                                     label='Friday chats', pane_start='shell-start')
            self.launch_pasted = False
            output = '%12\n'
        elif action == 'display-message':
            pane, fmt = command[command.index('-t') + 1], command[-1]
            if fmt == drafts.helper_close.CLOSE_FORMAT:
                entry = self.panes.get(pane)
                if entry is None:
                    return CompletedProcess(args, 1, '', '')
                output = f"900:1000\t{pane}\t{entry['tmux_session']}\t{entry['pane_pid']}\n"
            elif fmt.startswith('#{window_id}'):
                output = f"@30\t{self.panes[pane]['cwd']}\t\n"
            elif fmt.startswith('#{window-size}'):
                output = 'latest\t100\t30\n'
            elif fmt == '#{pane_id}':
                if pane not in self.panes:
                    return CompletedProcess(args, 0, '', f"can't find pane: {pane}\n")
                output = pane + '\n'
            else:
                self.fail(f'Unexpected format {fmt}')
        elif action == 'list-panes':
            output = ''.join(pane + '\n' for pane in self.panes)
        elif action == 'load-buffer':
            self.launch_buffer = kwargs['input'].decode()
            output = ''
        elif action == 'paste-buffer':
            self.launch_pasted = True
            output = ''
        elif action == 'send-keys':
            self.assertEqual(command, ['send-keys', '-t', '%12', 'Enter'])
            if self.register:
                argv = shlex.split(self.launch_buffer)
                thread = argv[argv.index('--session-id') + 1]
                entry = self.panes['%12']
                name = next(cmd[cmd.index('@chat_name') + 1] for cmd in reversed(self.commands) if '@chat_name' in cmd)
                self.panes['%12'] = dict(entry, **{key: value for key, value in agent(
                    '%12', thread, name, cwd=entry['cwd']).items() if key not in ('label', 'tmux_session', 'pane_start')})
            output = ''
        elif action == 'capture-pane':
            self.assertEqual(command[-2:], ['-t', '%12'])
            output = self.screen
        elif action == 'kill-pane':
            self.kills.append(command[-1])
            self.panes.pop(command[-1])
            output = ''
        elif action in ('set-option', 'delete-buffer'):
            output = ''
        else:
            self.fail(f'Unexpected tmux call {command}')
        return CompletedProcess(args, 0, output, '')

    def hold(self, title='Server', chat=' work  CHAT '):
        return notes.hold(title, ['Use the new port.', 'Keep <policy> and check startup.'], chat, now=self.now)['id']

    def trust(self, folder):
        self.claude_json.write_text(json.dumps({'projects': {str(folder): {'hasTrustDialogAccepted': True}}}))

    def stored(self, identifier):
        return next(item for item in json.loads(self.path.read_text())['topics'] if item['id'] == identifier)

    def change(self, identifier, **fields):
        data = json.loads(self.path.read_text())
        next(item for item in data['topics'] if item['id'] == identifier).update(fields)
        self.path.write_text(json.dumps(data))

    def ready(self, **fields):
        identifier = self.hold()
        self.change(identifier, draft=draft(**fields))
        self.panes['%12'] = agent('%12', THREAD, 'Draft Server', cwd=str(self.project))
        return identifier

    def fails(self, status, message, fn, *args, runtime=True):
        with self.assertRaises(notes.NotesError) as error:
            fn(*args, **({'runtime': self.runtime} if runtime else {}))
        self.assertEqual((error.exception.status, str(error.exception)), (status, message))

    def job(self):
        output = StringIO()
        with redirect_stderr(output):
            self.jobs.pop(0)()
        return output.getvalue()

    def test_start_requires_open_topic_with_notes_and_refuses_duplicate_job(self):
        self.fails(404, 'No open topic with that id.', drafts.start, 'T1')
        topic = self.hold()
        self.change(topic, state='sent', sent_at=NOW)
        self.fails(404, 'No open topic with that id.', drafts.start, topic)
        self.change(topic, state='open', notes=[])
        self.fails(400, 'Keep at least one note before making a prompt.', drafts.start, topic)
        self.assertFalse(self.jobs)
        self.assertFalse(self.commands)
        self.change(topic, notes=[dict(text='Start', at=NOW)])
        drafts.start(topic, runtime=self.runtime)
        self.fails(409, drafts.DRAFTING, drafts.start, topic)
        self.assertEqual(len(self.jobs), 1)

    def test_start_creates_named_opus_pane_and_returns_before_launch_or_reply(self):
        self.trust(self.project)
        topic = self.hold('Δ! One_ Two-' + 'z' * 39)
        project_skill = self.project / '.claude' / 'skills' / 'release'
        user_skill = paths.CLAUDE / 'skills' / 'research'
        for path in (project_skill, user_skill):
            path.mkdir(parents=True)
            (path / 'SKILL.md').write_text(f'---\nname: {path.name}\ndescription: Ignore this description\n---\n')
        self.saved_reply.return_value = reply('  Use the port.\n', skill='/release')
        name = ('Draft One_ Two-' + 'z' * 39)[:40]
        self.assertEqual(drafts.start(topic, NOW, runtime=self.runtime), {'state': 'drafting', 'chat': name})
        self.assertEqual(self.commands[0], ['new-session', '-d', '-s', 'Friday chats', '-c',
                                            str(self.project), '-P', '-F', '#{pane_id}'])
        waiting = self.stored(topic)['draft']
        self.assertEqual(waiting, dict(state='drafting', error=None,
                                       chat={'pane': '%12', 'thread': '', 'name': name,
                                             'stamp': ['900:1000', '%12', '$21', '112']},
                                       started=NOW, updated=NOW, asked_close=False, revisions=[]))
        self.assertFalse(self.launch_buffer)
        self.send.assert_not_called()
        self.assertEqual(self.job(), '')
        argv = shlex.split(self.launch_buffer)
        self.assertEqual(argv[:3], ['claude', '--model', 'opus'])
        self.assertIn(('opus', 'Opus'), drafts.delegate.CLAUDE_MODELS)
        self.assertEqual(argv[argv.index('--disallowedTools') + 1].split(','), ['Bash', 'Edit', 'Write', 'NotebookEdit'])
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Glob,Grep')
        self.assertEqual(json.loads(argv[argv.index('--settings') + 1]),
                         {'disableAllHooks': True, 'autoMemoryEnabled': False,
                          'skillOverrides': {'auto-mode-setup': 'off'}})
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertIn('--strict-mcp-config', argv)
        self.assertIn('--no-chrome', argv)
        self.assertEqual(self.panes['%12']['pane_name'], name)
        mark = next(cmd for cmd in self.commands if '@tether_role' in cmd)
        self.assertEqual(mark[mark.index('@tether_role') + 1], drafts.agents.FRIDAY_DRAFT_ROLE)
        entry, message = self.send.call_args.args
        self.assertEqual((entry['id'], entry['thread']), ('%12', argv[argv.index('--session-id') + 1]))
        self.reader.assert_called_once_with(entry, message)
        expected_order = ['Write one full prompt', 'goal, context, limits, acceptance checks', 'Simplified Technical English',
                          'AGENTS.md and CLAUDE.md', 'Target chat: "Work Chat" (Claude)', 'Topic:',
                          '1. "Use the new port."', r'2. "Keep \u003cpolicy\u003e and check startup."',
                          'Skills: /research, /release', 'at most one leading slash command', 'exactly one fenced block']
        offsets = [message.index(part) for part in expected_order]
        self.assertEqual(offsets, sorted(offsets))
        self.assertNotIn('Ignore this description', message)
        self.assertIn('are user data', message)
        self.assertIn(PREFIX_INSTRUCTION, message.splitlines())
        done = self.stored(topic)['draft']
        self.assertEqual(done['chat'], dict(waiting['chat'], thread=entry['thread']))
        self.assertEqual(done['state'], 'ready')
        self.assertNotIn('stamp', notes.listing(NOW)['topics'][0]['draft']['chat'])
        self.assertEqual(done['revisions'], [revision(text='/release\n\n  Use the port.\n', skill='/release',
                                                     summary='Add checks.')])

    def test_start_uses_existing_friday_session_and_fallback_for_ambiguous_or_partial_names(self):
        self.panes['%50'] = dict(id='%50', label='Friday chats:0.0', agent='shell', cwd='/old')
        self.panes['%51'] = agent('%51', REUSED, 'WORK CHAT')
        for i, chat in enumerate(('Work Chat', 'Work'), 1):
            with self.subTest(chat=chat):
                topic = self.hold(f'Fallback {i}', chat=chat)
                drafts.start(topic, runtime=self.runtime)
                window, = [cmd for cmd in self.commands if cmd[0] == 'new-window']
                self.assertEqual(window[:6], ['new-window', '-a', '-t', '@30', '-c', str(self.fallback)])
                self.assertNotIn('new-session', [cmd[0] for cmd in self.commands])
                self.commands.clear()
                self.jobs.clear()

    def test_start_replaces_previous_temp_chat_and_keeps_revisions(self):
        topic = self.ready(asked_close=True)
        old = self.stored(topic)['draft']['revisions']
        drafts.start(topic, runtime=self.runtime)
        self.assertEqual(self.kills, ['%12'])
        self.assertFalse(self.stored(topic)['draft']['asked_close'])
        self.job()
        self.assertEqual(self.stored(topic)['draft']['revisions'][:1], old)
        self.assertEqual(self.stored(topic)['draft']['revisions'][1]['rev'], 2)

    def test_start_uses_the_codex_skill_folder_with_a_cap(self):
        self.target.update(agent='codex')
        self.trust(self.project)
        for n in range(81):
            folder = paths.CODEX / 'skills' / f'skill-{n:02}'
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text('Skill')
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.assertEqual(self.panes['%12']['cwd'], str(self.project))
        self.job()
        _, message = self.send.call_args.args
        self.assertIn(' (Codex)', message)
        names = next(line for line in message.splitlines() if line.startswith('Skills: ')).removeprefix('Skills: ')
        self.assertEqual(names.split(', '), [f'/skill-{n:02}' for n in range(80)])

    def test_untrusted_target_uses_private_empty_fallback_but_keeps_target_skills(self):
        self.claude_json.write_text(json.dumps({'projects': {str(self.project): {'hasTrustDialogAccepted': 1}}}))
        skill = self.project / '.claude' / 'skills' / 'release'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('Skill')
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.assertEqual(self.panes['%12']['cwd'], str(self.fallback))
        self.assertEqual(self.fallback.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.fallback.stat().st_uid, os.geteuid())
        self.assertEqual(list(self.fallback.iterdir()), [])
        self.job()
        _, prompt = self.send.call_args.args
        self.assertIn('Target chat: "Work Chat" (Claude)', prompt)
        self.assertIn('Skills: /release', prompt.splitlines())

    def test_no_target_uses_friday_drafts_and_never_home(self):
        self.trust(paths.HOME)
        drafts.start(self.hold(chat=''), runtime=self.runtime)
        self.assertEqual(self.panes['%12']['cwd'], str(self.fallback))
        self.assertNotEqual(self.panes['%12']['cwd'], str(paths.HOME))

    def test_trusted_antigravity_target_still_uses_friday_drafts(self):
        self.target.update(agent='antigravity')
        self.trust(self.project)
        drafts.start(self.hold(), runtime=self.runtime)
        self.assertEqual(self.panes['%12']['cwd'], str(self.fallback))
        self.job()
        self.assertIn('Target chat: "Work Chat" (Antigravity)', self.send.call_args.args[1])

    def test_trusted_home_target_uses_friday_drafts(self):
        paths.HOME.mkdir()
        self.trust(paths.HOME)
        self.target['cwd'] = str(paths.HOME)
        drafts.start(self.hold(), runtime=self.runtime)
        self.assertEqual(self.panes['%12']['cwd'], str(self.fallback))

    def test_home_override_refuses_to_create_a_draft_pane(self):
        paths.HOME.mkdir()
        topic = self.hold(chat='')
        with patch.dict(os.environ, TETHER_FRIDAY_DRAFTS=str(paths.HOME)):
            drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertFalse(self.commands)
        self.send.assert_not_called()
        self.assertEqual(self.stored(topic)['draft']['error'], drafts.NO_START)

    def test_trust_inherits_from_an_ancestor_of_the_canonical_cwd(self):
        nested = self.project / 'packages' / 'app'
        nested.mkdir(parents=True)
        linked = self.root / 'linked'
        linked.symlink_to(nested, target_is_directory=True)
        self.target['cwd'] = str(linked)
        self.trust(self.project)
        drafts.start(self.hold(), runtime=self.runtime)
        self.assertEqual(self.panes['%12']['cwd'], str(nested))

    def test_bad_claude_json_is_untrusted(self):
        for index, payload in enumerate((b'{bad', b'[]', b'{"projects":[]}', b'\xff')):
            with self.subTest(payload=payload):
                self.claude_json.write_bytes(payload)
                drafts.start(self.hold(f'Bad settings {index}'), runtime=self.runtime)
                self.assertEqual(self.panes['%12']['cwd'], str(self.fallback))
                self.assertEqual(self.claude_json.read_bytes(), payload)

    def test_confirmed_trust_dialog_closes_by_stamp_without_sending_input(self):
        self.register = False
        self.fallback = paths.HOME / 'drafts'
        self.screen = TRUST_SCREEN.replace('/srv/user', str(self.fallback))
        with patch.dict(os.environ, TETHER_FRIDAY_DRAFTS=str(self.fallback)):
            topic = self.hold(chat='')
            before = self.stored(topic)['notes']
            drafts.start(topic, runtime=self.runtime)
        self.job()
        saved = self.stored(topic)
        self.assertEqual(saved['notes'], before)
        self.assertEqual(saved['draft']['state'], 'failed')
        self.assertEqual(saved['draft']['error'],
                         'Claude does not trust ~/drafts yet. Open Claude in ~/drafts once, choose Yes, '
                         'then ask Friday to make the prompt again.')
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(saved['draft']['chat']['stamp'], ['900:1000', '%12', '$21', '112'])
        self.assertTrue(saved['draft']['chat']['gone'])
        self.assertEqual(self.kills, ['%12'])
        self.send.assert_not_called()
        self.assertEqual([cmd for cmd in self.commands if cmd[0] == 'send-keys'],
                         [['send-keys', '-t', '%12', 'Enter']])  # Only the verified shell launch.

    def test_trust_dialog_wrapped_in_a_50_column_tab_is_recognized(self):
        # Captured live from Claude Code 2.1.330 in a 50-column draft window; the question wraps over 5 lines.
        screen = (Path(__file__).parent / 'fixtures/claude_2.1.330_trust/narrow_50.txt').read_text()
        self.assertTrue(drafts._is_trust_dialog(screen, '/srv/user/.tether/friday-drafts'))
        self.assertFalse(drafts._is_trust_dialog(screen, '/srv/user/.tether'))
        # A folder too long for the width wraps too; that never matches, so the start ends in the generic timeout.
        wrapped = screen.replace(' /srv/user/.tether/friday-drafts\n', ' /srv/user/.tether/fri\n day-drafts\n')
        self.assertFalse(drafts._is_trust_dialog(wrapped, '/srv/user/.tether/friday-drafts'))

    def test_partial_or_wrong_folder_trust_dialog_keeps_the_generic_timeout(self):
        self.register = False
        expected = TRUST_SCREEN.replace('/srv/user', str(self.fallback))
        for index, screen in enumerate((
                TRUST_SCREEN, expected.replace('Accessing workspace:', 'Another message'),
                expected.replace('Is this a project you created or one you trust?', 'Continue?'),
                expected.replace('Yes, I trust this folder', 'Yes'))):
            with self.subTest(screen=screen):
                self.screen = screen
                topic = self.hold(f'Other screen {index}', chat='')
                drafts.start(topic, runtime=self.runtime)
                before = self.elapsed
                self.job()
                self.assertEqual(self.elapsed - before, 60)
                self.assertStartTimeout(self.stored(topic)['draft']['error'])
        self.send.assert_not_called()

    def test_long_trust_error_remains_visible_with_the_original_notes(self):
        folder = self.root / ('workspace-' + 'x' * 120)
        self.screen = TRUST_SCREEN.replace('/srv/user', str(folder))
        topic = self.hold(chat='')
        with patch.dict(os.environ, TETHER_FRIDAY_DRAFTS=str(folder)):
            drafts.start(topic, runtime=self.runtime)
        self.job()
        saved, = notes.listing(self.now)['topics']
        error = saved['draft']['error']
        self.assertGreater(len(error), 300)
        self.assertEqual(error.count(str(folder)), 2)
        self.assertEqual(saved['notes'], self.stored(topic)['notes'])
        self.send.assert_not_called()

    def test_trust_dialog_does_not_close_a_pane_with_a_changed_stamp(self):
        self.register = False
        self.screen = TRUST_SCREEN.replace('/srv/user', str(self.fallback))
        topic = self.hold(chat='')
        drafts.start(topic, runtime=self.runtime)

        def change_after_capture(args, **kwargs):
            result = self.tmux(args, **kwargs)
            if args[1] == 'capture-pane':
                self.panes['%12']['pane_pid'] += 1
            return result

        self.run.side_effect = change_after_capture
        self.job()
        self.assertEqual(self.stored(topic)['draft']['state'], 'failed')
        self.assertEqual(self.kills, [])
        self.send.assert_not_called()

    def test_start_timeout_sets_failed_and_never_sends(self):
        self.register = False
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.assertIn(drafts.NO_START, self.job())
        self.assertEqual(self.elapsed, 60)
        self.send.assert_not_called()
        self.assertStartTimeout(self.stored(topic)['draft']['error'])
        self.assertEqual(self.kills, ['%12'])
        self.assertFalse(notes._open_chat(self.stored(topic)['draft']))

    def test_failed_start_can_be_closed_over_http_with_empty_thread(self):
        self.sign_in()
        self.register = False
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertEqual(self.request('POST', '/draft-close', {'id': topic, 'pane': '%12', 'thread': ''}),
                         (200, {'state': 'closed'}))
        self.assertEqual(self.kills, ['%12'])

    def test_failed_job_preserves_the_stamp_if_tmux_refuses_cleanup(self):
        self.register = False
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)

        def refuse_kill(args, **kwargs):
            if args[1] == 'kill-pane':
                raise CalledProcessError(1, args)
            return self.tmux(args, **kwargs)

        self.run.side_effect = refuse_kill
        self.assertIn('cleanup: CloseFailed', self.job())
        saved = self.stored(topic)['draft']
        self.assertEqual(saved['state'], 'failed')
        self.assertTrue(notes._open_chat(saved))
        self.run.side_effect = self.tmux
        self.assertEqual(drafts.close(topic, '%12', '', runtime=self.runtime), {'state': 'closed'})
        self.assertEqual(self.kills, ['%12'])

    def test_second_start_after_a_failure_has_only_the_new_pane(self):
        self.register = False
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertNotIn('%12', self.panes)
        self.register = True
        drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertEqual(self.kills, ['%12'])
        self.assertEqual(self.stored(topic)['draft']['state'], 'ready')

    def test_prompt_waits_for_idle_after_the_expected_conversation_registers(self):
        topic = self.hold()
        self.status.side_effect = [{'state': 'Working'}, {'state': 'Idle'}, {'state': 'Idle'}]
        drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertEqual(self.elapsed, .5)
        self.send.assert_called_once()
        self.assertEqual(self.stored(topic)['draft']['state'], 'ready')

    def test_unexpected_conversation_never_receives_the_prompt_or_other_keys(self):
        def register_wrong_thread(args, **kwargs):
            result = self.tmux(args, **kwargs)
            if args[1] == 'send-keys':
                self.panes['%12']['thread'] = REUSED
            return result

        self.run.side_effect = register_wrong_thread
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertStartTimeout(self.stored(topic)['draft']['error'])
        self.send.assert_not_called()
        self.assertEqual([cmd for cmd in self.commands if cmd[0] == 'send-keys'],
                         [['send-keys', '-t', '%12', 'Enter']])

    def test_start_refuses_if_the_shell_did_not_stay_in_the_selected_folder(self):
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.panes['%12']['cwd'] = str(paths.HOME)
        self.job()
        self.assertEqual(self.stored(topic)['draft']['error'], drafts.NO_START)
        self.assertNotIn('send-keys', [cmd[0] for cmd in self.commands])
        self.send.assert_not_called()

    def test_start_refuses_a_reused_shell_before_launch(self):
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.panes['%12']['pane_pid'] += 1
        self.assertIn(drafts.NO_START, self.job())
        self.assertNotIn('send-keys', [cmd[0] for cmd in self.commands])
        self.assertEqual(self.stored(topic)['draft']['state'], 'failed')
        self.assertFalse(notes._open_chat(self.stored(topic)['draft']))
        self.assertFalse(self.kills)

    def test_job_timeout_and_invalid_replies_have_spoken_errors(self):
        cases = [
            (None, drafts.NO_REPLY), ('ordinary answer', drafts.NO_BLOCK),
            ('```tether-draft\n{broken\n```', drafts.INVALID_REPLY),
            (reply('  /unknown\nDeploy.'), 'The draft started with a command character.'),
        ]
        for i, (answer, failure) in enumerate(cases):
            with self.subTest(failure=failure):
                self.saved_reply.return_value = answer
                topic = self.hold(f'Case {i}')
                before = self.elapsed
                drafts.start(topic, runtime=self.runtime)
                log = self.job()
                self.assertEqual(len(log.splitlines()), 1)
                self.assertIn(failure, log)
                saved = self.stored(topic)['draft']
                self.assertEqual((saved['state'], saved['error'], saved['revisions']), ('failed', failure, []))
                self.assertEqual(self.elapsed - before, 360 if answer is None else 0)
                self.assertEqual(self.kills, ['%12'] * (i + 1))

    def test_command_prefixes_are_refused_and_later_slashes_are_text(self):
        for prompt in ('!rm -rf x', '# memo', ' /x', '@file', '?help', '$skill'):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(notes.NotesError, 'command character'):
                drafts._parse(reply(prompt), [])
        prompt = 'Check these paths.\n/src/module.py'
        self.assertEqual(drafts._parse(reply(prompt), [])[0]['text'], prompt)
        self.assertEqual(drafts._parse(reply(prompt, skill='/research'), ['/research'])[0]['text'],
                         '/research\n\n' + prompt)

    def test_send_refuses_a_legacy_command_prefix(self):
        topic = self.ready(revisions=[revision(text='! echo unsafe')])
        self.fails(400, 'The draft started with a command character.', drafts.send, topic, 1, '%42', TARGET)
        self.send.assert_not_called()

    def test_start_provision_and_transcript_failures_do_not_escape_jobs(self):
        topic = self.hold()
        self.run.side_effect = OSError('tmux error')
        self.assertEqual(drafts.start(topic, runtime=self.runtime)['state'], 'drafting')
        self.assertIn(drafts.NO_START, self.job())
        self.assertEqual(self.stored(topic)['draft']['error'], drafts.NO_START)
        self.run.side_effect = self.tmux
        self.saved_reply.side_effect = ValueError('transcript error')
        drafts.start(topic, runtime=self.runtime)
        self.assertIn(drafts.NO_REPLY, self.job())
        self.assertEqual(self.stored(topic)['draft']['error'], drafts.NO_REPLY)

    def test_launch_error_cleans_up_the_created_shell(self):
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        with patch.object(drafts.terminal_input, 'start_agent', side_effect=OSError('launch error')):
            self.assertIn(drafts.NO_START, self.job())
        self.assertEqual(self.kills, ['%12'])
        self.assertFalse(notes._open_chat(self.stored(topic)['draft']))

    def test_window_placement_failure_cleans_up_only_by_creation_stamp(self):
        self.panes['%50'] = dict(id='%50', label='Friday chats:0.0', agent='shell', cwd='/old')
        for reused in (False, True):
            with self.subTest(reused=reused):
                def refuse_placement(args, **kwargs):
                    if args[1] == 'set-option' and drafts.helper_close.groups.BY in args:
                        if reused:
                            self.panes['%12']['pane_pid'] += 1
                        return CompletedProcess(args, 1, '', 'Placement failed.')
                    return self.tmux(args, **kwargs)

                self.run.side_effect = refuse_placement
                topic = self.hold(f'Place {reused}')
                drafts.start(topic, runtime=self.runtime)
                self.assertIn(drafts.NO_START, self.job())
                self.assertEqual(self.kills, ['%12'])
                self.assertEqual('%12' in self.panes, reused)
                self.assertFalse(notes._open_chat(self.stored(topic)['draft']))

    def test_unknown_skill_is_dropped_without_a_summary_claim(self):
        topic = self.hold()
        self.saved_reply.return_value = reply(skill='/unknown', summary='This will run the suggested skill.')
        drafts.start(topic, runtime=self.runtime)
        self.assertEqual(self.job().splitlines(), [f'Friday draft {topic}: skill_dropped: true'])
        self.assertEqual(self.stored(topic)['draft']['revisions'],
                         [revision(text='Write the checks.', summary='')])

    def test_parser_uses_last_block_and_enforces_the_contract(self):
        text, dropped = drafts._parse(reply('Earlier') + '\n````tether-draft\r\n'
                                     + '{"skill":"/research","summary":"Read it.","prompt":"  Later.  "}'
                                     + '\r\n````', ['/research'])
        self.assertEqual((text, dropped), (dict(text='/research\n\n  Later.  ', skill='/research', summary='Read it.'), False))
        invalid = [reply(3), reply(' '), reply('x' * 20001), reply('x' * 20000, skill='/research'),
                   reply(skill=3), reply(skill='research'), reply(summary=['Words']),
                   reply(summary='word ' * 41), reply(summary='s' * 401), reply('control\x1b'),
                   '```tether-draft\n["x"]\n```',
                   '```tether-draft\n{"skill":null,"summary":"","prompt":"P","extra":1}\n```',
                   '```tether-draft\n{"skill":null,"summary":"","prompt":"P","prompt":"Q"}\n```']
        for sample in invalid:
            with self.subTest(sample=sample[:100]), self.assertRaisesRegex(notes.NotesError, 'block was invalid'):
                drafts._parse(reply() + '\n' + sample, ['/research'])
        self.assertEqual(len(drafts._parse(reply('x' * 20000, summary='a ' * 40), [])[0]['text']), 20000)

    def test_edit_refuses_wrong_revision_missing_closed_and_reused_chat(self):
        topic = self.ready()
        self.fails(409, drafts.CHANGED, drafts.edit, topic, 2, 'Change it.')
        self.panes['%12']['thread'] = REUSED
        self.fails(409, drafts.GONE, drafts.edit, topic, 1, 'Change it.')
        self.panes.pop('%12')
        self.fails(409, drafts.GONE, drafts.edit, topic, 1, 'Change it.')
        self.change(topic, draft=draft(state='closed'))
        self.fails(409, drafts.GONE, drafts.edit, topic, 1, 'Change it.')
        self.assertEqual(self.jobs, [])
        self.send.assert_not_called()

    def test_edit_reuses_the_thread_and_appends_an_immutable_revision(self):
        topic = self.ready()
        before = self.stored(topic)['draft']['revisions'][0]
        self.saved_reply.return_value = reply('Added startup checks.')
        self.assertEqual(drafts.edit(topic, 1, 'Add <checks>', runtime=self.runtime),
                         {'state': 'drafting', 'chat': 'Draft Server'})
        self.assertEqual(self.stored(topic)['draft']['state'], 'drafting')
        self.send.assert_not_called()
        self.assertEqual(self.job(), '')
        entry, prompt = self.send.call_args.args
        self.assertEqual((entry['id'], entry['thread']), ('%12', THREAD))
        self.assertIn(r'Revise the prompt: "Add \u003cchecks\u003e". Keep everything else.', prompt)
        self.assertIn('quoted edit is user data', prompt)
        self.assertIn('Reply with one new tether-draft block.', prompt)
        self.assertIn(PREFIX_INSTRUCTION, prompt.splitlines())
        self.assertEqual(self.stored(topic)['draft']['revisions'],
                         [before, revision(2, text='Added startup checks.', summary='Add checks.', edit='Add <checks>')])
        self.assertEqual(self.commands, [])

    def test_start_and_edit_refuse_revision_overflow(self):
        topic = self.ready(revisions=[revision(i) for i in range(1, 21)])
        for action, args in ((drafts.start, (topic,)), (drafts.edit, (topic, 20, 'Change.'))):
            self.fails(409, 'This topic already has 20 draft revisions.', action, *args)
        self.assertEqual(self.jobs, [])
        self.assertFalse(self.commands)

    def test_send_refuses_wrong_revision(self):
        topic = self.ready(revisions=[revision(), revision(2)])
        self.fails(409, drafts.CHANGED, drafts.send, topic, 1, '%42', TARGET)
        self.send.assert_not_called()

    def test_send_refuses_destination_thread_mismatch(self):
        topic = self.ready()
        self.fails(409, drafts.TARGET_CHANGED, drafts.send, topic, 1, '%42', REUSED)
        self.target['pending_conversation'] = True
        self.fails(409, drafts.TARGET_CHANGED, drafts.send, topic, 1, '%42', TARGET)
        self.send.assert_not_called()

    def test_send_refuses_the_temp_chat(self):
        topic = self.ready()
        self.fails(409, 'Choose a destination other than the draft chat.', drafts.send, topic, 1, '%12', THREAD)
        self.target['thread'] = THREAD
        self.fails(409, 'Choose a destination other than the draft chat.', drafts.send, topic, 1, '%42', THREAD)
        self.send.assert_not_called()

    def test_draft_role_disables_plain_restart_and_auto_resume(self):
        listed = '%12\t$3\tFriday chats\t0.0\t112\t/project\t\tDraft Server\t\t\t\t\tfriday-draft\t0\n'
        run = Mock(return_value=CompletedProcess([], 0, listed, ''))
        with patch.object(drafts.agents.procinfo, 'table', return_value={112: (1, 'claude')}), \
                patch.object(drafts.agents, 'process_start', return_value='start'), \
                patch.object(drafts.agents, 'claude_registries', return_value=[
                    dict(pid=112, sessionId=THREAD, cwd='/project', registry='/registry', tmux='fake.%12')]):
            entry, = drafts.agents.discover({}, run, repair_zoom=False)
        self.assertTrue(entry['friday_draft'])
        self.assertNotIn('role', entry)
        self.assertFalse(server.auto_resume._eligible(entry))
        self.assertTrue(server.auto_resume._eligible(dict(entry, friday_draft=False)))
        with self.assertRaisesRegex(restart_tab.Refused, 'Ask Friday to make the prompt again.') as error:
            restart_tab.restart(dict(pane='%12', thread=THREAD, session='Friday chats', project=''),
                                sessions=lambda: [entry], forget_sessions=Mock(), run=self.run)
        self.assertEqual(error.exception.status, 409)
        self.run.assert_not_called()

    def test_send_delivers_stored_text_exactly_and_marks_topic_sent_once(self):
        text = '/research\n\n  Keep each line.\n\n'
        topic = self.ready(revisions=[revision(text=text, skill='/research')])
        before = self.stored(topic)['draft']
        self.assertEqual(drafts.send(topic, 1, '%42', TARGET, runtime=self.runtime),
                         dict(sent=True, chat='Work Chat', rev=1, length=len(text), ask_close=True))
        self.send.assert_called_once_with(self.target, text, user=True)
        saved = self.stored(topic)
        self.assertEqual((saved['state'], saved['sent_to'], saved['sent_at'], saved['draft']),
                         ('sent', 'Work Chat', NOW, before))
        self.fails(409, 'This topic was already sent.', drafts.send, topic, 1, '%42', TARGET)
        self.assertEqual(self.send.call_count, 1)

    def test_send_rereads_under_lock_when_a_concurrent_edit_starts(self):
        topic = self.ready()
        read_entries = self.runtime.entries
        racing = False

        def entries():
            nonlocal racing
            if not racing:
                racing = True
                drafts.edit(topic, 1, 'Change it.', runtime=replace(self.runtime, entries=read_entries))
            return read_entries()

        self.runtime = replace(self.runtime, entries=entries)
        self.fails(409, drafts.CHANGED, drafts.send, topic, 1, '%42', TARGET)
        self.assertEqual(self.stored(topic)['draft']['state'], 'drafting')
        self.send.assert_not_called()

    def test_send_rechecks_destination_and_surfaces_terminal_race(self):
        topic = self.ready()
        self.runtime = replace(self.runtime, entries=Mock(side_effect=[
            [self.target], [dict(self.target, thread=REUSED)],
            [self.target], [self.target],
        ]))
        self.fails(409, drafts.TARGET_CHANGED, drafts.send, topic, 1, '%42', TARGET)
        self.send.assert_not_called()
        self.send.side_effect = drafts.terminal_input.ConversationChanged('Restarted.')
        self.fails(409, drafts.TARGET_CHANGED, drafts.send, topic, 1, '%42', TARGET)
        self.assertEqual(self.stored(topic)['state'], 'open')

    def test_server_exact_send_reuses_terminal_submit_without_text_changes(self):
        for text in ('  Keep leading and trailing space.  \n', '/ls\n\n  Keep this.\n', '/research\n\nRead the docs.'):
            with self.subTest(text=text), patch.object(server, 'live_status', return_value={'state': 'Idle'}), \
                    patch.object(server, 'capture') as capture, patch.object(server.threading, 'Thread') as record, \
                    patch.object(server, 'terminal_submit') as submit, patch.object(server, 'task_user_delivery'), \
                    patch.object(server.continuations, 'prepare') as wrap, patch.object(server.shell_log, 'folder_note') as folder:
                entry = dict(self.target, agent='codex')
                server.send_text(entry, text, held=True, user=True, exact=True)
                submit.assert_called_once_with(entry, text)
                wrap.assert_not_called()
                folder.assert_not_called()
                capture.assert_not_called()
                record.assert_not_called()

    def test_close_only_matching_chat_and_is_idempotent(self):
        topic = self.ready()
        original = self.stored(topic)['draft']['chat']
        for pane, thread in (('%42', TARGET), ('%12', REUSED)):
            self.fails(409, drafts.GONE, drafts.close, topic, pane, thread)
        self.assertFalse(self.kills)
        for _ in range(2):
            self.assertEqual(drafts.close(topic, '%12', THREAD, runtime=self.runtime), {'state': 'closed'})
        self.assertEqual(self.kills, ['%12'])
        saved = self.stored(topic)['draft']
        self.assertEqual((saved['state'], saved['chat'], saved['revisions']), ('closed', original, [revision()]))

    def test_close_marks_a_reused_pane_gone(self):
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.job()
        thread = self.stored(topic)['draft']['chat']['thread']
        self.panes['%12'].update(thread=REUSED, pane_pid=999)
        self.assertEqual(drafts.close(topic, '%12', thread, runtime=self.runtime), {'state': 'closed'})
        self.assertFalse(self.kills)
        self.assertEqual(self.stored(topic)['draft']['state'], 'closed')

    def test_hand_closed_chat_closes_and_drops_without_killing(self):
        self.sign_in()
        for explicit_close, note in ((True, None), (False, None), (False, 1)):
            with self.subTest(explicit_close=explicit_close, note=note):
                topic = self.ready()
                self.change(topic, notes=[{'text': 'Last note.', 'at': NOW}])
                self.panes.pop('%12')
                if explicit_close:
                    self.assertEqual(self.request('POST', '/draft-close', {'id': topic, 'pane': '%12', 'thread': THREAD}),
                                     (200, {'state': 'closed'}))
                self.assertEqual(self.request('POST', '/drop', {'id': topic, 'note': note}),
                                 (200, {'dropped': 'topic' if note is None else 'note', 'topic': 'Server', 'left': 0}))
        self.assertFalse(self.kills)

    def test_live_chat_must_close_before_drop_or_replacement(self):
        self.sign_in()
        topic = self.ready()

        def refuse_kill(args, **kwargs):
            if args[1] == 'kill-pane':
                raise CalledProcessError(1, args)
            return self.tmux(args, **kwargs)

        self.run.side_effect = refuse_kill
        self.assertEqual(self.request('POST', '/drop', {'id': topic}),
                         (409, {'error': 'Close the draft chat first.'}))
        self.fails(503, drafts.CLOSE_FAILED, drafts.close, topic, '%12', THREAD)
        self.fails(503, drafts.CLOSE_FAILED, drafts.start, topic)
        self.assertEqual(self.stored(topic)['draft']['state'], 'ready')
        self.assertFalse(self.jobs)
        self.run.side_effect = self.tmux
        self.assertEqual(self.request('POST', '/drop', {'id': topic}),
                         (200, {'dropped': 'topic', 'topic': 'Server', 'left': 0}))
        self.assertEqual(self.kills, ['%12'])

    def test_serve_drop_closes_an_open_draft_chat(self):
        self.sign_in()
        topic = self.ready()
        status, result = self.request('POST', '/drop', {
            'id': topic, 'count': 2, 'texts': ['Use the new port.', 'Keep <policy> and check startup.']})
        self.assertEqual((status, result), (200, {'dropped': 'topic', 'topic': 'Server', 'left': 0}))
        self.assertEqual(self.kills, ['%12'])
        self.assertEqual(notes.listing(self.now)['topics'], [])

    def test_draft_send_returns_409_after_edit_and_suppresses_already_asked_question(self):
        self.sign_in()
        topic = self.ready(asked_close=True, revisions=[revision(), revision(2)])
        send = {'id': topic, 'rev': 1, 'pane': '%42', 'thread': TARGET}
        self.assertEqual(self.request('POST', '/draft-send', send), (409, {'error': drafts.CHANGED}))
        self.send.assert_not_called()
        status, result = self.request('POST', '/draft-send', dict(send, rev=2))
        self.assertEqual(status, 200)
        self.assertIs(result['ask_close'], False)

    def test_close_uses_creation_stamp_before_the_thread_registers(self):
        self.sign_in()
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.assertEqual(self.request('POST', '/draft-close', {'id': topic, 'pane': '%12', 'thread': ''}),
                         (200, {'state': 'closed'}))
        self.assertEqual(self.kills, ['%12'])

    def test_close_uses_the_creation_stamp_after_the_thread_disappears(self):
        topic = self.hold()
        drafts.start(topic, runtime=self.runtime)
        self.job()
        pane = self.panes['%12']
        old_thread = pane.pop('thread')
        pane['agent'] = 'shell'
        self.assertEqual(drafts.close(topic, '%12', old_thread, runtime=self.runtime), {'state': 'closed'})
        self.assertEqual(self.kills, ['%12'])

    def test_stale_job_sweep_only_closes_by_creation_stamp(self):
        for same_stamp in (True, False):
            with self.subTest(same_stamp=same_stamp):
                topic = self.hold(f'Stale {same_stamp}')
                drafts.start(topic, runtime=self.runtime)
                if not same_stamp:
                    stamp = self.stored(topic)['draft']['chat']['stamp']
                    self.panes['%12'] = dict(agent('%12', THREAD, 'Other'),
                                             tmux_session=stamp[2], pane_pid=int(stamp[3]) + 1)
                    saved = self.stored(topic)['draft']
                    self.change(topic, draft=dict(saved, chat=dict(saved['chat'], thread=THREAD)))
                self.advance(notes.DRAFT_STALE + 1)
                drafts.sweep(runtime=self.runtime)
                self.assertEqual(self.kills, ['%12'])
                self.assertEqual(self.stored(topic)['draft']['state'], 'closed')
                self.assertEqual(self.stored(topic)['draft']['error'],
                                 notes.DRAFT_STOPPED if same_stamp else 'The draft chat was already gone.')

    def test_close_checks_tmux_stamp_before_preparing_to_kill(self):
        topic = self.ready()
        registry = self.runtime.entries()
        self.runtime = replace(self.runtime, entries=lambda: registry)
        self.panes['%12'] = dict(self.panes['%12'], pane_pid=999)
        self.assertEqual(drafts.close(topic, '%12', THREAD, runtime=self.runtime), {'state': 'closed'})
        drafts.helper_close.groups.closing.assert_not_called()
        self.assertFalse(self.kills)

    def test_close_revalidates_after_preparing_to_kill(self):
        topic = self.ready()
        original = self.runtime.entries()
        changed = [dict(entry, pane_pid=999) if entry['id'] == '%12' else entry for entry in original]
        self.runtime = replace(self.runtime, entries=Mock(side_effect=[original, changed]))
        self.assertEqual(drafts.close(topic, '%12', THREAD, runtime=self.runtime), {'state': 'closed'})
        drafts.helper_close.groups.closing.assert_called_once()
        self.assertFalse(self.kills)

    def test_failed_edit_keeps_bound_chat_open_and_explains_the_next_edit(self):
        topic = self.ready()
        self.saved_reply.return_value = 'No draft block.'
        drafts.edit(topic, 1, 'Change.', runtime=self.runtime)
        self.job()
        saved = self.stored(topic)['draft']
        self.assertEqual(saved['state'], 'failed')
        self.assertTrue(notes._open_chat(saved))
        self.fails(409, 'Ask Friday to make the prompt again.', drafts.edit, topic, 1, 'Change.')
        self.saved_reply.return_value = reply()
        drafts.start(topic, runtime=self.runtime)
        self.job()
        self.assertEqual(self.stored(topic)['draft']['state'], 'ready')
        self.assertEqual(self.kills, ['%12'])

    def test_failed_edit_marks_a_chat_that_disappeared_gone(self):
        topic = self.ready()
        drafts.edit(topic, 1, 'Change.', runtime=self.runtime)
        self.panes.pop('%12')
        self.job()
        saved = self.stored(topic)['draft']
        self.assertEqual(saved['state'], 'failed')
        self.assertFalse(notes._open_chat(saved))
        self.assertFalse(self.kills)
        self.fails(409, 'Ask Friday to make the prompt again.', drafts.edit, topic, 1, 'Change.')

    def run_live_script(self, *args):
        self.folder.mkdir()
        output = StringIO()
        runtime = replace(self.runtime, spawn=lambda job: job())
        script = runpy.run_path(str(ROOT / 'scripts/friday_draft_live.py'))
        with (patch.object(server, 'friday_draft_runtime', return_value=runtime),
              patch.object(Path, 'cwd', return_value=ROOT), patch.object(sys, 'path', list(sys.path)),
              redirect_stdout(output), redirect_stderr(StringIO())):
            result = script['main'](args)
        return result, output.getvalue()

    def test_live_script_uses_named_target_but_sends_only_to_the_draft_pane(self):
        self.trust(self.project)
        result, output = self.run_live_script('--chat', 'Work Chat')
        self.assertEqual(result, 0, output)
        self.assertIn(f'Launch folder: {self.project}', output)
        self.assertEqual(self.stored('T1')['chat'], 'Work Chat')
        self.assertEqual([call.args[0]['id'] for call in self.send.call_args_list], ['%12', '%12'])
        self.assertIn('PASS edit rev 2', output)
        self.assertIn('PASS verify (%12 is gone)', output)

    def test_live_script_accepts_success_without_a_target(self):
        self.trust(self.fallback)
        result, output = self.run_live_script()
        self.assertEqual(result, 0, output)
        self.assertIn(f'Launch folder: {self.fallback}', output)
        self.assertIn('PASS edit rev 2', output)
        self.assertNotIn('PASS trust-refusal', output)

    def test_live_script_accepts_a_trust_refusal_only_after_verifying_removal(self):
        self.register = False
        self.screen = TRUST_SCREEN.replace('/srv/user', str(self.fallback))
        result, output = self.run_live_script()
        self.assertEqual(result, 0, output)
        self.assertIn(f'Launch folder: {self.fallback}', output)
        self.assertIn('PASS verify (%12 is gone)\nPASS trust-refusal', output)
        self.assertEqual(self.kills, ['%12'])
        self.send.assert_not_called()

    def test_live_script_rejects_trust_refusal_with_a_named_target(self):
        self.trust(self.project)
        self.register = False
        self.screen = TRUST_SCREEN.replace('/srv/user', str(self.project))
        result, output = self.run_live_script('--chat', 'Work Chat')
        self.assertEqual(result, 1, output)
        self.assertIn(f'FAIL start (Claude does not trust {self.project} yet.', output)
        self.assertIn('PASS verify (%12 is gone)', output)
        self.assertNotIn('PASS trust-refusal', output)
        self.send.assert_not_called()

    def test_live_script_verify_reports_returncode_and_stderr_for_an_unknown_result(self):
        verify = runpy.run_path(str(ROOT / 'scripts/friday_draft_live.py'))['verify_gone']
        for code, stdout, stderr in ((0, '%11\n%12\n', ''), (1, '', 'permission denied\n')):
            with self.subTest(stderr=stderr):
                run = Mock(return_value=CompletedProcess([], code, stdout, stderr))
                with self.assertRaises(RuntimeError) as error:
                    verify('%12', paths.TMUX, run)
                self.assertIn(f'returncode={code}, stderr={stderr!r}', str(error.exception))
        for code, stdout, stderr in ((0, '%11\n%120\n', ''), (1, '', 'no server running on /tmp/x\n')):
            with self.subTest(gone=stdout or stderr), redirect_stdout(StringIO()) as printed:
                verify('%12', paths.TMUX, Mock(return_value=CompletedProcess([], code, stdout, stderr)))
                self.assertIn('PASS verify (%12 is gone)', printed.getvalue())

    def test_close_during_an_edit_stops_the_job_from_overwriting_closed_state(self):
        topic = self.ready()
        drafts.edit(topic, 1, 'Change.', runtime=self.runtime)
        drafts.close(topic, '%12', THREAD, runtime=self.runtime)
        self.job()
        self.send.assert_not_called()
        self.assertEqual(self.stored(topic)['draft']['state'], 'closed')

    def test_sweep_closes_old_keeps_younger_and_handles_a_gone_chat(self):
        old = self.ready(updated=NOW - drafts.CLOSE_AGE - 1)
        young = self.hold('Young')
        gone = self.hold('Gone')
        self.change(young, draft=draft(updated=NOW - drafts.CLOSE_AGE,
                                       chat={'pane': '%13', 'thread': TARGET, 'name': 'Draft Young'}))
        self.panes['%13'] = agent('%13', TARGET, 'Draft Young')
        self.change(gone, draft=draft(updated=NOW - drafts.CLOSE_AGE - 2,
                                      chat={'pane': '%14', 'thread': TARGET, 'name': 'Draft Gone'}))
        self.panes['%14'] = agent('%14', REUSED, 'Other conversation')
        drafts.sweep(runtime=self.runtime)
        self.assertEqual(self.kills, ['%12'])
        self.assertEqual(self.stored(old)['draft']['state'], 'closed')
        self.assertEqual(self.stored(young)['draft']['state'], 'ready')
        self.assertEqual((self.stored(gone)['draft']['state'], self.stored(gone)['draft']['error']),
                         ('closed', 'The draft chat was already gone.'))

    def test_sweep_can_close_a_chat_after_its_sent_topic_leaves_the_list(self):
        topic = self.ready(updated=NOW - drafts.CLOSE_AGE + 1)
        self.change(topic, state='sent', sent_at=NOW - notes.SENT_AGE - 1)
        self.assertEqual(notes.listing(NOW), {'topics': []})
        notes.hold('Other', ['Note.'])
        drafts.sweep(runtime=self.runtime)
        self.assertFalse(self.kills)
        self.advance(2)
        drafts.sweep(runtime=self.runtime)
        self.assertEqual(self.kills, ['%12'])

    def test_listing_starts_a_sweep_at_most_once_per_hour_without_running_it_inline(self):
        old = self.ready(updated=NOW - drafts.CLOSE_AGE - 1)
        self.sign_in()
        self.assertEqual(self.request('GET', '')[0], 200)
        self.assertEqual(self.stored(old)['draft']['state'], 'ready')
        self.assertEqual(self.request('GET', '')[0], 200)
        self.advance(3599)
        self.assertEqual(self.request('GET', '')[0], 200)
        self.assertEqual(len(self.jobs), 1)
        self.job()
        self.assertEqual(self.kills, ['%12'])
        self.advance(1)
        self.assertEqual(self.request('GET', '')[0], 200)
        self.assertEqual(len(self.jobs), 1)

    def sign_in(self):
        self.enterContext(patch.object(server.auth, 'MODE', 'token'))
        self.enterContext(patch.object(server.auth, 'token', return_value='draft-test-secret'))

    def request(self, method, suffix, payload=None, *, signed_in=True, origin='https://phone.test',
                write_header=True, peer='127.0.0.1', extra_headers=None):
        handler = object.__new__(server.Handler)
        handler.client_address = (peer, 1)
        handler.log_request = Mock()
        body = json.dumps(payload).encode() if payload is not None else b''
        headers = [f'{method} {notes.PREFIX}{suffix} HTTP/1.1', 'Host: phone.test', f'Content-Length: {len(body)}']
        if origin is not None:
            headers.append(f'Origin: {origin}')
        headers.extend(f'{key}: {value}' for key, value in (extra_headers or {}).items())
        if signed_in:
            headers.append(f'Cookie: {server.auth.COOKIE}={server.auth.session_value()}')
        if write_header:
            headers.append('X-Codex-Mobile: 1')
        handler.rfile = BytesIO('\r\n'.join(headers + ['', '']).encode() + body)
        handler.wfile = BytesIO()
        handler.handle_one_request()
        head, payload = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return int(head.split()[1]), json.loads(payload)

    def test_all_draft_routes_validate_types_and_preserve_auth(self):
        self.sign_in()
        cases = {
            'draft': ({'id': 'T1'}, {'id': [False]}),
            'draft-edit': ({'id': 'T1', 'rev': 1, 'edit': 'Edit.'}, {'id': 1, 'rev': True, 'edit': []}),
            'draft-send': ({'id': 'T1', 'rev': 1, 'pane': '%42', 'thread': TARGET},
                           {'id': None, 'rev': 1.0, 'pane': 42, 'thread': []}),
            'draft-close': ({'id': 'T1', 'pane': '%12', 'thread': THREAD},
                            {'id': 'first', 'pane': '12', 'thread': True}),
            'draft-asked': ({'id': 'T1'}, {'id': 1}),
        }
        for action, (good, bad) in cases.items():
            path = '/' + action
            for key, value in bad.items():
                with self.subTest(action=action, key=key):
                    self.assertEqual(self.request('POST', path, dict(good, **{key: value}))[0], 400)
                    self.assertEqual(self.request('POST', path, {k: v for k, v in good.items() if k != key})[0], 400)
            for options, status in (({'write_header': False}, 403), ({'origin': 'https://other.test'}, 403),
                                    ({'signed_in': False}, 401)):
                self.assertEqual(self.request('POST', path, good, **options)[0], status)
            self.assertEqual(self.request('POST', path, good)[0], 404)
        for instruction in ('', ' ', 'x' * 1001):
            self.assertEqual(self.request('POST', '/draft-edit', {'id': 'T1', 'rev': 1, 'edit': instruction})[0], 400)
        self.assertEqual(self.request('GET', '/target?chat=Work+Chat', signed_in=False)[0], 401)
        self.assertEqual(self.request('GET', '/target')[0], 400)
        self.assertEqual(self.request('GET', '/draft')[0], 404)
        self.assertFalse(self.commands)

    def test_http_draft_lifecycle_and_asked_close_flag(self):
        self.sign_in()
        topic = self.hold()
        self.assertEqual(self.request('POST', '/draft', {'id': topic}),
                         (200, {'state': 'drafting', 'chat': 'Draft Server'}))
        self.assertEqual(self.request('POST', '/draft', {'id': topic}), (409, {'error': drafts.DRAFTING}))
        self.job()
        temp = self.stored(topic)['draft']['chat']
        self.assertEqual(self.request('POST', '/draft-edit', {'id': topic, 'rev': 1, 'edit': 'Edit.'}),
                         (200, {'state': 'drafting', 'chat': 'Draft Server'}))
        self.job()
        self.assertEqual(self.request('POST', '/draft-send', {'id': topic, 'rev': 2, 'pane': '%42', 'thread': TARGET}),
                         (200, {'sent': True, 'chat': 'Work Chat', 'rev': 2, 'length': len('Write the checks.'),
                                'ask_close': True}))
        for expected in (True, False):
            self.assertEqual(self.request('POST', '/draft-asked', {'id': topic}),
                             (200, {'asked_close': True, 'ask_close': expected}))
        self.assertTrue(self.stored(topic)['draft']['asked_close'])
        self.assertEqual(self.request('GET', '?topic=Server&scope=open')[0], 404)
        status, result = self.request('GET', '?topic=the+server+topic&scope=draft')
        self.assertEqual(status, 200)
        self.assertEqual((result['topics'][0]['id'], result['topics'][0]['state']), (topic, 'sent'))
        self.assertEqual(self.request('POST', '/draft-close', {'id': topic, 'pane': temp['pane'], 'thread': temp['thread']}),
                         (200, {'state': 'closed'}))
        another = self.hold('Server two')
        self.change(another, draft=draft(chat={'pane': '%13', 'thread': REUSED, 'name': 'Draft Server two'}))
        self.assertEqual(self.request('GET', '?topic=Server&scope=draft')[1]['topics'][0]['draft']['state'], 'closed')
        self.assertEqual(self.kills, [temp['pane']])

    def friday_target(self, chat='Work Chat', *, token='test-friday', **kwargs):
        headers = kwargs.pop('extra_headers', {})
        if token is not None:
            headers[server.command_history.FRIDAY_HEADER] = token
        options = {'signed_in': False, 'origin': None, **kwargs}
        with patch.object(server.command_history, 'friday_token', return_value='test-friday'):
            return self.request('GET', '/friday-target?' + urlencode({'chat': chat}), extra_headers=headers, **options)

    def test_friday_target_requires_loopback_peer(self):
        self.assertEqual(self.friday_target(peer='192.0.2.7'), (403, {'error': 'Friday access refused.'}))

    def test_friday_target_checks_token(self):
        for token in ('wrong', None):
            with self.subTest(token=token):
                self.assertEqual(self.friday_target(token=token), (403, {'error': 'Friday access refused.'}))

    def test_friday_target_refuses_browser_and_forwarding_headers(self):
        self.sign_in()
        for header in ('Origin', 'Cookie', 'X-Forwarded-For', 'Forwarded'):
            with self.subTest(header=header):
                self.assertEqual(self.friday_target(extra_headers={header: ''}),
                                 (403, {'error': 'Friday access refused.'}))
        self.assertEqual(self.friday_target(token=None, signed_in=True), (403, {'error': 'Friday access refused.'}))
        for mode in ('token', 'proxy'):
            with self.subTest(mode=mode), patch.object(server.auth, 'MODE', mode):
                self.assertEqual(self.request('GET', '/target?chat=Work+Chat', signed_in=False, origin=None,
                                              extra_headers={server.command_history.FRIDAY_HEADER: 'test-friday'})[0], 403)
        self.assertFalse(notes.handles(notes.FRIDAY_TARGET + '?chat=Work+Chat'))

    def test_friday_target_resolves_exactly_one_live_chat(self):
        self.assertEqual(self.friday_target(' Work  CHAT '),
                         (200, {'pane': '%42', 'thread': TARGET, 'name': 'Work Chat', 'agent': 'claude'}))
        self.assertEqual(self.friday_target('Work'), (404, {'error': 'No chat called Work.'}))
        self.panes['%41'] = agent('%41', REUSED, 'WORK CHAT', kind='codex')
        self.assertEqual(self.friday_target(),
                         (409, {'error': 'Several chats are called Work Chat: Work Chat, WORK CHAT. Which one?'}))
        self.assertEqual(self.jobs, [])

    def test_target_resolve_matches_only_the_exact_visible_name(self):
        self.sign_in()

        def resolve(name):
            return self.request('GET', '/target?' + urlencode({'chat': name}))

        self.assertEqual(resolve('  woRK \t CHat '), (200, {'pane': '%42', 'thread': TARGET,
                                                            'name': 'Work Chat', 'agent': 'claude'}))
        for name in ('Work', 'Work-Chat', 'Work Chats', 'Missing'):
            self.assertEqual(resolve(name), (404, {'error': f'No chat called {name}.'}))
        self.panes['%41'] = agent('%41', REUSED, 'WORK  CHAT', kind='codex')
        self.assertEqual(resolve('Work Chat'),
                         (409, {'error': 'Several chats are called Work Chat: Work Chat, WORK  CHAT. Which one?'}))
        self.panes.pop('%41')
        self.target.update(pending_conversation=True)
        self.assertEqual(resolve('Work Chat'), (404, {'error': 'No chat called Work Chat.'}))
        self.assertEqual(self.jobs, [])


class TranscriptTests(unittest.TestCase):
    def test_reply_reader_uses_the_matching_completed_turn_after_the_baseline(self):
        folder = self.enterContext(TemporaryDirectory())
        path = Path(folder) / 'session.jsonl'

        def append(*records):
            with path.open('a') as stream:
                for record in records:
                    stream.write(json.dumps(record) + '\n')

        def user(text):
            return {'type': 'user', 'message': {'role': 'user', 'content': text}}

        def assistant(text, stop='end_turn', **fields):
            return {'type': 'assistant', 'message': {'role': 'assistant', 'stop_reason': stop,
                    'content': [{'type': 'text', 'text': text}]}, **fields}

        end = {'type': 'system', 'subtype': 'turn_duration'}
        prompt = 'Revise the prompt: "Add startup checks."'
        append(user(prompt), assistant('old reply'), end)
        read = server.draft_reply_reader({'transcript': str(path)}, prompt)
        self.assertIsNone(read())
        append(user('Someone else'), assistant('their reply'), end)
        self.assertIsNone(read())
        append(user(prompt), assistant('tool progress', 'tool_use'), assistant('sidechain', isSidechain=True),
               assistant('new reply'))
        self.assertIsNone(read())
        append(end, user('Another prompt'), assistant('another reply'), end)
        self.assertEqual(read(), 'new reply')

    def test_reply_reader_ignores_a_final_answer_rejected_by_a_stop_hook(self):
        folder = self.enterContext(TemporaryDirectory())
        path = Path(folder) / 'session.jsonl'
        read = server.draft_reply_reader({'transcript': str(path)}, 'Prompt')
        records = [{'type': 'user', 'message': {'role': 'user', 'content': 'Prompt'}},
                   {'type': 'assistant', 'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': 'Bad'}},
                   {'type': 'attachment', 'attachment': {'type': 'hook_blocking_error'}},
                   {'type': 'assistant', 'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': 'Good'}},
                   {'type': 'system', 'subtype': 'turn_duration'}]
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        self.assertEqual(read(), 'Good')
