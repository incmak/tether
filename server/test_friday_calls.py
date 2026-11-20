"""Exercise Friday call scans with synthetic journal records."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import friday_calls


def pipecat(message, caller='pipecat.workers.runner:run:74', level='DEBUG'):
    return f'2030-01-01 00:00:00.000 | {level:8} | {caller} - {message}'


def peer():
    return pipecat('Initializing new peer connection', friday_calls.PEER + '215')


def runner(action, name='runner-synthetic'):
    return pipecat(f"WorkerRunner '{name}': {action} running")


def context(*messages):
    return pipecat(f'FridayLLM#0: Generating LLM response {list(messages)!r}', friday_calls.LLM + '356')


def message(role, content, **extra):
    return dict(role=role, content=content, **extra)


class FridayCallsTest(unittest.TestCase):
    def setUp(self):
        self.scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(self.scratch) / 'calls'
        self.state = self.directory / 'state.json'
        self.rows = []
        self.run_journal = self.enterContext(patch.object(friday_calls.subprocess, 'run', side_effect=self.journal))
        self.enterContext(patch.dict(os.environ, {
            'TETHER_FRIDAY_CALLS': str(self.directory),
            'TETHER_JOURNALCTL': 'fake-journalctl',
        }))
        self.base = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000
        self.now = self.base / 1_000_000 + 3600
        self.enterContext(patch.object(friday_calls.time, 'time', side_effect=lambda: self.now))

    def journal(self, command, **kwargs):
        self.assertEqual(command[:7], ['fake-journalctl', '-u', 'tether-friday', '-o', 'json', '--no-pager', '--all'])
        options = command[7:]
        rows = self.rows
        if options[:1] == ['-n']:
            rows = rows[-int(options[1]):]
        elif options and options[0].startswith('--after-cursor='):
            cursor = options[0].split('=', 1)[1]
            after = next(i for i, row in enumerate(rows) if row['__CURSOR'] == cursor)
            rows = rows[after + 1:]
        elif options:
            self.assertEqual(options[0], '--since')
            if options[1].startswith('@'):
                cutoff = float(options[1][1:]) * 1_000_000
                rows = [row for row in rows if int(row['__REALTIME_TIMESTAMP']) >= cutoff]
            else:
                self.assertEqual(options[1], '2030-01-01 00:00')
        return subprocess.CompletedProcess(command, 0, '\n'.join(map(json.dumps, rows)), '')

    def append(self, text, pid='23', **fields):
        index = len(self.rows)
        self.rows.append({'__CURSOR': f'cursor-{index}', '__REALTIME_TIMESTAMP': str(self.base + index * 1_000_000),
                          '_PID': pid, 'MESSAGE': text, **fields})

    def start(self, pid='23', name='runner-synthetic'):
        self.append(peer(), pid)
        self.append(runner('started', name), pid)

    def cli(self, *arguments):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = friday_calls.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def scan(self, historical=True):
        args = ('--since', '2030-01-01 00:00') if historical else ()
        result, stdout, stderr = self.cli('scan', *args)
        self.assertEqual((result, stderr), (0, ''))
        return stdout.splitlines()

    def digests(self):
        return sorted(self.directory.glob('*/*.json'))

    def digest(self):
        paths = self.digests()
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_rewritten_state_and_shortened_reads_merge_without_duplicates(self):
        # Friday moves and rewrites its state message and shortens old tool results on each request.
        self.start()
        system = message('system', 'Synthetic instructions')
        state = lambda now: message('user', friday_calls.STATE_PREFIX + '{"now":"%s"}' % now)
        ask = message('user', 'Read the chat')
        call = message('assistant', None, tool_calls=[
            {'id': 'read-1', 'function': {'name': 'read_last_reply', 'arguments': '{}'}}])
        full = message('tool', '{"text":"long reply"}', tool_call_id='read-1')
        short = message('tool', '{"text":"long...","shortened":true}', tool_call_id='read-1')
        answer = message('assistant', 'It says hello.')
        again = message('user', 'Thanks')
        news = message('user', friday_calls.EVENT_PREFIX + 'A chat finished.')
        self.append(context(system, state('1'), ask))
        self.append(context(system, state('1'), ask, call, full))
        self.append(context(system, ask, call, short, answer, state('2'), again, news))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual([m['role'] for m in data['transcript']], ['user', 'assistant', 'tool', 'assistant', 'user', 'user'])
        self.assertEqual(data['turns'], 2)
        self.assertEqual(data['flags'], {})

    def test_startup_noise_raises_no_flag(self):
        self.start()
        for noise in friday_calls.KNOWN_NOISE:
            self.append(pipecat('Synthetic: ' + noise + ' detail', 'pipecat.synthetic:x:1', 'ERROR'))
        self.append(pipecat('Something new broke', 'pipecat.synthetic:x:1', 'ERROR'))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual(data['flags'], {'pipecat_error': 1})
        self.assertEqual(len(data['events']), len(friday_calls.KNOWN_NOISE) + 1)

    def test_mismatched_context_without_trim_is_flagged_as_gap(self):
        self.start()
        system = message('system', 'Synthetic instructions')
        self.append(context(system, message('user', 'First')))
        self.append(context(system, message('user', 'Unrelated')))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual(data['flags'].get('transcript_gap'), 1)
        self.assertTrue(data['transcript'][1].get('gap'))

    def test_clean_call_has_one_private_digest_and_summary_line(self):
        self.start()
        system = message('system', 'Synthetic instructions')
        user = message('user', 'Please send the update')
        tool = message('assistant', None, tool_calls=[
            {'id': 'tool-1', 'function': {'name': 'send_to_chat', 'arguments': '{"text":"mock update"}'}}])
        reply = message('tool', '{"error":"readback required"}', tool_call_id='tool-1')
        self.append(context(system, user))
        self.append(context(system, user, tool, reply))
        for _ in range(3):
            self.append('Friday blocked send_to_chat: readback required')
        self.append(context(system, user, tool, reply, message('user', 'please send the update!')))
        self.append('Friday metrics {"stop_to_first_audio":0.8,"llm_calls":2}')
        self.append('Friday metrics {"stop_to_first_audio":0.4,"llm_calls":2}')
        self.append('Friday net {"packetsLost":3,"jitter":0.002}')
        self.append('Friday net {"packetsLost":1,"jitter":0.005}')
        self.append('Friday loop {}')
        self.append('Friday tts {}')
        self.append(runner('finished'))
        lines = self.scan()
        self.assertEqual(len(lines), 1)
        data = self.digest()
        self.assertEqual((data['pid'], data['ended'], data['duration_s'], data['turns']), ('23', 'clean', 13, 2))
        self.assertEqual(data['flags'], {
            'blocked:send_to_chat': 3, 'blocked_repeat': 1, 'tool_error': 1, 'user_repeat': 1})
        self.assertEqual(data['metrics']['max_stop_to_first_audio'], 0.8)
        self.assertAlmostEqual(data['metrics']['median_stop_to_first_audio'], 0.6)
        self.assertEqual(data['metrics']['llm_calls'], 4)
        self.assertEqual(data['net'], {'max_packetsLost': 3, 'max_jitter': 0.005})
        self.assertEqual([event['kind'] for event in data['events']], ['blocked'] * 3)
        self.assertEqual(data['transcript'][1]['tool_calls'],
                         [{'id': 'tool-1', 'name': 'send_to_chat', 'arguments': '{"text":"mock update"}'}])
        self.assertEqual(data['transcript'][2]['tool_call_id'], 'tool-1')
        json_path = self.digests()[0]
        md_path = json_path.with_suffix('.md')
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in (json_path, md_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(self.state.exists())
        start = datetime.fromisoformat(data['start'])
        self.assertEqual(json_path, self.directory / f'{start:%Y-%m-%d}' / f'{start:%H%M%S}-000-23-runner-synthetic.json')
        self.assertEqual(lines, [f"friday-call {start:%H:%M} 13s turns=2 ended=clean "
                                 f"flags=blocked:send_to_chat:3,blocked_repeat:1,tool_error:1,user_repeat:1 "
                                 f"digest={md_path}"])
        md = md_path.read_text()
        self.assertLess(md.index('→ send_to_chat('), md.index('✗ blocked send_to_chat: readback required'))
        self.assertEqual(self.cli('show', str(json_path)), (0, md, ''))

    def test_trimmed_context_uses_longest_overlap(self):
        self.start()
        user = message('user', 'First request')
        answer = message('assistant', 'First answer')
        second = message('user', 'Second request')
        third = message('assistant', 'Second answer')
        self.append(context(user, answer, second))
        self.append('Friday context trimmed 4 -> 3 messages; next LLM turn is cold')
        self.append(context(answer, second, third))
        self.append(runner('finished'))
        self.scan()
        self.assertEqual(self.digest()['transcript'], [user, answer, second, third])

    def test_trimmed_context_without_overlap_marks_gap(self):
        self.start()
        user = message('user', 'First request')
        answer = message('assistant', 'Second answer')
        self.append(context(user))
        self.append('Friday context trimmed 4 -> 2 messages; next LLM turn is cold')
        self.append(context(answer))
        self.append(runner('finished'))
        self.scan()
        self.assertEqual(self.digest()['transcript'], [user, {'gap': True}, answer])

    def test_pid_change_ends_call(self):
        self.start()
        self.append('Friday models loaded in 0.1s', pid='24')
        self.scan()
        data = self.digest()
        self.assertEqual(data['ended'], 'abrupt')
        self.assertEqual(data['flags'], {'ended_abrupt': 1})
        self.assertEqual(data['events'], [])

    def test_resume_replays_the_start_then_emits_once(self):
        self.append('Friday models loaded in 0.3s')
        self.assertEqual(self.scan(historical=False), [])
        starting_state = json.loads(self.state.read_text())
        self.start()
        self.append(context(message('user', 'A synthetic request')))
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(json.loads(self.state.read_text()), starting_state)
        self.assertEqual(self.digests(), [])
        self.append(runner('finished'))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(self.digest()['turns'], 1)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved, {'cursor': self.rows[-1]['__CURSOR'], 'done': [self.digest()['id']], 'pending': []})
        self.scan()
        self.assertEqual(json.loads(self.state.read_text()), saved)

    def test_unparseable_context_marks_transcript_and_version(self):
        self.start()
        self.append(pipecat('FridayLLM#0: Generating LLM response [synthetic invalid repr]', friday_calls.LLM + '356'))
        self.append(runner('finished'))
        with patch.object(friday_calls, 'pipecat_version', return_value='1.2.3'):
            self.scan()
        data = self.digest()
        self.assertEqual(data['transcript'], 'unparsed')
        self.assertEqual(data['flags'], {'transcript_unparsed': 1})
        self.assertEqual(data['pipecat_version'], '1.2.3')

    def test_first_run_starts_at_the_tail(self):
        self.start()
        self.append(runner('finished'))
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(self.digests(), [])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], self.rows[-1]['__CURSOR'])

    def test_failed_checkpoint_retries_before_printing_once(self):
        self.assertEqual(self.scan(historical=False), [])
        self.start()
        self.append(runner('finished'))
        with patch.object(friday_calls, 'write_state', side_effect=OSError('synthetic disk error')):
            result, stdout, stderr = self.cli('scan')
        self.assertEqual((result, stderr), (1, 'friday-call error: synthetic disk error\n'))
        self.assertEqual(len(self.digests()), 1)
        self.assertTrue(self.digests()[0].with_suffix('.md').exists())
        subsequent_lines = self.scan(historical=False)
        self.assertEqual(stdout, '')
        self.assertEqual(len(subsequent_lines), 1)
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(len(self.digests()), 1)

    def test_refuses_symlinked_state_file(self):
        self.scan(historical=False)
        target = self.directory.parent / 'other-state.json'
        target.write_text(self.state.read_text())
        self.state.unlink()
        self.state.symlink_to(target)
        self.start()
        self.append(runner('finished'))
        result, stdout, stderr = self.cli('scan')
        self.assertEqual((result, stdout), (1, ''))
        self.assertIn('friday-call error:', stderr)
        self.assertEqual(self.digests(), [])

    def test_failed_print_replays_pending_before_reading_journal(self):
        self.scan(historical=False)
        self.start()
        self.append(runner('finished'))
        with patch.object(friday_calls, 'print', create=True, side_effect=BrokenPipeError('closed')):
            with self.assertRaisesRegex(BrokenPipeError, 'closed'):
                friday_calls.scan(self.directory)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved['done'], [self.digest()['id']])
        [pending] = saved['pending']
        self.assertEqual(pending['id'], self.digest()['id'])
        with patch.object(friday_calls, 'journal', side_effect=friday_calls.JournalError('offline')):
            self.assertEqual(self.cli('scan'), (1, pending['line'] + '\n', 'friday-call error: offline\n'))
        self.assertEqual(json.loads(self.state.read_text())['pending'], [])
        self.assertEqual(self.scan(historical=False), [])

    def test_digest_names_distinguish_milliseconds_under_one_pid(self):
        for name, offset in [('runner/x', 0), ('runner:x', 250_000), ('runner_x', 250_500)]:
            self.append(runner('started', name), __REALTIME_TIMESTAMP=str(self.base + offset))
            self.append(runner('finished', name), __REALTIME_TIMESTAMP=str(self.base + offset + 100))
        self.assertEqual(len(self.scan()), 3)
        paths = self.digests()
        start = friday_calls.decode_entry(self.rows[0]).at
        self.assertEqual(sorted(path.stem for path in paths), [
            f'{start:%H%M%S}-000-23-runner_x', f'{start:%H%M%S}-250-23-runner_x',
            f'{start:%H%M%S}-250-23-runner_x-2',
        ])
        self.assertEqual(len({json.loads(path.read_text())['id'] for path in paths}), 3)
        self.scan()
        self.assertEqual(self.digests(), paths)

    def test_first_run_replays_open_call_and_skips_finished_calls(self):
        self.start(name='earlier')
        self.append(runner('finished', 'earlier'))
        anchor = self.rows[-1]['__CURSOR']
        self.start()
        self.append(context(message('user', 'Still here')))
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], anchor)
        self.assertEqual(self.digests(), [])
        self.append(runner('finished'))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(self.digest()['turns'], 1)

    def test_first_run_without_anchor_preserves_lookback(self):
        self.append(runner('started', 'outside lookback'), __REALTIME_TIMESTAMP=str(self.base - 48_000_000_000))
        self.append(runner('finished', 'outside lookback'), __REALTIME_TIMESTAMP=str(self.base - 47_999_000_000))
        self.start()
        self.assertEqual(self.scan(historical=False), [])
        self.assertIsNone(json.loads(self.state.read_text())['cursor'])
        self.now += 60
        self.append(runner('finished'))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.scan(historical=False), [])

    def test_first_run_reads_twelve_hours_and_skips_a_recent_finish(self):
        self.append(runner('started'), __REALTIME_TIMESTAMP=str(self.base - 10 * 3600 * 1_000_000))
        self.append(runner('finished'))
        self.now = int(self.rows[-1]['__REALTIME_TIMESTAMP']) / 1e6 + 1
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(self.run_journal.call_args.args[0][-2:], ['--since', f'@{self.now - 12 * 3600:.6f}'])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], self.rows[-1]['__CURSOR'])
        self.now += 60
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(self.digests(), [])

    def test_grace_and_peer_expiry_use_the_clock_before_journal_read(self):
        self.scan(historical=False)
        self.start()
        self.append(runner('finished'))
        self.now = int(self.rows[-1]['__REALTIME_TIMESTAMP']) / 1e6 + 1
        read_journal = friday_calls.journal

        def slow_journal(*args, **kwargs):
            self.now += 60
            return read_journal(*args, **kwargs)

        with patch.object(friday_calls, 'journal', slow_journal):
            self.assertEqual(self.scan(historical=False), [])
        self.append('Friday metrics {"llm_calls":2}')
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.digest()['metrics']['llm_calls'], 2)
        cursor = self.rows[-1]['__CURSOR']
        self.append(peer())
        self.now = int(self.rows[-1]['__REALTIME_TIMESTAMP']) / 1e6 + 1
        with patch.object(friday_calls, 'journal', slow_journal):
            self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], cursor)

    def test_runner_distinguishes_calls_started_at_the_same_time_under_one_pid(self):
        self.directory.mkdir()
        start = datetime.fromtimestamp(self.base / 1e6).astimezone().isoformat()
        legacy_id = f'{start}-23'
        self.state.write_text(json.dumps({'cursor': None, 'done': [legacy_id]}))
        for name in ('runner/x', 'runner:x'):
            self.append(runner('started', name), __REALTIME_TIMESTAMP=str(self.base))
            self.append(runner('finished', name), __REALTIME_TIMESTAMP=str(self.base))
        self.assertEqual(len(self.scan(historical=False)), 2)
        ids = {json.loads(path.read_text())['id'] for path in self.digests()}
        self.assertEqual(ids, {f'{start}-23-runner/x', f'{start}-23-runner:x'})
        self.assertEqual(set(json.loads(self.state.read_text())['done']), ids | {legacy_id})

    def test_bad_text_is_replaced_and_digest_failure_does_not_stop_the_scan(self):
        self.scan(historical=False)
        for name in ('text', 'broken', 'markdown-write', 'last'):
            self.start(name=name)
            self.append(context(message('user', 'literal surrogate \ud800')))
            self.append('Friday bye \udfff')
            self.append(runner('finished', name))

        render = friday_calls.markdown
        write = friday_calls.atomic_write

        def fail_one(data, timeline):
            if data['id'].endswith('-broken'):
                raise RuntimeError('private detail')
            return render(data, timeline)

        def fail_markdown_write(path, content):
            if path.name.endswith('-markdown-write.md'):
                raise OSError('private detail')
            write(path, content)

        with (patch.object(friday_calls, 'markdown', side_effect=fail_one),
              patch.object(friday_calls, 'atomic_write', side_effect=fail_markdown_write)):
            result, stdout, stderr = self.cli('scan')
        self.assertEqual(result, 1)
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 2)
        # A failed write is retried in full on the next healthy scan.
        lines += self.scan(historical=False)
        self.assertEqual(len(lines), 4)
        normal, broken, markdown_write, last = [json.loads(path.read_text()) for path in self.digests()]
        for data in (normal, markdown_write, last):
            self.assertEqual(data['transcript'], [message('user', 'literal surrogate ?')])
            self.assertEqual(data['events'][0]['text'], 'Friday bye ?')
        self.assertEqual(broken, {
            'id': friday_calls.decode_entry(self.rows[6]).at.isoformat() + '-23-broken',
            'start': friday_calls.decode_entry(self.rows[6]).at.isoformat(),
            'end': friday_calls.decode_entry(self.rows[9]).at.isoformat(),
            'error': 'RuntimeError', 'flags': {'digest_error': 1},
        })
        self.assertIn('flags=digest_error:1', lines[1])
        self.assertNotIn('private detail', lines[1])
        self.assertTrue(lines[2].endswith('-markdown-write.md'))
        self.assertEqual(self.scan(historical=False), [])

    def test_refuses_symlink_directories_and_does_not_follow_digest_symlinks(self):
        target = Path(self.scratch) / 'target'
        target.mkdir(mode=0o755)
        self.directory.symlink_to(target, target_is_directory=True)
        self.start()
        self.append(runner('finished'))
        start = friday_calls.decode_entry(self.rows[1]).at
        day = self.directory / f'{start:%Y-%m-%d}'
        args = ('scan', '--since', '2030-01-01 00:00')
        for link in (self.directory, day):
            with self.subTest(link=link):
                if link == day:
                    self.directory.mkdir()
                    day.symlink_to(target, target_is_directory=True)
                result, stdout, stderr = self.cli(*args)
                self.assertEqual((result, stdout), (1, ''))
                self.assertIn('symlink', stderr)
                self.assertIn(str(link), stderr)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
                self.assertEqual(list(target.iterdir()), [])
                link.unlink()
        day.mkdir()
        base = day / f'{start:%H%M%S}-000-23-runner-synthetic'
        external = target / 'outside.json'
        external.write_text(json.dumps({'id': f'{start.isoformat()}-23-runner-synthetic'}))
        base.with_suffix('.json').symlink_to(external)
        self.assertEqual(len(self.scan()), 1)
        self.assertTrue(base.with_suffix('.json').is_symlink())
        self.assertTrue(base.with_name(base.name + '-2').with_suffix('.json').is_file())

    def test_repeated_peer_init_does_not_create_phantom_calls(self):
        self.append(peer(), __REALTIME_TIMESTAMP=str(self.base - 60_000_000))
        self.append(peer())
        self.append(peer())
        self.append(runner('started'))
        self.append(peer())
        self.append(runner('finished'))
        self.start()
        self.append(runner('finished'))
        self.assertEqual(len(self.scan()), 2)
        first, second = [json.loads(path.read_text()) for path in self.digests()]
        self.assertEqual((first['ended'], second['ended']), ('clean', 'clean'))
        self.assertEqual(first['start'], friday_calls.decode_entry(self.rows[3]).at.isoformat())
        self.assertEqual([event['kind'] for event in first['events']], ['reconnect'])

    def test_different_runner_ends_open_call_and_replays_the_preceding_peer(self):
        self.scan(historical=False)
        self.start()
        self.append(peer())
        self.append(runner('started', 'next'))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(json.loads(self.state.read_text())['cursor'], 'cursor-1')
        first = self.digest()
        self.assertEqual(first['ended'], 'abrupt')
        self.assertEqual([event['kind'] for event in first['events']], ['reconnect'])
        self.append(runner('finished', 'next'))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.scan(historical=False), [])

    def test_waits_for_post_finish_metrics_before_emitting(self):
        self.append('idle')
        self.scan(historical=False)
        self.start()
        self.append(runner('finished'))
        finish_at = friday_calls.decode_entry(self.rows[-1]).at
        self.now = finish_at.timestamp() + 1
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], 'cursor-0')
        self.append('Friday metrics {"llm_calls":2,"stop_to_first_audio":0.4}')
        self.append('Friday net {"packetsLost":3}')
        self.append('Friday bye {}')
        self.now = finish_at.timestamp() + 6
        self.append('Friday metrics {"llm_calls":80}', __REALTIME_TIMESTAMP=str(int(self.now * 1e6)))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.scan(historical=False), [])
        data = self.digest()
        self.assertEqual(data['end'], finish_at.isoformat())
        self.assertEqual(data['metrics']['llm_calls'], 2)
        self.assertEqual(data['net']['max_packetsLost'], 3)
        self.assertEqual([event['kind'] for event in data['events']], ['bye'])

    def test_async_tool_result_updates_existing_message_but_keeps_full_text(self):
        self.start()
        pending = message('tool', '{"status":"pending"}', tool_call_id='async-1')
        full = message('tool', '{"text":"ready"}', tool_call_id='async-1')
        short = message('tool', '{"shortened":true}', tool_call_id='async-1')
        second_pending = message('tool', 'Working', tool_call_id='async-2')
        second_full = message('tool', 'Done', tool_call_id='async-2')
        user = message('user', 'Check both requests')
        answer = message('assistant', 'Both are ready.')
        self.append(context(user, pending, second_pending))
        self.append(context(user, full, second_pending, answer))
        self.append(context(short, second_full, message('user', 'Thanks')))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual([item for item in data['transcript'] if item.get('role') == 'tool'], [full, second_full])
        md = self.digests()[0].with_suffix('.md').read_text()
        self.assertIn('**tool:** {"text":"ready"}', md)
        self.assertIn('**tool:** Done', md)
        self.assertNotIn('shortened', md)

    def test_keeps_developer_role_and_skips_unknown_role(self):
        self.start()
        developer = message('developer', 'Developer guidance')
        user = message('user', 'Original request')
        self.append(context(developer, message(['unexpected'], 'Ignore this role'), user))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual(data['transcript'], [developer, user])
        self.assertEqual(data['flags'], {'unknown_role': 1})
        self.assertIn('**developer:** Developer guidance', self.digests()[0].with_suffix('.md').read_text())

    def test_skips_bad_tool_calls_and_checkpoints_oversized_context(self):
        self.scan(historical=False)
        self.start(name='malformed')
        tool = {'id': 'valid', 'function': {'name': 'lookup', 'arguments': '{}'}}
        self.append(context(message('assistant', 'Checking', tool_calls=[None, {'function': None}, tool])))
        self.append(runner('finished', 'malformed'))
        self.start(name='oversized')
        self.append(context(message('user', 'é' * (2 * 1024 * 1024))))
        self.append(runner('finished', 'oversized'))
        with patch.object(friday_calls, 'pipecat_version', return_value='synthetic'):
            self.assertEqual(len(self.scan(historical=False)), 2)
        malformed, oversized = [json.loads(path.read_text()) for path in self.digests()]
        self.assertEqual(malformed['transcript'], [
            message('assistant', 'Checking', tool_calls=[{'id': 'valid', 'name': 'lookup', 'arguments': '{}'}]),
        ])
        self.assertEqual(oversized['transcript'], 'unparsed')
        self.assertEqual(json.loads(self.state.read_text())['cursor'], self.rows[-1]['__CURSOR'])
        self.assertEqual(self.scan(historical=False), [])

    def test_unexpected_parser_exception_does_not_stop_catch_up(self):
        self.scan(historical=False)
        self.start()
        self.append(context(message('user', 'A synthetic request')))
        self.append(runner('finished'))
        with (patch.object(friday_calls, 'context_messages', side_effect=RuntimeError('synthetic private detail')),
              patch.object(friday_calls, 'pipecat_version', return_value='synthetic')):
            self.assertNotIn('synthetic private detail', self.scan(historical=False)[0])
        self.assertEqual(self.digest()['transcript'], 'unparsed')
        self.assertEqual(json.loads(self.state.read_text())['cursor'], self.rows[-1]['__CURSOR'])

    def test_tightens_existing_output_and_day_directory_modes(self):
        start = friday_calls.decode_entry({'__CURSOR': 'dummy', '__REALTIME_TIMESTAMP': str(self.base)}).at
        day = self.directory / f'{start:%Y-%m-%d}'
        day.mkdir(parents=True)
        for directory in (self.directory, day):
            directory.chmod(0o755)
        self.start()
        self.append(runner('finished'))
        self.scan()
        for directory in (self.directory, day):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_end_of_call_media_warning_stays_in_events_without_flag(self):
        self.start()
        self.append(pipecat(friday_calls.MEDIA_WARNING, level='WARNING'))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual(data['flags'], {})
        self.assertEqual([event['kind'] for event in data['events']], ['pipecat_warning'])

    def test_rejoins_split_context_and_decodes_byte_array(self):
        self.start()
        user = message('user', 'A synthetic résumé')
        line = context(user).encode()
        at = line.index('é'.encode()) + 1
        self.append(list(line[:at]), _LINE_BREAK='line-max')
        self.append(list(line[at:]))
        line = context(user, message('assistant', 'A synthetic reply'))
        at = line.index('reply')
        self.append(line[:at])
        self.append(line[at:])
        self.append(list(b'Friday bye {"reason":"\xff"}'))
        self.append(runner('finished'))
        self.scan()
        data = self.digest()
        self.assertEqual(data['transcript'], [user, message('assistant', 'A synthetic reply')])
        self.assertIn('\ufffd', data['events'][0]['text'])

    def test_reconnect_then_lifecycle_finish_calls_in_order(self):
        self.assertEqual(self.scan(historical=False), [])
        self.start()
        self.append('Friday bye {}')
        self.append(peer())
        self.append(runner('finished'))
        self.append(runner('started', 'runner-next'))
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.append('Stopping synthetic service.', MESSAGE_ID='de5b426a63be47a7b6ac3eaac82e2f6f')
        self.assertEqual(len(self.scan(historical=False)), 1)
        self.assertEqual(self.scan(historical=False), [])
        self.assertEqual([json.loads(path.read_text())['ended'] for path in self.digests()], ['clean', 'abrupt'])

    def test_flags_count_transcript_patterns_and_earlier_media_warnings(self):
        self.start()
        self.append(pipecat(friday_calls.MEDIA_WARNING, level='WARNING'))
        self.append('Friday LLM error: synthetic')
        self.append('Friday corrected false success')
        self.append('Friday dropped stray text: 4 chars')
        self.append('Friday Smart Turn unavailable; VAD fallback 0.8s: synthetic')
        self.append(pipecat('Synthetic failure', level='ERROR'))
        self.append(context(
            message('user', "Don't send that"),
            message('assistant', "Sorry, I couldn't send it."),
            message('user', 'Dont send that to the sample group'),
            message('tool', 'Invalid sample'),
            message('user', 'Yes'),
            message('assistant', 'Confirm the action'),
            message('user', 'please do'),
            message('user', 'No, which chat?'),
        ))
        self.append(runner('finished'))
        self.scan()
        self.assertEqual(self.digest()['flags'], {
            'assistant_failure': 1, 'corrected_false_success': 1, 'dropped_stray_text': 1,
            'llm_error': 1, 'pipecat_error': 1, 'pipecat_warning': 1, 'smart_turn_unavailable': 1,
            'tool_error': 1, 'user_correction': 1, 'user_repeat': 2,
        })

    def test_journalctl_failure_is_one_line_and_watch_retries_distinct_errors(self):
        first = subprocess.CompletedProcess([], 1, '', 'synthetic first\nextra detail\n')
        second = subprocess.CompletedProcess([], 1, '', 'synthetic second\n')
        self.run_journal.side_effect = [first, first, first, second]
        self.assertEqual(self.cli('scan'), (1, '', 'friday-call error: synthetic first extra detail\n'))
        self.assertFalse(self.state.exists())
        with patch.object(friday_calls.time, 'sleep', side_effect=[None, None, KeyboardInterrupt]):
            self.assertEqual(self.cli('watch', '--interval', '1'), (130, (
                'friday-call-watch error: synthetic first extra detail\n'
                'friday-call-watch error: synthetic second\n'), ''))


if __name__ == '__main__':
    unittest.main()
