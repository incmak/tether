import { useSyncExternalStore } from 'react'

// Read a reply aloud in the natural server voice (Kokoro, /mobile/api/tts; server/tts.py), as Live mode does.
// The first clip is one short sentence so audio starts fast; the next clip loads while one plays.
// Without the server voice, the best English voice on the device reads the rest.
export const canSpeak = typeof window !== 'undefined' && 'speechSynthesis' in window
const Context = typeof window === 'undefined' ? undefined : window.AudioContext
  ?? (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
export const canReadAloud = canSpeak || !!Context

// Markdown to words: code blocks are named, not read; links read their text; marks and table pipes go.
export function speakable(markdown: string) {
  return markdown
    .replace(/```[\w-]*\n[\s\S]*?(```|$)/g, ' (code block) ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/gm, '')
    .replace(/^\s*\|?[\s:|-]+\|[\s:|-]*$/gm, '')
    .replace(/\|/g, ', ')
    .replace(/(\*\*|__|\*|_|~~)(\S[^*_~]*?)\1/g, '$2')
    .replace(/https?:\/\/\S+/g, 'link')
    .replace(/[ \t]+/g, ' ')
    .trim()
}

let speaking: string | null = null
const listeners = new Set<() => void>()
const set = (id: string | null) => { speaking = id; listeners.forEach(listener => listener()) }
let context: AudioContext | undefined
let source: AudioBufferSourceNode | null = null
let playback: AbortController | null = null
let generation = 0
let ttsDown = false

const holdPrefix = 'tether-read-aloud-hold:'
const playName = 'tether-read-aloud-play'
const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined
const channel = locks && typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('tether-read-aloud') : undefined
const ownHolds = new Set<string>()
const watchedHolds = new Set<string>()
const endedHolds = new Set<string>()
let localHolds = 0
let lockHeld = false
let held = false
const heldListeners = new Set<() => void>()

function updateHeld() {
  const next = localHolds > 0 || lockHeld
  if (held === next) return
  held = next
  heldListeners.forEach(listener => listener())
}

async function refreshHeld() {
  if (!locks) return held
  try {
    const state = await locks.query()
    const names = new Set((state.held ?? []).map(lock => lock.name).filter((name): name is string => !!name?.startsWith(holdPrefix)))
    for (const name of endedHolds) {
      if (!names.has(name)) endedHolds.delete(name)
    }
    lockHeld = false
    for (const name of names) {
      if (endedHolds.has(name)) continue
      lockHeld = true
      if (ownHolds.has(name) || watchedHolds.has(name)) continue
      watchedHolds.add(name)
      void locks.request(name, { mode: 'exclusive' }, () => {
        // The unique hold has ended. Other watchers can briefly acquire the same name next.
        endedHolds.add(name)
      }).then(() => { void refreshHeld() }, () => {}).finally(() => watchedHolds.delete(name))
    }
    updateHeld()
    return localHolds > 0 || names.size > 0
  } catch {
    // Keep the last UI snapshot. Playback needs a successful check.
    return true
  }
}

if (channel) {
  channel.onmessage = ({ data }) => {
    if (data?.t === 'hold') stopSpeaking()
    if (data?.t === 'hold' || data?.t === 'release') void refreshHeld()
  }
}
if (locks) window.addEventListener('focus', () => void refreshHeld())

/** How long Live waits for other tabs to stop reading aloud before it gives up. The mic stays closed. */
export const READ_ALOUD_STOP_MS = 5000

/** `ready` is true when no tab reads aloud any more, false when a reader did not stop in time. */
export function holdReadAloud(): { release: () => void; ready: Promise<boolean> } {
  stopSpeaking()
  localHolds++
  updateHeld()

  const pending = new AbortController()
  const signal = pending.signal
  const drain = new AbortController()
  const release = () => {
    if (signal.aborted) return
    localHolds--
    updateHeld()
    pending.abort()
    drain.abort()
  }
  if (!locks) return { release, ready: Promise.resolve(true) }

  const name = holdPrefix + (crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`)
  const released = new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }))
  ownHolds.add(name)
  const ready = new Promise<boolean>(resolve => {
    void locks.request(name, { mode: 'exclusive', signal }, () => {
      if (signal.aborted) return
      channel?.postMessage({ t: 'hold' })
      // Earlier readers must stop before this exclusive request runs. The mic must not hear them, so a reader
      // that does not stop in time makes ready false: the caller keeps the mic closed.
      const deadline = setTimeout(() => { resolve(false); drain.abort() }, READ_ALOUD_STOP_MS)
      void locks.request(playName, { mode: 'exclusive', signal: drain.signal }, () => { resolve(true) })
        .catch(() => {}).finally(() => clearTimeout(deadline))
      return released
    }).catch(() => {}).finally(() => {
      ownHolds.delete(name)
      channel?.postMessage({ t: 'release' })
      void refreshHeld()
      resolve(false)
    })
  })
  return { release, ready }
}

function subscribeHeld(listener: () => void) {
  if (!heldListeners.size) void refreshHeld()
  heldListeners.add(listener)
  return () => { heldListeners.delete(listener) }
}

export function useReadAloudHeld(): boolean {
  return useSyncExternalStore(subscribeHeld, () => held)
}

function splitParts(text: string) {
  const parts: string[] = []
  let group = ''
  let limit = 120
  for (const [piece] of text.matchAll(/.+?(?:[.!?。！？]+["'”’)\]]*(?=\s|$)|$)/gm)) {
    let sentence = piece.trim()
    if (!sentence) continue
    if (group && group.length + 1 + sentence.length > limit) {
      parts.push(group)
      group = ''
    }
    while (sentence.length > limit) {
      const wordEnd = sentence.slice(0, limit + 1).search(/\s+\S*$/)
      // If a single word exceeds the limit, split it without breaking a surrogate pair.
      const lastUnit = sentence.charCodeAt(limit - 1)
      const cut = wordEnd > 0 ? wordEnd : limit - Number(lastUnit >= 0xD800 && lastUnit <= 0xDBFF)
      parts.push(sentence.slice(0, cut))
      sentence = sentence.slice(cut).trimStart()
      limit = 300
    }
    if (!parts.length) {
      parts.push(sentence)
      limit = 300
    } else {
      group = group ? `${group} ${sentence}` : sentence
    }
  }
  if (group) parts.push(group)
  return parts
}

async function clip(audio: AudioContext, ready: Promise<boolean>, text: string, stop: AbortSignal) {
  const signal = AbortSignal.any([stop, AbortSignal.timeout(20_000)])
  try {
    const response = await fetch('/mobile/api/tts', {
      method: 'POST', credentials: 'same-origin', signal,
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ text }),
    })
    if (signal.aborted) return null
    if (response.status === 404 || response.status === 503 || !/^audio\//i.test(response.headers.get('content-type') ?? '')) {
      ttsDown = true
      return null
    }
    if (!response.ok) return null
    const bytes = await response.arrayBuffer()
    if (signal.aborted) return null
    // Fetch handles abort itself. Decoding and an autoplay-blocked resume need an explicit deadline.
    const decoded = audio.decodeAudioData(bytes)
    return await new Promise<AudioBuffer | null>((resolve, reject) => {
      const abort = () => resolve(null)
      signal.addEventListener('abort', abort, { once: true })
      void Promise.all([decoded, ready])
        .then(([buffer, resumed]) => resolve(resumed ? buffer : null), reject)
        .finally(() => signal.removeEventListener('abort', abort))
    })
  } catch { return null }
}

function play(audio: AudioContext, buffer: AudioBuffer) {
  const node = audio.createBufferSource()
  node.buffer = buffer
  node.connect(audio.destination)
  const done = new Promise<void>(resolve => {
    node.onended = () => {
      if (source === node) source = null
      node.disconnect()
      resolve()
    }
  })
  node.start()
  source = node
  return done
}

function systemVoice() {
  const voices = speechSynthesis.getVoices().filter(voice => voice.lang.toLowerCase().startsWith('en'))
  const score = (voice: SpeechSynthesisVoice) => (/premium|enhanced|neural|natural/i.test(voice.name) ? 4 : 0)
    + (/google|siri|samantha|serena|daniel|karen/i.test(voice.name) ? 2 : 0) + (voice.localService ? 1 : 0)
  return voices.sort((a, b) => score(b) - score(a))[0] ?? null
}

// Safari refuses speech that no tap started, and then fires no event. Give up when nothing has started by then.
const START_LIMIT = 3000

function playOnDevice(text: string, stop: AbortSignal) {
  return new Promise<boolean>(resolve => {
    const utterance = new SpeechSynthesisUtterance(text)
    utterance.voice = systemVoice()
    utterance.lang = utterance.voice?.lang ?? 'en-US'
    let started = false
    const watch: { timer?: ReturnType<typeof setTimeout> } = {}
    const finish = (read: boolean) => { clearTimeout(watch.timer); stop.removeEventListener('abort', abort); resolve(read) }
    const abort = () => finish(false)
    stop.addEventListener('abort', abort, { once: true })
    utterance.onstart = () => { started = true }
    utterance.onend = () => finish(true)
    utterance.onerror = () => finish(false)
    try { speechSynthesis.speak(utterance) } catch { finish(false); return }
    watch.timer = setTimeout(() => {
      if (started || speechSynthesis.speaking || speechSynthesis.pending) return
      speechSynthesis.cancel()
      finish(false)
    }, START_LIMIT)
  })
}

function readParts(id: string, parts: string[], stop: AbortSignal) {
  let audio: AudioContext | undefined
  let ready: Promise<boolean> | undefined
  if (Context && !ttsDown) {
    try {
      audio = context ??= new Context()
      // Call resume before the first await so the Read aloud tap unlocks audio.
      ready = audio.resume().then(() => true, () => false)
    } catch { /* Web Audio failed. Continue on the device. */ }
  }
  const read = () => playParts(id, parts, stop, audio, ready)
  return locks ? locks.request(playName, { mode: 'shared', signal: stop }, read) : read()
}

async function playParts(id: string, parts: string[], stop: AbortSignal, audio?: AudioContext, ready?: Promise<boolean>) {
  if (stop.aborted) return false
  if ((locks && await refreshHeld()) || stop.aborted) return false
  set(id)
  let at = 0
  if (audio && ready) {
    try {
      let next = clip(audio, ready, parts[at], stop)
      for (; at < parts.length; at++) {
        const buffer = await next
        if (stop.aborted) return false
        if (!buffer) break
        // Live can open after the deadline even if this clip is still loading.
        if ((locks && await refreshHeld()) || stop.aborted) return false
        const playing = play(audio, buffer)
        if (at + 1 < parts.length) next = clip(audio, ready, parts[at + 1], stop)
        await playing
        if (stop.aborted) return false
      }
    } catch { /* Web Audio failed. Continue from the current part on the device. */ }
  }
  for (; at < parts.length; at++) {
    if (stop.aborted || !canSpeak || (locks && await refreshHeld()) || stop.aborted) return false
    if (!await playOnDevice(parts[at], stop)) return false
  }
  return !stop.aborted
}

// Safari speaks only after one speak() call made during a tap (WebKit SpeechSynthesis::speak). The device voice may be
// needed later, after the server voice fails, when the tap is over. So an empty utterance in the tap unlocks it now.
let deviceUnlocked = false
function unlockDeviceVoice() {
  if (held || deviceUnlocked || !canSpeak || !navigator.userActivation?.isActive) return
  deviceUnlocked = true
  try { speechSynthesis.speak(new SpeechSynthesisUtterance('')) } catch { /* The device voice stays locked. */ }
}

export function speak(id: string, markdown: string, onEnd?: () => void) {
  if (localHolds) return
  stopSpeaking()
  if (!canReadAloud) return
  unlockDeviceVoice()
  const parts = splitParts(speakable(markdown))
  if (!parts.length) return
  const current = generation
  const controller = playback = new AbortController()
  const finish = (read: boolean) => {
    if (generation !== current) return
    playback = null
    if (speaking !== null) set(null)
    if (read) onEnd?.()
  }
  void readParts(id, parts, controller.signal).then(finish, () => finish(false))
}

// Auto-read starts with no tap, and browsers keep audio locked until a tap. While auto-read is on, each tap or key
// resumes the audio, so the next reply can play. Returns the cleanup.
export function keepAudioUnlocked() {
  if (!Context && !canSpeak) return () => {}
  const unlock = () => {
    unlockDeviceVoice()
    if (!Context || context?.state === 'running') return
    try { void (context ??= new Context()).resume().catch(() => {}) } catch { /* No audio here: the device voice reads. */ }
  }
  window.addEventListener('click', unlock, true)
  window.addEventListener('keydown', unlock, true)
  return () => {
    window.removeEventListener('click', unlock, true)
    window.removeEventListener('keydown', unlock, true)
  }
}

export function stopSpeaking() {
  const cancelVoice = canSpeak && (!localHolds || playback !== null)
  generation++
  playback?.abort()
  playback = null
  source?.stop()
  source = null
  if (cancelVoice) speechSynthesis.cancel()
  if (speaking !== null) set(null)
}

export function useSpeaking() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener) } }, () => speaking)
}
