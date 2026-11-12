from datetime import datetime
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import attention


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def entry(pane='%1', thread='t1', agent='claude', **extra):
    return dict(id=pane, thread=thread, agent=agent, **extra)


APPROVAL = {'state': 'Approval', 'approval': {'text': 'Bash command\nRun the migration?'}}


class World:
    """Fake panes: status and last reply per pane, and a turn-end marker the observer compares."""

    def __init__(self, store):
        self.store, self.status, self.reply, self.finished = store, {}, {}, {}

    def turn(self, pane, reply, at):
        self.status[pane], self.reply[pane], self.finished[pane] = {'state': 'Idle'}, reply, at

    def observe(self, *entries):
        return self.store.observe(entries, lambda e: self.status.get(e['id'], {'state': 'Working'}),
                                  lambda e: self.reply.get(e['id']), lambda e: self.finished.get(e['id']))


class AttentionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'attention.json'
        self.clock = Clock()
        self.on_urgent = mock.Mock()
        self.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world = World(self.store)

    def tearDown(self):
        self.dir.cleanup()

    def test_reply_needs_you_yes_starts_an_ask_with_the_next_line(self):
        self.world.turn('%1', 'Built it.\n\nNext: **approve** the deploy to staging.\n\nNeeds you: yes', 10)
        self.world.observe(entry())
        ask = self.store.ask('%1', 't1')
        self.assertEqual(ask['kind'], 'reply')
        self.assertEqual(ask['next'], 'approve the deploy to staging.')
        self.assertEqual(ask['since'], self.clock.now)
        self.assertFalse(ask['opened'])
        self.assertIsNone(ask['snoozed_until'])
        self.assertNotIn('reply', ask)
        self.assertFalse(ask['urgent'])

    def test_urgent_text_uses_last_matching_line_and_strips_markdown(self):
        for prefix in ('Urgent:', '**Urgent:**', '- **URGENT:**', '• Urgent:', '> **urgent:**', '`Urgent:`'):
            with self.subTest(prefix=prefix):
                self.assertEqual(attention.urgent_text(f'Urgent: old\n{prefix} **Choose** `now`?\nNeeds you: yes'),
                                 'Choose now?')
        for text in (None, '', 'No urgent ask.', 'Urgent:', 'An Urgent: aside'):
            self.assertIsNone(attention.urgent_text(text))
        self.assertEqual(attention.urgent_text('Urgent: ' + 'x' * 200), 'x' * (attention.LIMIT - 1) + '…')

    def test_new_urgent_replies_notify_once_each_and_keep_the_first_since(self):
        self.world.turn('%1', 'Next: normal\nNeeds you: yes', 10)
        self.world.observe(entry())
        since = self.store.ask('%1', 't1')['since']
        self.on_urgent.assert_not_called()
        self.clock.now += 60
        reply = 'Next: normal\nUrgent: May I proceed?\nNeeds you: yes'
        self.world.turn('%1', reply, 20)
        self.world.observe(entry())
        ask = self.store.ask('%1', 't1')
        self.assertEqual((ask['urgent'], ask['next'], ask['since']), (True, 'May I proceed?', since))
        self.on_urgent.assert_called_once_with(ask)
        self.world.observe(entry())
        self.world.turn('%1', reply, 30)  # A re-read with a new turn marker still has the same reply key.
        self.world.observe(entry())
        self.on_urgent.assert_called_once()
        self.world.turn('%1', 'New context.\n' + reply, 40)
        self.world.observe(entry())
        self.assertEqual(self.on_urgent.call_count, 2)
        self.world.turn('%1', 'Next: normal again\nNeeds you: yes', 50)
        self.world.observe(entry())
        self.assertFalse(self.store.ask('%1', 't1')['urgent'])
        self.assertEqual(self.on_urgent.call_count, 2)

    def test_urgent_requires_needs_you_yes(self):
        self.world.turn('%1', 'Urgent: Ignore this?\nNeeds you: no', 10)
        self.world.observe(entry())
        self.assertEqual(self.store.waiting(), [])
        self.on_urgent.assert_not_called()

    def test_urgent_callback_runs_outside_lock_with_a_public_copy_and_can_fail(self):
        locked, observed = [], []
        def notify(ask):
            locked.append(self.store.lock.locked())
            if not locked[-1]:
                observed.append((self.store.ask('%1', 't1'), dict(ask)))
            ask['next'] = 'Do not change the stored ask'
            raise RuntimeError('notification failed')
        self.on_urgent.side_effect = notify
        self.world.turn('%1', 'Urgent: Choose?\nNeeds you: yes', 10)
        self.assertTrue(self.world.observe(entry()))
        self.assertEqual(locked, [False])
        self.assertEqual(observed, [(self.store.ask('%1', 't1'), self.store.ask('%1', 't1'))])
        self.assertNotIn('reply', observed[0][1])
        self.assertEqual(self.store.ask('%1', 't1')['next'], 'Choose?')
        self.assertTrue(json.loads(self.path.read_text())['asks']['%1']['urgent'])
        self.world.observe(entry())
        self.on_urgent.assert_called_once()

    def test_urgent_restart_does_not_notify_again_and_old_asks_default_to_false(self):
        reply = 'Urgent: Choose?\nNeeds you: yes'
        self.world.turn('%1', reply, 10)
        self.world.observe(entry())
        notify = mock.Mock()
        again = attention.Attention(self.path, self.clock, on_urgent=notify)
        self.assertTrue(again.ask('%1', 't1')['urgent'])
        self.assertFalse(again.note_reply('%1', 't1', reply))
        notify.assert_not_called()
        data = json.loads(self.path.read_text())
        del data['asks']['%1']['urgent']
        self.path.write_text(json.dumps(data))
        old = attention.Attention(self.path, self.clock, on_urgent=notify)
        self.assertFalse(old.ask('%1', 't1')['urgent'])

    def test_next_falls_back_to_the_last_line_and_is_clipped(self):
        self.world.turn('%1', 'x ' * 200 + '\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.assertEqual(len(self.store.ask('%1', 't1')['next']), attention.LIMIT)
        self.assertTrue(self.store.ask('%1', 't1')['next'].endswith('…'))

    def test_reply_without_status_or_with_no_starts_nothing(self):
        self.world.turn('%1', 'Done.\nNeeds you: no', 10)
        self.world.turn('%2', 'Done.', 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.assertEqual(self.store.waiting(), [])

    def test_prompt_on_screen_starts_an_ask(self):
        self.world.status['%1'] = APPROVAL
        self.world.observe(entry())
        ask = self.store.ask('%1', 't1')
        self.assertEqual(ask['kind'], 'approval')
        self.assertIn('Run the migration?', ask['next'])

    def test_prompt_leaving_the_screen_clears_its_ask(self):
        self.world.status['%1'] = APPROVAL
        self.world.observe(entry())
        self.world.status['%1'] = {'state': 'Working'}
        self.world.observe(entry())
        self.assertIsNone(self.store.ask('%1', 't1'))

    def test_next_turn_needs_you_no_clears(self):
        self.world.turn('%1', 'Next: pick a name.\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.world.turn('%1', 'Picked one myself.\nNeeds you: no', 20)
        self.world.observe(entry())
        self.assertIsNone(self.store.ask('%1', 't1'))

    def test_a_new_turn_without_a_status_line_clears(self):
        # You answered in the terminal and the agent ran another turn: the old ask is stale.
        self.world.turn('%1', 'Next: pick a name.\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.world.turn('%1', 'Done, named it web.', 20)
        self.world.observe(entry())
        self.assertIsNone(self.store.ask('%1', 't1'))

    def test_a_later_yes_keeps_the_first_since_and_resets_opened(self):
        self.world.turn('%1', 'Next: pick a name.\nNeeds you: yes', 10)
        self.world.observe(entry())
        first = self.store.ask('%1', 't1')['since']
        self.store.act('%1', 't1', 'opened')
        self.clock.now += 600
        self.world.turn('%1', 'Next: pick a colour.\nNeeds you: yes', 20)
        self.world.observe(entry())
        ask = self.store.ask('%1', 't1')
        self.assertEqual((ask['since'], ask['next'], ask['opened']), (first, 'pick a colour.', False))

    def test_sending_text_clears(self):
        self.world.turn('%1', 'Next: pick a name.\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.assertTrue(self.store.clear('%1', 't1'))
        self.world.observe(entry())  # the same reply, read again, does not start it again
        self.assertIsNone(self.store.ask('%1', 't1'))

    def test_answering_a_prompt_does_not_restart_it_while_it_is_still_on_screen(self):
        self.world.status['%1'] = APPROVAL
        self.world.observe(entry())
        self.store.clear('%1', 't1')
        self.clock.now += attention.QUIET + 1
        self.world.observe(entry())
        self.assertIsNone(self.store.ask('%1', 't1'))
        self.world.status['%1'] = {'state': 'Working'}
        self.world.observe(entry())
        self.world.status['%1'] = APPROVAL  # the same question, asked again later
        self.world.observe(entry())
        self.assertIsNotNone(self.store.ask('%1', 't1'))

    def test_handled_clears(self):
        self.world.turn('%1', 'Next: pick a name.\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.assertIsNone(self.store.act('%1', 't1', 'handled') and None)
        self.assertIsNone(self.store.ask('%1', 't1'))
        self.world.observe(entry())
        self.assertIsNone(self.store.ask('%1', 't1'))

    def test_pane_going_away_or_changing_conversation_clears(self):
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.turn('%2', 'Next: b.\nNeeds you: yes', 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.assertEqual(len(self.store.waiting()), 2)
        self.world.observe(entry('%2', 't3'))
        self.assertEqual(self.store.waiting(), [])

    def test_snooze_hides_for_a_day_then_returns_with_its_first_since(self):
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.observe(entry())
        since = self.store.ask('%1', 't1')['since']
        snoozed = self.store.act('%1', 't1', 'snooze')
        self.assertEqual(snoozed['snoozed_until'], self.clock.now + attention.SNOOZE)
        self.assertEqual((self.store.waiting(), self.store.snoozed()), ([], 1))
        self.clock.now += attention.SNOOZE + 1
        self.world.observe(entry())
        ask = self.store.waiting()[0]
        self.assertEqual((ask['since'], ask['snoozed_until'], self.store.snoozed()), (since, None, 0))

    def test_opened_only_marks_it_opened(self):
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.observe(entry())
        ask = self.store.act('%1', 't1', 'opened')
        self.assertTrue(ask['opened'])
        self.assertEqual(len(self.store.waiting()), 1)

    def test_stale_conversation_is_refused(self):
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.assertIs(self.store.act('%1', 'old', 'handled'), False)
        self.assertFalse(self.store.clear('%1', 'old'))
        self.assertIsNotNone(self.store.ask('%1', 't1'))
        self.assertIsNone(self.store.ask('%1', 'old'))

    def test_restart_reloads_asks_and_does_not_raise_a_cleared_reply_again(self):
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.turn('%2', 'Next: b.\nNeeds you: yes', 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.store.act('%1', 't1', 'snooze')
        self.store.act('%2', 't2', 'handled')
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data['asks']), {'%1'})
        again = attention.Attention(self.path, self.clock)
        World.__init__(self.world, again)
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.turn('%2', 'Next: b.\nNeeds you: yes', 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.assertEqual(again.ask('%1', 't1')['snoozed_until'], self.clock.now + attention.SNOOZE)
        self.assertIsNone(again.ask('%2', 't2'))

    def test_skips_shell_ambiguous_and_pending_panes(self):
        for pane in ('%1', '%2', '%3'):
            self.world.turn(pane, 'Next: a.\nNeeds you: yes', 10)
        self.world.observe(entry(agent='shell'), entry('%2', ambiguous=True), entry('%3', None, pending_conversation=True))
        self.assertEqual(self.store.waiting(), [])

    def test_waiting_is_oldest_first(self):
        self.world.turn('%2', 'Next: b.\nNeeds you: yes', 10)
        self.world.observe(entry('%2', 't2'))
        self.clock.now += 50
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.assertEqual([ask['pane'] for ask in self.store.waiting()], ['%2', '%1'])

    def test_digest_is_off_by_default_and_sends_once_a_day(self):
        self.world.turn('%1', 'Next: a.\nNeeds you: yes', 10)
        self.world.observe(entry())
        morning = datetime(2026, 11, 8, 9, 5)
        self.assertIsNone(self.store.digest_due(morning))
        self.store.set_digest(True, 9)
        self.assertIsNone(self.store.digest_due(datetime(2026, 11, 8, 8, 59)))
        self.clock.now += 3 * 86400
        payload = self.store.digest_due(morning)
        self.assertEqual(payload['body'], '1 tab waits on you, oldest 3 days.')
        self.assertIsNone(self.store.digest_due(datetime(2026, 11, 8, 15, 0)))
        with self.assertRaises(ValueError):
            self.store.set_digest(True, 24)


class Handler:
    def __init__(self, path):
        self.path, self.sent = path, None

    def respond(self, status, payload):
        self.sent = (status, payload)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = attention.Attention(Path(self.dir.name) / 'a.json', Clock())
        self.store.note_reply('%1', 't1', 'Next: a.\nNeeds you: yes')
        self.panes = {'%1': entry()}

    def tearDown(self):
        self.dir.cleanup()

    def choose(self, pane):
        if pane not in self.panes:
            raise ValueError('gone')
        return self.panes[pane]

    def post(self, action, body):
        handler = Handler(f'{attention.PREFIX}/{action}')
        attention.serve(handler, body, self.choose, self.store)
        return handler.sent

    def test_routes(self):
        self.assertTrue(attention.handles('/mobile/api/attention'))
        self.assertTrue(attention.handles('/mobile/api/attention/snooze'))
        self.assertFalse(attention.handles('/mobile/api/attention/other'))

    def test_opened_snooze_and_handled(self):
        status, body = self.post('opened', {'session': '%1', 'thread': 't1'})
        self.assertEqual((status, body['ask']['opened'], body['waiting']), (200, True, 1))
        status, body = self.post('snooze', {'session': '%1', 'thread': 't1'})
        self.assertEqual((status, body['waiting']), (200, 0))
        status, body = self.post('handled', {'session': '%1', 'thread': 't1'})
        self.assertEqual((status, body), (200, {'ask': None, 'waiting': 0}))

    def test_stale_pair_is_409(self):
        self.assertEqual(self.post('handled', {'session': '%1', 'thread': 'old'})[0], 409)
        self.assertEqual(self.post('handled', {'session': '%9', 'thread': 't1'})[0], 409)
        self.panes['%1'] = entry(thread='t2')  # the tab moved on, but the page still sends t2 for an old ask
        self.assertEqual(self.post('handled', {'session': '%1', 'thread': 't2'})[0], 409)
        self.assertIsNotNone(self.store.ask('%1', 't1'))

    def test_bad_input_is_400(self):
        self.assertEqual(self.post('handled', {'session': 'x', 'thread': 't1'})[0], 400)
        self.assertEqual(self.post('digest', {'enabled': 'yes', 'hour': 9})[0], 400)
        self.assertEqual(self.post('digest', {'enabled': True, 'hour': 7}), (200, {'digest': {'enabled': True, 'hour': 7}}))


class AnswerEndpointTest(unittest.TestCase):
    """Exercise POST dispatch with mocked writes; no sockets or terminals."""

    def setUp(self):
        import server
        self.server = server
        self.config = entry(writable=True)
        for target in ('choose', 'send_text', 'type_text', 'menu_choose', 'respond_approval', 'broadcast_message'):
            patcher = mock.patch.object(server, target)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        self.choose.return_value = self.config
        patcher = mock.patch.object(server.attention.STORE, 'clear')
        self.clear = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, path, **body):
        raw = json.dumps(dict(session='%1', thread='t1', **body)).encode()
        handler = object.__new__(self.server.Handler)
        handler.path = '/mobile/api/' + path
        handler.headers = {'Content-Length': str(len(raw)), 'Host': 'tether.test',
                           'Origin': 'https://tether.test', 'X-Codex-Mobile': '1'}
        handler.rfile = io.BytesIO(raw)
        handler.deny = lambda: False
        handler.respond = mock.Mock()
        handler.do_POST()
        return handler.respond.call_args.args[0]

    def test_message_clears_only_after_success(self):
        self.send_text.return_value = 'submitted'
        self.assertEqual(self.post('message', message='Use default'), 200)
        self.send_text.assert_called_once_with(self.config, 'Use default')
        self.clear.assert_called_once_with('%1', 't1')
        self.clear.reset_mock()
        self.send_text.side_effect = RuntimeError('refused')
        with mock.patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(self.post('message', message='Use default'), 503)
        self.clear.assert_not_called()

    def test_type_clears_only_when_submitted(self):
        self.assertEqual(self.post('type', text='Default', enter=False), 200)
        self.clear.assert_not_called()
        self.assertEqual(self.post('type', text='Default', enter=True), 200)
        self.clear.assert_called_once_with('%1', 't1')

    def test_menu_preserves_the_free_text_answer_and_clears(self):
        self.assertEqual(self.post('menu', choice=1, answer='Default'), 200)
        self.menu_choose.assert_called_once_with(self.config, 1, label=None, answer='Default')
        self.clear.assert_called_once_with('%1', 't1')

    def test_approval_preserves_optional_typed_feedback_and_clears(self):
        self.assertEqual(self.post('approval', action='approve'), 200)
        self.clear.assert_called_once_with('%1', 't1')
        self.clear.reset_mock()
        self.assertEqual(self.post('approval', action='reject', feedback='Use the safer plan'), 200)
        self.respond_approval.assert_called_with(self.config, None, 'reject', choice=None, feedback='Use the safer plan')
        self.clear.assert_called_once_with('%1', 't1')

    def test_broadcast_only_clears_successful_sends(self):
        self.broadcast_message.return_value = {'results': [
            {'pane': '%1', 'result': 'sent'}, {'pane': '%2', 'result': 'sent_busy'},
            {'pane': '%3', 'result': 'skipped'}]}
        self.assertEqual(self.post('broadcast', message='Default'), 200)
        self.assertEqual(self.clear.call_args_list, [mock.call('%1'), mock.call('%2')])

    def test_stale_conversation_does_not_send_or_clear(self):
        self.config['thread'] = 'new'
        self.assertEqual(self.post('message', message='Default'), 409)
        self.send_text.assert_not_called()
        self.clear.assert_not_called()


if __name__ == '__main__':
    unittest.main()
