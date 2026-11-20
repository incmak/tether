"""Plan, apply and undo with one fake tmux server."""
import unittest_isolated  # noqa: F401
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('task_tree_sort_cli', Path(__file__).with_name('task_tree_sort.py'))
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
sort = cli.sort


def thread(number):
    return f'01234567-89ab-4cde-8fab-{number:012d}'


class FakeTmux:
    def __init__(self):
        self.entries = {}
        self.options = {}
        self.writes = []
        self.server = '812:12000'
        self.fail = None
        self.time_out = None
        self.on_write = None

    def pane(self, number, name, owner=None, role='', group_by='', kind=None, session='$1', moved_by=None):
        pane = f'%{number}'
        self.entries[pane] = {'id': pane, 'agent': 'claude', 'registry': 'fixture',
                              'thread': thread(number), 'tmux_session': session, 'pane_name': name}
        options = {'pane_id': pane, '@group_by': group_by, '@tether_role': role,
                   'session_id': session, '@tether_helper': ''}
        if owner is not None:
            record = {'owner_pane': f'%{owner}', 'owner_thread': thread(owner), 'sent_at': 10,
                      'opener_name': self.entries[f'%{owner}']['pane_name']}
            if kind:
                record['kind'] = kind
            if moved_by:
                record['moved_by'] = moved_by
            options['@tether_helper'] = json.dumps(record, indent=None)  # undo must preserve these bytes
        self.options[pane] = options

    def discover(self, config, run, *, repair_zoom):
        if repair_zoom:
            raise AssertionError('Discovery must not repair views during a sort')
        return list(self.entries.values())

    def run(self, cmd, **kwargs):
        action = cmd[1]
        if action == 'display-message':
            if cmd[3] == '-t':
                pane = cmd[4]
                self.assert_command(cmd[5], '#{pid}:#{start_time}\t#{pane_id}\t#{session_id}\t#{@tether_helper}')
                state = self.options[pane]
                output = '\t'.join((self.server, pane, state['session_id'], state['@tether_helper']))
            else:
                self.assert_command(cmd[2:], ['-p', '#{pid}:#{start_time}'])
                output = self.server
            return subprocess.CompletedProcess(cmd, 0, stdout=output + '\n', stderr='')
        if action == 'list-panes':
            self.assert_command(cmd[2:4], ['-a', '-F'])
            fields = re.findall(r'#\{([^}]+)\}', cmd[4])
            output = '\n'.join('\t'.join(options.get(field, '') for field in fields)
                               for options in self.options.values())
            return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr='')
        if action == 'set-option':
            self.assert_command(cmd[2:4], ['-p', '-t'])
            pane, key, value = cmd[4:]
            self.assert_command(key, '@tether_helper')
            if self.fail == pane:
                raise subprocess.CalledProcessError(1, cmd)
            self.options[pane][key] = value
            self.writes.append(pane)
            if self.time_out == pane:
                self.time_out = None
                raise subprocess.TimeoutExpired(cmd, 5)
            if self.on_write:
                self.on_write()
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f'Unexpected tmux command {cmd}')

    def assert_command(self, actual, expected):
        if actual != expected:
            raise AssertionError(f'Unexpected tmux arguments {actual!r}. Expected {expected!r}.')


class TaskTreeSortTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'plan'
        self.tmux = FakeTmux()
        self.tmux.pane(1, 'B446 Root')
        self.tmux.pane(2, 'B446 Workers', 1)
        self.tmux.pane(3, 'B887 Handoff', 2, kind='helper')
        self.enterContext(patch.object(sort.agents, 'discover', side_effect=self.tmux.discover))

    def cli(self, *args):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            result = cli.main(args, run=self.tmux.run)
        return result, out.getvalue(), error.getvalue()

    def run_plan(self):
        ok, _, error = self.cli('plan', '--out', str(self.folder))
        self.assertEqual((ok, error), (0, ''))
        self.assertEqual(self.tmux.writes, [])
        document = json.loads((self.folder / 'plan.json').read_text())
        return {row['pane']: row for row in document['panes']}

    def test_plan_lists_child_handoff_and_worker_with_no_id(self):
        self.tmux.pane(4, 'No backlog ID', 1)
        self.tmux.pane(5, 'Review of worker', 2, role='reviewer')
        self.tmux.pane(6, 'Nested no backlog ID', 2)
        self.tmux.pane(7, 'Own task', 1, kind='own')
        self.tmux.pane(8, 'Named | tab', 1, kind='helper')
        rows = self.run_plan()
        self.assertEqual(rows['%2']['new']['home_pane'], '%1')
        self.assertEqual(rows['%2']['after'], '%1')
        self.assertEqual(rows['%3']['new']['kind'], 'own')
        self.assertEqual(rows['%3']['new']['owner_pane'], '%2')
        self.assertNotIn('home_pane', rows['%3']['new'])
        self.assertEqual(rows['%4']['new']['kind'], 'own')
        self.assertEqual(rows['%5']['after'], '%1')
        self.assertEqual(rows['%5']['new']['home_pane'], '%2')
        self.assertEqual(rows['%6']['after'], '%1')
        self.assertEqual(rows['%7']['new'], rows['%7']['old'])
        self.assertEqual(rows['%8']['after'], '%1')
        review = (self.folder / 'move-list.md').read_text()
        self.assertIn('| Tab | Now | After | Reason |', review)
        self.assertIn('B446 Workers (%2) | Task | Child of B446 Root (%1)', review)
        self.assertIn(r'Named \| tab', review)
        self.assertIn('8 panes checked.', review)

    def test_plan_respects_manual_placements_and_binding(self):
        self.tmux.pane(4, 'B901 Manually moved in', 1, kind='helper', moved_by='user')
        self.tmux.pane(5, 'B446 Manually grouped', 1, role='advisor', group_by='user')
        self.tmux.pane(6, 'B446 Unconfirmed', 1)
        self.tmux.entries['%6']['pending_conversation'] = True
        self.tmux.pane(7, 'B446 Another session', 1, session='$2')
        self.tmux.pane(8, 'B446 Old opener', 1)
        self.tmux.entries['%1']['thread'] = thread(9)
        rows = self.run_plan()
        for pane in ('%4', '%5', '%6', '%7', '%8'):
            with self.subTest(pane=pane):
                self.assertEqual(rows[pane]['new'], rows[pane]['old'])
        self.assertEqual(rows['%8']['reason'], 'Placement owner cannot be confirmed')

    def test_plan_rejects_a_legacy_two_tab_loop(self):
        self.tmux.pane(4, 'B5 One')
        self.tmux.pane(5, 'B5 Two', 4)
        self.tmux.pane(4, 'B5 One', 5)
        rows = self.run_plan()
        for pane in ('%4', '%5'):
            with self.subTest(pane=pane):
                self.assertEqual(rows[pane]['new'], rows[pane]['old'])
                self.assertIsNone(rows[pane]['after'])

    def test_apply_and_undo_restore_exact_records(self):
        rows = self.run_plan()
        backup = self.folder / 'undo.json'
        planned = (self.folder / 'plan.json').read_text()
        self.assertEqual(backup.read_text(), planned)
        backup.write_text('replace this before apply')

        def first_write():
            self.assertEqual(backup.read_text(), planned)
            self.tmux.on_write = None

        self.tmux.on_write = first_write
        self.assertEqual(self.cli('apply', str(self.folder)), (0, 'Updated 2 records.\n', ''))
        for pane, row in rows.items():
            self.assertEqual(sort.helper_tabs._record(self.tmux.options[pane]['@tether_helper']), row['new'])
        self.assertEqual(self.cli('undo', str(self.folder)), (0, 'Restored 2 records.\n', ''))
        for pane, row in rows.items():
            self.assertEqual(self.tmux.options[pane]['@tether_helper'], row['raw'])

    def test_apply_and_undo_preserve_delivery_between_check_and_write(self):
        run = self.tmux.run
        original = {pane: dict(options) for pane, options in self.tmux.options.items()}
        for action, sent_at in (('apply', 20), ('undo', 30)):
            with self.subTest(action=action):
                self.folder = self.folder.with_name(action)
                self.tmux.options = {pane: dict(options) for pane, options in original.items()}
                self.tmux.writes.clear()
                self.run_plan()
                if action == 'undo':
                    self.assertEqual(self.cli('apply', str(self.folder))[0], 0)

                def delivery(cmd, **kw):
                    if cmd[1] == 'set-option':
                        with (sort.paths.TETHER / 'helper-record.lock').open('a') as handle:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if cmd[1] == 'display-message' and '-t' in cmd and cmd[4] == '%2':
                        record = json.loads(self.tmux.options['%2']['@tether_helper'])
                        record.update(sent_at=sent_at, before=f'reply-{sent_at}', unknown='keep me')
                        self.tmux.options['%2']['@tether_helper'] = json.dumps(record)
                    return run(cmd, **kw)

                with patch.object(self.tmux, 'run', side_effect=delivery):
                    result, _, error = self.cli(action, str(self.folder))
                self.assertEqual((result, error), (0, ''))
                record = json.loads(self.tmux.options['%2']['@tether_helper'])
                self.assertEqual((record['sent_at'], record['before'], record['unknown']),
                                 (sent_at, f'reply-{sent_at}', 'keep me'))
                self.assertEqual(record.get('kind'), 'helper' if action == 'apply' else None)

    def test_apply_refuses_when_a_planned_or_unchanged_pane_changes(self):
        self.run_plan()
        self.tmux.entries['%1']['thread'] = thread(9)
        original = self.tmux.options['%3']['@tether_helper']
        self.tmux.options['%3']['@tether_helper'] = json.dumps(dict(json.loads(original), kind='own'))
        result, _, error = self.cli('apply', str(self.folder))
        self.assertEqual(result, 1)
        self.assertIn('%1 changed conversations', error)
        self.assertIn('%3 changed its helper record', error)
        self.assertEqual(self.tmux.writes, [])

    def test_apply_refuses_when_tmux_restarts(self):
        self.run_plan()
        self.tmux.server = '812:17000'
        result, _, error = self.cli('apply', str(self.folder))
        self.assertEqual(result, 1)
        self.assertIn('tmux server restarted', error)
        self.assertEqual(self.tmux.writes, [])

    def test_undo_refuses_when_a_pane_record_changes_after_apply(self):
        self.run_plan()
        self.assertEqual(self.cli('apply', str(self.folder))[0], 0)
        self.tmux.writes.clear()
        self.tmux.options['%2']['@tether_helper'] = json.dumps({'owner_pane': '%3', 'sent_at': 34})
        result, _, error = self.cli('undo', str(self.folder))
        self.assertEqual(result, 1)
        self.assertIn('%2 changed its helper record', error)
        self.assertEqual(self.tmux.writes, [])

    def test_failed_write_rolls_back_previous_writes(self):
        rows = self.run_plan()
        self.tmux.fail = '%3'
        result, _, error = self.cli('apply', str(self.folder))
        self.assertEqual(result, 1)
        self.assertIn('Tmux could not write pane %3', error)
        self.assertIn('Restored the records', error)
        self.assertEqual(self.tmux.writes, ['%2', '%2'])
        self.assertEqual(self.tmux.options['%2']['@tether_helper'], rows['%2']['raw'])

    def test_conversation_change_during_apply_prevents_later_write(self):
        rows = self.run_plan()

        def first_write():
            self.tmux.entries['%3']['thread'] = thread(9)
            self.tmux.on_write = None

        self.tmux.on_write = first_write
        result, _, error = self.cli('apply', str(self.folder))
        self.assertEqual(result, 1)
        self.assertIn('%3 changed conversations', error)
        self.assertEqual(self.tmux.writes, ['%2', '%2'])
        self.assertEqual(self.tmux.options['%2']['@tether_helper'], rows['%2']['raw'])

    def test_timeout_after_write_restores_that_record_too(self):
        rows = self.run_plan()
        self.tmux.time_out = '%3'
        result, _, error = self.cli('apply', str(self.folder))
        self.assertEqual(result, 1)
        self.assertIn('Restored the records', error)
        self.assertEqual(self.tmux.writes, ['%2', '%3', '%3', '%2'])
        for pane, row in rows.items():
            self.assertEqual(self.tmux.options[pane]['@tether_helper'], row['raw'])


if __name__ == '__main__':
    unittest.main()
