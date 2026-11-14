"""Lesson checks use temporary files and mocked models, never live agents or tmux."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import advisor
import autonomy
import decisions
import lessons
import lessons_check as check
import paths

PID = 'abcdef012345'
LABEL = 'Claude, Fable 5.2 (latest), high effort'


class LessonsCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for key in ('CONFIG', 'TETHER', 'HOME', 'CACHE', 'CLAUDE', 'CODEX'):
            self.enterContext(patch.object(paths, key, self.root / key.lower()))
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        self.enterContext(patch.object(check, '_running', False))
        self.main = dict(agent='claude', model='latest', effort='high')
        self.settings = self.enterContext(patch.object(advisor, 'read', return_value={'main': self.main}))
        self.resolve = self.enterContext(patch.object(advisor, 'resolve', return_value='claude-fable-5-2'))
        self.label = self.enterContext(patch.object(autonomy, 'advisor_label', return_value=LABEL))
        self.which = self.enterContext(patch.object(check.shutil, 'which', side_effect=lambda name: '/mock/' + name))
        self.run = self.enterContext(patch.object(check.subprocess, 'run'))
        self.thread = self.enterContext(patch.object(check.threading, 'Thread'))

    def add(self, text='When checks fail, read the output.', **options):
        return lessons.add(PID, text, options.get('kind', 'avoid'), 'user', 'codex', force_new=True,
                           quote=options.get('quote', ''), pane=options.get('pane', ''))['lesson']

    def entry(self, row):
        return dict(id=row['id'], verdict='keep', confidence='high', reason='The notes support this lesson.',
                    text='')

    def advice(self, row):
        return check._parse(json.dumps({'lessons': [self.entry(row)]}), [row])

    def save(self, row=None, **changes):
        data = dict(version=1, state='done', started=10.0, finished=20.0, agent='claude', label=LABEL,
                    count=1, skipped=0, error='', advice=self.advice(row) if row else {})
        data.update(changes)
        lessons._write(check.path(), data)
        return data

    def run_job(self, reply):
        self.run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'result': reply}))
        target = self.thread.call_args.kwargs
        target['target'](*target['args'])
        return check.status()

    def test_idle_invalid_files_and_private_atomic_state(self):
        self.assertEqual(check.path(), paths.CONFIG / 'lessons-check.json')
        self.assertEqual(check.status(), {'state': 'idle'})
        paths.CONFIG.mkdir()
        for raw in ('bad json', '[]', '{}', '{"version": 1}', '\udcff'):
            check.path().write_bytes(raw.encode('utf-8', errors='surrogateescape'))
            self.assertEqual(check.status(), {'state': 'idle'})
        row = self.add()
        valid = self.save(row)
        for changes in ({'version': True}, {'state': 'oops'}, {'started': float('nan')}, {'count': True},
                        {'finished': 'bad'}, {'advice': []}, {'advice': {'bad': {}}}):
            lessons._write(check.path(), dict(valid, **changes))
            self.assertEqual(check.status(), {'state': 'idle'})
        self.save(row)
        self.assertEqual(check.path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(check.status()), {'state', 'started', 'finished', 'label', 'count',
                                               'skipped', 'error', 'advice'})

    def test_restart_marks_failed_and_saves_it_once(self):
        self.save(state='running', finished=None)
        result = check.status()
        self.assertEqual((result['state'], result['error']), ('failed', check.RESTARTED))
        self.assertIsInstance(result['finished'], float)
        self.assertEqual(json.loads(check.path().read_text())['state'], 'failed')
        self.assertEqual(check.status(), result)

    def test_status_filters_removed_and_edited_lessons_and_read_failure_keeps_advice(self):
        rows = [self.add(f'When case {index} occurs, read the notes.') for index in range(3)]
        advice = {key: value for row in rows for key, value in self.advice(row).items()}
        self.save(advice=advice)
        lessons.remove(PID, rows[0]['id'])
        lessons.edit(PID, rows[1]['id'], 'When checks fail, read every error first.')
        self.assertEqual(check.status()['advice'], self.advice(rows[2]))
        with patch.object(lessons, 'all_entries', side_effect=OSError('bad lesson file')):
            self.assertEqual(check.status()['advice'], advice)
        self.assertEqual(json.loads(check.path().read_text())['advice'], advice)

    def test_start_refuses_disabled_empty_and_running(self):
        self.settings.return_value = {'main': advisor.OFF}
        with self.assertRaisesRegex(ValueError, 'Turn on the advisor in Settings › Agents first.'):
            check.start()
        self.settings.return_value = {'main': self.main}
        with self.assertRaisesRegex(ValueError, 'There are no lessons to check.'):
            check.start()
        self.add()
        result = check.start()
        self.assertEqual((result['state'], result['label'], result['count']), ('running', LABEL, 1))
        self.thread.assert_called_once()
        self.assertTrue(self.thread.call_args.kwargs['daemon'])
        with self.assertRaisesRegex(ValueError, check.BUSY):
            check.start()
        self.run.assert_not_called()
        self.label.assert_called_once_with(self.main)

    def test_simultaneous_starts_only_create_one_job(self):
        self.add()
        barrier, results = Barrier(2), []
        def start():
            barrier.wait(timeout=5)
            try:
                results.append(check.start()['state'])
            except ValueError as error:
                results.append(str(error))
        threads = [Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ['running', check.BUSY])
        self.thread.assert_called_once()

    def test_review_order_cap_and_skipped_count(self):
        rows = [self.add(f'When case {index} occurs, read the notes.') for index in range(60)]
        global_row = rows[-1]
        lessons.keep(PID, global_row['id'])
        lessons.promote(PID, global_row['id'])
        rows.append(self.add('When the extra check fails, read the notes.'))
        lessons.dispute(PID, rows[35]['id'], '%1', 'The notes differ.')
        lessons.reinforce(PID, rows[20]['id'])
        lessons.reinforce(PID, rows[20]['id'])
        expected = [row for group in lessons.review()['groups'] for row in group['lessons']]
        result = check.start()
        self.assertEqual((result['count'], result['skipped']), (60, 1))
        checked = self.thread.call_args.kwargs['args'][1]
        self.assertEqual([row['id'] for row in checked], [row['id'] for row in expected[:60]])
        self.assertEqual([row['id'] for row in checked[:2]], [rows[35]['id'], rows[20]['id']])

    def test_previous_advice_stays_until_success_and_lessons_do_not_change(self):
        row = self.add()
        other = self.add('When another check fails, read the notes.')
        before = lessons.path(PID).read_bytes()
        old = dict(self.advice(row), **self.advice(other))
        self.save(advice=old)
        self.assertEqual(check.start()['advice'], old)
        entry = self.entry(row)
        entry['verdict'] = 'remove'
        result = self.run_job(json.dumps({'lessons': [entry]}))
        self.assertEqual((result['state'], result['advice'][row['id']]['verdict']), ('done', 'remove'))
        self.assertEqual(set(result['advice']), {row['id']})
        self.assertEqual(result['error'], '')
        self.assertFalse(check._running)
        self.assertEqual(lessons.path(PID).read_bytes(), before)
        self.assertFalse((paths.TETHER / 'projects' / PID / 'advisor-log.md').exists())
        check.start()  # The slot is available again.

    def test_daemon_worker_completes_with_mocked_model_and_is_joined(self):
        row = self.add()
        threads = []
        def make_thread(**options):
            thread = Thread(**options)
            threads.append(thread)
            return thread
        self.thread.side_effect = make_thread
        reply = json.dumps({'lessons': [self.entry(row)]})
        self.run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'result': reply}))
        result = check.start()
        self.assertEqual(result['state'], 'running')
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(thread.daemon)
        self.assertEqual(check.status()['state'], 'done')
        self.assertFalse(check._running)

    def test_prompt_has_only_requested_lesson_fields_and_bounded_project_evidence(self):
        row = self.add(quote='The user said this.')
        lessons.dispute(PID, row['id'], '%1', 'The notes say otherwise.')
        global_row = self.add('When global checks fail, read the notes.')
        lessons.keep(PID, global_row['id'])
        lessons.promote(PID, global_row['id'])
        answers = [dict(question='q' * 350, answer='a' * 500) for _ in range(25)]
        with patch.object(decisions, 'entries', return_value=answers) as entries:
            text = check._prompt(lessons.all_entries()['lessons'])
        entries.assert_called_once_with(PID)
        self.assertIn('Never guess', text)
        self.assertIn('Do not edit files.', text)
        evidence_text, lesson_text = text.split('\nEvidence:\n')[1].split('\nLessons:\n')
        evidence = json.loads(evidence_text)
        self.assertEqual(evidence[0]['notes'], str(paths.TETHER / 'projects' / PID / 'notes') + '/')
        self.assertEqual(len(evidence[0]['answers']), 20)
        self.assertEqual(evidence[0]['answers'][0], dict(question='q' * 300, answer='a' * 300))
        payload = json.JSONDecoder().raw_decode(lesson_text)[0]
        self.assertEqual(payload[0]['project_name'], 'All projects')
        project_row = payload[1]
        self.assertEqual(set(project_row), {'id', 'project_name', 'kind', 'text', 'status', 'hits',
                                           'created', 'quote', 'disputes'})
        self.assertEqual(project_row['created'], datetime.fromtimestamp(row['created']).strftime('%Y-%m-%d'))
        self.assertEqual(project_row['disputes'], ['The notes say otherwise.'])
        self.assertEqual(project_row['quote'], 'The user said this.')
        with patch.object(decisions, 'entries', side_effect=OSError('bad answers')):
            self.assertIn('\nEvidence:\n[]\nLessons:', check._prompt(lessons.all_entries()['lessons']))

    def test_claude_command_read_only_resolved_model_and_auto_effort(self):
        self.run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'result': 'reply'}))
        self.assertEqual(check._agent(self.main, 'request'), 'reply')
        command = self.run.call_args.args[0]
        self.assertEqual(command[:2], ['/mock/claude', '-p'])
        self.assertNotIn('request', command)
        for flag, value in (('--model', 'claude-fable-5-2'), ('--effort', 'high'), ('--output-format', 'json'),
                            ('--tools', 'Read,Grep,Glob'), ('--allowedTools', 'Read,Grep,Glob'), ('--setting-sources', '')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('--strict-mcp-config', command)
        self.assertIn('--no-session-persistence', command)
        self.assertEqual(self.run.call_args.kwargs, dict(cwd=str(paths.TETHER / 'projects'),
                         input='request', capture_output=True, text=True, timeout=900))
        self.resolve.assert_called_with('claude', 'latest')
        self.resolve.return_value = None
        check._agent(dict(self.main, effort='auto'), 'request')
        command = self.run.call_args.args[0]
        self.assertNotIn('--effort', command)
        self.assertNotIn('--model', command)

    def test_codex_command_read_only_output_model_and_effort(self):
        self.resolve.return_value = 'codex-model'
        output_paths = []
        def run(command, **options):
            output = Path(command[command.index('-o') + 1])
            output_paths.append(output)
            output.write_text('reply from file')
            return SimpleNamespace(returncode=0, stdout='ignored')
        self.run.side_effect = run
        main = dict(agent='codex', model='latest', effort='high')
        self.assertEqual(check._agent(main, 'request'), 'reply from file')
        command = self.run.call_args.args[0]
        self.assertEqual(command[:5], ['/mock/codex', 'exec', '--sandbox', 'read-only', '--skip-git-repo-check'])
        for flag, value in (('-C', str(paths.TETHER / 'projects')), ('-m', 'codex-model'),
                            ('-c', 'model_reasoning_effort=high')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(command[-1], '-')
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral'):
            self.assertIn(flag, command)
        disabled = {command[at + 1] for at, part in enumerate(command) if part == '--disable'}
        self.assertTrue({'hooks', 'plugins', 'apps', 'browser_use', 'computer_use'} <= disabled)
        self.assertEqual(self.run.call_args.kwargs['timeout'], 900)
        self.assertEqual(self.run.call_args.kwargs['input'], 'request')
        self.assertEqual(self.run.call_args.kwargs['cwd'], str(paths.TETHER / 'projects'))
        self.resolve.assert_called_with('codex', 'latest')
        self.resolve.return_value = None
        check._agent(dict(main, effort='auto'), 'request')
        command = self.run.call_args.args[0]
        self.assertNotIn('-m', command)
        self.assertNotIn('-c', command)
        self.assertTrue(all(not output.exists() for output in output_paths))

    def test_reason_cut_at_a_space_survives_save_and_status(self):
        row = self.add()
        reason = 'a ' * 150 + 'b'
        advice = check._parse(json.dumps({'lessons': [dict(self.entry(row), reason=reason)]}), [row])
        self.assertEqual(advice[row['id']]['reason'], ('a ' * 150).rstrip())
        self.save(advice=advice)
        self.assertEqual(check.status()['advice'], advice)

    def test_parse_valid_entries_first_wins_and_bad_entries_dropped(self):
        rows = [self.add(f'When case {i} occurs, read the notes.') for i in range(3)]
        items = [None, [], self.entry(rows[0]), dict(self.entry(rows[0]), verdict='remove'),
                 dict(self.entry(rows[1]), verdict='edit', text=' When  errors occur, read every error. '),
                 dict(self.entry(rows[2]), verdict='unsure', reason=' a\n b\t' + 'c' * 400),
                 dict(self.entry(rows[0]), id='unknown'), dict(self.entry(rows[0]), id=[]),
                 dict(self.entry(rows[0]), id=rows[1]['id'], verdict='bad')]
        reply = 'Here is the JSON:\n```json\n' + json.dumps({'lessons': items}) + '\n```'
        result = check._parse(reply, rows)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[rows[0]['id']]['verdict'], 'keep')
        self.assertEqual(result[rows[1]['id']]['text'], 'When errors occur, read every error.')
        self.assertEqual(result[rows[2]['id']]['reason'], ('a b ' + 'c' * 400)[:300])
        self.assertEqual(result[rows[2]['id']]['text'], '')
        self.assertEqual(result[rows[0]['id']]['hash'], hashlib.sha256(rows[0]['text'].encode()).hexdigest()[:16])

    def test_parse_downgrades_invalid_edit_and_fails_without_valid_entries(self):
        row = self.add()
        for text in (None, '', 'Not a lesson', 'When unsure, skip review.', 'When ' + 'x' * 200):
            item = dict(self.entry(row), verdict='edit', text=text)
            entry = check._parse(json.dumps({'lessons': [item]}), [row])[row['id']]
            self.assertEqual((entry['verdict'], entry['text'], entry['reason']), ('unsure', '', item['reason']))
        bad_items = [dict(self.entry(row), **change) for change in (
            {'verdict': 'bad'}, {'confidence': 'bad'}, {'reason': ''}, {'reason': ' \n '},
            {'reason': 1}, {'id': 'unknown'})]
        for reply in ('garbage', '[]', '{}', '{"lessons": {}}', '{"lessons": []}',
                      json.dumps({'lessons': bad_items})):
            with self.assertRaisesRegex(ValueError, check.UNREADABLE):
                check._parse(reply, [row])

    def test_failures_preserve_old_advice_and_release_job_slot(self):
        row = self.add()
        advice = self.save(row)['advice']
        for failure, message in ((subprocess.TimeoutExpired('agent', 900), 'The advisor took too long. Try again.'),
                                 (OSError('private detail'), check.FAILED), (RuntimeError('unexpected'), check.FAILED)):
            check.start()
            self.run.side_effect = failure
            result = self.run_job('unused')
            self.assertEqual((result['state'], result['error'], result['advice']), ('failed', message, advice))
            self.assertFalse(check._running)
        self.run.side_effect = None
        check.start()
        result = self.run_job('not JSON')
        self.assertEqual((result['state'], result['error']), ('failed', check.UNREADABLE))

    def test_missing_binary_and_model_error_envelope_fail(self):
        self.add()
        self.which.side_effect = None
        self.which.return_value = None
        check.start()
        result = self.run_job('unused')
        self.assertEqual((result['state'], result['error']), ('failed', 'Install Claude or Codex to use the advisor.'))
        self.run.assert_not_called()
        self.which.return_value = '/mock/claude'
        for response in (SimpleNamespace(returncode=1, stdout='private detail'),
                         SimpleNamespace(returncode=0, stdout='{"is_error": true, "result": "private detail"}')):
            check.start()
            self.run.side_effect = lambda *args, **kwargs: response
            result = self.run_job('unused')
            self.assertEqual((result['state'], result['error']),
                             ('failed', 'The advisor could not check the lessons. Try again.'))
            self.assertFalse(check._running)

    def test_thread_start_and_unexpected_prompt_failure_end_failed(self):
        self.add()
        self.thread.return_value.start.side_effect = RuntimeError('no threads')
        with self.assertRaisesRegex(ValueError, check.FAILED):
            check.start()
        self.assertFalse(check._running)
        self.assertEqual(check.status()['state'], 'failed')
        self.thread.return_value.start.side_effect = None
        check.start()
        with patch.object(check, '_prompt', side_effect=RuntimeError('unexpected')):
            result = self.run_job('unused')
        self.assertEqual((result['state'], result['error']), ('failed', check.FAILED))
        self.assertFalse(check._running)

    def test_result_write_failure_retries_failed_state_and_releases_slot(self):
        row = self.add()
        check.start()
        write = lessons._write
        def fail_once(file, data):
            if data['state'] == 'done':
                raise OSError('write failed')
            write(file, data)
        with patch.object(lessons, '_write', side_effect=fail_once):
            result = self.run_job(json.dumps({'lessons': [self.entry(row)]}))
        self.assertEqual((result['state'], result['error']), ('failed', check.FAILED))
        self.assertFalse(check._running)


if __name__ == '__main__':
    unittest.main()
