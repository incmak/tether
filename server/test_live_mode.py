"""Live helpers use fake session rows and an injectable Claude process."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import hashlib
import io
import json
import os
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch

import live_mode
import server
from test_tts import handler


def row(identifier, state='Idle', kind=None, text='', **extra):
    return dict(id=identifier, label=identifier, agent='codex', state=state,
                needs={'kind': kind, 'text': text} if kind else None, **extra)


class BriefTests(unittest.TestCase):
    def test_urgent_first_and_at_most_four_sentences(self):
        rows = [row('idle'), row('build', 'Working'), row('plan', 'Approval', 'plan', 'Ship version 2'),
                row('question', 'Input needed', 'question', 'Which database?'),
                row('tether-bugs', 'Approval', 'approval', 'Bash · npm test')]
        result = live_mode.brief(rows)
        self.assertEqual([pane['id'] for pane in result['panes']], ['tether-bugs', 'plan', 'question', 'build', 'idle'])
        panes = {pane['id']: pane for pane in result['panes']}
        self.assertEqual(panes['tether-bugs']['line'], 'tether-bugs wants to run: Bash npm test')
        self.assertEqual(panes['question']['line'], 'question asks: Which database?')
        self.assertEqual(panes['plan']['line'], 'plan wants you to review the plan: Ship version 2')
        self.assertEqual(panes['build']['line'], 'build is working')
        self.assertEqual(panes['idle']['line'], '')
        self.assertEqual(result['speech'], 'Three sessions need you. tether-bugs wants to run: Bash npm test; '
                         'plan wants you to review the plan: Ship version 2. One is working. One is idle.')
        self.assertEqual(rows[0]['id'], 'idle')

    def test_count_wording_and_names(self):
        rows = [row('ask', 'Input needed', 'question', 'Which database?', pane_name='API', agent_name='ignored'),
                *[row(f'working-{at}', 'Working') for at in range(3)],
                *[row(f'idle-{at}') for at in range(5)]]
        result = live_mode.brief(rows)
        self.assertEqual(result['panes'][0]['name'], 'API')
        self.assertEqual(result['speech'], 'One session needs you. API asks: Which database. Three are working. Five are idle.')
        self.assertEqual(live_mode.brief([row('x', agent_name='Agent chat')])['panes'][0]['name'], 'Agent chat')

    def test_idle_and_empty_brief(self):
        self.assertEqual(live_mode.brief([row('x'), row('y')])['speech'], 'Two are idle.')
        self.assertEqual(live_mode.brief([]), {'panes': [], 'speech': 'No active sessions.'})
        self.assertNotIn('idle', live_mode.brief([row('x', 'Unavailable')])['speech'])

    def test_long_lines_and_many_waiting_sessions(self):
        rows = [row(str(at), 'Approval', 'approval', 'x' * 400) for at in range(20)]
        result = live_mode.brief(rows)
        self.assertTrue(result['speech'].startswith('20 sessions need you.'))
        self.assertLess(len(result['speech']), 400)
        self.assertLess(len(result['panes'][0]['line']), 220)
        self.assertEqual(len(result['panes']), 20)

    def test_route_reuses_session_rows_with_no_extra_terminal_calls(self):
        entries = [{'id': '%1'}, {'id': '%2'}]
        rows = [row('%1', 'Working'), row('%2')]
        value = handler('/mobile/api/live/brief', method='GET')
        with patch.object(server, 'sessions', return_value=entries) as sessions, \
                patch.object(server, 'session_row', side_effect=rows) as session_row, \
                patch('subprocess.run', side_effect=AssertionError('No terminal calls')):
            server.Handler.do_GET(value)
        sessions.assert_called_once_with()
        self.assertEqual([call.args[0] for call in session_row.call_args_list], entries)
        self.assertEqual(json.loads(value.wfile.getvalue())['speech'], 'One is working. One is idle.')


class SummaryTests(unittest.TestCase):
    def setUp(self):
        live_mode.CACHE.clear()
        self.addCleanup(live_mode.CACHE.clear)
        self.text = 'Tests pass for Tether version 2. There are 19 fixes. ' + 'More background detail. ' * 20
        self.urlopen = self.enterContext(patch('urllib.request.urlopen', side_effect=urllib.error.URLError('down')))

    def local_reply(self, content):
        self.urlopen.side_effect = None
        response = self.urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({'choices': [{'message': {'content': content}}]}).encode()

    def test_local_llm_success_uses_messages_options_url_and_cache(self):
        self.local_reply('Tether version 2 passes all 19 tests. Ready to ship.')
        run = Mock(side_effect=AssertionError('No Claude call'))
        with patch.dict(os.environ, TETHER_LLM_URL='http://127.0.0.1:7697/'):
            spoken = live_mode.summary(self.text, run=run)
        self.assertEqual(spoken, 'Tether version 2 passes all 19 tests. Ready to ship.')
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:7697/v1/chat/completions')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data), {
            'messages': [{'role': 'system', 'content': live_mode.PROMPT.split('\n\nAgent reply:', 1)[0]},
                         {'role': 'user', 'content': self.text}],
            'temperature': 0.2, 'max_tokens': 120, 'chat_template_kwargs': {'enable_thinking': False}})
        self.assertEqual(self.urlopen.call_args.kwargs['timeout'], 8)
        self.assertEqual(live_mode.summary(self.text, run=run), spoken)
        self.urlopen.assert_called_once()
        run.assert_not_called()

    def test_summary_strips_think_blocks_and_needs_you_from_every_source(self):
        output = '<think>Secret reasoning.\nMore thoughts.</think>\n**Tests pass.**\nnEeDs YoU: no\nReady to ship.\nNeeds you: yes'
        expected = 'Tests pass. Ready to ship.'
        run = Mock(return_value=Mock(returncode=0, stdout=output))
        self.assertEqual(live_mode.summary(output, run=run), expected)
        self.urlopen.assert_not_called()
        self.local_reply(output)
        self.assertEqual(live_mode.summary(self.text, run=run), expected)
        run.assert_not_called()
        live_mode.CACHE.clear()
        self.urlopen.side_effect = urllib.error.URLError('down')
        self.assertEqual(live_mode.summary(self.text, run=run), expected)
        run.assert_called_once()

    def test_bad_local_responses_fall_back_to_claude(self):
        run = Mock(return_value=Mock(returncode=0, stdout='Claude summary.'))
        for body in (b'not json', b'{}', b'{"choices":[]}', b'{"choices":[{"message":{"content":null}}]}'):
            live_mode.CACHE.clear()
            self.local_reply('')
            self.urlopen.return_value.__enter__.return_value.read.return_value = body
            self.assertEqual(live_mode.summary(self.text, run=run), 'Claude summary.')
        live_mode.CACHE.clear()
        self.local_reply('<think>Only thoughts.</think>\nNeeds you: no')
        self.assertEqual(live_mode.summary(self.text, run=run), 'Claude summary.')

    def test_short_plain_text_passes_through_without_claude(self):
        run = Mock(side_effect=AssertionError('No Claude call'))
        self.assertEqual(live_mode.summary('**Tether** passes all 19 tests. Ready to ship.', run=run),
                         'Tether passes all 19 tests. Ready to ship.')
        self.assertEqual(live_mode.summary('x' * 240, run=run), 'x' * 240)
        run.assert_not_called()
        self.urlopen.assert_not_called()

    def test_claude_success_uses_pane_icons_flags_and_environment(self):
        run = Mock(return_value=Mock(returncode=0, stdout='Tether version 2 passes all 19 tests. The fixes are ready.'))
        with patch.dict(os.environ, TMUX='private', TMUX_PANE='%9'):
            spoken = live_mode.summary(self.text, pane='%9', run=run)
        self.assertEqual(spoken, 'Tether version 2 passes all 19 tests. The fixes are ready.')
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['claude', '-p'])
        self.assertIn(self.text, command[2])
        self.assertEqual(command[3:], ['--model', 'haiku', '--output-format', 'text', '--tools', '',
                         '--settings', '{"disableAllHooks":true}', '--no-session-persistence',
                         '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'])
        options = run.call_args.kwargs
        self.assertEqual(options['timeout'], 20)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertNotIn('TMUX', options['env'])
        self.assertNotIn('TMUX_PANE', options['env'])
        self.assertEqual(options['env']['ENABLE_CLAUDEAI_MCP_SERVERS'], 'false')
        self.assertEqual(options['env']['CLAUDE_CODE_AUTO_CONNECT_IDE'], '0')

    def test_claude_failure_timeout_and_empty_reply_fall_back(self):
        for run in (Mock(return_value=Mock(returncode=1, stdout='oops')),
                    Mock(side_effect=FileNotFoundError()),
                    Mock(side_effect=subprocess.TimeoutExpired('claude', 20)),
                    Mock(return_value=Mock(returncode=0, stdout=' '))):
            live_mode.CACHE.clear()
            self.assertEqual(live_mode.summary(self.text, run=run), 'Tests pass for Tether version 2. There are 19 fixes.')

    def test_fallback_strips_markdown_and_keeps_decimal_numbers(self):
        text = '# Result\n**Tether** version 2.1 passes [19 tests](https://example.test).\n- `API` works.\n' + 'Detail. ' * 60
        run = Mock(return_value=Mock(returncode=1))
        self.assertEqual(live_mode.summary(text, run=run), 'Result Tether version 2.1 passes 19 tests. API works.')
        self.assertEqual(live_mode.plain('> **Go**\n```py\nsecret()\n```\n![diagram](x)\n| a | b |\n|---|---|\nhttps://x.test'),
                         'Go (code block) diagram , a , b , link')

    def test_long_outputs_are_plain_and_bounded_to_two_sentences(self):
        run = Mock(return_value=Mock(returncode=0, stdout='**First** sentence. Second sentence. Third sentence.'))
        self.assertEqual(live_mode.summary(self.text, run=run), 'First sentence. Second sentence.')
        live_mode.CACHE.clear()
        run.return_value.stdout = 'word ' * 100
        self.assertLessEqual(len(live_mode.summary(self.text, run=run)), 240)
        live_mode.CACHE.clear()
        self.assertLessEqual(len(live_mode.summary('x' * 600, run=Mock(side_effect=OSError()))), 240)

    def test_cache_uses_text_sha256_and_ignores_pane(self):
        run = Mock(return_value=Mock(returncode=0, stdout='Tether passes 19 tests.'))
        first = live_mode.summary(self.text, pane='%1', run=run)
        self.assertEqual(live_mode.summary(self.text, pane='%2', run=run), first)
        run.assert_called_once()
        self.assertIn(hashlib.sha256(self.text.encode()).hexdigest(), live_mode.CACHE)

    def test_cache_is_lru_and_bounded(self):
        run = Mock(side_effect=AssertionError('No Claude call'))
        for at in range(128):
            live_mode.summary(str(at), run=run)
        live_mode.summary('0', run=run)
        live_mode.summary('128', run=run)
        self.assertEqual(len(live_mode.CACHE), 128)
        self.assertIn(hashlib.sha256(b'0').hexdigest(), live_mode.CACHE)
        self.assertNotIn(hashlib.sha256(b'1').hexdigest(), live_mode.CACHE)

    def test_route_returns_summary_and_rejects_bad_bodies(self):
        value = handler('/mobile/api/live/summary', {'text': '**Tests pass.**', 'pane': '%1'})
        server.Handler.do_POST(value)
        self.assertEqual(json.loads(value.wfile.getvalue()), {'summary': 'Tests pass.'})
        for body in ([], {}, {'text': 1}, {'text': ''}):
            value = handler('/mobile/api/live/summary', body)
            live_mode.post(value)
            value.send_response.assert_called_once_with(400)
        value = handler('/mobile/api/live/summary', {'text': 'Hello.'})
        value.headers['Content-Length'] = str(live_mode.LIMIT + 1)
        live_mode.post(value)
        value.send_response.assert_called_once_with(413)
        value = handler('/mobile/api/live/summary', {'text': 'Hello.'})
        value.rfile = io.BytesIO(b'not json')
        live_mode.post(value)
        value.send_response.assert_called_once_with(400)

    def test_route_auth_and_csrf_precede_summary(self):
        for change in ('signed-out', 'origin', 'header'):
            value = handler('/mobile/api/live/summary', {'text': 'Hello.'})
            if change == 'signed-out':
                value.deny.return_value = True
            elif change == 'origin':
                value.headers['Origin'] = 'https://other.test'
            else:
                del value.headers['X-Codex-Mobile']
            with patch.object(live_mode, 'summary') as summary:
                server.Handler.do_POST(value)
            summary.assert_not_called()
            if change != 'signed-out':
                value.send_response.assert_called_once_with(403)
        value = handler('/mobile/api/live/brief', method='GET')
        value.deny.return_value = True
        with patch.object(server, 'sessions') as sessions:
            server.Handler.do_GET(value)
        sessions.assert_not_called()


if __name__ == '__main__':
    unittest.main()
