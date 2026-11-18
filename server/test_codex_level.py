import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import audit
import codex_level
import paths
import projects

PID = 'abcdef012345'


def tmux_result(stdout='', code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr='')


class Handler:
    def __init__(self, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = path
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (('CONFIG', self.root / 'config'), ('TETHER', self.root / 'tether'), ('CODEX', self.root / 'codex')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CODEX_HOME', None)
        codex_level._cache.update(at=0.0, value=None)
        codex_level._today_seen.clear()
        runs = mock.patch.object(codex_level, 'runs', return_value={'today': 0, 'running': 0, 'panes': []})
        runs.start()
        self.addCleanup(runs.stop)

    def write_global(self, text):
        path = self.root / 'config' / 'codex-level'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def write_project(self, text):
        path = self.root / 'tether' / 'projects' / PID / 'codex-level'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class Resolve(Base):
    def test_missing_files_mean_off(self):
        self.assertEqual(codex_level.resolve(PID), {'level': 0, 'source': 'global', 'global': 0, 'project': None})

    def test_global_level(self):
        self.write_global('3\n')
        self.assertEqual(codex_level.resolve()['level'], 3)
        self.assertEqual(codex_level.resolve(PID)['source'], 'global')

    def test_bad_global_file_means_off(self):
        for text in ('7', 'two', '', '-1', '2 3'):
            self.write_global(text)
            self.assertEqual(codex_level.read_global(), 0, text)

    def test_override_on_beats_global(self):
        self.write_global('1')
        self.write_project('{"override": true, "level": 4}')
        self.assertEqual(codex_level.resolve(PID), {'level': 4, 'source': 'project', 'global': 1,
                                                     'project': {'override': True, 'level': 4}})

    def test_override_on_can_lower_the_level(self):
        self.write_global('3')
        self.write_project('{"override": true, "level": 0}')
        self.assertEqual(codex_level.resolve(PID)['level'], 0)

    def test_override_off_keeps_global(self):
        self.write_global('2')
        self.write_project('{"override": false, "level": 4}')
        resolved = codex_level.resolve(PID)
        self.assertEqual((resolved['level'], resolved['source']), (2, 'global'))
        self.assertEqual(resolved['project'], {'override': False, 'level': 4})

    def test_bad_project_file_is_ignored(self):
        self.write_global('2')
        for text in ('not json', '[]', '{"override": true, "level": 9}', '{"override": "yes", "level": 3}',
                     '{"override": true, "level": true}', '{"level": 3}'):
            self.write_project(text)
            self.assertEqual(codex_level.resolve(PID)['level'], 2, text)

    def test_bad_project_id_is_ignored(self):
        self.write_global('1')
        self.assertEqual(codex_level.resolve('../../etc')['level'], 1)

    def test_set_and_read_back(self):
        codex_level.set_global(2)
        codex_level.set_project(PID, True, 3)
        self.assertEqual((self.root / 'config' / 'codex-level').read_text(), '2\n')
        self.assertEqual(codex_level.resolve(PID)['level'], 3)
        for bad in (6, -1, '2', True, None):
            with self.assertRaises(codex_level.LevelError):
                codex_level.set_global(bad)
        with self.assertRaises(codex_level.LevelError):
            codex_level.set_project(PID, 'on', 2)
        with self.assertRaises(codex_level.LevelError):
            codex_level.set_project('nope', True, 2)


class Level5AndRun(Base):
    def test_level_5_gives_ui_to_codex(self):
        codex_level.set_global(5)
        text = codex_level.rules(codex_level.resolve())
        self.assertIn('Codex level 5 of 5 (Codex does everything)', text)
        self.assertIn('UI design and prototyping, code and tests', text)
        self.assertNotIn('stay with you', text)
        self.assertIn('codex-lane', text)

    def test_run_defaults_to_auto_and_bad_files_fall_back(self):
        self.assertEqual(codex_level.read_run(), {'model': 'auto', 'effort': 'auto', 'fast': False})
        codex_level.run_path().parent.mkdir(parents=True, exist_ok=True)
        codex_level.run_path().write_text('{"model": "x; rm", "effort": "auto", "fast": false}')
        self.assertEqual(codex_level.read_run()['model'], 'auto')
        for bad in ({'model': 'auto', 'effort': 'huge', 'fast': False}, {'model': 'auto', 'effort': 'auto', 'fast': 1},
                    {'model': '-x', 'effort': 'auto', 'fast': False}, {'model': 'auto'}):
            with self.assertRaises(codex_level.LevelError):
                codex_level.set_run(bad)

    def test_run_rule_names_the_options(self):
        codex_level.set_run({'model': 'gpt-6.1-sol', 'effort': 'high', 'fast': True})
        codex_level.set_global(2)
        text = codex_level.rules(codex_level.resolve())
        self.assertIn('`-m gpt-6.1-sol -c model_reasoning_effort="high" -c service_tier="fast"`', text)
        self.assertNotIn('Choose the', text)
        auto = codex_level.run_rule({'model': 'auto', 'effort': 'auto', 'fast': False})
        self.assertIn('Choose the model and the effort', auto)
        self.assertIn('Do not turn on fast mode', auto)
        self.assertNotIn('Pass these options', auto)

    def test_models_come_from_the_codex_cache(self):
        (self.root / 'codex').mkdir(parents=True, exist_ok=True)
        (self.root / 'codex' / 'models_cache.json').write_text(json.dumps({'models': [
            {'slug': 'gpt-a', 'display_name': 'GPT A', 'visibility': 'list', 'additional_speed_tiers': ['fast'],
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'ultra'}, {'effort': 'weird'}]},
            {'slug': 'hidden', 'visibility': 'hide', 'supported_reasoning_levels': []}]}))
        self.assertEqual(codex_level.models(), [{'slug': 'gpt-a', 'name': 'GPT A', 'efforts': ['low', 'ultra'], 'fast': True}])


class Rules(Base):
    def test_off_is_one_short_rule(self):
        text = codex_level.rules({'level': 0, 'source': 'global'})
        self.assertIn('Codex level 0 of 5 (Off)', text)
        self.assertNotIn('codex-lane', text)

    def test_every_level_keeps_ui_with_claude_and_safe_exec(self):
        for level in range(1, 5):
            text = codex_level.rules({'level': level, 'source': 'global'})
            self.assertIn(f'Codex level {level} of 5', text)
            self.assertIn('UI design and prototyping stay with you', text)
            self.assertIn('codex-lane', text)
            self.assertIn('</dev/null', text)
            self.assertIn('Do not change the level', text)
            self.assertLess(len(text), 1200)

    def test_source_is_named(self):
        self.assertIn('for this project', codex_level.rules({'level': 2, 'source': 'project'}))


class Hook(Base):
    def test_pane_project_from_tmux(self):
        calls = []

        def tmux(*args):
            calls.append(args)
            return tmux_result(PID + '\n')
        self.write_global('1')
        self.write_project('{"override": true, "level": 3}')
        out = json.loads(codex_level.hook_output({'cwd': '/x', 'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'},
                                                 {'TMUX': '/tmp/tmux', 'TMUX_PANE': '%7'}, tmux))
        self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('Codex level 3 of 5', out['hookSpecificOutput']['additionalContext'])
        self.assertEqual(calls, [('display-message', '-p', '-t', '%7', '#{@project}')])  # read only

    def test_pane_without_project_uses_global(self):
        self.write_global('2')
        out = json.loads(codex_level.hook_output({'cwd': '/x'}, {'TMUX': '/t', 'TMUX_PANE': '%7'}, lambda *a: tmux_result('\n')))
        self.assertIn('Codex level 2 of 5', out['hookSpecificOutput']['additionalContext'])

    def test_cwd_fallback_needs_one_project(self):
        listing = f'/work\t{PID}\n/work\t{PID}\n/other\tffffffffffff\n'
        self.assertEqual(codex_level.hook_project('/work', {}, lambda *a: tmux_result(listing)), PID)
        listing += '/work\t111111111111\n'
        self.assertIsNone(codex_level.hook_project('/work', {}, lambda *a: tmux_result(listing)))
        self.assertIsNone(codex_level.hook_project('relative', {}, lambda *a: tmux_result(listing)))

    def test_tmux_failure_falls_back_to_global(self):
        def broken(*args):
            raise subprocess.TimeoutExpired('tmux', 2)
        self.write_global('4')
        out = json.loads(codex_level.hook_output({'cwd': '/x'}, {'TMUX': '/t', 'TMUX_PANE': '%1'}, broken))
        self.assertIn('Codex level 4 of 5', out['hookSpecificOutput']['additionalContext'])

    def test_script_prints_json_and_exits_0(self):
        env = dict(os.environ, MOBILE_TMUX_CONFIG=str(self.root / 'config'), TETHER_HOME=str(self.root / 'tether'), MOBILE_TMUX_TMUX='/bin/false')
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        self.write_global('2')
        script = Path(codex_level.__file__)
        done = subprocess.run([sys.executable, str(script), 'hook'], input='{"cwd": "/nowhere-at-all"}',
                              capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(done.returncode, 0)
        self.assertIn('Codex level 2 of 5', json.loads(done.stdout)['hookSpecificOutput']['additionalContext'])
        bad = subprocess.run([sys.executable, str(script), 'hook'], input='{not json', capture_output=True, text=True,
                             env=env, timeout=10)
        self.assertEqual((bad.returncode, bad.stdout), (0, ''))


class Runs(unittest.TestCase):
    def setUp(self):
        codex_level._cache.update(at=0.0, value=None)
        codex_level._today_seen.clear()

    def test_is_run(self):
        yes = [['codex', 'exec', 'hi'], ['/usr/bin/codex', '-c', 'x=1', 'exec', '-'], ['codex', 'e', 'x'],
               ['node', '/usr/lib/node_modules/@openai/codex/bin/codex.js', 'exec', 'x'],
               ['bash', '/opt/u/bin/codex-lane', '--', 'x'], ['codex-lane', '--max-min', '5', '--', 'x'],
               ['/x/codex', '--model', 'gpt', '--full-auto', 'exec', 'x']]
        no = [['codex'], ['codex', 'resume'], ['grep', 'codex', 'exec'], ['codex', 'app-server', '--listen', 'x'],
              ['bash', '-c', 'codex exec x'], ['rg', 'codex-lane'], [], ['vim', 'codex_level.py']]
        for argv in yes:
            self.assertTrue(codex_level.is_run(argv), argv)
        for argv in no:
            self.assertFalse(codex_level.is_run(argv), argv)

    def test_running_counts_once_and_maps_panes(self):
        procs = {
            100: (1, ['zsh']),                                  # pane %1
            101: (100, ['claude']),
            102: (101, ['bash', '/b/codex-lane', '--', 'x']),   # a run
            103: (102, ['node', '/n/codex.js', 'exec', 'x']),   # inside it: not counted again
            104: (103, ['/n/codex', 'exec', 'x']),
            200: (1, ['zsh']),                                  # pane %2
            201: (200, ['codex', 'exec', 'y']),
            300: (1, ['codex', 'exec', 'z']),                   # no pane
            400: (1, ['zsh']),                                  # pane %3, idle
        }
        total, by_pane = codex_level.running(procs, {'%1': 100, '%2': 200, '%3': 400})
        self.assertEqual(total, 3)
        self.assertEqual(by_pane, {'%1': 1, '%2': 1})

    def test_processes_reads_proc(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            (proc / '42').mkdir()
            (proc / '42' / 'stat').write_text('42 (co dex) S 7 42 42')
            (proc / '42' / 'cmdline').write_bytes(b'codex\0exec\0hi\0')
            (proc / 'self').mkdir()
            (proc / '43').mkdir()  # gone between listing and reading
            self.assertEqual(codex_level.processes(proc), {42: (7, ['codex', 'exec', 'hi'])})

    def test_today_counts_exec_sessions_only(self):
        with tempfile.TemporaryDirectory() as temp:
            now = time.mktime((2026, 11, 3, 12, 0, 0, 0, 0, -1))
            folder = Path(temp) / 'sessions' / '2026' / '11' / '03'
            folder.mkdir(parents=True)
            (folder / 'rollout-a.jsonl').write_text('{"type":"session_meta","payload":{"originator":"codex_exec"}}\n')
            (folder / 'rollout-b.jsonl').write_text('{"type":"session_meta","payload":{"source":"vscode"}}\n')
            (folder / 'rollout-c.jsonl').write_text('{"type":"session_meta","payload":{"originator":"codex_exec","source":"exec"}}\n')
            (folder / 'rollout-d.jsonl').write_text('{"type":"sess')  # still being written
            (folder / 'notes.txt').write_text('codex_exec')
            yesterday = Path(temp) / 'sessions' / '2026' / '11' / '02'
            yesterday.mkdir(parents=True)
            (yesterday / 'rollout-z.jsonl').write_text('{"payload":{"originator":"codex_exec"}}\n')
            self.assertEqual(codex_level.today_count(now, temp), 2)
            self.assertNotIn(f'{folder}/rollout-d.jsonl', codex_level._today_seen)
            (folder / 'rollout-d.jsonl').write_text('{"type":"session_meta","payload":{"originator":"codex_exec"}}\n')
            self.assertEqual(codex_level.today_count(now, temp), 3)
            self.assertEqual(codex_level.today_count(now, Path(temp) / 'missing'), 0)

    def test_runs_is_cached(self):
        calls = []

        def tmux(*args):
            calls.append(args)
            return tmux_result('%1\t100\tmain\t0.0\n')
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp) / 'proc'
            for pid, ppid, argv in ((100, 1, b'zsh\0'), (101, 100, b'codex\0exec\0x\0')):
                (proc / str(pid)).mkdir(parents=True)
                (proc / str(pid) / 'stat').write_text(f'{pid} (x) S {ppid} 0')
                (proc / str(pid) / 'cmdline').write_bytes(argv)
            first = codex_level.runs(None, tmux, proc, Path(temp) / 'codex')
            second = codex_level.runs(None, tmux, proc, Path(temp) / 'codex')
        self.assertEqual(first, {'today': 0, 'running': 1, 'panes': [{'pane': '%1', 'label': 'main 0.0', 'running': 1}]})
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)


class Http(Base):
    def test_get_and_post(self):
        handler = Handler('/mobile/api/codex-level')
        codex_level.get(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual((handler.body['global'], handler.body['level'], len(handler.body['levels'])), (0, 0, 6))
        handler = Handler('/mobile/api/codex-level', {'level': 2})
        codex_level.post(handler)
        self.assertEqual((handler.status, handler.body['global']), (200, 2))
        handler = Handler('/mobile/api/codex-level', {'project': PID, 'override': True, 'level': 4})
        codex_level.post(handler)
        self.assertEqual((handler.body['level'], handler.body['source']), (4, 'project'))
        handler = Handler(f'/mobile/api/codex-level?project={PID}')
        codex_level.get(handler)
        self.assertEqual(handler.body['project'], {'override': True, 'level': 4})

    def test_bad_requests(self):
        for body in ({'level': 9}, {'level': '2'}, {'project': 'x', 'override': True, 'level': 1}, [1]):
            handler = Handler('/mobile/api/codex-level', body)
            codex_level.post(handler)
            self.assertEqual(handler.status, 400, body)
        handler = Handler('/mobile/api/codex-level?project=../x')
        codex_level.get(handler)
        self.assertEqual(handler.status, 400)
        self.assertTrue(codex_level.handles('/mobile/api/codex-level?project=abc'))
        self.assertFalse(codex_level.handles('/mobile/api/codex-levels'))

    def test_level_audit_records_successful_change_and_undo(self):
        codex_level.set_global(3)
        with audit.acting('ui', device='Chrome on Mac'):
            for body, status in (({'level': 1, 'undo': 'yes'}, 400), ({'level': 1}, 200), ({'level': 1}, 200)):
                handler = self.post(body)
                self.assertEqual(handler.status, status, handler.body)
            undo = self.post({'level': 3, 'undo': True, 'revision': handler.body['change']['revision']})
            self.assertEqual(undo.status, 200, undo.body)
        rows = audit.query(kinds=['setting.codex_level'])['entries']
        self.assertCountEqual([(row['summary'], row['old'], row['new'], row.get('undo')) for row in rows],
                              [('Codex level 3 → 1', 3, 1, None), ('Undo: Codex level 1 → 3', 1, 3, True)])
        self.assertTrue(all(row['actor'] == {'source': 'ui', 'device': 'Chrome on Mac'} for row in rows))

    def post(self, body):
        handler = Handler(codex_level.PATH, body)
        codex_level.post(handler)
        return handler

    def test_undo_restores_the_value_the_server_replaced(self):
        # The UI may show 3 while the server holds 4: the change and its Undo use the server's 4 (B56 review).
        codex_level.set_global(3)
        codex_level.set_global(4)
        with audit.acting('ui'):
            change = self.post({'level': 1}).body['change']
            self.assertEqual((change['old'], change['new']), (4, 1))
            undo = self.post({'level': change['old'], 'undo': True, 'revision': change['revision']})
        self.assertEqual(undo.status, 200, undo.body)
        self.assertEqual(codex_level.read_global(), 4)

    def test_stale_undo_is_refused_even_when_the_value_came_back(self):
        # B56 review: another device wrote 1 → 2 → 1. The value matches, but the Undo is for an older change.
        codex_level.set_global(3)
        codex_level.set_project(PID, True, 2)
        with audit.acting('ui'):
            change = self.post({'level': 1}).body['change']
            project = self.post({'project': PID, 'override': True, 'level': 0}).body['change']
            self.assertEqual(project['old'], {'level': 2, 'override': True})
            for body in ({'level': 2}, {'level': 1}, {'project': PID, 'override': False, 'level': 0},
                         {'project': PID, 'override': True, 'level': 0}):
                self.assertEqual(self.post(body).status, 200)
            for body in ({'level': 3, 'undo': True, 'revision': change['revision']},
                         {'level': 3, 'undo': True},
                         {'project': PID, 'override': True, 'level': 2, 'undo': True, 'revision': project['revision']}):
                handler = self.post(body)
                self.assertEqual((handler.status, handler.body), (409, {'error': codex_level.STALE_UNDO}), body)
        self.assertEqual((codex_level.read_global(), codex_level.read_project(PID)), (1, {'override': True, 'level': 0}))
        self.assertFalse([row for row in audit.query()['entries'] if row.get('undo')])

    def test_level_audit_records_project_override_change_at_the_same_level(self):
        codex_level.set_project(PID, False, 2)
        projects.set_name(PID, 'Example Project')
        with audit.acting('ui'):
            handler = Handler(codex_level.PATH, {'project': PID, 'override': True, 'level': 2})
            codex_level.post(handler)
        self.assertEqual(handler.status, 200, handler.body)
        row, = audit.query(kinds=['setting.codex_level'])['entries']
        self.assertEqual(row['project'], 'Example Project')
        self.assertEqual(row['summary'], 'Codex level for Example Project: 2 (override off) → 2 (override on)')
        self.assertEqual((row['old'], row['new'], row['actor']),
                         ('2 (override off)', '2 (override on)', {'source': 'ui'}))

    def test_run_audit_records_changed_model_effort_and_fast(self):
        handler = Handler(codex_level.PATH, {'run': {'model': 'gpt-test', 'effort': 'high', 'fast': True}})
        with audit.acting('ui'):
            codex_level.post(handler)
        self.assertEqual(handler.status, 200, handler.body)
        row, = audit.query(kinds=['setting.codex_run'])['entries']
        self.assertEqual((row['old'], row['new'], row['actor']),
                         ('auto · auto · fast off', 'gpt-test · high · fast on', {'source': 'ui'}))


if __name__ == '__main__':
    unittest.main()
