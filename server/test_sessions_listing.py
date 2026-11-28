"""Session list sharing and ETag tests: mocked discovery, no tmux, no terminals."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import paths
import server
import terminal_input
from own_time import own_time


class ListingParentsTests(unittest.TestCase):
    def setUp(self):
        thread = '0f8fad5b-d9cb-469f-a165-70867728950e'
        self.entries = [
            {'id': '%1', 'agent': 'claude', 'thread': thread, 'agent_name': 'Current opener'},
            {'id': '%2', 'agent': 'codex'},
            {'id': '%3', 'agent': 'shell'},
            {'id': '%4', 'agent': 'antigravity', 'thread': '1f8fad5b-d9cb-469f-a165-70867728950e',
             'pending_conversation': True},
            {'id': '%5', 'agent': 'claude'},
        ]
        self.record = {'owner_pane': '%1', 'owner_thread': thread, 'sent_at': 1000}
        self.run = self.enterContext(patch.object(server.subprocess, 'run'))
        self.enterContext(patch.object(server, 'sessions', return_value=self.entries))
        self.enterContext(patch.object(server, 'session_row', side_effect=lambda entry: {'id': entry['id']}))
        self.enterContext(patch.object(server, 'settings', return_value={'thread': None}))
        self.enterContext(patch.object(server.groups, 'all_groups', return_value={}))
        self.enterContext(patch.object(server.auto_resume, 'notice', return_value={'failed': False}))
        self.enterContext(patch.object(server.review_links, 'reviewed_by', return_value={'%1': ['%5']}))
        self.enterContext(patch.object(server.review_links, 'paused', return_value={}))
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.review_file = Path(temp) / 'review-links.json'
        self.enterContext(patch.object(server.review_links, 'FILE', self.review_file))

    def listing(self):
        return {row['id']: row for row in server.build_listing()['sessions']}

    def tmux_rows(self, rows, returncode=0, *, roles=None, grouping=None):
        roles, grouping = roles or {}, grouping or {}
        self.run.return_value = subprocess.CompletedProcess(
            [], returncode, stdout=''.join(f'{pane}\t{grouping.get(pane, "")}\t{roles.get(pane, "")}\t{raw}\t$1\t\t\t\n'
                                          for pane, raw in rows))

    def test_valid_parent_records_use_one_tmux_call(self):
        self.tmux_rows([('%2', json.dumps(self.record)), ('%5', json.dumps(dict(self.record, owner_pane='%2')))])
        rows = self.listing()
        self.assertEqual(rows['%2']['parent'], '%1')
        self.assertEqual(rows['%5']['parent'], '%2')
        self.assertEqual(rows['%1']['reviewers'], ['%5'])
        self.assertNotIn('parent', rows['%1'])
        self.run.assert_called_once_with(
            [paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}\t#{@group_by}\t#{@tether_role}\t#{@tether_helper}\t#{session_id}\t#{@tether_done}\t#{@tether_reopened}\t#{@tether_done_at}'],
            capture_output=True, text=True, timeout=5)

    def test_saved_review_grant_is_listed_for_current_conversation_only(self):
        self.tmux_rows([])
        thread = self.entries[0]['thread']
        caps = [dict(cap=True, worker='%1', worker_thread=value, saved_at=0, paused=True,
                     counts={'%2:': {'rounds': rounds}})
                for value, rounds in ((thread, 3), ('17b9c72a-a916-46a0-bc10-bc41a8c6249a', 4))]
        self.review_file.write_text(json.dumps(caps))
        rows = self.listing()
        self.assertEqual(rows['%1']['review_grant'],
                         {'rounds': 3, 'limit': 3, 'token': ':0:3', 'thread': thread})
        self.assertNotIn('review_grant', rows['%2'])
        with patch.object(server.review_links, 'grantable', side_effect=OSError('store unreadable')):
            self.assertNotIn('review_grant', self.listing()['%1'])

    def test_invalid_parent_records_are_ignored(self):
        for raw in ('{', '[]', json.dumps(dict(self.record, sent_at=True)),
                    json.dumps(dict(self.record, owner_pane='2')), json.dumps(dict(self.record, owner_pane='%99')),
                    json.dumps(dict(self.record, owner_pane='%3')), json.dumps(dict(self.record, owner_pane='%2')),
                    json.dumps(dict(self.record, owner_thread='1f8fad5b-d9cb-469f-a165-70867728950e'))):
            with self.subTest(raw=raw):
                self.tmux_rows([('%2', raw), ('%5', json.dumps(self.record))])
                rows = self.listing()
                self.assertNotIn('parent', rows['%2'])
                self.assertEqual(rows['%5']['parent'], '%1')
        self.entries.append({'id': '5', 'agent': 'claude'})
        self.tmux_rows([('5', json.dumps(self.record))])
        self.assertNotIn('parent', self.listing()['5'])

    def test_unknown_owner_thread_keeps_parent(self):
        for changes in ({'owner_thread': None}, {'owner_pane': '%4'}, {'owner_pane': '%5'}):
            with self.subTest(changes=changes):
                record = dict(self.record, **changes)
                self.tmux_rows([('%2', json.dumps(record))])
                row = self.listing()['%2']
                self.assertEqual(row['parent'], record['owner_pane'])
                self.assertEqual(row['owner'], row['parent'])
                self.assertTrue(row['opened_by']['live'])

    def test_lists_placement_and_opener_separately_when_opener_leaves(self):
        home_thread = '1f8fad5b-d9cb-469f-a165-70867728950e'
        home = self.entries[-1]
        home['thread'] = home_thread
        record = dict(self.record, kind='helper', home_pane='%5', home_thread=home_thread, opener_name='Old opener')
        self.tmux_rows([('%2', json.dumps(record))])
        row = self.listing()['%2']
        self.assertEqual(row, {'id': '%2', 'parent': '%1', 'owner': '%5', 'helper': True, 'reviewers_paused': [],
                               'task': '%5', 'task_depth': 1, 'task_role': 'Worker',
                               'opened_by': {'pane': '%1', 'name': 'Current opener', 'live': True}})
        self.entries[0]['thread'] = home_thread
        for opener_present in (True, False):
            with self.subTest(opener_present=opener_present):
                if not opener_present:
                    self.entries.pop(0)
                row = self.listing()['%2']
                self.assertNotIn('parent', row)
                self.assertEqual(row['owner'], '%5')
                self.assertEqual(row['opened_by'], {'pane': '%1', 'name': 'Old opener', 'live': False})
        for changes in ({'thread': self.record['owner_thread']}, {'thread': None},
                        {'thread': home_thread, 'agent': 'shell'}):
            with self.subTest(changes=changes):
                home.update(changes)
                self.assertNotIn('owner', self.listing()['%2'])
        self.assertNotIn('opened_by', self.listing()['%5'])

    def test_helper_filter_requires_valid_record_role_or_kind_and_no_manual_placement(self):
        helper = dict(self.record, kind='helper', home_pane='%1', home_thread=self.record['owner_thread'])
        for record, role, group_by, expected in (
                (self.record, '', '', False), (self.record, 'reviewer', '', True),
                (self.record, 'advisor', '', True), (self.record, 'second-advisor', '', True),
                (self.record, 'advisor', 'user', False), (helper, '', '', True),
                (helper, '', 'user', False), (dict(self.record, kind='own', home_pane=None), '', '', False),
                (dict(self.record, sent_at=True), 'reviewer', '', False)):
            with self.subTest(record=record, role=role, group_by=group_by):
                self.tmux_rows([('%2', json.dumps(record))], roles={'%2': role}, grouping={'%2': group_by})
                row = self.listing()['%2']
                self.assertEqual(row.get('helper'), True if expected else None)
                if record.get('kind') == 'own':
                    self.assertNotIn('owner', row)
                    self.assertEqual(row['parent'], '%1')

    def test_invalid_new_values_are_ignored_and_legacy_record_still_applies(self):
        record = dict(self.record, kind=['helper'], home_pane='5', home_thread=17, opener_name='A' * 41)
        raw = json.dumps(record)
        self.assertEqual(server.helper_tabs._record(raw), self.record)
        self.entries.pop(0)
        self.tmux_rows([('%2', raw)], roles={'%2': 'reviewer'})
        row = self.listing()['%2']
        self.assertEqual(row['opened_by'], {'pane': '%1', 'name': None, 'live': False})
        self.assertEqual(row['helper'], True)

    def test_escalated_only_for_the_current_worker_whose_review_waits_on_mak(self):
        self.tmux_rows([])
        cap = {'cap': True, 'worker': '%1', 'worker_thread': self.record['owner_thread'], 'escalate': 'Review needed.'}
        stale = dict(cap, worker='%2', worker_thread='1f8fad5b-d9cb-469f-a165-70867728950e')
        parent_decides = {'cap': True, 'worker': '%4', 'paused': True, 'parent_stage': {'pane': '%3', 'thread': None, 'notice': 'Open findings.'}}
        waiting = {'cap': True, 'worker': '%5', 'paused': True}  # the alert went out; Mak has not answered yet
        self.review_file.write_text(json.dumps([cap, stale, parent_decides, waiting]))
        rows = self.listing()
        self.assertIs(rows['%1']['escalated'], True)
        self.assertNotIn('escalated', rows['%2'])
        self.assertNotIn('escalated', rows['%4'])
        self.assertIs(rows['%5']['escalated'], True)
        with patch.object(server.review_links, '_read', side_effect=OSError('Unreadable')):
            self.assertNotIn('escalated', self.listing()['%1'])
            self.assertEqual(self.listing()['%1']['reviewers'], ['%5'])

    def test_tmux_read_error_keeps_listing_and_reviewers(self):
        self.tmux_rows([('%2', json.dumps(self.record))], returncode=1)
        for error in (None, subprocess.TimeoutExpired([], 5), OSError()):
            with self.subTest(error=error):
                self.run.side_effect = error
                rows = self.listing()
                self.assertEqual(list(rows), ['%1', '%2', '%3', '%4', '%5'])
                self.assertEqual(rows['%1']['reviewers'], ['%5'])
                self.assertNotIn('parent', rows['%2'])


class SessionContextTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(server.agents, 'METRICS', self.folder))
        self.enterContext(patch.object(server.antigravity, 'CACHE', self.folder))
        self.enterContext(patch.object(server.antigravity.procinfo, 'start_ms', return_value=1000))
        self.status = self.enterContext(patch.object(server, 'shared_status', return_value={'state': 'Idle'}))
        self.enterContext(patch.object(server, 'tab_folder', return_value=''))
        self.enterContext(patch.object(server.tab_brief.STORE, 'get', return_value=None))
        self.enterContext(patch.object(server.attention.STORE, 'ask', return_value=None))
        self.claude = {'id': '%1', 'agent': 'claude', 'thread': 'claude-thread'}
        self.agy = {'id': '%2', 'agent': 'antigravity', 'thread': 'agy-thread', 'process_start': '100'}
        self.rollout = self.folder / 'codex.jsonl'
        self.codex = {'id': '%3', 'agent': 'codex', 'thread': 'codex-thread',
                      'codex_binding': 'open', 'transcript': str(self.rollout)}

    def report(self, entry, percent, **changes):
        thread = entry['thread']
        report = {'conversation_id': thread, 'reported_at': 2,
                  'context_window': {'used_percentage': percent}, **changes}
        (self.folder / (thread + '.json')).write_text(json.dumps(report))

    def append_event(self, kind='token_count', **payload):
        with self.rollout.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': kind, **payload}}) + '\n')

    def append_context(self, tokens, limit=10000):
        self.append_event(info={'last_token_usage': {'total_tokens': tokens},
                                'total_token_usage': {'total_tokens': 1000000}, 'model_context_window': limit})

    def context(self, entry):
        return server.session_row(entry)['context_percent']

    def test_session_rows_report_each_agents_own_unrounded_percent(self):
        self.report(self.claude, 79.99)
        self.report(self.agy, 82.5)
        self.append_context(8000)
        rows = server.session_rows([self.claude, self.agy, self.codex])
        self.assertEqual([row['context_percent'] for row in rows], [79.99, 82.5, 80])

    def test_missing_stale_and_invalid_reports_do_not_warn(self):
        self.report(self.claude, 95)
        for changes in ({'thread': 'other-claude'}, {'pending_conversation': True}, {'agent': 'shell'}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.context(dict(self.claude, **changes)))
        for changes in ({'conversation_id': 'other-agy'}, {'reported_at': 0.5}, {'context_window': None}):
            with self.subTest(changes=changes):
                self.report(self.agy, 95, **changes)
                self.assertIsNone(self.context(self.agy))
        for percent in (None, True, float('nan'), float('inf'), '90'):
            with self.subTest(percent=percent):
                self.report(self.claude, percent)
                self.assertIsNone(self.context(self.claude))

    def test_codex_context_follows_the_rollout_and_reuses_the_cached_position(self):
        self.append_context(9000)
        self.append_event('task_complete')
        with self.rollout.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'output': 'x' * (300 * 1024)}}) + '\n')
        self.assertEqual(self.context(self.codex), 90)
        self.append_event(info=None)  # An account-only update keeps the preceding conversation usage.
        self.append_event(info={})
        with patch.object(terminal_input, '_rollout_read', wraps=terminal_input._rollout_read) as read:
            self.assertEqual(self.context(self.codex), 90)
            self.assertLess(sum(call.args[1] for call in read.call_args_list), 1024)
            read.reset_mock()
            self.assertEqual(self.context(self.codex), 90)
            read.assert_not_called()
        self.append_context(7999)
        self.assertAlmostEqual(self.context(self.codex), 79.99)
        self.append_context(9000, None)
        self.assertIsNone(self.context(self.codex))
        replacement = self.rollout.with_suffix('.new')
        replacement.write_text('')
        replacement.replace(self.rollout)
        self.assertIsNone(self.context(self.codex))

    def test_codex_suppresses_context_while_the_conversation_is_unconfirmed(self):
        self.append_context(9500)
        for changes in ({'codex_binding': 'guess'}, {'pending_conversation': True}, {'ambiguous': True}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.context(dict(self.codex, **changes)))
        self.status.return_value = {'state': 'Idle', '_codex_fresh': True}
        row = server.session_row(self.codex)
        self.assertIsNone(row['context_percent'])
        self.assertNotIn('_codex_fresh', row)


class SessionsListingTests(unittest.TestCase):
    def setUp(self):
        server.LISTING_CACHE.update(at=0.0, generation=None, body=b'', tag='')
        self.builds = 0
        self.rows = [{'id': '%1', 'state': 'Idle'}]

        def build():
            self.builds += 1
            return {'sessions': list(self.rows), 'default_thread': None}
        self.clock = [100.0]
        patches = [patch.object(server, 'build_listing', side_effect=build),
                   patch.object(server, 'discover', return_value=[]),
                   patch.object(server.window_size, 'apply'),
                   patch.object(server, 'settings', return_value={}),
                   own_time(server, monotonic=Mock(side_effect=lambda: self.clock[0]))]
        for item in patches:
            self.enterContext(item)
        server.forget_sessions()

    def test_pages_within_a_second_share_one_build(self):
        first = server.sessions_listing()
        self.clock[0] += 0.5
        self.assertEqual(server.sessions_listing(), first)
        self.assertEqual(self.builds, 1)
        self.assertEqual(json.loads(first[0])['sessions'], self.rows)
        self.assertTrue(first[1].startswith('W/"'))

    def test_new_list_after_a_second_or_a_tmux_write(self):
        server.sessions_listing()
        self.clock[0] += 1.1
        server.sessions_listing()
        self.assertEqual(self.builds, 2)
        self.clock[0] += 0.1
        server.forget_sessions()  # a tmux write: the next list must be new at once
        server.sessions_listing()
        self.assertEqual(self.builds, 3)

    def test_tag_changes_with_the_body(self):
        body, tag = server.sessions_listing()
        self.rows = [{'id': '%1', 'state': 'Working'}]
        self.clock[0] += 2
        body2, tag2 = server.sessions_listing()
        self.assertNotEqual(tag, tag2)
        self.rows = [{'id': '%1', 'state': 'Idle'}]
        self.clock[0] += 2
        self.assertEqual(server.sessions_listing(), (body, tag))

    def test_waiting_pages_share_a_build_in_progress(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            self.builds += 1
            started.set()
            gate.wait(2)
            return {'sessions': [], 'default_thread': None}
        with patch.object(server, 'build_listing', side_effect=slow):
            threads = [threading.Thread(target=server.sessions_listing) for _ in range(4)]
            for thread in threads:
                thread.start()
            started.wait(2)
            gate.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(self.builds, 1)

    def test_a_write_during_a_build_is_not_cached_as_new(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            self.builds += 1
            started.set()
            gate.wait(2)
            return {'sessions': [{'id': '%1', 'name': 'old'}], 'default_thread': None}
        with patch.object(server, 'build_listing', side_effect=slow):
            thread = threading.Thread(target=server.sessions_listing)
            thread.start()
            started.wait(2)
            server.forget_sessions()  # a rename lands while the old rows are being built
            gate.set()
            thread.join(2)
        self.rows = [{'id': '%1', 'name': 'new'}]
        body, _tag = server.sessions_listing()
        self.assertEqual(self.builds, 2)
        self.assertEqual(json.loads(body)['sessions'], self.rows)

    def test_a_slow_build_still_serves_the_next_second(self):
        def slow():
            self.builds += 1
            self.clock[0] += 1.2  # the build itself takes longer than a second
            return {'sessions': [], 'default_thread': None}
        with patch.object(server, 'build_listing', side_effect=slow):
            first = server.sessions_listing()
            self.clock[0] += 0.5
            self.assertEqual(server.sessions_listing(), first)
        self.assertEqual(self.builds, 1)

    def route(self, if_none_match=None):
        handler = server.Handler.__new__(server.Handler)
        handler.command, handler.path = 'GET', '/mobile/api/sessions'
        handler.headers = {'Accept-Encoding': 'gzip', 'Sec-Fetch-Site': 'same-origin'}
        if if_none_match:
            handler.headers['If-None-Match'] = if_none_match
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        handler.deny = Mock(return_value=False)
        handler.do_GET()
        return handler.send_response.call_args.args[0], dict(c.args for c in handler.send_header.call_args_list), handler.wfile.getvalue()

    def test_unchanged_list_answers_304_without_a_body(self):
        status, headers, body = self.route()
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        status, headers2, body2 = self.route(headers['ETag'])
        self.assertEqual((status, body2), (304, b''))
        self.assertNotIn('Content-Length', headers2)  # RFC 9110 8.6: only the 200's own length would be right
        self.assertEqual(headers2['ETag'], headers['ETag'])
        self.assertNotIn('Content-Encoding', headers2)

    def test_etag_matches(self):
        tag = 'W/"abc"'
        for header, expected in ((None, False), ('', False), ('W/"abc"', True), ('"abc"', True), ('"x", W/"abc"', True),
                                 ('*', True), ('W/"abd"', False), ('abc', False)):
            with self.subTest(header=header):
                self.assertEqual(server.etag_matches(header, tag), expected)


if __name__ == '__main__':
    unittest.main()
