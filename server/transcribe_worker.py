"""Optional voice-to-text worker for the phone's mic button.

Runs apart from server.py because it needs faster-whisper (server.py stays stdlib-only).
Listens on 127.0.0.1 only; server.py forwards signed-in uploads from /mobile/api/transcribe.

    uv venv ~/.local/share/mobile-tmux/whisper-venv
    VIRTUAL_ENV=~/.local/share/mobile-tmux/whisper-venv uv pip install faster-whisper 'av>=16,<17'
    (faster-whisper 1.x passes av.open(metadata_errors=...), which PyAV 17+ no longer accepts.)
    ~/.local/share/mobile-tmux/whisper-venv/bin/python transcribe_worker.py

Settings (environment): MOBILE_TMUX_WHISPER_MODEL (small), MOBILE_TMUX_WHISPER_LANGUAGE (en, empty = detect),
MOBILE_TMUX_WHISPER_THREADS (8), MOBILE_TMUX_TRANSCRIBE_PORT (7684), MOBILE_TMUX_WHISPER_PROMPT (word hints),
MOBILE_TMUX_WHISPER_LIVE_MODEL (base.en: the fast model for text shown while the user still speaks),
MOBILE_TMUX_WHISPER_BEAM (1: the final pass's beam size; 5 was 1.7x slower with the same text on test clips).

Engine: Parakeet (NVIDIA's parakeet-tdt-0.6b-v2 in ONNX int8, through onnx-asr) when onnx-asr is installed:
    VIRTUAL_ENV=... uv pip install 'onnx-asr[cpu,hub]'
Whisper pads every clip to 30 s, so a 3 s clip took 2.6 s on 8 cores; Parakeet converts only the real audio
(0.3 s for the same clip, 15 Nov 2026). The first start downloads the model (about 650 MB).
TETHER_VOICE_ENGINE=whisper keeps Whisper; Whisper is also used when Parakeet cannot load.
TETHER_VOICE_MODEL (nemo-parakeet-tdt-0.6b-v2: English; -v3 adds 24 European languages).
Parakeet has no word-hint prompt: the one-word hints in TETHER_WHISPER_PROMPT fix the spelling afterwards.

Long recordings: the page tags each recording with X-Recording. While the user speaks, each live request may start a
background pass of the accurate model over the finished sentences. On stop, the final pass converts only the rest,
so the wait after stop stays short for any length (a 33 s clip took 9.8 s with one whole pass).
"""
import io
import json
import re
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from faster_whisper import WhisperModel, decode_audio

LIMIT = 10 * 1024 * 1024
PORT = int(os.environ.get('MOBILE_TMUX_TRANSCRIBE_PORT', '7684'))
LANGUAGE = os.environ.get('MOBILE_TMUX_WHISPER_LANGUAGE', 'en') or None
# Words that Whisper often mishears in coding talk.
PROMPT = os.environ.get('TETHER_WHISPER_PROMPT') or os.environ.get('MOBILE_TMUX_WHISPER_PROMPT', 'tmux, Tailscale, Claude, Codex, Tether, Friday, approve, deny, preview, split the pane, git push, version, release, npm, repo, PWA, API, JSON.')
THREADS = int(os.environ.get('MOBILE_TMUX_WHISPER_THREADS', '8'))
BEAM = int(os.environ.get('MOBILE_TMUX_WHISPER_BEAM', '1'))


def load_parakeet():
    """(model, model with VAD) or None: then the Whisper models do the work."""
    if os.environ.get('TETHER_VOICE_ENGINE', 'parakeet') != 'parakeet':
        return None
    try:
        import onnx_asr
        import onnxruntime
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = THREADS
        model = onnx_asr.load_model(os.environ.get('TETHER_VOICE_MODEL', 'nemo-parakeet-tdt-0.6b-v2'), quantization='int8', sess_options=options)
        # 0.5 s of quiet ends a segment; a segment never runs over 20 s (the model's best length).
        return model, model.with_vad(onnx_asr.load_vad('silero'), min_silence_duration_ms=500, max_speech_duration_s=20)
    except Exception as exc:  # not installed, or the model download failed: keep voice working with Whisper
        print('Parakeet not used (%s: %s); using Whisper' % (type(exc).__name__, exc), file=sys.stderr, flush=True)
        return None


PARAKEET = load_parakeet()
PARAKEET_LOCK = threading.Lock()  # one conversion at a time: two would share the same cores
if PARAKEET is None:
    MODEL = WhisperModel(os.environ.get('MOBILE_TMUX_WHISPER_MODEL', 'small'), device='cpu', compute_type='int8', cpu_threads=THREADS)
    LIVE_MODEL = WhisperModel(os.environ.get('MOBILE_TMUX_WHISPER_LIVE_MODEL', 'base.en'), device='cpu', compute_type='int8', cpu_threads=THREADS)
else:
    MODEL = LIVE_MODEL = None


def hints(prompt):
    """Spelling fixes for Parakeet from the one-word hints: 'Tmux' -> 'tmux', 'tail scale' -> 'Tailscale', 'codex' -> 'Codex'.
    A space may split a hint only into two parts of 3 letters or more, so 'JS on' never becomes 'JSON'."""
    def forms(word):
        return [re.escape(word)] + [re.escape(word[:at]) + r'\s' + re.escape(word[at:]) for at in range(3, len(word) - 2)]
    words = (part.strip(' .') for part in prompt.split(','))
    return [(re.compile(r'(?<![\w-])(?:' + '|'.join(forms(word)) + r')(?![\w-])', re.IGNORECASE), word)
            for word in words if word and ' ' not in word]


HINTS = hints(PROMPT)
# Words Parakeet heard wrong in a test with the owner's voice (2026-11-15). Only non-words, or a word in a clear phrase,
# so normal speech never changes. spell() applies them before the hints.
MISHEARD = [(re.compile(pattern, re.IGNORECASE), meant) for pattern, meant in (
    (r'(?<![\w-])(?:tther|titha|tetha)(?![\w-])', 'Tether'),
    (r'(?<![\w-])portex(?![\w-])', 'Codex'),
    (r'(?<![\w-])tmux\s?pane?(?![\w-])', 'tmux pane'),
    (r'(?<![\w-])((?:push|merge|rebase|pull)(?: it| this| that)? (?:to|into|onto|from)) mean(?![\w-])', r'\1 main'),
)]
FINAL_LOCK = threading.Lock()
PHRASE_LOCK = threading.Lock()
LIVE_LOCK = threading.Lock()
RATE = 16000
COMMIT_AFTER = 4.0  # seconds of audio not yet converted by the accurate model before a background pass starts
KEEP_TAIL = 2.5  # never commit a word that ends in the last seconds: the user may be mid-word
QUIET_PEAK = 0.01  # a final pass with no VAD speech is retried without VAD when the audio peaks at this level or above (-40 dBFS)
PAUSE = 0.15  # seconds of quiet between two words that make a good place to cut
RECORDING_ID = re.compile(r'[A-Za-z0-9-]{8,64}')
# Guards for the accurate model. One temperature: the default retries each 30 s window at up to six temperatures,
# and under load that ran one final pass for 175 s (3 Nov 2026) while every later recording waited for FINAL_LOCK.
MAX_TOKENS = 224  # per 30 s window (Whisper's limit is 448 with the prompt); fast speech needs about 150
FINAL_BUDGET = float(os.environ.get('TETHER_WHISPER_FINAL_BUDGET', '20'))  # seconds; then the fast model converts the rest
WAITING = [0]  # final passes waiting or running; a background pass stops at its next segment so they go first
WAITING_LOCK = threading.Lock()


class Recording:
    def __init__(self):
        self.done = (0.0, '')  # (seconds converted by the accurate model, their text); replaced as one value
        self.tried = 0.0  # length at the last pass that found nothing to commit
        self.lock = threading.Lock()  # held by a background pass; the final pass waits for it
        self.used = time.monotonic()


RECORDINGS = {}
RECORDINGS_LOCK = threading.Lock()


class PhraseTooLong(ValueError):
    pass


def recording(key, create=True):
    with RECORDINGS_LOCK:
        now = time.monotonic()
        for old in [name for name, item in RECORDINGS.items() if now - item.used > 600]:
            del RECORDINGS[old]
        item = RECORDINGS.get(key)
        if item is None and create:
            item = RECORDINGS[key] = Recording()
        if item:
            item.used = now
        return item


def join(*parts):
    return ' '.join(part.strip() for part in parts if part.strip())


def accurate(samples, before='', words=False, until=None, stop=None, phrase=False):
    """The accurate model's segments and where it stopped: (segments, None) when it converted everything, else
    (segments, seconds converted). before: the text so far, so the model keeps the same style across parts.
    until: a time.monotonic() deadline, also for the wait on FINAL_LOCK. stop: returns True when a final pass waits.
    The checks run between segments; the token cap bounds the time of one 30 s window.
    phrase=True uses PHRASE_LOCK and skips VAD: the browser already cut the phrase."""
    prompt = join(PROMPT, before[-200:])
    lock = PHRASE_LOCK if phrase else FINAL_LOCK
    if not lock.acquire(timeout=max(0.0, until - time.monotonic()) if until else -1):
        return [], 0.0
    try:
        segments, _ = MODEL.transcribe(samples, language=LANGUAGE, initial_prompt=prompt, vad_filter=not phrase, beam_size=BEAM, word_timestamps=words,
                                       temperature=0.0, condition_on_previous_text=False, max_new_tokens=MAX_TOKENS)
        found = []
        for segment in segments:
            found.append(segment)
            if (until and time.monotonic() > until) or (stop and stop()):
                return found, segment.end
        return found, None
    finally:
        lock.release()


def quick(samples):
    """The fast model's text: live previews, and the rest of a final pass that ran out of time."""
    with LIVE_LOCK:
        segments, _ = LIVE_MODEL.transcribe(samples, initial_prompt=PROMPT, vad_filter=True, beam_size=1, temperature=0.0,
                                            condition_on_previous_text=False, without_timestamps=True, max_new_tokens=128)
        return join(*(segment.text for segment in segments))


def final(samples, before=''):
    """The accurate text of samples within FINAL_BUDGET seconds; the fast model converts what is left after that."""
    with WAITING_LOCK:
        WAITING[0] += 1
    try:
        segments, cut = accurate(samples, before, until=time.monotonic() + FINAL_BUDGET)
    finally:
        with WAITING_LOCK:
            WAITING[0] -= 1
    text = join(*(segment.text for segment in segments))
    if cut is None:
        return text
    print('final over budget: accurate upto=%.1fs of %.1fs, fast model does the rest' % (cut, len(samples) / RATE), file=sys.stderr, flush=True)
    return join(text, quick(samples[int(cut * RATE):]))


def final_waits():
    return WAITING[0] > 0


def settled(segments, cut):
    """The text and end time of the words that end before cut (seconds into this part), or None.
    Whole segments often run to the end of the clip while the user speaks, so cut between words.
    Prefer a pause between words (PAUSE or longer) in the last 3 s before cut; cut in its middle so no word loses its start."""
    words = [word for segment in segments for word in (segment.words or ())]
    last = max((at for at, word in enumerate(words) if word.end <= cut), default=-1)
    if last < 0:
        return None
    gaps = [at for at in range(last + 1) if at + 1 < len(words) and words[at + 1].start - words[at].end >= PAUSE
            and words[at].end >= words[last].end - 3.0]
    at = gaps[-1] if gaps else last
    end = (words[at].end + words[at + 1].start) / 2 if at + 1 < len(words) and words[at + 1].start > words[at].end else words[at].end
    return ''.join(word.word for word in words[:at + 1]).strip(), end


def commit(item, samples):
    """Background pass: convert the audio after item.done and keep the words that end before the last KEEP_TAIL seconds."""
    if not item.lock.acquire(blocking=False):
        return
    try:
        start, text = item.done
        length = len(samples) / RATE
        if final_waits():
            return
        segments, cut = accurate(samples[int(start * RATE):], text, words=True, stop=final_waits)
        found = settled(segments, min(length - KEEP_TAIL - start, cut if cut is not None else length))
        if found:
            item.done = (start + found[1], join(text, found[0]))
            print('commit audio=%.1fs upto=%.1fs' % (length, item.done[0]), file=sys.stderr, flush=True)
        else:
            item.tried = length
    except Exception as exc:  # a bad partial upload: the final pass still converts everything
        print('commit failed: %s' % type(exc).__name__, file=sys.stderr, flush=True)
    finally:
        item.lock.release()


def spell(text):
    """text with the hint spellings. A lower-case hint keeps a capital at the start of a sentence ('Repo is clean')."""
    def fix(match, hint):
        start = not text[:match.start()].strip() or text[:match.start()].rstrip()[-1] in '.!?'
        return hint[0].upper() + hint[1:] if start and hint.islower() and match.group()[0].isupper() else hint
    for pattern, meant in MISHEARD:
        text = pattern.sub(meant, text)
    for pattern, hint in HINTS:
        text = pattern.sub(lambda match: fix(match, hint), text)
    return text


def parakeet_segments(samples, wait=True):
    """[(end in seconds, text)] of the speech in samples; quiet parts are skipped.
    wait=False: None when another conversion runs, so previews never queue in front of a final pass."""
    if not PARAKEET_LOCK.acquire(blocking=wait):
        return None
    try:
        return [(segment.end, segment.text.strip()) for segment in PARAKEET[1].recognize(samples) if segment.text.strip()]
    finally:
        PARAKEET_LOCK.release()


def plain_text(samples):
    """The Parakeet text of samples without VAD, in pieces of 20 s or less (the model's memory grows with the clip)."""
    step = 20 * RATE
    with PARAKEET_LOCK:
        return spell(join(*(PARAKEET[0].recognize(samples[at:at + step]).strip() for at in range(0, len(samples), step))))


def level(samples):
    """(peak, RMS) of samples, for the log: a silent mic shows about 0."""
    import numpy as np  # here, not at the top: the CI server tests run without numpy and stub faster-whisper
    if not len(samples):
        return 0.0, 0.0
    return float(np.abs(samples).max()), float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def parakeet(samples, key=None, live=False, phrase=False):
    """The Parakeet text of samples. key: like the Whisper path, a live request keeps the text of the speech that ended
    KEEP_TAIL seconds or more before the end of the clip (VAD cuts at a 0.5 s pause, so that speech cannot change);
    later requests convert only what comes after it. item.lock covers the read, the conversion and the update of
    item.done, so a final pass waits for a running preview of the same recording and then reuses its text.
    A preview that finds the recording or the model busy returns the text so far at once."""
    if phrase:  # the browser already cut the phrase
        with PARAKEET_LOCK:
            return spell(PARAKEET[0].recognize(samples).strip())
    item = recording(key, create=live) if key else None
    if not item:
        found = parakeet_segments(samples, wait=not live)
        return spell(join(*(words for _, words in found or ())))
    if not item.lock.acquire(blocking=not live):
        return spell(item.done[1])
    try:
        start, text = item.done
        found = parakeet_segments(samples[int(start * RATE):], wait=not live)
        if found is None:
            return spell(text)
        if live:
            settled = [(end, words) for end, words in found if end <= len(samples) / RATE - start - KEEP_TAIL]
            if settled:
                item.done = (start + settled[-1][0], join(text, *(words for _, words in settled)))
        else:
            with RECORDINGS_LOCK:
                RECORDINGS.pop(key, None)
        return spell(join(text, *(words for _, words in found)))
    finally:
        item.lock.release()


def transcribe(audio, live=False, key=None, phrase=False):
    """live=True: quick rough text while the user speaks (fast model, greedy). Otherwise the accurate final text.
    Live passes use one temperature and a token cap: without them, mic noise can send the model into retry loops (34 s seen).
    key: the recording's X-Recording id. With it, the accurate text of finished sentences is reused (see the docstring at the top).
    phrase=True converts the whole clip, at most 30 seconds, without recording reuse or the composer queue."""
    if PARAKEET:
        samples = decode_audio(io.BytesIO(audio), sampling_rate=RATE)
        length = len(samples) / RATE
        if phrase and length > 30:
            raise PhraseTooLong('Live phrases must be 30 seconds or shorter.')
        text = parakeet(samples, key, live, phrase)
        if not text and not live and not phrase:
            # VAD found no speech. Quiet or far speech can still be there: the first Mac rescue (2026-11-15) lost 13.9 s here.
            peak, rms = level(samples)
            if peak >= QUIET_PEAK:
                text = plain_text(samples)
            print('no VAD speech: peak=%.3f rms=%.4f retry words=%d' % (peak, rms, len(text.split())), file=sys.stderr, flush=True)
        return text, length
    if phrase:
        samples = decode_audio(io.BytesIO(audio), sampling_rate=RATE)
        length = len(samples) / RATE
        if length > 30:
            raise PhraseTooLong('Live phrases must be 30 seconds or shorter.')
        segments, _ = accurate(samples, phrase=True)
        return join(*(segment.text for segment in segments)), length
    if key:
        samples = decode_audio(io.BytesIO(audio), sampling_rate=RATE)
        length = len(samples) / RATE
        item = recording(key, create=live)
        if live:
            start, text = item.done
            if length - start - KEEP_TAIL >= COMMIT_AFTER and length - item.tried >= 5 and not item.lock.locked():
                threading.Thread(target=commit, args=(item, samples), daemon=True).start()
            return join(text, quick(samples[int(start * RATE):])), length
        start, text = 0.0, ''
        if item:
            with WAITING_LOCK:
                WAITING[0] += 1  # the background pass stops at its next segment
            try:
                with item.lock:  # wait for a background pass that is still running
                    start, text = item.done
            finally:
                with WAITING_LOCK:
                    WAITING[0] -= 1
            with RECORDINGS_LOCK:
                RECORDINGS.pop(key, None)
        return join(text, final(samples[int(start * RATE):], text)), length
    if live:
        with LIVE_LOCK:
            segments, info = LIVE_MODEL.transcribe(io.BytesIO(audio), initial_prompt=PROMPT, vad_filter=True, beam_size=1, temperature=0.0,
                                                   condition_on_previous_text=False, without_timestamps=True, max_new_tokens=128)
            return ' '.join(segment.text.strip() for segment in segments).strip(), info.duration
    samples = decode_audio(io.BytesIO(audio), sampling_rate=RATE)
    return final(samples), len(samples) / RATE


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in ('/transcribe', '/transcribe?live=1'):
            self.respond(404, {'error': 'Not found'})
            return
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= LIMIT:
            self.respond(413, {'error': 'The recording is empty or too long.'})
            return
        started = time.monotonic()
        phrase = self.headers.get('X-Live-Phrase') == '1'
        key = self.headers.get('X-Recording', '')
        key = key if RECORDING_ID.fullmatch(key) else None
        try:
            text, duration = transcribe(self.rfile.read(size), live=self.path.endswith('?live=1'), key=key, phrase=phrase)
        except PhraseTooLong as exc:
            self.respond(413, {'error': str(exc)})
            return
        except Exception as exc:  # bad or unsupported audio
            self.respond(400, {'error': 'Could not read that recording (%s).' % type(exc).__name__})
            return
        took = round(time.monotonic() - started, 2)
        print('%s audio=%.1fs took=%.2fs words=%d' % ('live' if self.path.endswith('?live=1') else 'final', duration, took, len(text.split())), file=sys.stderr, flush=True)
        try:
            self.respond(200, {'text': text, 'audio_seconds': round(duration, 1), 'seconds': took})
        except BrokenPipeError:  # the phone or server.py gave up waiting
            pass

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
