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
            self.assertEqual(options, ['--since', '2030-01-01 00:00'])
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
        self.assertEqual((data['pid'], data['ended'], data['duration_s'], data['turns']), ('23', 'clean', 14, 2))
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
        self.assertEqual(json_path, self.directory / f'{start:%Y-%m-%d}' / f'{start:%H%M%S}-23.json')
        self.assertEqual(lines, [f"friday-call {start:%H:%M} 14s turns=2 ended=clean "
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
        self.assertEqual(saved, {'cursor': self.rows[-1]['__CURSOR'], 'done': [self.digest()['id']]})
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

    def test_new_peer_then_lifecycle_finish_calls_in_order(self):
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
        self.assertEqual([json.loads(path.read_text())['ended'] for path in self.digests()], ['abrupt', 'abrupt'])

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
