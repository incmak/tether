import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import catalog
import claude_probe

INIT = {'commands': [{'name': 'compact', 'description': 'Free up context', 'argumentHint': '[focus]'},
                     {'name': 'simplify', 'description': 'Review the changed code (bundled)'}, {'name': ''}],
        'claude_code_version': '9.9.9', 'account': {'email': 'a@b.c', 'subscriptionType': 'Max', 'token': 'never'}}
USAGE = {'rate_limits_available': True, 'subscription_type': 'Max',
         'rate_limits': {'five_hour': {'utilization': 12.5, 'resets_at': '2026-11-02T15:00:00Z'},
                         'seven_day': {'utilization': 40, 'resets_at': '2026-11-05T00:00:00+00:00'},
                         'seven_day_opus': None, 'extra': {'utilization': 1}}}


class ProbeTests(unittest.TestCase):
    def test_summarize_init_keeps_names_hints_and_account_only(self):
        found = claude_probe.summarize_init(INIT)
        self.assertEqual([item['name'] for item in found['commands']], ['compact', 'simplify'])
        self.assertEqual(found['commands'][0]['hint'], '[focus]')
        self.assertEqual(found['account'], {'email': 'a@b.c', 'plan': 'Max'})

    def test_usage_report_maps_windows(self):
        report = claude_probe.usage_report(USAGE, now=100)
        self.assertEqual([(w['label'], w['percent']) for w in report['windows']], [('5h', 12.5), ('7d', 40)])
        self.assertEqual(report['windows'][0]['resets_at'], 1793631600.0)
        self.assertEqual((report['reported_at'], report['source'], report['plan']), (100, 'live', 'Max'))

    def test_check_usage_caches_and_reports_errors(self):
        claude_probe._usage.update(at=0, report=None, error=None)
        with mock.patch.object(claude_probe, 'ask', return_value={'initialize': INIT, 'get_usage': USAGE}) as ask:
            first = claude_probe.check_usage(60)
            second = claude_probe.check_usage(60)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(first['report']['windows'], second['report']['windows'])
        claude_probe._usage['at'] = 0
        with mock.patch.object(claude_probe, 'ask', side_effect=RuntimeError('no claude')):
            self.assertEqual(claude_probe.check_usage(60)['error'], 'no claude')

    def test_ask_reads_matching_control_response(self):
        script = ('import json,sys\n'
                  'for line in sys.stdin:\n'
                  '    r=json.loads(line)\n'
                  '    print("noise"); print(json.dumps({"type":"system"}))\n'
                  '    print(json.dumps({"type":"control_response","response":{"subtype":"success","request_id":r["request_id"],"response":{"ok":r["request"]["subtype"]}}}), flush=True)\n')
        replies = claude_probe.ask(None, [{'subtype': 'initialize'}, {'subtype': 'get_usage'}], 10, command=[sys.executable, '-c', script])
        self.assertEqual(replies, {'initialize': {'ok': 'initialize'}, 'get_usage': {'ok': 'get_usage'}})
        with self.assertRaises(RuntimeError):
            claude_probe.ask(None, [{'subtype': 'initialize'}], 10, command=[sys.executable, '-c', 'pass'])


class CommandCacheTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.claude = root / 'config'
        self.project = root / 'project'
        self.cwd = self.project / 'nested'
        self.cwd.mkdir(parents=True)
        (self.project / '.git').mkdir()
        self.enterContext(mock.patch.object(claude_probe.paths, 'CLAUDE', self.claude))
        self.enterContext(mock.patch.object(claude_probe, '_init', {}))
        self.ask = self.enterContext(mock.patch.object(claude_probe, 'ask', return_value={'initialize': INIT}))

    def test_new_skill_refreshes_and_waits_for_an_in_flight_probe(self):
        skills = self.claude / 'skills'
        skills.mkdir(parents=True)
        first = claude_probe.commands(self.cwd)
        (skills / 'new-skill').mkdir()
        release = threading.Event()

        def refresh(*_args):
            if not release.wait(5):
                raise RuntimeError('test did not release the probe')
            return {'initialize': {'commands': [{'name': 'new-skill'}]}}

        self.ask.side_effect = refresh
        try:
            self.assertIsNone(claude_probe.commands(self.cwd, wait=0))
            thread = claude_probe._init[self.cwd]['thread']
            join = thread.join

            def release_and_join(timeout):
                release.set()
                join(timeout)

            with mock.patch.object(thread, 'join', side_effect=release_and_join) as wait:
                fresh = claude_probe.commands(self.cwd)
                wait.assert_called_once()
        finally:
            release.set()
        self.assertNotEqual(fresh, first)
        self.assertEqual([item['name'] for item in fresh['commands']], ['new-skill'])
        self.assertEqual(self.ask.call_count, 2)

    def test_unchanged_skill_reuses_the_probe_within_ttl(self):
        (self.claude / 'skills' / 'existing').mkdir(parents=True)
        first = claude_probe.commands(self.cwd)
        self.assertIsNotNone(first)
        self.assertIs(claude_probe.commands(self.cwd), first)
        self.ask.assert_called_once()

    def test_edit_in_project_ancestor_refreshes_without_a_directory_change(self):
        skill = self.project / '.claude' / 'skills' / 'example' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('Before')
        stamp = skill.stat().st_mtime_ns
        claude_probe.commands(self.cwd)
        skill.write_text('After')
        os.utime(skill, ns=(stamp, stamp + 1_000_000_000))
        self.ask.return_value = {'initialize': {'commands': [{'name': 'example', 'description': 'After'}]}}
        fresh = claude_probe.commands(self.cwd)
        self.assertEqual(fresh['commands'][0]['description'], 'After')
        self.assertEqual(self.ask.call_count, 2)

    def test_enabled_plugin_command_edit_refreshes(self):
        command = self.claude / 'plugins' / 'cache' / 'market' / 'example' / '1' / 'commands' / 'review.md'
        command.parent.mkdir(parents=True)
        (self.claude / 'settings.json').write_text('{"enabledPlugins": {"example@market": true}}')
        command.write_text('Before')
        stamp = command.stat().st_mtime_ns
        claude_probe.commands(self.cwd)
        command.write_text('After')
        os.utime(command, ns=(stamp, stamp + 1_000_000_000))
        self.ask.return_value = {'initialize': {'commands': [{'name': 'example:review', 'description': 'After'}]}}
        fresh = claude_probe.commands(self.cwd)
        self.assertEqual(fresh['commands'][0]['description'], 'After')
        self.assertEqual(self.ask.call_count, 2)


class MergeTests(unittest.TestCase):
    def test_probe_list_first_then_terminal_only_builtins(self):
        docs = [{'name': 'compact', 'description': 'Doc text', 'aliases': [], 'kind': 'builtin'},
                {'name': 'status', 'description': 'Show account', 'aliases': [], 'kind': 'builtin'},
                {'name': 'usage', 'description': 'Show usage', 'aliases': ['cost', 'stats'], 'kind': 'builtin'}]
        with mock.patch.object(catalog, 'claude_builtins', return_value=docs):
            items = catalog.merged_commands(claude_probe.summarize_init(INIT))
        by = {item['name']: item for item in items}
        self.assertEqual([item['name'] for item in items], ['compact', 'simplify', 'status', 'usage'])
        self.assertEqual(by['simplify']['description'], 'Review the changed code')
        self.assertEqual(by['simplify']['kind'], 'skill')
        self.assertEqual(by['compact']['hint'], '[focus]')
        self.assertTrue(by['status']['description'].startswith(catalog.TERMINAL))
        self.assertEqual(by['usage']['aliases'], ['cost', 'stats'])


if __name__ == '__main__':
    unittest.main()
