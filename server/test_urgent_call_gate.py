"""Pure checks for the BriskBot call driver. No credentials, calls, or models."""

import importlib.util
import io
from pathlib import Path
import struct
import unittest
import wave


SPEC = importlib.util.spec_from_file_location(
    'urgent_call_gate', Path(__file__).resolve().parents[1] / 'scripts/urgent_call_gate.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def pcm_wav(channels, width, rate, data):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        wav.writeframes(data)
    return output.getvalue()


class AudioTests(unittest.TestCase):
    def test_builds_pcm_wav_and_clamps_samples(self):
        audio = gate.make_wav([-2, -.5, 0, .5, 2], 16000)
        self.assertEqual(gate.audio_header(audio), {
            'format': 'WAV', 'riff_size': 46, 'encoding': 1, 'channels': 1,
            'sample_rate_hz': 16000, 'bits_per_sample': 16, 'data_size': 10,
        })
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            self.assertEqual(wav.getparams()[:4], (1, 2, 16000, 5))
            self.assertEqual(struct.unpack('<5h', wav.readframes(5)), (-32768, -16384, 0, 16384, 32767))

    def test_header_parser_skips_an_odd_sized_metadata_chunk(self):
        audio = pcm_wav(2, 1, 8000, b'\x80\x80')
        junk = b'JUNK' + struct.pack('<I', 1) + b'x\0'
        audio = audio[:4] + struct.pack('<I', len(audio) - 8 + len(junk)) + audio[8:12] + junk + audio[12:]
        facts = gate.audio_header(audio)
        self.assertEqual((facts['encoding'], facts['channels'], facts['sample_rate_hz'], facts['data_size']),
                         (1, 2, 8000, 2))
        self.assertEqual(gate.decode_wav(audio), [0, 0])

    def test_header_parser_reports_only_visible_wav_facts(self):
        audio = gate.make_wav([0], 16000)
        self.assertEqual(gate.audio_header(audio[:21]), {'format': 'WAV', 'riff_size': 38})
        self.assertEqual(gate.audio_header(b'OggS' + b'private' * 20), {'format': 'OggS'})
        self.assertEqual(gate.audio_header(b'\x00\x00\x00\x18ftyp'), {'format': 'ftyp'})
        self.assertEqual(gate.audio_header(b'private'), {'format': 'unknown'})

    def test_downsamples_tts_to_16_bit_mono(self):
        audio = gate.make_wav([0, .25, .75, -.5, 0, 0], 24000)
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            self.assertEqual(wav.getparams()[:4], (1, 2, 16000, 4))
            self.assertEqual(struct.unpack('<4h', wav.readframes(4)), (0, 16384, -16384, 0))

    def test_downmixes_stereo_and_upsamples_unsigned_pcm(self):
        audio = pcm_wav(2, 1, 8000, bytes([192, 128, 64, 128]))
        self.assertEqual(gate.decode_wav(audio), [.25, 0, -.25, -.25])

    def test_decodes_signed_24_bit_pcm(self):
        data = b''.join(value.to_bytes(3, 'little', signed=True) for value in (-8388608, 4194304))
        self.assertEqual(gate.decode_wav(pcm_wav(1, 3, 16000, data)), [-1, .5])

    def test_refuses_a_truncated_pcm_payload(self):
        audio = gate.make_wav([.25, .5], 16000)
        with self.assertRaises(wave.Error):
            gate.decode_wav(audio[:-1])

    def test_reports_non_pcm_wav_header_without_decoding(self):
        audio = gate.make_wav([0, 0], 16000)
        audio = audio[:20] + struct.pack('<H', 3) + audio[22:]
        self.assertEqual(gate.audio_header(audio)['encoding'], 3)
        with self.assertRaises(wave.Error):
            gate.decode_wav(audio)


def tone(sequence, key):
    return {'toneInfo': {'sequenceId': sequence, 'tone': key}}


class GateMeasuresTests(unittest.TestCase):
    def test_collects_through_hash_in_graph_sequence_order(self):
        events = [tone(4, 'tone2'), tone(3, 'pound'), tone(1, 'tone1'),
                  tone(2, 'star'), tone(1, 'tone1'), {'state': 'established'}]
        self.assertEqual(gate.collect_tones(events), '1*#')

    def test_reports_partial_tones_without_hash(self):
        self.assertEqual(gate.collect_tones([tone(12, 'tone0'), tone(11, 'tone9')]), '90')
        self.assertEqual(gate.collect_tones([tone(1, 'flash')]), '')

    def test_latency_includes_download_and_wav_decode_but_excludes_play_response(self):
        self.assertEqual(gate.latency_measures(5, 6, 6.25, 7, 7.125),
                         {'local_processing_latency_s': 2.125, 'stt_s': .25, 'tts_s': .75})

    def test_latency_rounds_to_milliseconds(self):
        self.assertEqual(gate.latency_measures(2.01, 2.09, 2.3451, 2.4567, 2.4581),
                         {'local_processing_latency_s': .448, 'stt_s': .255, 'tts_s': .112})

    def test_audible_latency_subtracts_reply_duration_from_server_times(self):
        reply = gate.make_wav([0.0] * 32000, 16000)  # 2 s of audio
        # Reply audio started at 13.5 s, 3.5 s after the recording ended at 10 s.
        self.assertEqual(gate.audible_latency({'at': 10_000}, {'at': 15_500}, reply), 3.5)

    def test_report_omits_credentials_and_keeps_diagnostics(self):
        location = 'https://recordings.example/secret.wav'
        post_token = 'fake-post-token'
        recording_token = 'fake-recording-token'
        events = [
            {'seq': 1, 'at': 1000, 'state': 'established', **tone(7, 'tone9')},
            {'seq': 2, 'at': 2000, 'opType': 'recordOperation', 'opStatus': 'completed',
             'recordingLocation': location, 'recordingAccessToken': recording_token,
             'resultInfo': {'code': 200, 'message': f'completed {location} {recording_token}'},
             'unrecognizedField': 'unrecognized value'},
        ]
        report = gate.render_report('2026-11-20', 'Run', events, {
            'transcript': f'Hello {post_token}', 'result_info': {'accessToken': 'nested-token', 'code': 200},
        }, (post_token,))
        for text in (location, post_token, recording_token, 'nested-token', 'recordingLocation',
                     'recordingAccessToken', 'accessToken', 'unrecognized value'):
            self.assertNotIn(text, report)
        for text in ('2026-11-20', '"at": 1000', '"at": 2000', 'established', 'tone9',
                     'completed', '"code": 200', 'Hello',
                     'Mak to confirm: did it ring on the lock screen within 10 s, was prompt audio clear?'):
            self.assertIn(text, report)

    def test_report_for_outcome_with_no_events(self):
        report = gate.render_report('2026-11-20', 'Outcome (expected declined)', [], {'error': 'Timed out.'})
        self.assertIn('Outcome (expected declined)', report)
        self.assertIn('None received', report)
        self.assertIn('Timed out.', report)

    def test_briskbot_base_url_must_be_an_https_origin(self):
        self.assertEqual(gate.https_url('https://example.test:3000/', origin=True), 'https://example.test:3000')
        for url in ('http://example.test', 'https://example.test/no', 'https://example.test/?q=yes',
                    'https://user@example.test', 'https://example.test/#fragment'):
            with self.subTest(url=url), self.assertRaises(gate.GateError):
                gate.https_url(url, origin=True)


if __name__ == '__main__':
    unittest.main()
