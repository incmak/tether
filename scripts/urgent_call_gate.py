#!/usr/bin/env python3
"""Run the BriskBot phone gate. Use Friday's Python environment for `run`."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import http.client
import io
import json
import os
from pathlib import Path
import signal
import struct
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import wave


RATE = 16000
POLL_SECONDS = 0.5
HTTP_TIMEOUT = 10
PROMPTS = (
    'This is the Tether call test. Press each key from 1 to 9, then 0, then hash.',
    'Now say one short sentence after the beep. Press hash when you finish.',
)
RECORD_SETTINGS = dict(maxSeconds=20, initialSilence=5, maxSilence=3, stopTones=['#'], playBeep=True)
TONE_KEYS = {f'tone{digit}': digit for digit in '0123456789'} | {'star': '*', 'pound': '#'}
EVENT_FIELDS = ('seq', 'at', 'changeType', 'kind', 'state', 'resultInfo', 'toneInfo',
                'opType', 'opStatus', 'clientContext')
RECORDING_FIELDS = ('recordingLocation', 'recordingAccessToken')


class GateError(Exception):
    """An error message safe for the report and the terminal."""


def resample(samples, source_rate):
    if source_rate == RATE or not len(samples):
        return samples
    last = len(samples) - 1
    count = (len(samples) * RATE + source_rate - 1) // source_rate
    result = []
    for frame in range(count):
        left, fraction = divmod(frame * source_rate, RATE)
        a, b = samples[left], samples[min(left + 1, last)]
        result.append(a + (b - a) * fraction / RATE)
    return result


def make_wav(samples, source_rate):
    pcm = [max(-32768, min(32767, round(sample * 32768)))
           for sample in resample(samples, source_rate)]
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack(f'<{len(pcm)}h', *pcm))
    return output.getvalue()


def audio_header(header):
    """Read the facts visible in the first 64 bytes without retaining the bytes."""
    header = header[:64]
    if header[:4] == b'RIFF' and header[8:12] == b'WAVE':
        facts = {'format': 'WAV', 'riff_size': struct.unpack_from('<I', header, 4)[0]}
        offset = 12
        while offset + 8 <= len(header):
            kind, size = struct.unpack_from('<4sI', header, offset)
            offset += 8
            if kind == b'fmt ' and size >= 16 and offset + 16 <= len(header):
                encoding, channels, rate, _, _, bits = struct.unpack_from('<HHIIHH', header, offset)
                facts.update(encoding=encoding, channels=channels, sample_rate_hz=rate, bits_per_sample=bits)
            if kind == b'data':
                facts['data_size'] = size
                break
            offset += size + size % 2
        return facts

    for magic, name in ((b'OggS', 'OggS'), (b'fLaC', 'fLaC'), (b'ID3', 'ID3'),
                        (b'\x1a\x45\xdf\xa3', 'EBML'), (b'\x30\x26\xb2\x75\x8e\x66\xcf\x11', 'ASF')):
        if header.startswith(magic):
            return {'format': name}
    return {'format': 'ftyp' if header[4:8] == b'ftyp' else 'unknown'}


def decode_wav(audio):
    """Decode integer PCM WAV to normalized 16 kHz mono samples."""
    with wave.open(io.BytesIO(audio), 'rb') as wav:
        channels, width, rate, frames, _, _ = wav.getparams()
        pcm = wav.readframes(frames)
    if len(pcm) != channels * width * frames or rate < 1:
        raise wave.Error('Invalid PCM data length or sample rate')
    if width == 1:
        values = [sample - 128 for sample in pcm]
    elif width in (2, 4):
        sample_format = 'h' if width == 2 else 'i'
        values = struct.unpack(f'<{len(pcm) // width}{sample_format}', pcm)
    else:
        values = [int.from_bytes(pcm[i:i + width], 'little', signed=True) for i in range(0, len(pcm), width)]
    scale = channels * (1 << (width * 8 - 1))
    mono = [sum(values[i:i + channels]) / scale for i in range(0, len(values), channels)]
    return resample(mono, rate)


def collect_tones(events):
    # Graph's tone sequence can differ from BriskBot's callback arrival order.
    keys = {}
    for event in events:
        tone = event.get('toneInfo') or {}
        key = TONE_KEYS.get(tone.get('tone'))
        if key is not None and isinstance(tone.get('sequenceId'), int):
            keys.setdefault(tone['sequenceId'], key)
    collected = []
    for _, key in sorted(keys.items()):
        collected.append(key)
        if key == '#':
            break
    return ''.join(collected)


def latency_measures(t0, stt_start, stt_end, tts_end, t1):
    return {'local_processing_latency_s': round(t1 - t0, 3), 'stt_s': round(stt_end - stt_start, 3),
            'tts_s': round(tts_end - stt_end, 3)}


def audible_latency(record_event, reply_event, reply_wav):
    """Server time from the end of the recording to the start of the reply audio.

    Uses BriskBot's server times: (reply play completed - reply duration) - record completed.
    It includes the silence wait that ends a recording without a stop tone.
    """
    with wave.open(io.BytesIO(reply_wav), 'rb') as wav:
        duration = wav.getnframes() / wav.getframerate()
    return round((reply_event['at'] / 1000 - duration) - record_event['at'] / 1000, 3)


def render_report(started, check, events, measures, secrets=()):
    hidden = set(secrets)
    hidden.update(event.get(field) for event in events for field in RECORDING_FIELDS)
    hidden = sorted((str(secret) for secret in hidden if secret), key=len, reverse=True)

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()
                    if k.lower() != 'recordinglocation' and 'token' not in k.lower()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            for secret in hidden:
                value = value.replace(secret, '[redacted]')
        return value

    lines = ['', f'## {started} — {check}', '', 'Events (server time in milliseconds)']
    lines.extend(f'- {json.dumps(scrub({k: e[k] for k in EVENT_FIELDS if k in e}), ensure_ascii=False)}'
                 for e in events)
    if not events:
        lines.append('- None received')
    lines.extend(('', 'Measures'))
    lines.extend(f'- {key}: {json.dumps(value, ensure_ascii=False)}' for key, value in scrub(measures).items())
    lines.extend(('', 'Mak to confirm: did it ring on the lock screen within 10 s, was prompt audio clear?', ''))
    return '\n'.join(lines)


def https_url(url, *, origin=False):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                 and parsed.password is None and parsed.port != 0 and not parsed.fragment)
        if valid and (not origin or (parsed.path in ('', '/') and not parsed.query)):
            return f'https://{parsed.netloc}' if origin else url
    except ValueError:
        pass
    raise GateError('Expected an HTTPS BriskBot origin.' if origin else 'Invalid recording URL.')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # urllib can forward Authorization on a redirect.
        return None


class BriskBot:
    def __init__(self, origin, token):
        self.origin = https_url(origin, origin=True)
        self.token = token
        self.opener = build_opener(NoRedirect)

    @classmethod
    def from_config(cls):
        origin, token_file = os.environ.get('BRISKBOT_URL'), os.environ.get('BRISKBOT_POST_TOKEN_FILE')
        if not origin or not token_file:
            raise GateError('Set BRISKBOT_URL and BRISKBOT_POST_TOKEN_FILE.')
        try:
            token = Path(token_file).expanduser().read_text().strip()
        except (OSError, UnicodeError):
            raise GateError('Could not read the BriskBot URL or token file.') from None
        if not token or any(char.isspace() for char in token):
            raise GateError('The BriskBot token file must contain one token.')
        return cls(origin, token)

    def fetch(self, action, url, token, method='GET', data=None, content_type=None, timeout=HTTP_TIMEOUT):
        headers = {'Authorization': f'Bearer {token}'}
        if content_type:
            headers['Content-Type'] = content_type
        try:
            with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=timeout) as reply:
                return reply.read(), reply.headers.get('Content-Type')
        except HTTPError as error:
            error.close()
            raise GateError(f'{action} returned HTTP {error.code}.') from None
        except (OSError, ValueError, http.client.HTTPException):
            raise GateError(f'{action} request failed.') from None

    def request(self, action, call_id=None, *, after=None, payload=None, timeout=HTTP_TIMEOUT):
        url = self.origin + '/agent/call' + (f'/{action}' if action != 'create' else '')
        query = {} if call_id is None else {'id': call_id}
        if after is not None:
            query['after'] = after
        if query:
            url += '?' + urlencode(query)
        kind = None
        if isinstance(payload, dict):
            payload, kind = json.dumps(payload).encode(), 'application/json'
        elif payload is not None:
            kind = 'audio/wav'
        method = 'GET' if action == 'events' else 'POST'
        body, _ = self.fetch(action, url, self.token, method, payload, kind, timeout)
        if action != 'hangup':
            try:
                response = json.loads(body)
                if isinstance(response, dict):
                    return response
            except (ValueError, UnicodeError):
                pass
            raise GateError(f'{action} returned an invalid response.')

    def recording(self, event):
        url, token = (event.get(field) for field in RECORDING_FIELDS)
        if not url or not token:
            raise GateError('The record operation returned no recording URL or token.')
        return self.fetch('Recording download', https_url(url), token)


@contextmanager
def finish_request_before_interrupt():
    """Read the created call ID, or finish a hangup, before handling SIGINT."""
    interrupted = False

    def defer(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    previous = signal.signal(signal.SIGINT, defer)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if interrupted:
            raise KeyboardInterrupt


class Call:
    def __init__(self, bot):
        self.bot = bot
        self.id = None
        self.seq = 0
        self.events = []
        self.state = None
        self.state_times = {}
        self.termination = None

    def create(self):
        with finish_request_before_interrupt():
            self.started = time.monotonic()
            self.id = self.bot.request('create').get('id')
            if not self.id:
                raise GateError('Create returned no call ID.')
            # BriskBot only returns an ID after Graph creates the call in establishing.
            self.state = 'establishing'
            self.state_times[self.state] = round(time.monotonic() - self.started, 3)

    def hangup(self):
        if self.id and self.state != 'terminated':
            with finish_request_before_interrupt():
                self.bot.request('hangup', self.id)

    def poll(self, timeout):
        batch = self.bot.request('events', self.id, after=self.seq, timeout=timeout)
        self.observed_at = time.monotonic()
        events = batch['events']
        self.events.extend(events)
        self.seq = batch['seq']
        for event in events:
            if event.get('state') == 'terminated' and (self.termination is None or event.get('resultInfo')):
                self.termination = event
        self.state = 'terminated' if self.termination or self.state == 'terminated' else batch.get('state')
        for state in [event.get('state') for event in events] + [self.state]:
            if state:
                self.state_times.setdefault(state, round(self.observed_at - self.started, 3))

    def wait(self, predicate, deadline):
        result = predicate()
        while not result and self.state != 'terminated':
            poll_start = time.monotonic()
            remaining = deadline - poll_start
            if remaining <= 0:
                break
            self.poll(min(HTTP_TIMEOUT, remaining))
            result = predicate()
            if not result and self.state != 'terminated':
                time.sleep(max(0, min(poll_start + POLL_SECONDS, deadline) - time.monotonic()))
        return result

    def wait_op(self, op, seconds):
        def finished():
            return next((event for event in self.events if event.get('clientContext') == op
                         and event.get('opStatus') in ('completed', 'failed')), None)

        event = self.wait(finished, time.monotonic() + seconds)
        if not event:
            raise GateError('Call terminated during an operation.' if self.state == 'terminated'
                            else 'Timed out waiting for an operation.')
        if event['opStatus'] == 'failed':
            raise GateError('The call operation failed. See the event result.')
        return event

    def operation(self, action, payload):
        op = self.bot.request(action, self.id, payload=payload).get('op')
        if not op:
            raise GateError(f'{action} returned no operation ID.')
        return op


def friday_speech():
    """Load only Friday's speech models, using the same local files and cache."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
    import friday_agent as friday

    env = os.environ
    folder = Path(env.get('TETHER_FRIDAY_MODELS') or friday.paths.HOME / '.local/share/tether/friday-models').expanduser()
    env['HF_HOME'] = str(folder / 'hf')
    env['HF_HUB_OFFLINE'] = '1'
    import numpy as np
    from pocket_tts import TTSModel
    import sherpa_onnx
    import torch

    torch.set_num_threads(int(env.get('TETHER_FRIDAY_TTS_THREADS', '2')))
    parakeet = folder / 'parakeet-v3'
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(parakeet / 'encoder.int8.onnx'), decoder=str(parakeet / 'decoder.int8.onnx'),
        joiner=str(parakeet / 'joiner.int8.onnx'), tokens=str(parakeet / 'tokens.txt'),
        num_threads=friday.stt_threads(env), model_type='nemo_transducer')
    tts = TTSModel.load_model()
    voice = tts.get_state_for_audio_prompt(friday.voice_name(env))

    def speak(text):
        audio = tts.generate_audio(voice, text).flatten().tolist()
        return make_wav(audio, tts.sample_rate)

    def transcribe(samples):
        audio = np.asarray(samples, dtype=np.float32)
        stream = recognizer.create_stream()
        stream.accept_waveform(RATE, audio)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()

    return speak, transcribe


def recording_samples(bot, event, measures):
    audio, content_type = bot.recording(event)
    facts = audio_header(audio[:64])
    measures['recording'] = {'content_type': content_type, 'size_bytes': len(audio), **facts}
    if facts['format'] != 'WAV':
        return None
    try:
        return decode_wav(audio)
    except (wave.Error, EOFError):
        return None


def run_gate(call, measures):
    speak, transcribe = friday_speech()
    keypad_prompt, record_prompt = (speak(text) for text in PROMPTS)
    call.create()
    established = call.wait(lambda: 'established' in call.state_times, call.started + 60)
    if not established or call.state == 'terminated':
        raise GateError('Call terminated before the prompts.' if call.state == 'terminated'
                        else 'The call did not establish within 60 seconds.')

    first_tone = len(call.events)
    call.bot.request('tones', call.id)
    tones_deadline = time.monotonic() + 60
    keypad_op = call.operation('play', keypad_prompt)
    call.wait(lambda: collect_tones(call.events[first_tone:]).endswith('#'), tones_deadline)
    keys = collect_tones(call.events[first_tone:])
    measures['keypad_keys'] = keys
    measures['keypad_complete'] = all(key in keys for key in '1234567890#')
    if call.state == 'terminated':
        raise GateError('Call terminated during the keypad check.')

    call.wait_op(keypad_op, max(0, tones_deadline - time.monotonic()))
    call.wait_op(call.operation('play', record_prompt), 30)
    record = call.wait_op(call.operation('record', RECORD_SETTINGS), 30)
    t0 = call.observed_at
    measures['record_completed_observed_s_from_create'] = round(t0 - call.started, 3)
    samples = recording_samples(call.bot, record, measures)
    if samples is None:
        raise GateError('The recording format is unsupported. STT skipped.')
    stt_start = time.monotonic()
    text = transcribe(samples)
    stt_end = time.monotonic()
    del samples
    measures['transcript'] = text
    if not text:
        raise GateError('Parakeet returned no transcript.')
    reply = speak(f'I heard: {text}. Goodbye.')
    tts_end = time.monotonic()
    t1 = time.monotonic()
    try:
        reply_op = call.operation('play', reply)
    finally:
        measures['reply_play_requested_s_from_create'] = round(t1 - call.started, 3)
        measures.update(latency_measures(t0, stt_start, stt_end, tts_end, t1))
    reply_done = call.wait_op(reply_op, 30)
    measures['record_result_info'] = record.get('resultInfo')
    measures['speech_end_to_reply_audible_s'] = audible_latency(record, reply_done, reply)


def run_outcome(call):
    call.create()
    if not call.wait(lambda: call.state == 'terminated', call.started + 120):
        raise GateError('No termination observed within 120 seconds.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Run checks 1–4 and 6')
    outcome = commands.add_parser('outcome', help='Record the result of one call without prompts')
    outcome.add_argument('--expect', choices=('declined', 'unanswered', 'hungup'), required=True)
    for command in (run, outcome):
        command.add_argument('--out', type=Path, required=True, help='Markdown report to append to')
    args = parser.parse_args(argv)
    check = 'Run (checks 1–4 and 6)' if args.command == 'run' else f'Outcome (check 5, expected {args.expect})'
    started = datetime.now().astimezone().isoformat(timespec='seconds')
    measures, call, status = {}, None, 0
    try:
        with args.out.expanduser().open('a', encoding='utf-8') as report:
            try:
                call = Call(BriskBot.from_config())
                if args.command == 'run':
                    run_gate(call, measures)
                else:
                    run_outcome(call)
            except KeyboardInterrupt:
                measures['error'], status = 'Interrupted.', 130
            except GateError as error:
                measures['error'], status = str(error), 1
            except Exception as error:
                # Exception text may contain HTTP data or a recording URL.
                measures['error'], status = f'Gate failed ({type(error).__name__}).', 1
            finally:
                if call:
                    try:
                        call.hangup()
                    except KeyboardInterrupt:
                        measures['error'], status = 'Interrupted.', 130
                    except GateError as error:
                        measures['hangup_error'] = str(error)
                        status = status or 1
                    measures['state_timeline_s'] = call.state_times
                    if args.command == 'run':
                        for state in ('establishing', 'established'):
                            measures[f'time_to_{state}_s'] = call.state_times.get(state)
                    if call.state == 'terminated':
                        measures['result_info'] = (call.termination or {}).get('resultInfo')
                section = render_report(started, check, call.events if call else [], measures,
                                        (call.bot.token,) if call else ())
                report.write(section)
    except OSError:
        print('Could not append to the gate report.', file=sys.stderr)
        return status or 1
    if status:
        print('The gate did not complete. See the report.', file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.exit(main())
