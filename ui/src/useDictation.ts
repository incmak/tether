import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { errorText } from './api'
import { playChime, prepareChime } from './sound'
import { stopSpeaking } from './speech'
import { reportVoice } from './voiceLog'

interface SpeechRecognizer {
  continuous: boolean
  interimResults: boolean
  lang: string
  maxAlternatives: number
  onresult: ((event: { results: ArrayLike<{ isFinal: boolean; [index: number]: { transcript: string } }> }) => void) | null
  onend: (() => void) | null
  onerror: ((event: { error: string }) => void) | null
  onaudiostart: (() => void) | null
  onsoundstart: (() => void) | null
  onspeechstart: (() => void) | null
  onnomatch: (() => void) | null
  start: () => void
  stop: () => void
  abort: () => void
}
type SpeechConstructor = new () => SpeechRecognizer
let speechOffUntil = 0
const SPEECH_OFF_MS = 5 * 60 * 1000
// A device whose speech service hears nothing twice uses the server voice for a day.
// One silent tap is not enough: a person may tap the mic and say nothing.
const SPEECH_DEAF_KEY = 'tether.speech-deaf-until'
const OLD_DEAF_KEY = 'tether.speechDeafUntil'
const SPEECH_DEAF_MS = 24 * 60 * 60 * 1000
const STRIKE_WINDOW_MS = 10 * 60 * 1000
let emptyStrikes = 0, lastStrikeAt = 0
const speechOff = () => { speechOffUntil = Date.now() + SPEECH_OFF_MS }

interface DictationOptions {
  text: string
  setText: (text: string) => void
  chime: boolean
  handsFree?: boolean
  onHandsFreeDone?: () => void
  onFinished?: () => void
}

// Share browser speech and the recorder fallback between task and chat input.
export function useDictation({ text, setText, chime, handsFree, onHandsFreeDone, onFinished }: DictationOptions) {
  const [listening, setListening] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState('')
  const recorder = useRef<MediaRecorder | null>(null)
  const stopSpeech = useRef<(() => void) | null>(null)
  const finalDraft = useRef<Promise<string> | null>(null)
  const converting = useRef<AbortController | null>(null)
  const cleanupRecording = useRef<(() => void) | null>(null)
  const disposed = useRef(false)
  const textNow = useRef(text)
  useLayoutEffect(() => { textNow.current = text }, [text])
  useEffect(() => {
    disposed.current = false
    return () => { disposed.current = true; cleanupRecording.current?.() }
  }, [])
  async function toggle() {
    if (stopSpeech.current) { stopSpeech.current(); return }
    if (recorder.current) { if (recorder.current.state !== 'inactive') recorder.current.stop(); return }
    stopSpeaking()
    setError('')
    if (chime) { prepareChime(); await playChime() }
    if (disposed.current) return
    const Recognition = speechConstructor()
    let speechDeafUntil = 0
    try { localStorage.removeItem(OLD_DEAF_KEY) } catch { /* the first version marked a device for 7 days after one silent tap */ }
    try { speechDeafUntil = Number(localStorage.getItem(SPEECH_DEAF_KEY) || 0) } catch { /* use the in-memory cooldown */ }
    if (Recognition && Date.now() >= speechOffUntil && Date.now() >= speechDeafUntil) startSpeech(Recognition)
    else await startRecorder()
  }
  function startSpeech(Recognition: SpeechConstructor) {
    const android = /Android/i.test(navigator.userAgent)
    const base = textNow.current.trimEnd()
    let finish: (text: string) => void = () => undefined
    const final = new Promise<string>(resolve => { finish = resolve })
    finalDraft.current = final
    void final.then(() => { if (finalDraft.current === final) finalDraft.current = null })
    let recognition: SpeechRecognizer
    let earlier = '', committed = '', interim = '', received = false, stopped = false, done = false, restarts = 0
    let failed = false, emptyEnds = 0, offForError = false, stoppedAt = 0
    const trail: string[] = []
    const trace = (token: string) => { trail.push(token); if (trail.length > 24) trail.shift() }
    let pause: number | undefined
    const started = Date.now()
    const join = (...parts: string[]) => parts.filter(Boolean).join(' ')
    const words = () => join(earlier, committed, interim).replace(/(?<=\S) slash (?=\S)/gi, '/')
    const detach = () => {
      recognition.onresult = null; recognition.onend = null; recognition.onerror = null
      recognition.onaudiostart = null; recognition.onsoundstart = null; recognition.onspeechstart = null; recognition.onnomatch = null
    }
    const cleanup = () => {
      done = true
      window.clearInterval(timer); window.clearTimeout(pause)
      stopSpeech.current = null; cleanupRecording.current = null
      if (recognition) { detach(); recognition.abort() }
    }
    const complete = (message?: string) => {
      if (done) return
      if (!received && !stopped && (failed || emptyEnds >= 3)) { fallback(); return }
      // Only a real attempt turns speech off. A quick tap on and off is not one.
      if (!received && stopped) message = undefined
      // A real attempt: the person stopped after 3 s or more and no words came back.
      const empty = !received && stopped && stoppedAt - started >= 3000  // time to the tap, not to a late end
      const deaf = empty && !trail.includes('sound') && !trail.includes('speech')
      if (received) emptyStrikes = 0
      if (empty) {
        emptyStrikes = Date.now() - lastStrikeAt <= STRIKE_WINDOW_MS ? emptyStrikes + 1 : 1
        lastStrikeAt = Date.now()
      }
      const second = empty && emptyStrikes >= 2
      if (second) {
        emptyStrikes = 0
        // The in-memory rule also covers storage that cannot be read or written.
        speechOff()
        if (deaf) { try { localStorage.setItem(SPEECH_DEAF_KEY, String(Date.now() + SPEECH_DEAF_MS)) } catch { /* the in-memory rule holds */ } }
      }
      console.info('[dictation]', trail.join(' '))
      const spoken = words(), next = join(base, spoken)
      reportVoice({ kind: 'dictation', event: deaf ? 'deaf' : spoken ? 'ok' : 'empty', trail: trail.join(' '), ms: Date.now() - started })
      cleanup(); setText(next); finish(next); setListening(false)
      if (message !== undefined) setError(message)
      else if (second && deaf && !handsFree) setError(`This device's speech service heard nothing twice, so Tether uses the server voice here for 24 hours. Tap the mic and speak again. (speech: ${trail.join(' ')})`)
      else if (second && !handsFree) setError(`No speech heard twice, so Tether uses the server voice for 5 minutes. Tap the mic and speak again. (speech: ${trail.join(' ')})`)
      else if (!spoken && !handsFree) setError(`No speech heard. Tap the mic and speak. (speech: ${trail.join(' ')})`)
      onFinished?.()
      if (handsFree && spoken && next !== base) onHandsFreeDone?.()
    }
    const stop = () => {
      if (done || stopped) return
      trace('stop')
      stopped = true; stoppedAt = Date.now()
      recognition.stop()
      // Some browsers never fire onend after stop; finish with the words so far.
      window.clearTimeout(pause); pause = window.setTimeout(() => { trace('timeout'); complete() }, 1500)
    }
    const fallback = () => {
      // Empty ends with sound heard mean a quiet person, not a dead service.
      if ((emptyEnds >= 3 && !trail.includes('sound') && !trail.includes('speech')) || offForError) speechOff()
      console.info('[dictation] fallback', trail.join(' '))
      reportVoice({ kind: 'dictation', event: 'fallback', trail: trail.join(' '), ms: Date.now() - started })
      // Say why the slow server voice runs, so the next report shows the cause.
      setError(`Browser speech failed, so the server converts this (slower). (speech: ${trail.join(' ')})`)
      cleanup(); setListening(false)
      // A submit may already hold the speech promise. Resolve it with the recorder's final text.
      void startRecorder().then(() => {
        const next = finalDraft.current
        if (next && next !== final) void next.then(finish)
        else finish(base)
      })
    }
    const attach = () => {
      recognition.continuous = !android; recognition.interimResults = true
      recognition.lang = navigator.language || 'en-US'; recognition.maxAlternatives = 1
      recognition.onaudiostart = () => trace('audio')
      recognition.onsoundstart = () => trace('sound')
      recognition.onspeechstart = () => trace('speech')
      recognition.onnomatch = () => trace('nomatch')
      recognition.onresult = event => {
        trace('r' + event.results.length)
        const finals: string[] = [], partials: string[] = []
        for (const result of Array.from(event.results)) (result.isFinal ? finals : partials).push(result[0].transcript.trim())
        committed = join(...finals); interim = join(...partials); received = true
        setText(join(base, words()))
        if (handsFree) { window.clearTimeout(pause); pause = window.setTimeout(stop, 1800) }
      }
      recognition.onend = () => {
        trace('end')
        if (done) return
        if (!received && !stopped) emptyEnds++
        if (!received && !stopped && (failed || emptyEnds >= 3)) { fallback(); return }
        if (stopped || restarts >= (android ? 60 : 20)) { complete(); return }
        earlier = join(earlier, committed, interim); committed = ''; interim = ''; restarts++
        trace('restart')
        try { recognition.start(); trace('start') } catch {
          detach()
          trace('restart')
          try { recognition = new Recognition(); attach(); recognition.start(); trace('start') } catch { complete() }
        }
      }
      recognition.onerror = ({ error }) => {
        trace('e:' + error)
        if (error !== 'aborted' && error !== 'no-speech') failed = true  // a late start gives no-speech; 3 empty ends catch a dead service
        if (done || error === 'aborted' || error === 'no-speech') return
        if (received) { complete(); return }
        offForError = ['service-not-allowed', 'language-not-supported', 'audio-capture'].includes(error)
        if (error === 'not-allowed') { complete('Allow microphone access for this site, then try again.'); return }
        if (error === 'audio-capture') {
          complete('Could not start the microphone. Close other apps that use it, then try again.'); return
        }
        complete()
      }
    }
    const timer = window.setInterval(() => {
      const elapsed = Math.floor((Date.now() - started) / 1000)
      setSeconds(elapsed); if (elapsed >= 180) stop()
    }, 250)
    if (handsFree) pause = window.setTimeout(stop, 8000)
    stopSpeech.current = stop
    cleanupRecording.current = () => { cleanup(); finish(base) }
    setSeconds(0); setListening(true)
    try { recognition = new Recognition(); attach(); recognition.start(); trace('start') } catch { fallback() }
  }
  async function startRecorder() {
    let stream: MediaStream
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }) } catch (failure) {
      reportVoice({ kind: 'dictation', event: 'mic-error', detail: failure instanceof DOMException ? failure.name : '' })
      setError(failure instanceof DOMException && failure.name === 'NotAllowedError' ? 'Allow microphone access for this site, then try again.' : 'Could not start the microphone. Close other apps that use it, then try again.')
      return
    }
    if (disposed.current) { stream.getTracks().forEach(track => track.stop()); return }
    const label = stream.getAudioTracks()[0]?.label
    const type = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(kind => MediaRecorder.isTypeSupported(kind))
    const active = new MediaRecorder(stream, type ? { mimeType: type } : undefined)
    const chunks: Blob[] = []
    const started = Date.now()
    // Live text: about every 1.5 s, send the audio so far to the fast model and show its rough text in the box.
    // The accurate model replaces it after stop.
    const base = textNow.current.trimEnd()
    // One id per recording: the server converts finished sentences while the user speaks, so stop only waits for the rest.
    const recording = crypto.randomUUID?.() ?? `${started}-${Math.random().toString(36).slice(2)}`
    let finish: (text: string) => void = () => undefined
    const final = new Promise<string>(resolve => { finish = resolve })
    finalDraft.current = final
    void final.then(() => { if (finalDraft.current === final) finalDraft.current = null })
    const show = (text: string) => setText(`${base}${base && text ? ' ' : ''}${text}`)
    let asking = false, askedAt = 0, done = false
    const askLive = async () => {
      // Wait for 2 s of audio, then ask at most every 1.5 s. Cancel a slow request so the next one can go.
      if (asking || done || Date.now() - started < 2000 || Date.now() - askedAt < 1500) return
      asking = true; askedAt = Date.now()
      const slow = new AbortController(), cancel = window.setTimeout(() => slow.abort(), 8000)
      try {
        const audio = new Blob(chunks, { type: active.mimeType || type || 'audio/webm' })
        const response = await fetch('/mobile/api/transcribe', { method: 'POST', body: audio, credentials: 'same-origin', signal: slow.signal, headers: { 'X-Codex-Mobile': '1', 'X-Live': '1', 'X-Recording': recording, 'Content-Type': audio.type } })
        const data = response.ok ? await response.json() as { text?: string } : {}
        if (!done && data.text) show(data.text.trim())
      } catch { /* live text is a preview; the final pass still runs */ } finally { window.clearTimeout(cancel); asking = false }
    }
    const timer = window.setInterval(() => { const elapsed = Math.floor((Date.now() - started) / 1000); setSeconds(elapsed); if (elapsed >= 180 && active.state !== 'inactive') active.stop() }, 250)
    active.ondataavailable = event => { if (event.data.size) { chunks.push(event.data); if (active.state === 'recording') void askLive() } }
    active.onstop = () => {
      const stopped = Date.now()
      done = true
      cleanupRecording.current = null
      window.clearInterval(timer); stream.getTracks().forEach(track => track.stop()); recorder.current = null; setListening(false)
      const heard = pause?.stop() ?? true
      const audio = new Blob(chunks, { type: active.mimeType || type || 'audio/webm' })
      if (!heard) { setText(base); finish(base); return }
      if (Date.now() - started < 600 || !audio.size) { setText(base); setError('Tap the mic, speak, then tap stop.'); finish(base); return }
      void transcribeAudio(audio, base, recording, label).then(({ text, ok }) => {
        reportVoice({ kind: 'dictation', event: 'recorder', ms: Date.now() - stopped })
        if (ok && text.trim() && text !== base) emptyStrikes = 0  // words came back, so the next silent tap is a first one
        finish(text)
        // Send at once; submit still waits for this final text. Rough text after a failure waits for the person to check it.
        if (pause && ok && text.trim() && text !== base) onHandsFreeDone?.()
      })
    }
    const pause = handsFree ? watchForPause(stream, () => { if (active.state !== 'inactive') active.stop() }) : null
    cleanupRecording.current = () => {
      done = true
      active.onstop = null
      active.ondataavailable = null
      window.clearInterval(timer); pause?.stop()
      if (active.state !== 'inactive') active.stop()
      stream.getTracks().forEach(track => track.stop()); recorder.current = null
      finish(base)
    }
    recorder.current = active
    active.start(1000); setSeconds(0); setListening(true)
  }
  // Returns the new draft; ok is false when the accurate text did not come and the box keeps the rough live text.
  // The server answers within about 25 s (transcribe_worker.py FINAL_BUDGET); 60 s means something is wrong.
  async function transcribeAudio(audio: Blob, base: string, recording: string, label: string | undefined): Promise<{ text: string; ok: boolean }> {
    setTranscribing(true)
    const cancel = new AbortController()
    converting.current = cancel
    let late = false
    const limit = window.setTimeout(() => { late = true; cancel.abort() }, 60000)
    try {
      const response = await fetch('/mobile/api/transcribe', { method: 'POST', body: audio, credentials: 'same-origin', signal: cancel.signal, headers: { 'X-Codex-Mobile': '1', 'X-Recording': recording, 'Content-Type': audio.type } })
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again, then try the mic.')
      const data = await response.json() as { text?: string; error?: string }
      if (!response.ok) throw new Error(data.error || 'Voice-to-text failed.')
      const text = (data.text ?? '').trim()
      if (!text) { setText(base); setError(label ? `No speech heard on ${label}. Check the mic or choose another input, then try again.` : 'No speech heard. Tap the mic and speak.'); return { text: base, ok: true } }
      const next = `${base}${base ? ' ' : ''}${text}`
      setText(next)
      return { text: next, ok: true }
    } catch (failure) {
      if (late) setError('Voice-to-text took too long. The box has the rough text: check it, then send.')
      else if (!cancel.signal.aborted) setError(`${errorText(failure)} The box has the rough text: check it, then send.`)
      return { text: textNow.current, ok: false }
    } finally { window.clearTimeout(limit); converting.current = null; setTranscribing(false); onFinished?.() }
  }
  const canRecord = !!speechConstructor() || (typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia)
  return {
    listening, transcribing, seconds, error, setError, canRecord,
    active: () => !!stopSpeech.current || !!recorder.current,
    toggle,
    cancelConverting: () => converting.current?.abort(),
    stop: () => { stopSpeech.current?.(); if (recorder.current && recorder.current.state !== 'inactive') recorder.current.stop() },
    finalText: () => finalDraft.current,
  }
}

function speechConstructor() {
  const speech = window as unknown as { SpeechRecognition?: SpeechConstructor; webkitSpeechRecognition?: SpeechConstructor }
  return speech.SpeechRecognition ?? speech.webkitSpeechRecognition
}

// Listen to the mic level. After speech, 1.8 s of quiet calls done. No speech in the first 8 s also calls done.
// stop() returns whether any speech was heard.
function watchForPause(stream: MediaStream, done: () => void) {
  const Context = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Context) return null
  const context = new Context()
  const analyser = context.createAnalyser()
  analyser.fftSize = 1024
  context.createMediaStreamSource(stream).connect(analyser)
  const samples = new Float32Array(analyser.fftSize)
  const started = Date.now()
  let heard = false, quietSince = Date.now(), floor = 0.01
  const timer = window.setInterval(() => {
    analyser.getFloatTimeDomainData(samples)
    const level = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length)
    // The room noise level, learned in the first half second.
    if (Date.now() - started < 500) { floor = Math.max(floor, level); return }
    const speech = level > Math.max(0.02, floor * 2.5)
    if (speech) { heard = true; quietSince = Date.now() }
    if (heard ? Date.now() - quietSince > 1800 : Date.now() - started > 8000) done()
  }, 100)
  return { stop: () => { window.clearInterval(timer); void context.close(); return heard } }
}
