"""Subagent reads use isolated transcripts and never write to real terminals."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server
import subagents
import tool_details


class SubagentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.main = self.root / 'session.jsonl'
        self.folder = self.root / 'session' / 'subagents'
        self.folder.mkdir(parents=True)
        self.config = {'agent': 'claude', 'transcript': str(self.main),
                       'owner_pid': 123, 'process_start': '456'}
        self.now = 1800000000
        self.owner_start = self.enterContext(patch.object(subagents, 'process_start', return_value='456'))
        self.start_ms = self.enterContext(patch.object(subagents.procinfo, 'start_ms',
                                                     return_value=(self.now - 300) * 1000))
        self.clock = patch.object(subagents.time, 'time', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.enterContext(patch.object(subagents, '_fd_links', return_value=[]))
        self.enterContext(patch.object(subagents.task_process, 'LEDGER', self.root / 'task-stops.json'))
        self.process_children = self.enterContext(patch.object(subagents.task_process, 'children', return_value=[]))
        subagents._tasks_cache.clear()
        self.addCleanup(subagents._tasks_cache.clear)
        subagents._cache.clear()
        self.addCleanup(subagents._cache.clear)
        self.write(self.main, [self.record('assistant', 'main', model='main-model', usage={
            'input_tokens': 10, 'cache_read_input_tokens': 20,
            'cache_creation_input_tokens': 30, 'output_tokens': 4})])

    def stamp(self, seconds):
        return datetime.fromtimestamp(self.now + seconds, timezone.utc).isoformat().replace('+00:00', 'Z')

    def record(self, role, content, seconds=-120, **message):
        return {'type': role, 'isSidechain': True, 'agentId': 'abc', 'timestamp': self.stamp(seconds),
                'message': dict(content=content, **message)}

    def write(self, path, records):
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))

    def agent(self, identifier='abc', *, meta=None, records=None, age=30):
        path = self.folder / f'agent-{identifier}.jsonl'
        if records is None:
            records = [
                self.record('user', 'Check the files'),
                self.record('assistant', [
                    {'type': 'text', 'text': 'Work starts'},
                    {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'file_path': '/work/a.py'}},
                    {'type': 'tool_use', 'id': 'bash-1', 'name': 'Bash', 'input': {'command': 'false'}},
                    {'type': 'tool_use', 'id': 'pending-1', 'name': 'Grep', 'input': {'pattern': 'TODO'}}],
                    seconds=-110, model='first-model', usage={'input_tokens': 7, 'output_tokens': 3}),
                self.record('user', [
                    {'type': 'tool_result', 'tool_use_id': 'read-1', 'content': 'file contents'},
                    {'type': 'tool_result', 'tool_use_id': 'bash-1', 'content': 'Process exited with code 2'}], seconds=-100),
                self.record('assistant', [{'type': 'text', 'text': 'Report complete'}], seconds=-90,
                            model='last-model', usage={'input_tokens': 100, 'cache_read_input_tokens': 20,
                                                       'cache_creation_input_tokens': 4, 'output_tokens': 6})]
        self.write(path, records)
        os.utime(path, (self.now - age, self.now - age))
        if meta is not None:
            (self.folder / f'agent-{identifier}.meta.json').write_text(json.dumps(meta))
        return path

    def test_running_finished_stopped_and_elapsed_time(self):
        self.agent('running', age=900)
        self.agent('stopped', meta={'stoppedByUser': True}, age=901)
        self.agent('finished', meta={'finished': True}, age=1000)
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertEqual(entries['running']['state'], 'running')
        self.assertIsNone(entries['running']['ended'])
        self.assertEqual(entries['running']['seconds'], 120)
        for identifier, state in (('stopped', 'stopped'), ('finished', 'finished')):
            self.assertEqual(entries[identifier]['state'], state)
            self.assertEqual(entries[identifier]['ended'], self.stamp(-90))
            self.assertEqual(entries[identifier]['seconds'], 30)
        self.assertIsNone(subagents.detail(self.config, 'running')['report'])
        self.assertIsNone(subagents.detail(self.config, 'stopped')['report'])

    def test_live_owner_keeps_two_hour_old_log_running(self):
        self.agent(age=2 * 60 * 60)
        self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'running')
        self.owner_start.assert_called_once_with(123)
        self.start_ms.assert_called_once_with('456')

    def test_agent_from_before_owner_started_is_stopped(self):
        self.agent(records=[self.record('user', 'Old agent', seconds=-360)])
        entry = subagents.listing(self.config)['agents'][0]
        self.assertEqual(entry['state'], 'stopped')
        self.assertEqual(entry['summary'], 'Claude exited before this agent finished.')

    def test_owner_start_allows_five_seconds_of_clock_skew(self):
        self.agent('within', records=[self.record('user', 'Within tolerance', seconds=-305)])
        self.agent('outside', records=[self.record('user', 'Outside tolerance', seconds=-306)])
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertEqual(entries['within']['state'], 'running')
        self.assertEqual(entries['outside']['state'], 'stopped')
        self.owner_start.assert_called_once_with(123)

    def test_changed_owner_stops_cached_agent_without_file_changes(self):
        self.agent()
        self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'running')
        self.owner_start.return_value = '789'
        entry = subagents.listing(self.config)['agents'][0]
        self.assertEqual(entry['state'], 'stopped')
        self.assertEqual(entry['summary'], 'Claude exited before this agent finished.')

    def test_missing_or_pending_owner_stops_agent(self):
        self.agent()
        for missing in ('owner_pid', 'process_start'):
            config = dict(self.config)
            del config[missing]
            with self.subTest(missing=missing):
                self.assertEqual(subagents.listing(config)['agents'][0]['state'], 'stopped')
        self.assertEqual(subagents.listing(dict(self.config, pending_conversation=True))['agents'][0]['state'],
                         'stopped')
        self.owner_start.assert_not_called()
        self.start_ms.assert_not_called()

    def test_owner_lookup_errors_stop_agent(self):
        self.agent()
        for reader in (self.owner_start, self.start_ms):
            for error in (OSError, ValueError):
                with self.subTest(reader=reader, error=error):
                    reader.side_effect = error('unavailable')
                    self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'stopped')
                    reader.side_effect = None

    def test_live_owner_with_unknown_agent_start_is_running(self):
        self.agent(records=[{'type': 'user', 'message': {'content': 'No timestamp'}}], age=7200)
        entry = subagents.listing(self.config)['agents'][0]
        self.assertEqual(entry['state'], 'running')
        self.assertIsNone(entry['started'])
        self.assertIsNone(entry['seconds'])

    def notification(self, identifier, status, summary='Task result'):
        return (f'<task-notification>\n<task-id>{identifier}</task-id>\n'
                f'<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>')

    def test_states_from_meta_and_notifications_with_precedence(self):
        cases = (
            ('completed', {}, 'completed', 'finished'),
            ('failed', {'finished': True}, 'failed', 'failed'),
            ('killed', {'finished': True}, 'killed', 'stopped'),
            ('user-stop', {'finished': True, 'stoppedByUser': True}, 'completed', 'stopped'),
            ('meta-stop', {'stoppedByUser': True}, None, 'stopped'),
        )
        records = []
        for identifier, meta, status, _ in cases:
            self.agent(identifier, meta=meta)
            if status:
                records.append(self.record('user', self.notification(identifier, status)))
        self.write(self.main, records)
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        for identifier, _, _, expected in cases:
            with self.subTest(identifier=identifier):
                self.assertEqual(entries[identifier]['state'], expected)
                self.assertIsNotNone(entries[identifier]['ended'])
                self.assertEqual(subagents.detail(self.config, identifier)['state'], expected)
        self.assertIsNone(entries['completed']['summary'])
        self.assertEqual(entries['failed']['summary'], 'Task result')
        self.assertIsNone(entries['meta-stop']['summary'])

    def test_notifications_decode_json_strings_and_text_parts_and_bound_summary(self):
        self.agent('string')
        self.agent('parts')
        self.write(self.main, [
            self.record('user', json.dumps(self.notification('string', 'failed', 'a' * 300))),
            self.record('user', [{'type': 'text', 'text': self.notification('parts', 'killed', 'Stopped &amp; done')}]),
            self.record('assistant', self.notification('parts', 'completed')),
        ])
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertEqual(entries['string']['state'], 'failed')
        self.assertEqual(entries['string']['summary'], 'a' * 240)
        self.assertEqual(entries['parts']['state'], 'stopped')
        self.assertEqual(entries['parts']['summary'], 'Stopped & done')

    def test_parent_detection_includes_tool_calls_before_retained_steps(self):
        self.agent('parent', meta={'spawnDepth': 1, 'toolUseId': 'main-spawn'}, records=[
            self.record('assistant', [{'type': 'tool_use', 'id': 'child-spawn', 'name': 'Agent'}] +
                        [{'type': 'text', 'text': 'step'} for _ in range(405)])])
        self.agent('child', meta={'spawnDepth': 2, 'toolUseId': 'child-spawn'})
        entries = {entry['id']: entry for entry in subagents.listing(self.config)['agents']}
        self.assertIsNone(entries['parent']['parent'])
        self.assertEqual(entries['child']['parent'], 'parent')
        self.assertEqual(entries['child']['depth'], 2)
        self.assertEqual(subagents.detail(self.config, 'child')['parent'], 'parent')

    def test_newest_nonempty_session_title_from_both_record_types(self):
        self.agent()
        self.write(self.main, [
            {'type': 'custom-title', 'customTitle': 'Original'},
            {'type': 'agent-name', 'agentName': 'New name'},
            {'type': 'custom-title', 'customTitle': '   '},
            {'type': 'agent-name', 'agentName': None},
        ])
        self.assertEqual(subagents.listing(self.config)['main']['title'], 'New name')
        with self.main.open('a') as stream:
            stream.write(json.dumps({'type': 'custom-title', 'customTitle': ' Latest '}) + '\n')
        self.assertEqual(subagents.listing(self.config)['main']['title'], 'Latest')

    def test_notification_cache_resumes_after_append_and_handles_partial_lines_and_rewrite(self):
        self.agent()
        self.write(self.main, [self.record('user', self.notification('abc', 'failed'))])
        # Put the notification and title well before the usage tail.
        with self.main.open('a') as stream:
            stream.write(json.dumps({'type': 'agent-name', 'agentName': 'Early title'}) + '\n')
            stream.write((json.dumps({'type': 'system', 'text': 'x' * 1024}) + '\n') * 300)
        self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'failed')
        size = self.main.stat().st_size
        notice = json.dumps(self.record('user', self.notification('abc', 'killed', 'Stopped now')))
        with self.main.open('a') as stream:
            stream.write(notice[:30])
        self.assertEqual(subagents.listing(self.config)['agents'][0]['state'], 'failed')
        original = Path.open
        seeks = []

        def read(path, *args, **kwargs):
            opened = original(path, *args, **kwargs)
            if path != self.main:
                return opened
            wrapped = Mock(wraps=opened)
            wrapped.__enter__ = Mock(return_value=wrapped)
            wrapped.__exit__ = Mock(side_effect=lambda *args: opened.close())
            wrapped.seek.side_effect = lambda *args: (seeks.append(args), opened.seek(*args))[1]
            return wrapped

        with self.main.open('a') as stream:
            stream.write(notice[30:] + '\n')
        with patch.object(Path, 'open', read):
            result = subagents.listing(self.config)
        self.assertEqual(result['agents'][0]['state'], 'stopped')
        self.assertEqual(result['agents'][0]['summary'], 'Stopped now')
        self.assertEqual(result['main']['title'], 'Early title')
        self.assertEqual(seeks[-1], (size,))
        self.write(self.main, [self.record('user', self.notification('abc', 'completed'))])
        result = subagents.listing(self.config)
        self.assertEqual(result['agents'][0]['state'], 'finished')
        self.assertIsNone(result['main']['title'])

    def test_five_changes_to_one_file_leave_one_cache_entry(self):
        path = self.agent()
        for index in range(5):
            with path.open('a') as stream:
                stream.write(json.dumps(self.record('assistant', str(index))) + '\n')
            subagents._cached(path, subagents._parse)
        self.assertEqual(len(subagents._cache), 1)

    def test_listing_model_tokens_metadata_and_shape(self):
        self.agent(meta={'description': 'Check files', 'agentType': 'general-purpose', 'model': 'fallback',
                         'pickedEffortModel': 'other', 'effort': 'medium', 'spawnDepth': 2,
                         'requestShape': 'background'})
        listing = subagents.listing(self.config)
        self.assertEqual(listing['main'], {'model': 'main-model', 'context': 64, 'title': None})
        self.assertEqual(listing['agents'], [{
            'id': 'abc', 'description': 'Check files', 'type': 'general-purpose', 'model': 'last-model',
            'effort': 'medium', 'background': True, 'state': 'running', 'started': self.stamp(-120),
            'ended': None, 'seconds': 120, 'tokens': 130, 'tools': 3, 'depth': 2, 'parent': None, 'summary': None}])

    def test_main_and_agent_use_last_usage_record(self):
        records = [self.record('assistant', 'first', model='with-usage', usage={'input_tokens': 8}),
                   self.record('assistant', 'last', model='without-usage')]
        self.write(self.main, records)
        self.agent(records=records)
        listing = subagents.listing(self.config)
        self.assertEqual(listing['main'], {'model': 'with-usage', 'context': 8, 'title': None})
        self.assertEqual(listing['agents'][0]['model'], 'without-usage')
        self.assertEqual(listing['agents'][0]['tokens'], 8)

    def test_model_fallback_and_missing_usage_metadata(self):
        for meta, expected in (({'model': 'meta-model', 'pickedEffortModel': 'picked'}, 'meta-model'),
                               ({'pickedEffortModel': 'picked'}, 'picked'), ({}, None)):
            with self.subTest(meta=meta):
                self.agent(meta=meta, records=[self.record('user', 'prompt')])
                entry = subagents.listing(self.config)['agents'][0]
                self.assertEqual(entry['model'], expected)
                self.assertIsNone(entry['tokens'])
                self.assertEqual(entry['depth'], 1)
                self.assertFalse(entry['background'])

    def test_detail_prompt_steps_report_and_tool_status(self):
        self.agent(meta={'finished': True})
        detail = subagents.detail(self.config, 'abc')
        self.assertEqual(detail['prompt'], 'Check the files')
        self.assertEqual(detail['report'], 'Report complete')
        self.assertEqual(detail['steps'][0], {'kind': 'text', 'text': 'Work starts'})
        self.assertEqual(detail['steps'][1], {'kind': 'tool', 'id': 'read-1', 'name': 'Read',
                                             'title': '/work/a.py', 'status': 'complete', 'is_error': False})
        self.assertEqual(detail['steps'][2]['status'], 'complete')
        self.assertTrue(detail['steps'][2]['is_error'])
        self.assertEqual(detail['steps'][3]['status'], 'pending')
        self.assertEqual(detail['steps'][-1], {'kind': 'text', 'text': 'Report complete'})
        self.assertNotIn('steps_cut', detail)
        for key, value in subagents.listing(self.config)['agents'][0].items():
            self.assertEqual(detail[key], value)

    def test_text_parts_and_byte_bounds(self):
        long_text = '🙂' * 10000
        self.agent(meta={'finished': True}, records=[
            self.record('user', [{'type': 'text', 'text': 'First'}, {'type': 'image'},
                                 {'type': 'text', 'text': long_text}]),
            self.record('assistant', long_text)])
        detail = subagents.detail(self.config, 'abc')
        self.assertTrue(detail['prompt'].startswith('First\n'))
        for value, limit in ((detail['prompt'], 16384), (detail['report'], 16384),
                             (detail['steps'][0]['text'], 4000)):
            self.assertLessEqual(len(value.encode()), limit)
            self.assertIn('[truncated]', value)
            self.assertNotIn('�', value)

    def test_steps_keep_last_400_and_tools_count_all_calls(self):
        self.agent(meta={'finished': True}, records=[self.record('assistant', [
            {'type': 'tool_use', 'id': f'call-{index}', 'name': 'Read'} for index in range(405)]),
            self.record('user', [{'type': 'tool_result', 'tool_use_id': 'call-404', 'is_error': True}])])
        detail = subagents.detail(self.config, 'abc')
        self.assertEqual(len(detail['steps']), 400)
        self.assertEqual(detail['steps'][0]['id'], 'call-5')
        self.assertTrue(detail['steps'][-1]['is_error'])
        self.assertEqual(detail['tools'], 405)
        self.assertTrue(detail['steps_cut'])

    def test_tool_detail_reads_sidechain_and_keeps_main_lookup_isolated(self):
        path = self.agent()
        result = subagents.tool(self.config, 'abc', 'bash-1')
        self.assertEqual(result['id'], 'bash-1')
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['is_error'])
        self.assertIn('false', result['detail'])
        self.assertIn('Process exited with code 2', result['detail'])
        self.assertEqual(subagents.tool(self.config, 'abc', 'pending-1')['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Tool call is no longer available'):
            tool_details.lookup(dict(self.config, transcript=path), 'bash-1')
        with self.assertRaises(ValueError):
            subagents.tool(self.config, 'abc', 'unknown')

    def test_invalid_and_missing_ids_are_rejected_for_detail_and_tool(self):
        self.agent()
        for identifier in ('../x', '', 'x/y', '/abc', 'x\\y', 'a' * 65, 'abc\n', None, 'missing'):
            for reader, args in ((subagents.detail, (identifier,)), (subagents.tool, (identifier, 'read-1'))):
                with self.subTest(identifier=identifier, reader=reader.__name__):
                    with self.assertRaisesRegex(ValueError, '^That agent is no longer available\\.$'):
                        reader(self.config, *args)

    def test_path_escape_via_symlink_is_rejected(self):
        self.write(self.root / 'outside.jsonl', [self.record('user', 'private')])
        (self.folder / 'agent-escape.jsonl').symlink_to(self.root / 'outside.jsonl')
        self.assertEqual(subagents.listing(self.config)['agents'], [])
        for reader, args in ((subagents.detail, ('escape',)), (subagents.tool, ('escape', 'read-1'))):
            with self.assertRaisesRegex(ValueError, '^That agent is no longer available\\.$'):
                reader(self.config, *args)

    def test_metadata_cannot_escape_folder(self):
        self.agent()
        outside = self.root / 'outside.json'
        outside.write_text('{"description":"private","finished":true}')
        (self.folder / 'agent-abc.meta.json').symlink_to(outside)
        entry = subagents.listing(self.config)['agents'][0]
        self.assertIsNone(entry['description'])
        self.assertEqual(entry['state'], 'running')

    def test_non_claude_and_missing_transcript_or_folder_return_empty(self):
        self.agent()
        for config in (dict(self.config, agent='codex'), dict(self.config, agent='antigravity'),
                       {'agent': 'claude'}, {'agent': 'claude', 'transcript': ''},
                       dict(self.config, transcript=str(self.root / 'missing.jsonl'))):
            with self.subTest(config=config):
                self.assertEqual(subagents.listing(config), {'main': {'model': None, 'context': None, 'title': None}, 'agents': [], 'tasks': []})
                with self.assertRaises(ValueError):
                    subagents.detail(config, 'abc')

    def test_malformed_lines_and_metadata_are_skipped(self):
        path = self.agent()
        original = path.read_bytes()
        path.write_bytes(b'{bad\nnull\n[]\n\xff\n{"type":"assistant","message":null}\n' + original + b'{unfinished')
        (self.folder / 'agent-abc.meta.json').write_text('{broken')
        (self.folder / 'agent-broken.jsonl').write_text('null\n[]\n{broken')
        listing = subagents.listing(self.config)
        self.assertEqual([entry['id'] for entry in listing['agents']], ['abc'])
        self.assertEqual(listing['agents'][0]['tokens'], 130)
        self.assertEqual(listing['agents'][0]['started'], self.stamp(-120))
        self.assertEqual(subagents.detail(self.config, 'abc')['prompt'], 'Check the files')
        self.assertEqual(subagents.tool(self.config, 'abc', 'read-1')['status'], 'complete')

    def test_unreadable_files_are_skipped(self):
        path = self.agent()
        original = Path.open

        def read(path_, *args, **kwargs):
            if path_ == path:
                raise PermissionError('unreadable')
            return original(path_, *args, **kwargs)

        with patch.object(Path, 'open', read):
            self.assertEqual(subagents.listing(self.config)['agents'], [])
            with self.assertRaises(ValueError):
                subagents.detail(self.config, 'abc')

    def test_missing_timestamps_and_usage_return_nulls(self):
        self.agent(records=[{'type': 'assistant', 'message': {'content': 'hello'}}], age=1000)
        entry = subagents.listing(self.config)['agents'][0]
        for key in ('started', 'ended', 'seconds', 'tokens', 'model'):
            self.assertIsNone(entry[key])

    def test_newest_40_sorted_by_started_time(self):
        for index in reversed(range(43)):
            self.agent(f'agent_{index}', records=[self.record('user', 'prompt', seconds=-1000 + index)])
        entries = subagents.listing(self.config)['agents']
        self.assertEqual([entry['id'] for entry in entries], [f'agent_{index}' for index in range(3, 43)])

    def test_main_reads_only_tail_and_ignores_later_non_usage_records(self):
        self.agent()
        self.main.write_bytes(b'x' * (subagents.TAIL_BYTES + 100) + b'\n' + json.dumps(
            self.record('assistant', 'recent', model='tail-model', usage={'input_tokens': 9})).encode() + b'\n' +
            json.dumps(self.record('assistant', 'no usage', model='ignore')).encode() + b'\n')
        original = Path.open
        stream = None

        def read(path, *args, **kwargs):
            nonlocal stream
            opened = original(path, *args, **kwargs)
            if path == self.main:
                stream = Mock(wraps=opened)
                stream.__enter__ = Mock(return_value=stream)
                stream.__exit__ = Mock(side_effect=lambda *args: opened.close())
                return stream
            return opened

        with patch.object(Path, 'open', read):
            self.assertEqual(subagents.listing(self.config)['main'], {'model': 'tail-model', 'context': 9, 'title': None})
        stream.read.assert_called_once_with(subagents.TAIL_BYTES)
        self.assertGreater(stream.seek.call_args_list[1].args[0], 0)

    def test_cache_reuses_reads_and_invalidates_on_transcript_and_meta_changes(self):
        path = self.agent(meta={})
        first = subagents.listing(self.config)
        with patch.object(Path, 'open', side_effect=AssertionError('unchanged files must stay cached')):
            self.assertEqual(subagents.listing(self.config), first)
            subagents.detail(self.config, 'abc')
        with path.open('a') as stream:
            stream.write(json.dumps(self.record('assistant', 'New report', model='new-model',
                                                usage={'input_tokens': 99})) + '\n')
        (self.folder / 'agent-abc.meta.json').write_text('{"finished":true}')
        self.write(self.main, [self.record('assistant', 'main', model='new-main', usage={'output_tokens': 3})])
        listing = subagents.listing(self.config)
        self.assertEqual(listing['main'], {'model': 'new-main', 'context': 3, 'title': None})
        self.assertEqual(listing['agents'][0]['state'], 'finished')
        self.assertEqual(listing['agents'][0]['tokens'], 99)
        self.assertEqual(subagents.detail(self.config, 'abc')['report'], 'New report')

    def test_cache_observes_mtime_with_same_size_and_size_with_same_mtime(self):
        path = self.agent(records=[self.record('assistant', 'one')])
        subagents.detail(self.config, 'abc')
        info = path.stat()
        path.write_text(path.read_text().replace('one', 'two'))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        self.assertEqual(subagents.detail(self.config, 'abc')['steps'][0]['text'], 'two')
        info = path.stat()
        with path.open('a') as stream:
            stream.write(json.dumps(self.record('assistant', 'three')) + '\n')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(subagents.detail(self.config, 'abc')['steps'][-1]['text'], 'three')

    def test_cached_state_and_duration_age_without_file_changes(self):
        self.agent(age=899)
        self.assertEqual(subagents.detail(self.config, 'abc')['state'], 'running')
        with patch.object(subagents.time, 'time', return_value=self.now + 2):
            entry = subagents.detail(self.config, 'abc')
            self.assertEqual(entry['state'], 'running')
            self.assertEqual(entry['seconds'], 122)

    def test_cache_is_bounded_and_response_mutation_does_not_change_it(self):
        self.agent()
        detail = subagents.detail(self.config, 'abc')
        detail['steps'][0]['text'] = 'changed'
        self.assertEqual(subagents.detail(self.config, 'abc')['steps'][0]['text'], 'Work starts')
        with patch.object(subagents, 'CACHE_LIMIT', 3):
            for index in range(10):
                self.agent(str(index))
            subagents.listing(self.config)
            self.assertLessEqual(len(subagents._cache), 3)

    def test_routes_select_session_and_require_auth(self):
        self.agent()
        for route in ('subagents', 'subagent?id=abc', 'subagent-tool?agent=abc&id=read-1'):
            with self.subTest(route=route):
                separator = '&' if '?' in route else '?'
                handler = object.__new__(server.Handler)
                handler.path = '/mobile/api/' + route + separator + 'session=%2512'
                handler.deny = Mock(return_value=False)
                handler.respond = Mock()
                with patch.object(server, 'choose', return_value=self.config) as choose:
                    handler.do_GET()
                    choose.assert_called_once_with('%12')
                self.assertEqual(handler.respond.call_args.args[0], 200)
                handler.deny.return_value = True
                with patch.object(server, 'choose') as choose:
                    handler.do_GET()
                    choose.assert_not_called()

    def test_routes_handle_value_errors_like_tool_route(self):
        for route in ('tool?id=missing', 'subagent?id=../x', 'subagent-tool?agent=../x&id=call'):
            handler = object.__new__(server.Handler)
            handler.path = '/mobile/api/' + route
            handler.deny = Mock(return_value=False)
            handler.respond = Mock()
            with patch.object(server, 'choose', return_value=self.config):
                handler.do_GET()
            handler.respond.assert_called_once_with(503, {'error': 'The conversation is currently unavailable.'})


class ProcessTasksTests(unittest.TestCase):
    stamp = SubagentTests.stamp
    record = SubagentTests.record
    write = SubagentTests.write
    agent = SubagentTests.agent
    notification = SubagentTests.notification

    def setUp(self):
        SubagentTests.setUp(self)
        self.old = '11111111-1111-1111-1111-111111111111'
        self.new = '22222222-2222-2222-2222-222222222222'
        self.project = self.root / 'projects' / 'project'
        self.folder = self.project / self.new / 'subagents'
        self.folder.mkdir(parents=True)
        self.old_folder = self.project / self.old / 'subagents'
        self.old_folder.mkdir(parents=True)
        self.main = self.project / f'{self.new}.jsonl'
        self.first = self.project / f'{self.old}.jsonl'
        self.write(self.main, [])
        self.write(self.first, [])
        self.config.update(transcript=str(self.main), thread=self.new)
        self.tmp = self.root / 'claude-tmp'
        self.tasks = self.tmp / 'cwd-slug' / self.old / 'tasks'
        self.tasks.mkdir(parents=True)
        self.enterContext(patch.object(subagents, 'TASKS_TMP', self.tmp))
        self.enterContext(patch.object(subagents, 'PROJECTS', self.project.parent))
        self.fds = self.enterContext(patch.object(subagents, '_fd_links', return_value=[self.tasks]))

    def old_agent(self, identifier='abc', *, meta=None):
        path = self.old_folder / f'agent-{identifier}.jsonl'
        self.write(path, [self.record('user', 'Original prompt', seconds=-200),
                          self.record('assistant', 'Old step', seconds=-190)])
        if meta is not None:
            path.with_suffix('.meta.json').write_text(json.dumps(meta))
        (self.tasks / f'{identifier}.output').symlink_to(path)
        return path

    def task_start(self, identifier, *, name='Bash', command='sleep 600', description='Wait', seconds=-100):
        tool_id = 'tool-' + identifier
        call = self.record('assistant', [{'type': 'tool_use', 'id': tool_id, 'name': name,
                                         'input': {'command': command, 'description': description}}], seconds=seconds)
        result = self.record('user', [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': 'background'}], seconds=seconds)
        result['toolUseResult'] = {('taskId' if name == 'Monitor' else 'backgroundTaskId'): identifier}
        return [call, result]

    def output(self, identifier, text='', seconds=-20):
        path = self.tasks / f'{identifier}.output'
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        os.utime(path, (self.now + seconds, self.now + seconds))
        return path

    def listed_tasks(self):
        return {item['id']: item for item in subagents.listing(self.config)['tasks']}

    def states(self):
        return {item['id']: item['state'] for item in subagents.listing(self.config)['agents']}

    def test_dead_process_background_agent_without_link_in_owner_folder_is_stopped(self):
        # An older process that /resumes a session: its fd folder is its first session's tasks folder.
        # The dead process linked its agent elsewhere, so the agent cannot belong to this process.
        self.agent('dead', meta={'description': 'Dead wait', 'requestShape': 'background'})
        listed = {item['id']: item for item in subagents.listing(self.config)['agents']}
        self.assertEqual(listed['dead']['state'], 'stopped')
        self.assertEqual(listed['dead']['summary'], 'Claude exited before this agent finished.')

    def test_owned_background_agent_with_link_runs(self):
        path = self.agent('mine', meta={'description': 'Mine', 'requestShape': 'background'})
        (self.tasks / 'mine.output').symlink_to(path)
        self.assertEqual(self.states()['mine'], 'running')

    def test_new_background_agent_gets_grace_before_its_link(self):
        self.agent('new', meta={'requestShape': 'background'}, records=[
            self.record('user', 'Fresh prompt', seconds=-subagents.LINK_GRACE + 2)])
        self.assertEqual(self.states()['new'], 'running')

    def test_foreground_agent_needs_no_link(self):
        self.agent('front', meta={'description': 'Front'})
        self.assertEqual(self.states()['front'], 'running')

    def test_glob_fallback_keeps_time_rule(self):
        self.fds.return_value = []
        fallback = self.tmp / 'cwd-slug' / self.new / 'tasks'
        fallback.mkdir(parents=True)
        self.agent('dead', meta={'requestShape': 'background'})
        self.assertEqual(self.states()['dead'], 'running')

    def test_monitor_without_output_file_takes_state_from_its_process(self):
        self.write(self.main, self.task_start('monitor1', name='Monitor', command='tail -f log', description='Watch log'))
        with patch.object(subagents.task_process, 'find', return_value=777) as find:
            task = self.listed_tasks()['monitor1']
        self.assertEqual((task['kind'], task['state'], task['description']), ('monitor', 'running', 'Watch log'))
        self.assertEqual(find.call_args.args[2], None)
        with patch.object(subagents.task_process, 'find', return_value=None):
            self.assertEqual(self.listed_tasks()['monitor1']['state'], 'finished')
            subagents.task_process.record(subagents.task_process.key(self.tasks.resolve(), 'monitor1'))
            task = self.listed_tasks()['monitor1']
        self.assertEqual((task['state'], task['summary']), ('stopped', 'Stopped from Tether.'))

    def test_monitor_from_dead_process_is_stopped_without_a_process_scan(self):
        self.write(self.main, self.task_start('monitor1', name='Monitor', command='tail -f log', seconds=-400))
        with patch.object(subagents.task_process, 'find') as find:
            task = self.listed_tasks()['monitor1']
        find.assert_not_called()
        self.assertEqual(task['state'], 'stopped')

    def test_shell_killed_by_tether_reads_stopped_not_failed(self):
        self.output('shell01', 'out\n[exited with code 143]')
        self.write(self.main, self.task_start('shell01'))
        self.assertEqual(self.listed_tasks()['shell01']['state'], 'failed')
        subagents.task_process.record(subagents.task_process.key(self.tasks.resolve(), 'shell01'))
        task = self.listed_tasks()['shell01']
        self.assertEqual((task['state'], task['summary']), ('stopped', 'Stopped from Tether.'))

    def test_link_to_new_session_before_any_write_finds_middle_session_lines(self):
        # Seen live: /clear points the link at the new session at once; the agent's lines stay in the
        # session between the first and the current one.
        middle = self.project / '33333333-3333-3333-3333-333333333333' / 'subagents'
        middle.mkdir(parents=True)
        path = middle / 'agent-mid.jsonl'
        self.write(path, [self.record('user', 'Middle prompt', seconds=-200),
                          self.record('assistant', 'Middle step', seconds=-190)])
        path.with_suffix('.meta.json').write_text(json.dumps({'description': 'Middle wait'}))
        (self.tasks / 'mid.output').symlink_to(self.project / self.new / 'subagents' / 'agent-mid.jsonl')
        agents = {item['id']: item for item in subagents.listing(self.config)['agents']}
        self.assertEqual(agents['mid']['state'], 'running')
        self.assertEqual(agents['mid']['description'], 'Middle wait')
        self.assertEqual(subagents.detail(self.config, 'mid')['prompt'], 'Middle prompt')

    def test_silent_old_agent_survives_clear_and_can_be_opened(self):
        path = self.old_agent(meta={'description': 'Wait'})
        self.assertEqual([item['id'] for item in subagents.listing(self.config)['agents']], ['abc'])
        self.assertEqual(subagents.detail(self.config, 'abc')['prompt'], 'Original prompt')
        self.assertEqual(subagents._path(self.config, 'abc'), path)
        # A new chat need not have its own subagents folder yet.
        self.folder.rmdir()
        self.assertEqual(subagents.detail(self.config, 'abc')['state'], 'running')

    def test_split_agent_combines_prompt_steps_usage_meta_and_tool_results(self):
        old = self.old_agent(meta={'description': 'Old description', 'model': 'old'})
        self.write(old, [self.record('user', 'Original prompt', seconds=-200), self.record('assistant', [
            {'type': 'text', 'text': 'Old step'},
            {'type': 'tool_use', 'id': 'read-old', 'name': 'Read', 'input': {'file_path': 'a.py'}}], seconds=-190)])
        new = self.agent(records=[self.record('user', [
            {'type': 'tool_result', 'tool_use_id': 'read-old', 'content': 'Read result'}]),
            self.record('assistant', 'New step', usage={'input_tokens': 25})],
            meta={'description': 'New description'})
        (self.tasks / 'abc.output').unlink()
        (self.tasks / 'abc.output').symlink_to(new)
        detail = subagents.detail(self.config, 'abc')
        self.assertEqual(detail['prompt'], 'Original prompt')
        self.assertEqual([step.get('text') for step in detail['steps'] if step['kind'] == 'text'], ['Old step', 'New step'])
        self.assertEqual(detail['description'], 'New description')
        self.assertEqual(detail['tokens'], 25)
        self.assertEqual(detail['tools'], 1)
        self.assertEqual(detail['steps'][1]['status'], 'complete')
        self.assertEqual(subagents.tool(self.config, 'abc', 'read-old')['status'], 'complete')

    def test_current_agent_joins_first_session_file_without_output_symlink(self):
        self.old_agent()
        (self.tasks / 'abc.output').unlink()
        self.agent(records=[self.record('assistant', 'Current step')])
        detail = subagents.detail(self.config, 'abc')
        self.assertEqual(detail['prompt'], 'Original prompt')
        self.assertEqual([step['text'] for step in detail['steps']], ['Old step', 'Current step'])

    def test_finished_old_agent_is_excluded_but_current_finished_is_kept(self):
        self.old_agent(meta={'finished': True})
        self.agent('current', meta={'finished': True})
        self.assertEqual([item['id'] for item in subagents.listing(self.config)['agents']], ['current'])
        with self.assertRaises(ValueError):
            subagents.detail(self.config, 'abc')

    def test_old_agent_terminal_notification_excludes_it(self):
        self.old_agent()
        self.write(self.main, [self.record('user', self.notification('abc', 'killed'))])
        self.assertEqual(subagents.listing(self.config)['agents'], [])

    def test_agent_symlink_must_match_id_and_projects_layout(self):
        old = self.old_agent()
        (self.tasks / 'abc.output').unlink()
        (self.tasks / 'other.output').symlink_to(old)
        outside = self.root / 'agent-escape.jsonl'
        self.write(outside, [self.record('user', 'private')])
        (self.tasks / 'escape.output').symlink_to(outside)
        self.assertEqual(subagents.listing(self.config)['agents'], [])

    def test_task_footers_and_live_owner(self):
        records = []
        cases = [('shell00', '[exited with code 0]', 'finished'),
                 ('shell02', '[exited with code 2]', 'failed'),
                 ('shell03', '[killed]', 'stopped'), ('shell04', '', 'running')]
        for identifier, footer, _ in cases:
            records.extend(self.task_start(identifier))
            self.output(identifier, 'output\n' + footer + '\n')
        self.write(self.main, records)
        tasks = self.listed_tasks()
        for identifier, footer, state in cases:
            self.assertEqual(tasks[identifier]['state'], state)
            self.assertEqual(tasks[identifier]['seconds'], 80 if footer else 100)
            self.assertEqual(tasks[identifier]['ended'], self.stamp(-20) if footer else None)
            self.assertEqual(tasks[identifier]['summary'], footer if state in ('failed', 'stopped') else None)
        self.owner_start.return_value = 'dead'
        self.assertEqual(self.listed_tasks(), {})
        self.assertIsNone(subagents._tasks_dir(self.config))

    def test_footer_precedes_notification_and_notification_is_secondary(self):
        self.output('shell01', '[killed]')
        self.output('shell02', '')
        self.write(self.main, self.task_start('shell01') + self.task_start('shell02') + [
            self.record('user', self.notification('shell01', 'completed')),
            self.record('user', self.notification('shell02', 'failed', 'Notification failure'))])
        tasks = self.listed_tasks()
        self.assertEqual(tasks['shell01']['state'], 'stopped')
        self.assertEqual(tasks['shell01']['summary'], '[killed]')
        self.assertEqual(tasks['shell02']['state'], 'failed')
        self.assertEqual(tasks['shell02']['summary'], 'Notification failure')

    def test_monitor_in_old_running_agent_has_parent_and_kind(self):
        old = self.old_agent()
        with old.open('a') as stream:
            stream.write(''.join(json.dumps(record) + '\n' for record in self.task_start(
                'monitor1', name='Monitor', description='Watch events', command='tail -f events')))
        self.output('monitor1', '[killed]')
        task = self.listed_tasks()['monitor1']
        self.assertEqual((task['kind'], task['parent'], task['description'], task['command']),
                         ('monitor', 'abc', 'Watch events', 'tail -f events'))

    def test_foreground_call_before_clear_becomes_task_after_clear(self):
        old = self.old_agent()
        new = self.agent(records=[self.record('assistant', 'New session')])
        call, start = self.task_start('split01', description='Moved command', command='sleep 900')
        with old.open('a') as stream:
            stream.write(json.dumps(call) + '\n')
        with new.open('a') as stream:
            stream.write(json.dumps(start) + '\n')
        self.output('split01', '[exited with code 0]')
        task = self.listed_tasks()['split01']
        self.assertEqual(task['command'], 'sleep 900')
        self.assertEqual(task['description'], 'Moved command')
        self.assertEqual(task['parent'], 'abc')

    def test_earlier_chat_keeps_only_running_tasks_and_skips_foreground_files(self):
        self.write(self.first, self.task_start('older01') + self.task_start('older02'))
        self.output('older01', '[exited with code 0]')
        self.output('older02')
        # Output files with no start record are foreground Bash calls, not background tasks.
        self.output('unknown1')
        self.output('unknown2', '[killed]')
        tasks = self.listed_tasks()
        self.assertEqual(set(tasks), {'older02'})
        self.assertEqual(tasks['older02']['command'], 'sleep 600')

    def test_project_scan_requires_recent_mtime_and_tasks_dir_reference(self):
        third = self.project / '33333333-3333-3333-3333-333333333333.jsonl'
        unrelated = self.project / '44444444-4444-4444-4444-444444444444.jsonl'
        ancient = self.project / '55555555-5555-5555-5555-555555555555.jsonl'
        self.write(third, [self.record('user', str(self.tasks))] + self.task_start('third01'))
        self.write(unrelated, self.task_start('other01'))
        self.write(ancient, [self.record('user', str(self.tasks))] + self.task_start('oldest1'))
        for path in (third, unrelated):
            os.utime(path, (self.now, self.now))
        os.utime(ancient, (self.now - 1000, self.now - 1000))
        for identifier in ('third01', 'other01', 'oldest1'):
            self.output(identifier)
        original = Path.open
        def read(path, *args, **kwargs):
            self.assertNotEqual(path, ancient, 'An older unrelated transcript must never be opened')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', read):
            tasks = self.listed_tasks()
        self.assertEqual(tasks['third01']['command'], 'sleep 600')
        # Starts in unrelated or older transcripts are not read, so those files are not tasks.
        self.assertNotIn('other01', tasks)
        self.assertNotIn('oldest1', tasks)

    def test_tabs_of_one_project_share_one_task_scan_per_transcript(self):
        # B65: the scan cache was keyed by each tab's tasks folder, so every tab reread every recent
        # transcript in the project. Now one scan per file serves all tabs, and the mention check
        # still keeps each tab to its own folder.
        other = self.tmp / 'cwd-slug' / '66666666-6666-6666-6666-666666666666' / 'tasks'
        other.mkdir(parents=True)
        mine = self.project / '33333333-3333-3333-3333-333333333333.jsonl'
        theirs = self.project / '44444444-4444-4444-4444-444444444444.jsonl'
        self.write(mine, [self.record('user', f'see file://{self.tasks}/mine01.output')] + self.task_start('mine01'))
        self.write(theirs, [self.record('user', f'cat "{other}/their01.output"')] + self.task_start('their01'))
        for path in (mine, theirs):
            os.utime(path, (self.now, self.now))
        self.output('mine01')
        (other / 'their01.output').write_text('')
        self.assertIn('mine01', self.listed_tasks())
        self.assertNotIn('their01', self.listed_tasks())
        original = Path.open
        def read(path, *args, **kwargs):
            self.assertNotIn(path, (mine, theirs), 'A second tab must reuse the scan of an unchanged transcript')
            return original(path, *args, **kwargs)
        self.fds.return_value = [other]
        subagents._tasks_cache.clear()
        with patch.object(Path, 'open', read):
            tasks = {item['id'] for item in subagents.listing(self.config)['tasks']}
        self.assertIn('their01', tasks)
        self.assertNotIn('mine01', tasks)
        self.assertEqual(sum(1 for key in subagents._cache if isinstance(key, tuple) and key[0] == str(theirs)), 1)

    def test_task_refs_keep_only_task_folders_under_root_including_spaces(self):
        refs, root = set(), '/r/temp files'
        folder = root + '/my slug/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/tasks'
        subagents._task_refs(f'src/tasks, {root}/x/not-a-uuid/tasks, {root}/tasks, file://{folder}/x.output'
                             f' and "{folder}"', refs, root)
        self.assertEqual(refs, {folder})

    def test_tasks_root_with_spaces_still_matches_quoted_mentions(self):
        spaced = self.root / 'temp files'
        tasks = spaced / 'cwd slug' / self.old / 'tasks'
        tasks.mkdir(parents=True)
        self.enterContext(patch.object(subagents, 'TASKS_TMP', spaced))
        self.fds.return_value = [tasks]
        subagents._tasks_cache.clear()
        third = self.project / '33333333-3333-3333-3333-333333333333.jsonl'
        self.write(third, [self.record('user', f'cat "{tasks}/third01.output"')] + self.task_start('third01'))
        os.utime(third, (self.now, self.now))
        (tasks / 'third01.output').write_text('')
        self.assertIn('third01', self.listed_tasks())

    def test_fd_cache_expires_and_glob_fallback_validates_uuid(self):
        self.assertEqual(subagents._tasks_dir(self.config), self.tasks)
        self.assertEqual(subagents._tasks_dir(self.config), self.tasks)
        self.fds.assert_called_once()
        fallback = self.tmp / 'cwd-slug' / self.new / 'tasks'
        fallback.mkdir(parents=True)
        self.fds.return_value = []
        with patch.object(subagents.time, 'monotonic', return_value=subagents.time.monotonic() + 31):
            self.assertEqual(subagents._tasks_dir(self.config), fallback)
        subagents._tasks_cache.clear()
        self.assertIsNone(subagents._tasks_dir(dict(self.config, thread='../*')))

    def test_fd_directory_must_have_valid_layout_and_owned_root(self):
        outside = self.root / 'outside' / self.old / 'tasks'
        outside.mkdir(parents=True)
        self.fds.return_value = [outside, self.tasks.parent]
        self.assertIsNone(subagents._tasks_dir(self.config))
        subagents._tasks_cache.clear()
        with patch.object(subagents.os, 'getuid', return_value=-1):
            self.assertIsNone(subagents._tasks_dir(self.config))

    def test_task_output_tail_cut_and_escape_refusals(self):
        self.write(self.main, self.task_start('shell01') + self.task_start('short01'))
        self.output('shell01', b'a' * (64 * 1024) + b'\xfftail\n')
        detail = subagents.task(self.config, 'shell01')
        self.assertTrue(detail['output_cut'])
        self.assertTrue(detail['output'].endswith('�tail\n'))
        self.output('short01', 'short')
        self.assertFalse(subagents.task(self.config, 'short01')['output_cut'])
        (self.tasks / 'linked1.output').symlink_to(self.tasks / 'short01.output')
        for identifier in ('../shell01', '', 'linked1', 'unknown', None):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, subagents._UNAVAILABLE):
                subagents.task(self.config, identifier)

    def test_task_cache_reuses_reads_and_handles_append_partial_and_rewrite(self):
        self.output('shell01')
        records = self.task_start('shell01')
        self.write(self.main, records[:1])
        self.assertNotIn('shell01', self.listed_tasks())
        text = json.dumps(records[1])
        with self.main.open('a') as stream:
            stream.write(text[:40])
        self.assertNotIn('shell01', self.listed_tasks())
        with self.main.open('a') as stream:
            stream.write(text[40:] + '\n')
        self.assertEqual(self.listed_tasks()['shell01']['command'], 'sleep 600')
        with patch.object(Path, 'open', side_effect=AssertionError('Unchanged files must stay cached')):
            self.assertEqual(self.listed_tasks()['shell01']['command'], 'sleep 600')
        self.write(self.main, [])
        self.assertNotIn('shell01', self.listed_tasks())

    def test_tasks_are_bounded_oldest_first_and_command_is_bounded(self):
        records = []
        for index in range(43):
            identifier = f'task{index:03}'
            records.extend(self.task_start(identifier, seconds=-200 + index, command='x' * 2100))
            self.output(identifier)
        self.write(self.main, records)
        tasks = list(self.listed_tasks().values())
        self.assertEqual([item['id'] for item in tasks], [f'task{index:03}' for index in range(3, 43)])
        self.assertEqual(len(tasks[0]['command']), 2000)

    def test_task_route_selects_config_requires_auth_and_handles_unknown(self):
        self.write(self.main, self.task_start('shell01'))
        self.output('shell01', 'hello')
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/subagent-task?session=%2512&id=shell01'
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        with patch.object(server, 'choose', return_value=self.config) as choose:
            handler.do_GET()
            choose.assert_called_once_with('%12')
        self.assertEqual(handler.respond.call_args.args[1]['output'], 'hello')
        handler.deny.return_value = True
        with patch.object(server, 'choose') as choose:
            handler.do_GET()
            choose.assert_not_called()
        handler.deny.return_value = False
        handler.path = '/mobile/api/subagent-task?id=../bad'
        with patch.object(server, 'choose', return_value=self.config):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args, (503, {'error': 'The conversation is currently unavailable.'}))


if __name__ == '__main__':
    unittest.main()
