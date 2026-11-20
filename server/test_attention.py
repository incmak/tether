import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import datetime
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import attention
import lessons


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def entry(pane='%1', thread='t1', agent='claude', **extra):
    return dict(id=pane, thread=thread, agent=agent, **extra)


APPROVAL = {'state': 'Approval', 'approval': {'text': 'Bash command\nRun the migration?'}}


class World:
    """Fake panes with transcripts and a separate live marker for each turn end."""

    def __init__(self, store):
        self.store, self.status, self.reply, self.turn_ids, self.finished = store, {}, {}, {}, {}

    def turn(self, pane, reply, at, *, turn_id=...):
        self.status[pane], self.reply[pane], self.finished[pane] = {'state': 'Idle'}, reply, at
        self.turn_ids[pane] = f'{pane}:{at}' if turn_id is ... else turn_id

    def observe(self, *entries):
        return self.store.observe(entries, lambda e: self.status.get(e['id'], {'state': 'Working'}),
                                  lambda e: (self.reply.get(e['id']), self.turn_ids.get(e['id'])),
                                  lambda e: self.finished.get(e['id']))


class AttentionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'attention.json'
        self.review = self.enterContext(mock.patch.object(lessons, 'review', return_value={'due': False}))
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
        self.on_urgent.assert_not_called()
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.on_urgent.assert_called_once_with(ask)
        self.world.observe(entry())
        self.world.turn('%1', reply, 30, turn_id='%1:20')  # A different live marker can still point to the same turn.
        self.world.observe(entry())
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.on_urgent.assert_called_once()
        self.world.turn('%1', reply, 40)
        self.world.observe(entry())
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.assertEqual(self.on_urgent.call_count, 2)
        self.world.turn('%1', 'Next: normal again\nNeeds you: yes', 50)
        self.world.observe(entry())
        self.assertFalse(self.store.ask('%1', 't1')['urgent'])
        self.assertEqual(self.on_urgent.call_count, 2)

    def test_urgent_requires_needs_you_yes(self):
        self.world.turn('%1', 'Urgent: Ignore this?\nNeeds you: no', 10)
        self.world.observe(entry())
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.assertEqual(self.store.waiting(), [])
        self.on_urgent.assert_not_called()

    def test_opened_during_grace_never_pings(self):
        self.world.turn('%1', 'Urgent: Restore production?\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.on_urgent.assert_not_called()
        self.clock.now += 60
        self.store.act('%1', 't1', 'opened')
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.assertTrue(self.store.ask('%1', 't1')['opened'])
        self.on_urgent.assert_not_called()

    def test_unopened_pings_once_at_the_end_of_grace_even_when_send_returns_false(self):
        self.on_urgent.return_value = False
        self.world.turn('%1', 'Urgent: Roll back production?\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.on_urgent.assert_not_called()
        self.clock.now += attention.URGENT_GRACE - 1
        self.world.observe(entry())
        self.on_urgent.assert_not_called()
        self.clock.now += 1
        self.world.observe(entry())
        self.on_urgent.assert_called_once_with(self.store.ask('%1', 't1'))
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.on_urgent.assert_called_once()

    def test_urgent_callback_runs_outside_lock_with_a_public_copy_and_can_fail(self):
        locked, observed = [], []
        def notify(ask):
            locked.append(self.store.lock.locked())
            if not locked[-1]:
                saved = json.loads(self.path.read_text())['asks']['%1']
                observed.append((self.store.ask('%1', 't1'), dict(ask), saved['pinged']))
            ask['next'] = 'Do not change the stored ask'
            raise RuntimeError('notification failed')
        self.on_urgent.side_effect = notify
        self.world.turn('%1', 'Urgent: Choose?\nNeeds you: yes', 10)
        self.assertTrue(self.world.observe(entry()))
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.assertEqual(locked, [False])
        ask = self.store.ask('%1', 't1')
        self.assertEqual(observed, [(ask, ask, True)])
        self.assertNotIn('reply', observed[0][1])
        self.assertEqual(self.store.ask('%1', 't1')['next'], 'Choose?')
        self.assertTrue(json.loads(self.path.read_text())['asks']['%1']['urgent'])
        self.world.observe(entry())
        self.on_urgent.assert_called_once()

    def test_urgent_restart_does_not_notify_again_and_old_asks_default_to_false(self):
        reply = 'Urgent: Choose?\nNeeds you: yes'
        self.world.turn('%1', reply, 10)
        self.world.observe(entry())
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry())
        self.on_urgent.assert_called_once()
        notify = mock.Mock()
        again = attention.Attention(self.path, self.clock, on_urgent=notify)
        self.assertTrue(again.ask('%1', 't1')['urgent'])
        self.world.store = again
        self.world.observe(entry())
        notify.assert_not_called()
        data = json.loads(self.path.read_text())
        del data['asks']['%1']['ping_due']
        del data['asks']['%1']['pinged']
        self.path.write_text(json.dumps(data))
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=notify)
        self.world.observe(entry())
        notify.assert_not_called()
        del data['asks']['%1']['urgent']
        self.path.write_text(json.dumps(data))
        old = attention.Attention(self.path, self.clock, on_urgent=notify)
        self.assertFalse(old.ask('%1', 't1')['urgent'])

    def test_restart_during_grace_keeps_the_original_deadline(self):
        self.world.turn('%1', 'Urgent: Choose?\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.clock.now += attention.URGENT_GRACE - 1
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.observe(entry())
        self.on_urgent.assert_not_called()
        self.clock.now += 1
        self.world.observe(entry())
        self.world.observe(entry())
        self.on_urgent.assert_called_once_with(self.world.store.ask('%1', 't1'))

    def test_urgent_ping_is_suppressed_when_the_ask_changes_during_grace(self):
        for i, change in enumerate(('handled', 'clear', 'snooze', 'non-urgent', 'pane gone'), start=1):
            with self.subTest(change=change):
                pane = f'%{i}'
                self.world.turn(pane, 'Urgent: Choose?\nNeeds you: yes', 10)
                self.world.observe(entry(pane))
                entries = (entry(pane),)
                self.clock.now += 60
                if change in ('handled', 'snooze'):
                    self.store.act(pane, 't1', change)
                elif change == 'clear':
                    self.store.clear(pane, 't1')
                elif change == 'non-urgent':
                    self.world.turn(pane, 'Next: Choose later.\nNeeds you: yes', 20)
                    self.world.observe(*entries)
                    self.assertNotIn('ping_due', self.store.asks[pane])
                else:
                    entries = ()
                self.clock.now += attention.URGENT_GRACE - 60
                self.world.observe(*entries)
                self.on_urgent.assert_not_called()

    def test_snoozed_urgent_ask_never_pings_even_after_the_snooze_expires(self):
        self.world.turn('%1', 'Urgent: Restore production?\nNeeds you: yes', 10)
        self.world.observe(entry())
        ask = self.store.act('%1', 't1', 'snooze')
        self.clock.now = ask['snoozed_until'] - 1
        self.world.observe(entry())
        self.on_urgent.assert_not_called()
        self.clock.now += 1
        self.world.observe(entry())
        self.world.observe(entry())
        self.on_urgent.assert_not_called()  # snoozing means you saw it; the ask returns in Tether, not on Teams

    def test_different_urgent_reply_restarts_grace_and_resets_opened(self):
        self.world.turn('%1', 'Urgent: Restore production?\nNeeds you: yes', 10)
        self.world.observe(entry())
        self.store.act('%1', 't1', 'opened')
        self.clock.now += attention.URGENT_GRACE - 1
        self.world.turn('%1', 'Urgent: Roll back production?\nNeeds you: yes', 20)
        self.world.observe(entry())
        self.assertFalse(self.store.ask('%1', 't1')['opened'])
        self.clock.now += attention.URGENT_GRACE - 1
        self.world.observe(entry())
        self.on_urgent.assert_not_called()
        self.clock.now += 1
        self.world.observe(entry())
        self.world.observe(entry())
        self.on_urgent.assert_called_once_with(self.store.ask('%1', 't1'))

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
        self.world.turn('%2', '', 10, turn_id=None)  # The new conversation has no reply yet.
        self.world.observe(entry('%2', 't3'))
        self.assertEqual(self.store.waiting(), [])

    def test_bad_scan_and_restart_restore_open_ask_and_keep_handled_turn_quiet(self):
        self.world.turn('%1', 'Urgent: Deploy?\nNeeds you: yes', 10)
        self.world.turn('%2', 'Next: Pick a name.\nNeeds you: yes', 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.store.act('%1', 't1', 'opened')
        opened = self.store.act('%1', 't1', 'snooze')
        self.store.act('%2', 't2', 'handled')
        self.clock.now += 60
        self.world.observe(entry(agent='shell'), entry('%2', 't2', agent='shell'))
        self.assertEqual(self.store.waiting(), [])
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.observe(entry(), entry('%2', 't2'))
        self.assertEqual(self.world.store.ask('%1', 't1'), opened)
        self.assertIsNone(self.world.store.ask('%2', 't2'))
        self.clock.now += attention.URGENT_GRACE
        self.world.observe(entry(), entry('%2', 't2'))
        self.on_urgent.assert_not_called()

    def test_reboot_moves_open_ask_and_keeps_handled_turn_quiet_on_new_panes(self):
        self.world.turn('%1', 'Urgent: Deploy?\nNeeds you: yes', 10, turn_id='deploy')
        self.world.turn('%2', 'Next: Pick a name.\nNeeds you: yes', 10, turn_id='name')
        self.world.observe(entry(), entry('%2', 't2'))
        opened = self.store.ask('%1', 't1')
        self.store.act('%2', 't2', 'handled')
        self.clock.now += 60
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.observe(entry('saved', agent='codex'))  # Discovery falls back to the last chat when tmux is down.
        self.assertEqual(self.world.store.waiting(), [])
        self.world.turn('%8', 'Urgent: Deploy?\nNeeds you: yes', None, turn_id='deploy')
        self.world.turn('%9', 'Next: Pick a name.\nNeeds you: yes', None, turn_id='name')
        self.world.observe(entry('%8'), entry('%9', 't2'))
        restored = dict(opened, pane='%8')
        self.assertEqual(self.world.store.waiting(), [restored])
        self.clock.now += attention.URGENT_GRACE - 60
        self.world.observe(entry('%8'), entry('%9', 't2'))
        self.on_urgent.assert_called_once_with(restored)

    def test_conversation_change_parks_reply_but_discards_prompt(self):
        self.world.turn('%1', 'Next: Deploy?\nNeeds you: yes', 10, turn_id='deploy')
        self.world.status['%2'] = APPROVAL
        self.world.observe(entry(), entry('%2', 't2'))
        original = self.store.ask('%1', 't1')
        self.world.status['%1'] = {'state': 'Working'}
        self.world.observe(entry('%1', 'other'))
        self.assertEqual(self.store.waiting(), [])
        self.world.status['%2'] = {'state': 'Working'}
        self.world.observe(entry(), entry('%2', 't2'))
        self.assertEqual(self.store.waiting(), [original])

    def test_same_words_in_a_new_turn_raise_a_new_ask_after_handled(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        self.world.turn('%1', reply, 10)
        self.world.observe(entry())
        self.store.act('%1', 't1', 'handled')
        self.clock.now += 20
        self.world.turn('%1', reply, 20)
        self.world.observe(entry())
        self.assertEqual([(ask['next'], ask['since']) for ask in self.store.waiting()], [('deploy?', self.clock.now)])

    def test_cleared_reply_and_no_reply_stay_cleared_after_restart(self):
        self.world.turn('%1', 'Next: deploy?\nNeeds you: yes', 10)
        self.world.turn('%2', 'Next: review?\nNeeds you: yes', 10)
        panes = (entry(), entry('%2', 't2'))
        self.world.observe(*panes)
        self.store.clear('%1', 't1')
        self.world.turn('%2', 'Reviewed.\nNeeds you: no', 20)
        self.world.observe(*panes)
        self.assertEqual(self.store.waiting(), [])
        self.world.observe()
        self.world.store = attention.Attention(self.path, self.clock)
        self.world.observe(*panes)
        self.assertEqual(self.world.store.waiting(), [])

    def test_stale_codex_turn_stays_handled_after_newer_turn_and_restart(self):
        pane = entry(agent='codex')
        reply = 'Next: deploy?\nNeeds you: yes'
        self.world.turn('%1', reply, 10, turn_id='A')
        self.world.observe(pane)
        self.store.act('%1', 't1', 'handled')
        self.world.turn('%1', 'Done.\nNeeds you: no', 20, turn_id='B')
        self.world.observe(pane)
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.turn('%1', reply, 20, turn_id='A')
        self.world.observe(pane)
        self.assertEqual(self.world.store.waiting(), [])
        self.assertEqual(json.loads(self.path.read_text())['turns']['t1']['turn'], 'B')

    def test_stale_turn_from_old_store_does_not_clear_newer_ask(self):
        self.path.write_text(json.dumps({'turns': {'t1': {'turn': 0, 'at': self.clock.now}}}))
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        pane = entry(agent='antigravity')
        self.world.turn('%1', 'Next: deploy?\nNeeds you: yes', 20, turn_id=1)
        self.world.observe(pane)
        self.world.turn('%1', 'Done.\nNeeds you: no', 30, turn_id=0)
        self.world.observe(pane)
        self.assertEqual([ask['next'] for ask in self.world.store.waiting()], ['deploy?'])

    def test_turn_history_retains_the_latest_fifty_ids_across_restart(self):
        pane = entry(agent='antigravity')
        for step in range(51):
            self.world.turn('%1', 'Done.\nNeeds you: no', step, turn_id=step)
            self.world.observe(pane)
        self.assertEqual(json.loads(self.path.read_text())['turns']['t1'].get('history'), list(range(1, 51)))
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.turn('%1', 'Next: deploy?\nNeeds you: yes', 51, turn_id=1)
        self.world.observe(pane)
        self.assertEqual(self.world.store.waiting(), [])

    def test_upgrade_adopts_old_digests_without_changing_asks(self):
        reply = 'Urgent: Deploy?\nNeeds you: yes'
        digest = hashlib.sha256(reply.encode()).hexdigest()[:24]
        original = dict(pane='%1', thread='t1', since=self.clock.now - 500, next='Deploy?', kind='reply',
                        opened=True, snoozed_until=None, urgent=True)
        self.path.write_text(json.dumps({'version': 1, 'asks': {'%1': dict(original, reply=digest)},
                                         'seen': {'%2': {'thread': 't2', 'reply': digest}}}))
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        panes = (entry(), entry('%2', 't2'))
        for pane in ('%1', '%2'):
            self.world.turn(pane, reply, 10)
        self.world.observe(*panes)
        self.assertEqual(self.world.store.waiting(), [original])
        data = json.loads(self.path.read_text())
        self.assertEqual(data['turns'], {'t1': {'turn': '%1:10', 'history': ['%1:10'], 'at': self.clock.now},
                                         't2': {'turn': '%2:10', 'history': ['%2:10'], 'at': self.clock.now}})
        self.world.turn('%2', reply, 20)
        self.world.observe(*panes)
        self.assertEqual(self.world.store.ask('%2', 't2')['since'], self.clock.now)
        data = json.loads(self.path.read_text())
        self.assertNotIn('reply', data['asks']['%2'])
        self.assertNotIn('reply', data['seen'].get('%2', {}))

    def test_upgrade_moves_legacy_asks_by_conversation_across_restarts(self):
        reply = 'Urgent: Deploy?\nNeeds you: yes'
        digest = hashlib.sha256(reply.encode()).hexdigest()[:24]
        original = dict(pane='%1', thread='t1', since=self.clock.now - 500, next='Deploy?', kind='reply',
                        opened=True, snoozed_until=None, urgent=True)
        for source in ('handled', 'open'):
            with self.subTest(source=source):
                seen = {'%1': {'thread': 't1', 'reply': digest}} if source == 'handled' else {}
                asks = {'%1': dict(original, reply=digest)} if source == 'open' else {}
                self.path.write_text(json.dumps({'seen': seen, 'asks': asks}))
                self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
                self.world.observe()
                saved = json.loads(self.path.read_text())
                self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
                self.world.turn('%8', reply, 10, turn_id='deploy')
                self.world.observe(entry('%8'))
                expected = [dict(original, pane='%8')] if source == 'open' else []
                self.assertEqual(self.world.store.waiting(), expected)
                self.assertIn('t1', saved.get('legacy', {}))
                self.assertNotIn('t1', json.loads(self.path.read_text())['legacy'])

    def test_upgrade_legacy_digest_expires_from_first_load(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        digest = hashlib.sha256(reply.encode()).hexdigest()[:24]
        self.path.write_text(json.dumps({'seen': {'%1': {'thread': 't1', 'reply': digest}}}))
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.observe()
        self.clock.now += 20 * 86400
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.clock.now += 11 * 86400
        self.world.turn('%1', reply, 10)
        self.world.observe(entry())
        self.assertEqual([ask['since'] for ask in self.world.store.waiting()], [self.clock.now])

    def test_empty_reply_retries_the_same_finished_marker(self):
        self.world.turn('%1', '', 10, turn_id=None)
        self.world.observe(entry())
        self.world.turn('%1', 'Next: deploy?\nNeeds you: yes', 10, turn_id='deploy')
        self.world.observe(entry())
        self.assertEqual([ask['next'] for ask in self.store.waiting()], ['deploy?'])

    def test_upgrade_adopts_legacy_reply_after_empty_startup_read(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        digest = hashlib.sha256(reply.encode()).hexdigest()[:24]
        self.path.write_text(json.dumps({'seen': {'%1': {'thread': 't1', 'reply': digest}}}))
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.turn('%1', '', 10, turn_id=None)
        self.world.observe(entry())
        self.world.turn('%1', reply, 20, turn_id='deploy')
        self.world.observe(entry())
        self.assertEqual(self.world.store.waiting(), [])
        self.assertEqual(json.loads(self.path.read_text())['turns']['t1']['turn'], 'deploy')

    def test_empty_reply_does_not_count_as_first_successful_read_without_turn_id(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        self.world.turn('%1', '', 10, turn_id=None)
        self.world.observe(entry())
        self.world.turn('%1', reply, 20, turn_id=None)
        self.world.observe(entry())
        self.assertEqual(self.store.waiting(), [])
        self.world.turn('%1', reply, 30, turn_id=None)
        self.world.observe(entry())
        self.assertEqual([ask['next'] for ask in self.store.waiting()], ['deploy?'])

    def test_missing_turn_id_needs_a_live_turn_end_not_a_file_change(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        versions = iter(range(100))
        observe = lambda: self.store.observe([entry()], lambda e: {'state': 'Idle'}, lambda e: (reply, None),
                                             lambda e: self.world.finished.get('%1'), lambda e: next(versions))
        self.world.finished['%1'] = 10
        observe()
        observe()  # a token-count record changes the file, not the turn end
        self.assertEqual(self.store.waiting(), [])
        self.world.finished['%1'] = 20
        observe()
        self.assertEqual([ask['next'] for ask in self.store.waiting()], ['deploy?'])

    def test_missing_turn_id_requires_a_live_turn_end(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        self.world.turn('%1', reply, None, turn_id=None)
        self.world.observe(entry())
        self.assertEqual(self.store.waiting(), [])
        self.world.turn('%1', reply, 10, turn_id=None)
        self.world.observe(entry())
        self.assertEqual([ask['next'] for ask in self.store.waiting()], ['deploy?'])
        self.store.act('%1', 't1', 'handled')
        self.world.turn('%1', reply, None, turn_id=None)
        self.world.observe(entry())
        self.assertEqual(self.store.waiting(), [])
        self.world.turn('%1', reply, 10, turn_id=None)
        self.world.store = attention.Attention(self.path, self.clock)
        self.world.observe(entry())
        self.assertEqual(self.world.store.waiting(), [])

    def test_live_handled_turn_stays_quiet_after_thirty_one_days_and_restart(self):
        pane = entry()
        self.world.turn('%1', 'Next: deploy?\nNeeds you: yes', 10)
        self.world.observe(pane)
        self.store.act('%1', 't1', 'handled')
        self.clock.now += 31 * 86400
        self.world.observe(pane)
        self.world.store = attention.Attention(self.path, self.clock, on_urgent=self.on_urgent)
        self.world.observe(pane)
        self.assertEqual(self.world.store.waiting(), [])

    def test_live_working_conversation_refreshes_turn_age_at_most_once_a_day(self):
        pane = entry(agent='codex')
        self.world.turn('%1', 'Done.\nNeeds you: no', 10)
        self.world.observe(pane)
        self.world.status['%1'] = {'state': 'Working'}
        self.clock.now += 86401
        self.assertTrue(self.world.observe(pane))
        refreshed = json.loads(self.path.read_text())['turns']['t1']['at']
        self.assertEqual(refreshed, self.clock.now)
        self.clock.now += 3600
        self.assertFalse(self.world.observe(pane))
        self.assertEqual(json.loads(self.path.read_text())['turns']['t1']['at'], refreshed)

    def test_prunes_only_turns_and_parked_asks_older_than_thirty_days(self):
        reply = 'Next: deploy?\nNeeds you: yes'
        for pane in ('%1', '%2'):
            self.world.turn(pane, reply, 10)
        self.world.observe(entry(), entry('%2', 't2'))
        self.store.act('%2', 't2', 'handled')
        self.world.observe()
        self.clock.now += 30 * 86400
        self.world.turn('%3', reply, 10)
        self.world.observe(entry('%3', 't3'))
        self.world.observe()
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data['turns']), {'t1', 't2', 't3'})
        self.assertEqual(set(data['parked']), {'t1', 't3'})
        self.clock.now += 1
        self.store.set_digest(True, 9)
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data['turns']), {'t3'})
        self.assertEqual(set(data['parked']), {'t3'})

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


    def test_digest_includes_lesson_review_with_or_without_asks(self):
        for asks in (False, True):
            for reason, count in (('disputed', 7), ('new', 7), ('time', 20)):
                with self.subTest(asks=asks, reason=reason):
                    self.store.set_digest(True, 9)
                    self.store.digest['sent'] = None
                    self.review.return_value = {'due': True, 'reason': reason, 'new': 5, 'disputed': 2, 'total': 20}
                    if asks:
                        self.world.turn('%1', 'Next: choose.\nNeeds you: yes', 10)
                        self.world.observe(entry())
                    payload = self.store.digest_due(datetime(2026, 11, 8, 9, 5))
                    expected = (f'1 tab waits on you, oldest 1 minutes. {count} lessons to review.' if asks
                                else f'Lessons to review: {count}.')
                    self.assertEqual(payload['body'], expected)
                    self.assertEqual(payload['url'], '/chat/?go=attention')
                    self.assertIsNone(self.store.digest_due(datetime(2026, 11, 8, 15)))

    def test_digest_review_count_of_one_is_singular(self):
        self.store.set_digest(True, 9)
        self.review.return_value = {'due': True, 'reason': 'disputed', 'new': 0, 'disputed': 1, 'total': 4}
        self.world.turn('%1', 'Next: choose.\nNeeds you: yes', 10)
        self.world.observe(entry())
        payload = self.store.digest_due(datetime(2026, 11, 8, 9, 5))
        self.assertEqual(payload['body'], '1 tab waits on you, oldest 1 minutes. 1 lesson to review.')

    def test_digest_review_error_keeps_previous_behavior(self):
        self.store.set_digest(True, 9)
        self.review.side_effect = RuntimeError('review unavailable')
        self.assertIsNone(self.store.digest_due(datetime(2026, 11, 8, 9)))
        self.world.turn('%1', 'Next: choose.\nNeeds you: yes', 10)
        self.world.observe(entry())
        payload = self.store.digest_due(datetime(2026, 11, 9, 9))
        self.assertEqual(payload['body'], '1 tab waits on you, oldest 1 minutes.')


class Handler:
    def __init__(self, path):
        self.path, self.sent = path, None

    def respond(self, status, payload):
        self.sent = (status, payload)


class TranscriptTurnTest(unittest.TestCase):
    fixtures = Path(__file__).parent / 'fixtures' / 'attention'

    def test_reply_and_turn_id_come_from_one_transcript_read(self):
        import server

        for agent, turn_id in (
                ('claude', '1f543b95-34b4-49c2-83ac-257609c6a380'),
                ('codex', '2026-11-08T08:30:12.000Z/msg_example'),
                ('antigravity', 3)):
            with self.subTest(agent=agent), mock.patch.object(server, 'notification_records', wraps=server.notification_records) as read:
                pane = entry(agent=agent, transcript=self.fixtures / f'{agent}.jsonl')
                self.assertEqual(server.notification_reply_turn(pane), ('Next: Deploy?\nNeeds you: yes', turn_id))
                read.assert_called_once_with(pane)

    def test_partial_record_is_read_again_after_restart_and_pane_move(self):
        import server

        def record(stamp, text):
            return json.dumps({'timestamp': stamp, 'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                'content': [{'type': 'output_text', 'text': text}]}}) + '\n'

        old = record('2026-11-08T08:30:12.000Z', 'Next: Deploy?\nNeeds you: yes')
        new = record('2026-11-08T09:00:00.000Z', 'Next: Merge?\nNeeds you: yes')
        for pane_id in ('%1', '%8'):
            with self.subTest(pane=pane_id), tempfile.TemporaryDirectory() as folder:
                path, transcript = Path(folder) / 'attention.json', Path(folder) / 'rollout.jsonl'
                transcript.write_text(old)
                store = attention.Attention(path, Clock())
                pane = entry('%1', agent='codex', transcript=transcript)
                status = lambda e: {'state': 'Idle'}
                finished = lambda e: None
                with mock.patch.object(server, 'timeline', return_value={'since': None, 'finished': None}):
                    store.observe([pane], status, server.notification_reply_turn, finished, server.notification_version)
                    store.act('%1', 't1', 'handled')
                    store = attention.Attention(path, Clock())  # restart
                    pane = entry(pane_id, agent='codex', transcript=transcript)
                    transcript.write_text(old + new[:40])  # the new record is still being written
                    store.observe([pane], status, server.notification_reply_turn, finished, server.notification_version)
                    self.assertEqual(store.waiting(), [])
                    transcript.write_text(old + new)
                    store.observe([pane], status, server.notification_reply_turn, finished, server.notification_version)
                self.assertEqual([ask['next'] for ask in store.waiting()], ['Merge?'])

    def test_codex_timestamp_without_payload_id_and_antigravity_step_zero(self):
        import server

        for agent, expected in (('codex', ('I will check.', '2026-11-08T08:30:11.000Z')),
                                ('antigravity', ('Checking.', 0))):
            with self.subTest(agent=agent):
                pane = entry(agent=agent, transcript=self.fixtures / f'{agent}.jsonl')
                self.assertEqual(server.notification_reply_turn(pane, server.notification_records(pane)[:1]), expected)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = attention.Attention(Path(self.dir.name) / 'a.json', Clock())
        self.store.note_reply('%1', 't1', 'Next: a.\nNeeds you: yes', 'first')
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

    def test_ping_state_is_private_in_public_asks_and_api_responses(self):
        self.store.note_reply('%1', 't1', 'Urgent: Choose now?\nNeeds you: yes', 'urgent')
        ask = self.store.asks['%1']
        private = {'ping_due', 'pinged'}
        self.assertTrue(private <= ask.keys())
        handler = Handler(attention.PREFIX)
        with mock.patch.object(attention, 'STORE', self.store):
            attention.get(handler)
        status, body = handler.sent
        self.assertEqual(status, 200)
        self.assertEqual(len(body['asks']), 1)
        status, opened = self.post('opened', {'session': '%1', 'thread': 't1'})
        self.assertEqual(status, 200)
        for public in (self.store.public(ask), body['asks'][0], opened['ask']):
            self.assertTrue(public['urgent'])
            self.assertFalse(private & public.keys())

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
        self.send_text.assert_called_once_with(self.config, 'Use default', user=True)
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
        self.respond_approval.assert_called_with(self.config, None, 'reject', choice=None, feedback='Use the safer plan',
                                                on_answer=mock.ANY)
        self.clear.assert_called_once_with('%1', 't1')

    def test_broadcast_only_clears_successful_sends(self):
        self.broadcast_message.return_value = {'results': [
            {'pane': '%1', 'result': 'sent'}, {'pane': '%2', 'result': 'sent_busy'},
            {'pane': '%3', 'result': 'skipped'}]}
        self.assertEqual(self.post('broadcast', message='Default'), 200)
        self.assertEqual(self.clear.call_args_list, [mock.call('%1'), mock.call('%2')])

    def test_teams_answer_clears_the_ask_only_when_delivered(self):
        """A Teams answer (B126) is your answer to the tab: the ask goes, as for an answer typed in Tether."""
        with tempfile.TemporaryDirectory() as folder:
            store = attention.Attention(Path(folder) / 'attention.json', Clock(), on_urgent=mock.Mock())
            item = {'pane': '%1', 'thread': 't1'}
            with mock.patch.object(self.server.attention, 'STORE', store), mock.patch.object(self.server.broadcast, 'deliver') as deliver:
                for result, left in (('skipped', 1), ('failed', 1), ('sent', 0), ('sent_busy', 0)):
                    with self.subTest(result=result):
                        store.note_reply('%1', 't1', f'Pick a plan for {result}.\nNeeds you: yes', result)
                        self.assertEqual(len(store.waiting()), 1)
                        deliver.return_value = {'results': [{'pane': '%1', 'name': 'work', 'result': result}]}
                        self.assertEqual(self.server.teams_answer('[Answer from Teams] B', item)['result'], result)
                        self.assertEqual(len(store.waiting()), left)
                # An answer for an earlier conversation of the pane leaves the new conversation's ask alone.
                store.note_reply('%1', 't2', 'Another one.\nNeeds you: yes', 'another')
                store.asks['%1']['thread'] = 't2'
                deliver.return_value = {'results': [{'pane': '%1', 'name': 'work', 'result': 'sent'}]}
                self.server.teams_answer('[Answer from Teams] B', item)
                self.assertEqual(len(store.waiting()), 1)

    def test_stale_conversation_does_not_send_or_clear(self):
        self.config['thread'] = 'new'
        self.assertEqual(self.post('message', message='Default'), 409)
        self.send_text.assert_not_called()
        self.clear.assert_not_called()


if __name__ == '__main__':
    unittest.main()
