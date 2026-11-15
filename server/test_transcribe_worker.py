import sys
import io
import types
import unittest
from types import SimpleNamespace as NS
from unittest import mock

# The worker loads faster-whisper models at import; replace them with stubs.
fake = types.ModuleType('faster_whisper')
fake.WhisperModel = mock.MagicMock()
fake.decode_audio = mock.MagicMock()
import os
from own_time import own_time
os.environ['TETHER_VOICE_ENGINE'] = 'whisper'  # never load or download Parakeet in tests
with mock.patch.dict(sys.modules, {'faster_whisper': fake}):
    import transcribe_worker as worker


def word(text, start, end):
    return NS(word=text, start=start, end=end)


class SettledTest(unittest.TestCase):
    def test_cuts_inside_one_long_segment(self):
        # One segment runs to the end of the clip: the old code saved nothing here.
        words = [word(' one', 0.0, 0.5), word(' two', 0.55, 1.0), word(' three', 1.05, 1.5), word(' four', 1.55, 2.0)]
        self.assertEqual(worker.settled([NS(words=words)], 1.2), ('one two', 1.025))

    def test_prefers_a_recent_pause(self):
        words = [word(' a', 0.0, 0.4), word(' b', 0.8, 1.2), word(' c', 1.25, 1.6), word(' d', 1.65, 2.0)]
        text, end = worker.settled([NS(words=words)], 1.7)
        self.assertEqual(text, 'a')
        self.assertAlmostEqual(end, 0.6)

    def test_ignores_an_old_pause(self):
        words = [word(' a', 0.0, 0.4), word(' b', 1.0, 1.4)] + [word(' w', 1.45 + i * 0.5, 1.9 + i * 0.5) for i in range(10)]
        text, end = worker.settled([NS(words=words)], 6.5)
        self.assertEqual(text.split()[:2], ['a', 'b'])
        self.assertGreater(end, 5.0)

    def test_nothing_settled(self):
        self.assertIsNone(worker.settled([NS(words=[word(' late', 3.0, 3.5)])], 2.0))
        self.assertIsNone(worker.settled([NS(words=None)], 2.0))

    def test_commit_moves_done_forward(self):
        item = worker.Recording()
        item.done = (2.0, 'hello')
        words = [word(' there', 0.0, 0.5), word(' friend', 0.55, 1.0), word(' tail', 6.0, 6.5)]
        samples = [0.0] * int(9.0 * worker.RATE)
        with mock.patch.object(worker, 'accurate', return_value=([NS(words=words)], None)) as accurate:
            worker.commit(item, samples)
        accurate.assert_called_once()
        self.assertEqual(accurate.call_args.kwargs, {'words': True, 'stop': worker.final_waits})
        self.assertEqual(item.done[1], 'hello there friend')
        self.assertAlmostEqual(item.done[0], 2.0 + 3.5)  # middle of the 1.0 s to 6.0 s pause
        self.assertFalse(item.lock.locked())



def segment(text, end):
    return NS(text=text, end=end, words=None)


class FinalPassTest(unittest.TestCase):
    """The 175 s hang (3 Nov 2026): a final pass must not retry forever or wait behind background passes."""

    def setUp(self):
        # The stub returns one shared mock for both models; give each its own.
        for name in ('MODEL', 'LIVE_MODEL'):
            patcher = mock.patch.object(worker, name, mock.MagicMock())
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_accurate_pass_uses_one_temperature_and_a_token_cap(self):
        worker.MODEL.transcribe.return_value = (iter([segment(' hi', 1.0)]), None)
        found, cut = worker.accurate([0.0] * 16000)
        self.assertEqual(([item.text for item in found], cut), ([' hi'], None))
        options = worker.MODEL.transcribe.call_args.kwargs
        self.assertEqual((options['temperature'], options['condition_on_previous_text'], options['max_new_tokens']), (0.0, False, worker.MAX_TOKENS))

    def test_final_over_budget_lets_the_fast_model_finish(self):
        clock = iter([0.0, 0.0, 5.0, 25.0, 26.0, 27.0])
        worker.MODEL.transcribe.return_value = (iter([segment(' first part', 10.0), segment(' second part', 20.0), segment(' never', 30.0)]), None)
        worker.LIVE_MODEL.transcribe.return_value = (iter([segment(' rough rest', 0.0)]), None)
        samples = [0.0] * (40 * worker.RATE)
        with own_time(worker, monotonic=mock.Mock(side_effect=lambda: next(clock, 99.0))), mock.patch.object(worker, 'FINAL_BUDGET', 20.0):
            self.assertEqual(worker.final(samples), 'first part second part rough rest')
        rest = worker.LIVE_MODEL.transcribe.call_args.args[0]
        self.assertEqual(len(rest), 20 * worker.RATE)  # the fast model starts where the accurate one stopped
        self.assertEqual(worker.WAITING[0], 0)

    def test_final_gives_up_on_a_busy_lock_and_uses_the_fast_model(self):
        worker.LIVE_MODEL.transcribe.return_value = (iter([segment(' all rough', 0.0)]), None)
        worker.FINAL_LOCK.acquire()
        try:
            with mock.patch.object(worker, 'FINAL_BUDGET', 0.05):
                self.assertEqual(worker.final([0.0] * 16000), 'all rough')
        finally:
            worker.FINAL_LOCK.release()
        worker.MODEL.transcribe.assert_not_called()

    def test_background_pass_stops_when_a_final_waits(self):
        item = worker.Recording()
        worker.MODEL.transcribe.return_value = (iter([segment(' a', 5.0), segment(' b', 10.0)]), None)
        worker.WAITING[0] += 1
        try:
            worker.commit(item, [0.0] * (20 * worker.RATE))
        finally:
            worker.WAITING[0] -= 1
        worker.MODEL.transcribe.assert_not_called()  # it does not even start
        self.assertEqual(item.done, (0.0, ''))

    def test_background_pass_stops_mid_way_and_keeps_what_settled(self):
        item = worker.Recording()
        words = [word(' one', 0.0, 1.0), word(' two', 1.5, 2.0)]
        segments = [NS(text=' one two', end=2.0, words=words), NS(text=' later', end=12.0, words=[word(' later', 11.0, 12.0)])]
        worker.MODEL.transcribe.return_value = (iter(segments), None)
        with mock.patch.object(worker, 'final_waits', side_effect=[False] + [True] * 5):
            worker.commit(item, [0.0] * (20 * worker.RATE))
        self.assertEqual(item.done[1], 'one')  # cut in the pause before "two"; "later" was never reached
        self.assertAlmostEqual(item.done[0], 1.25)


class PhraseTest(unittest.TestCase):
    def request(self, phrase='1', path='/transcribe'):
        value = worker.Handler.__new__(worker.Handler)
        value.path = path
        value.headers = {'Content-Length': '1', 'X-Live-Phrase': phrase, 'X-Recording': 'recording-123'}
        value.rfile = io.BytesIO(b'x')
        value.respond = mock.Mock()
        return value

    def test_phrase_uses_whole_clip_and_own_lock_without_vad_or_recording_reuse(self):
        samples = [0.0] * worker.RATE
        model = mock.Mock()
        def segments():
            self.assertTrue(worker.PHRASE_LOCK.locked())
            yield segment(' soft words', 1.0)
        model.transcribe.return_value = (segments(), None)
        with mock.patch.object(worker, 'MODEL', model), \
                mock.patch.object(worker, 'decode_audio', return_value=samples), \
                mock.patch.object(worker, 'FINAL_LOCK') as final_lock, \
                mock.patch.object(worker, 'recording') as recording, \
                mock.patch.object(worker, 'quick') as quick:
            value = self.request(path='/transcribe?live=1')
            worker.Handler.do_POST(value)
        self.assertEqual(value.respond.call_args.args[0], 200)
        self.assertEqual(value.respond.call_args.args[1]['text'], 'soft words')
        model.transcribe.assert_called_once_with(samples, language=worker.LANGUAGE, initial_prompt=worker.PROMPT,
                                                vad_filter=False, beam_size=worker.BEAM, word_timestamps=False,
                                                temperature=0.0, condition_on_previous_text=False, max_new_tokens=worker.MAX_TOKENS)
        final_lock.acquire.assert_not_called()
        final_lock.release.assert_not_called()
        recording.assert_not_called()
        quick.assert_not_called()
        self.assertFalse(worker.PHRASE_LOCK.locked())

    def test_duration_limit_rejects_over_30_seconds_before_model_call(self):
        with mock.patch.object(worker, 'MODEL') as model, mock.patch.object(worker, 'decode_audio') as decode:
            decode.return_value = [0.0] * (30 * worker.RATE + 1)
            value = self.request()
            worker.Handler.do_POST(value)
            self.assertEqual(value.respond.call_args.args[0], 413)
            model.transcribe.assert_not_called()
            decode.return_value = [0.0] * (30 * worker.RATE)
            model.transcribe.return_value = (iter([segment(' allowed', 30.0)]), None)
            self.assertEqual(worker.transcribe(b'x', phrase=True), ('allowed', 30.0))

    def test_phrase_releases_lock_after_model_error(self):
        with mock.patch.object(worker, 'MODEL') as model, \
                mock.patch.object(worker, 'decode_audio', return_value=[0.0]):
            model.transcribe.side_effect = RuntimeError('bad audio')
            value = self.request()
            worker.Handler.do_POST(value)
        self.assertEqual(value.respond.call_args.args[0], 400)
        self.assertFalse(worker.PHRASE_LOCK.locked())

    def test_only_header_value_one_selects_phrase_path(self):
        for phrase in ('', '0', 'true'):
            with mock.patch.object(worker, 'transcribe', return_value=('hi', 1.0)) as transcribe:
                worker.Handler.do_POST(self.request(phrase=phrase))
            transcribe.assert_called_once_with(b'x', live=False, key='recording-123', phrase=False)


class ParakeetTest(unittest.TestCase):
    """The Parakeet path: worker.PARAKEET is (plain model, model with VAD)."""
    def setUp(self):
        self.plain, self.vad = mock.MagicMock(), mock.MagicMock()
        patches = [mock.patch.object(worker, 'PARAKEET', (self.plain, self.vad)), mock.patch.dict(worker.RECORDINGS, clear=True),
                   mock.patch.object(worker, 'HINTS', worker.HINTS)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def segments(self, *items):
        self.vad.recognize.return_value = [NS(end=end, text=text) for end, text in items]

    def test_final_joins_vad_segments_and_skips_empty_ones(self):
        self.segments((1.0, ' hello '), (2.0, ' '), (3.0, 'there'))
        self.assertEqual(worker.parakeet([0.0] * 4 * worker.RATE), 'hello there')
        self.plain.recognize.assert_not_called()

    def test_phrase_uses_the_whole_clip_without_vad(self):
        self.plain.recognize.return_value = ' split the pane '
        self.assertEqual(worker.parakeet([0.0] * worker.RATE, phrase=True), 'split the pane')
        self.vad.recognize.assert_not_called()

    def test_live_keeps_settled_speech_and_final_converts_only_the_rest(self):
        samples = [0.0] * 10 * worker.RATE
        self.segments((3.0, 'one'), (7.0, 'two'), (9.5, 'three'))  # 7.0 ends 3 s before the end: settled; 9.5 does not
        self.assertEqual(worker.parakeet(samples, key='rec-12345', live=True), 'one two three')
        self.assertEqual(worker.RECORDINGS['rec-12345'].done, (7.0, 'one two'))
        self.segments((2.5, 'three four'))
        self.assertEqual(worker.parakeet([0.0] * 11 * worker.RATE, key='rec-12345'), 'one two three four')
        self.assertEqual(len(self.vad.recognize.call_args.args[0]), 4 * worker.RATE)  # from 7.0 s to 11.0 s
        self.assertNotIn('rec-12345', worker.RECORDINGS)

    def test_final_without_vad_speech_retries_loud_audio_in_pieces(self):
        self.segments()
        self.plain.recognize.side_effect = [' tether ', ' tmux ']
        with mock.patch.object(worker, 'decode_audio', return_value=[0.05] * 25 * worker.RATE), \
                mock.patch.object(worker, 'level', return_value=(0.05, 0.05)), mock.patch('sys.stderr'):
            self.assertEqual(worker.transcribe(b'x'), ('Tether tmux', 25.0))
        self.assertEqual([len(call.args[0]) for call in self.plain.recognize.call_args_list], [20 * worker.RATE, 5 * worker.RATE])

    def test_no_retry_for_silence_or_live_passes(self):
        self.segments()
        with mock.patch.object(worker, 'decode_audio', return_value=[0.0] * 4 * worker.RATE), mock.patch('sys.stderr'):
            with mock.patch.object(worker, 'level', return_value=(0.002, 0.001)):
                self.assertEqual(worker.transcribe(b'x'), ('', 4.0))
            with mock.patch.object(worker, 'level', return_value=(0.05, 0.05)):
                self.assertEqual(worker.transcribe(b'x', live=True, key='rec-live1'), ('', 4.0))
        self.plain.recognize.assert_not_called()

    def test_transcribe_uses_parakeet_and_limits_phrases(self):
        with mock.patch.object(worker, 'decode_audio', return_value=[0.0] * 31 * worker.RATE):
            with self.assertRaises(worker.PhraseTooLong):
                worker.transcribe(b'x', phrase=True)
            self.plain.recognize.assert_not_called()
            self.segments((2.0, 'hi'))
            self.assertEqual(worker.transcribe(b'x'), ('hi', 31.0))

    def test_misheard_words_from_the_voice_test_are_fixed(self):
        worker.HINTS = worker.hints('tmux, Codex, Tether')
        self.assertEqual(worker.spell('Open Tther on the server and split the Tmuxpan.'), 'Open Tether on the server and split the tmux pane.')
        self.assertEqual(worker.spell('Check the voice log in Titha. Ask Portex to review the diff and push it to mean.'),
                         'Check the voice log in Tether. Ask Codex to review the diff and push it to main.')
        self.assertEqual(worker.spell('What do you mean? A frying pan. I mean it.'), 'What do you mean? A frying pan. I mean it.')

    def test_hints_fix_spelling_and_keep_sentence_capitals(self):
        worker.HINTS = worker.hints('tmux, Codex, Tailscale, repo, API, split the pane.')
        self.assertEqual(worker.spell('Run Tmux on tail scale, then ask codex about the Api.'), 'Run tmux on Tailscale, then ask Codex about the API.')
        self.assertEqual(worker.spell('Repo is clean. Tmux is up, push the Repo'), 'Repo is clean. Tmux is up, push the repo')
        self.assertEqual(worker.spell('a reposition'), 'a reposition')
        worker.HINTS = worker.hints('JSON, Tailscale')
        self.assertEqual(worker.spell('Please leave JS on. Restart tail scale.'), 'Please leave JS on. Restart Tailscale.')

    def test_busy_preview_without_a_recording_returns_at_once(self):
        with worker.PARAKEET_LOCK:
            self.assertEqual(worker.parakeet([0.0] * worker.RATE, live=True), '')
        self.vad.recognize.assert_not_called()

    def test_busy_preview_returns_the_text_so_far_and_final_waits_for_the_recording(self):
        item = worker.recording('rec-busy-01')
        item.done = (3.0, 'so far')
        with worker.PARAKEET_LOCK:  # another recording's final pass runs
            self.assertEqual(worker.parakeet([0.0] * 9 * worker.RATE, key='rec-busy-01', live=True), 'so far')
        self.vad.recognize.assert_not_called()
        item.lock.acquire()  # a preview of the same recording runs; the final pass must wait, then reuse its text
        result = []
        thread = worker.threading.Thread(target=lambda: result.append(worker.parakeet([0.0] * 9 * worker.RATE, key='rec-busy-01')))
        thread.start()
        thread.join(0.2)
        self.assertTrue(thread.is_alive())
        item.done = (7.0, 'so far and more')
        self.segments((1.5, 'end'))
        item.lock.release()
        thread.join(2)
        self.assertEqual(result, ['so far and more end'])
        self.assertEqual(len(self.vad.recognize.call_args.args[0]), 2 * worker.RATE)


if __name__ == '__main__':
    unittest.main()
