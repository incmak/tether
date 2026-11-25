import type { KokoroTTS } from 'kokoro-js'

// Device voice: Friday's replies are spoken on this device, so a busy server CPU cannot break the audio.
// The server sends each sentence ({t: 'say'}); this side speaks it and reports start and end.
// Kokoro runs on WebGPU when this device is fast enough; else the built-in voice; else the server voice.
// Kokoro audio goes through a local WebRTC loopback, so the browser's echo canceller hears it and the
// mic does not send Friday's own voice back as speech.

const MODEL = 'onnx-community/Kokoro-82M-v1.0-ONNX'
const VOICE = 'af_heart'  // Same voice as the server Kokoro worker.
const PROBE = 'Hello, I am Friday. I am ready when you are.'
const FAST = 0.5          // Seconds of work per second of speech; above this a device is too slow.
const STORE = 'tether.fridayVoice.v1'
// A silent built-in voice can be one bad moment, so it gets a retry after a day; its lost reply is spoken by the server.
// Kokoro gets no retry: each probe loads 326 MB, and that crashed a phone tab (2026-11-06).
const RETRY_SYSTEM = 24 * 60 * 60 * 1000

export type VoiceKind = 'kokoro' | 'system'

interface Engine<T = unknown> {
  kind: VoiceKind
  prepare(text: string): Promise<T>
  play(item: T): Promise<void>
  stop(): void
  level(): number
  close(): void
}

interface Loop { stream: MediaStream; close: () => void }

async function loopback(input: MediaStream): Promise<Loop> {
  const a = new RTCPeerConnection()
  const b = new RTCPeerConnection()
  a.onicecandidate = event => { if (event.candidate) void b.addIceCandidate(event.candidate) }
  b.onicecandidate = event => { if (event.candidate) void a.addIceCandidate(event.candidate) }
  const output = new Promise<MediaStream>(resolve => { b.ontrack = event => resolve(event.streams[0] ?? new MediaStream([event.track])) })
  for (const track of input.getTracks()) a.addTrack(track, input)
  await a.setLocalDescription(await a.createOffer())
  await b.setRemoteDescription(a.localDescription!)
  await b.setLocalDescription(await b.createAnswer())
  await a.setRemoteDescription(b.localDescription!)
  return { stream: await output, close: () => { a.close(); b.close() } }
}

class KokoroEngine implements Engine<AudioBuffer> {
  kind = 'kokoro' as const
  private ctx = new AudioContext()
  private analyser = this.ctx.createAnalyser()
  private out = this.ctx.createMediaStreamDestination()
  private audio = new Audio()
  private loop: Loop | null = null
  private source: AudioBufferSourceNode | null = null
  private samples = new Float32Array(512)
  private tts: KokoroTTS

  constructor(tts: KokoroTTS) {
    this.tts = tts
    this.analyser.fftSize = 512
    this.analyser.connect(this.out)
    this.audio.autoplay = true
  }

  async start() {
    this.loop = await loopback(this.out.stream)
    this.audio.srcObject = this.loop.stream
    void this.audio.play().catch(() => {})
  }

  async prepare(text: string) {
    const raw = await this.tts.generate(text, { voice: VOICE })
    const buffer = this.ctx.createBuffer(1, raw.audio.length, raw.sampling_rate)
    buffer.copyToChannel(new Float32Array(raw.audio), 0)
    return buffer
  }

  async play(buffer: AudioBuffer) {
    await this.ctx.resume()
    if (this.audio.paused) void this.audio.play().catch(() => {})
    const source = this.ctx.createBufferSource()
    source.buffer = buffer
    source.connect(this.analyser)
    this.source = source
    await new Promise<void>(resolve => { source.onended = () => resolve(); source.start() })
    if (this.source === source) this.source = null
  }

  stop() { try { this.source?.stop() } catch { /* already ended */ } }

  level() {
    if (!this.source) return 0
    this.analyser.getFloatTimeDomainData(this.samples)
    let sum = 0
    for (const value of this.samples) sum += value * value
    return Math.sqrt(sum / this.samples.length)
  }

  close() {
    this.stop()
    this.audio.srcObject = null
    this.loop?.close()
    void this.ctx.close().catch(() => {})
  }
}

/** The best built-in English voice: enhanced or premium ones first. */
function systemVoice() {
  if (typeof speechSynthesis === 'undefined') return null
  const voices = speechSynthesis.getVoices().filter(voice => voice.lang.toLowerCase().startsWith('en'))
  const score = (voice: SpeechSynthesisVoice) => (/premium|enhanced|neural|natural/i.test(voice.name) ? 4 : 0)
    + (/google|siri|samantha|serena|daniel|karen/i.test(voice.name) ? 2 : 0) + (voice.localService ? 1 : 0)
  return voices.sort((a, b) => score(b) - score(a))[0] ?? null
}

class SystemEngine implements Engine<string> {
  kind = 'system' as const
  private speaking = false
  private voice: SpeechSynthesisVoice | null

  constructor(voice: SpeechSynthesisVoice | null) { this.voice = voice }

  async prepare(text: string) { return text }

  play(text: string) {
    return new Promise<void>((resolve, reject) => {
      const utterance = new SpeechSynthesisUtterance(text)
      this.voice ??= systemVoice()  // Android often lists its voices late.
      if (this.voice) utterance.voice = this.voice
      utterance.lang = this.voice?.lang ?? 'en-US'
      // Android can drop speech with no error. No start in 2 s means silence: fail over to the server voice.
      let started = false
      const silent = () => {
        this.speaking = false
        save({ system: 'silent', systemAt: Date.now() })  // Skip it on the next calls: its first reply would be lost.
        reject(new Error('The device voice did not start.'))
      }
      const watchdog = setTimeout(() => {
        if (started) return
        if (this.stopped) { this.speaking = false; resolve(); return }
        speechSynthesis.cancel()
        silent()
      }, 2000)
      utterance.onstart = () => {
        started = true
        if (saved()?.system) save({ system: undefined, systemAt: undefined })  // It works again.
      }
      utterance.onend = () => {
        clearTimeout(watchdog)
        this.speaking = false
        if (started || this.stopped) resolve(); else silent()
      }
      utterance.onerror = event => {
        clearTimeout(watchdog)
        this.speaking = false
        if (event.error === 'interrupted' || event.error === 'canceled') resolve()
        else reject(new Error(`The device voice failed: ${event.error}.`))
      }
      this.speaking = true
      this.stopped = false
      speechSynthesis.speak(utterance)
    })
  }

  private stopped = false
  stop() { this.stopped = true; speechSynthesis.cancel() }
  level() { return this.speaking ? 0.12 + Math.random() * 0.08 : 0 }
  close() { this.stop() }
}

async function voicesReady() {
  if (typeof speechSynthesis === 'undefined') return
  if (speechSynthesis.getVoices().length) return
  await new Promise<void>(resolve => {
    const done = () => { speechSynthesis.removeEventListener('voiceschanged', done); resolve() }
    speechSynthesis.addEventListener('voiceschanged', done)
    setTimeout(done, 1500)
  })
}

interface Saved { kokoro?: 'fast' | 'slow'; kokoroTry?: number; rtf?: number; system?: 'silent'; systemAt?: number }
/** True while a saved bad result still counts. A note with no time is old: retry it. */
const recent = (at: number | undefined, wait: number) => at !== undefined && Date.now() - at < wait
function saved(): Saved | null {
  try { return JSON.parse(localStorage.getItem(STORE) ?? 'null') as Saved | null } catch { return null }
}
function save(value: Saved) { try { localStorage.setItem(STORE, JSON.stringify({ ...saved(), ...value })) } catch { /* private mode */ } }

/** Kokoro on WebGPU, kept only when a short probe runs at least twice as fast as speech.
 * One model per page: each call reuses it, so a new call does not load another 326 MB onto the GPU. */
let model: Promise<KokoroTTS | null> | null = null

async function loadModel(): Promise<KokoroTTS | null> {
  const note = saved()
  if (note?.kokoro === 'slow') return null
  // A probe that started and never saved a result most likely crashed the tab: do not try again.
  if (note?.kokoro !== 'fast' && note?.kokoroTry) return null
  const gpu = (navigator as Navigator & { gpu?: { requestAdapter(): Promise<unknown> } }).gpu
  if (!gpu || !(await gpu.requestAdapter().catch(() => null))) return null
  save({ kokoroTry: Date.now() })
  let tts: KokoroTTS | null = null
  try {
    const { KokoroTTS } = await import('kokoro-js')
    tts = await KokoroTTS.from_pretrained(MODEL, { dtype: 'fp32', device: 'webgpu' })
    await tts.generate('Hi.', { voice: VOICE })  // Warm up shaders.
    const start = performance.now()
    const probe = await tts.generate(PROBE, { voice: VOICE })
    const rtf = (performance.now() - start) / 1000 / (probe.audio.length / probe.sampling_rate)
    save({ kokoro: rtf <= FAST ? 'fast' : 'slow', kokoroTry: undefined, rtf: Math.round(rtf * 100) / 100 })
    console.info('Friday Kokoro probe', { rtf })
    if (rtf <= FAST) return tts
  } catch (failure) {
    save({ kokoroTry: undefined })  // A load error is not a crash: a later call may try again.
    free(tts)
    throw failure
  }
  free(tts)  // Too slow: give its GPU memory back now.
  return null
}

/** Release a model's GPU and WASM memory. */
function free(tts: KokoroTTS | null) {
  void (tts as unknown as { model?: { dispose?: () => Promise<unknown> } } | null)?.model?.dispose?.().catch(() => {})
}

async function loadKokoro(): Promise<KokoroEngine | null> {
  model ??= loadModel().catch(failure => { model = null; throw failure })
  const tts = await model
  if (!tts) return null
  const engine = new KokoroEngine(tts)
  await engine.start()
  return engine
}

export interface VoiceEvents {
  onStart: (turn: number) => void
  onEnd: (turn: number, complete: boolean) => void
  onFail: (reason: string) => void
  onKind: (kind: VoiceKind | null) => void
}

/** Speaks the server's sentences in order, one turn at a time. */
export class DeviceVoice {
  private engine: Engine | null = null
  private events: VoiceEvents
  private turn = -1
  private expected = -1
  private played = 0
  private started = false
  private generation = 0
  private prepared: Promise<unknown> = Promise.resolve()
  private playing: Promise<void> = Promise.resolve()
  private closed = false

  constructor(events: VoiceEvents) { this.events = events }

  get kind() { return this.engine?.kind ?? null }

  /** The built-in voice now (if any), then Kokoro in the background when this device is fast enough. */
  async open() {
    await voicesReady()
    // Use the built-in voice even when no voice is listed yet: the device default still speaks.
    const silent = saved()?.system === 'silent' && recent(saved()?.systemAt, RETRY_SYSTEM)
    if (typeof speechSynthesis !== 'undefined' && !silent) this.use(new SystemEngine(systemVoice()))
    loadKokoro().then(engine => {
      if (!engine) return
      if (this.closed) { engine.close(); return }
      this.use(engine)
    }, failure => console.warn('Friday Kokoro unavailable', failure))
  }

  private use(engine: Engine) {
    // Switch between turns only, so one reply keeps one voice.
    const swap = () => {
      if (this.closed) { engine.close(); return }
      this.engine?.close()
      this.engine = engine
      this.events.onKind(engine.kind)
    }
    if (this.turn < 0) swap()
    else void this.playing.then(swap)
  }

  say(turn: number, seq: number, text: string) {
    const engine = this.engine
    if (!engine || this.closed) return
    if (turn !== this.turn) this.reset(turn)
    const generation = this.generation
    const ready = this.prepared.then(() => (generation === this.generation ? engine.prepare(text) : null))
    this.prepared = ready.catch(() => null)
    this.playing = this.playing.then(async () => {
      const item = await ready
      if (generation !== this.generation || item === null) return
      if (!this.started) { this.started = true; this.events.onStart(turn) }
      await engine.play(item)
      if (generation !== this.generation) return
      this.played += 1
      this.finish()
    }).catch(failure => {
      if (generation !== this.generation) return
      this.stop()
      this.events.onFail(failure instanceof Error ? failure.message : String(failure))
    })
    void seq
  }

  end(turn: number, count: number) {
    if (turn !== this.turn) return
    this.expected = count
    this.finish()
  }

  /** Barge-in or a server stop: drop the rest of this turn. */
  stop() {
    const { turn, started } = this
    this.generation += 1
    this.engine?.stop()
    this.turn = -1
    this.started = false
    if (started) this.events.onEnd(turn, false)
  }

  level() { return this.engine?.level() ?? 0 }

  close() {
    this.closed = true
    this.stop()
    this.engine?.close()
    this.engine = null
  }

  private reset(turn: number) {
    if (this.started) this.stop()
    this.generation += 1
    this.turn = turn
    this.expected = -1
    this.played = 0
    this.started = false
  }

  private finish() {
    if (this.expected < 0 || this.played < this.expected) return
    const { turn, started } = this
    this.turn = -1
    this.started = false
    if (started) this.events.onEnd(turn, true)
  }
}
