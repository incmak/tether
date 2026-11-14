import type { Api } from './api'

export interface BackgroundItem {
  id: string
  url: string
  source: 'upload' | 'ai'
  prompt?: string
  created: number
  width?: number
  height?: number
  /** A 480 px WebP for the Settings grid. Photos saved without an image encoder have none. */
  thumb?: string
  /** A 32 px WebP data URI, painted until the photo has loaded. */
  placeholder?: string
}
export type BackgroundSize = 'landscape' | 'square'
export type BackgroundJob = { state: 'running' } | { state: 'done'; item: BackgroundItem } | { state: 'failed'; error: string }
const JOB_KEY = 'tether.backgroundJob.v1'
const JOB_LIMIT = 320000

function clearStoredJob() {
  try { localStorage.removeItem(JOB_KEY) } catch { /* Storage may be unavailable. */ }
}

export async function listBackgrounds(api: Api): Promise<BackgroundItem[]> {
  return (await api<{ items: BackgroundItem[] }>('backgrounds')).items
}

export function uploadBackground(api: Api, file: File): Promise<BackgroundItem> {
  return api<BackgroundItem>('backgrounds/upload', file, undefined, undefined, 60000)
}

export async function deleteBackground(api: Api, id: string): Promise<void> {
  await api('backgrounds/delete', { id })
}

function pause(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted()
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 2000)
    signal.addEventListener('abort', abort, { once: true })
  })
}

/** Abort stops polling. The server job still finishes and keeps its image in the library. */
export async function pollBackground(api: Api, job: string, signal: AbortSignal, startedAt: number): Promise<BackgroundItem> {
  const remaining = JOB_LIMIT - (Date.now() - startedAt)
  const stop = AbortSignal.any([signal, AbortSignal.timeout(Math.max(0, remaining))])
  try {
    stop.throwIfAborted()
    if (remaining <= 0) throw new DOMException('Image job timed out', 'TimeoutError')
    for (;;) {
      await pause(stop)
      const result = await api<BackgroundJob>(`backgrounds/generate?job=${encodeURIComponent(job)}`, undefined, stop)
      stop.throwIfAborted()
      if (result.state === 'done') return result.item
      if (result.state === 'failed') throw new Error(result.error)
    }
  } finally { clearStoredJob() }
}

export async function generateBackground(api: Api, prompt: string, tuned: boolean, size: BackgroundSize = 'landscape', signal?: AbortSignal): Promise<BackgroundItem> {
  const startedAt = Date.now()
  const stop = signal ? AbortSignal.any([signal, AbortSignal.timeout(JOB_LIMIT)]) : AbortSignal.timeout(JOB_LIMIT)
  stop.throwIfAborted()
  const { job } = await api<{ job: string }>('backgrounds/generate', { prompt, tuned, size }, stop)
  stop.throwIfAborted()
  try { localStorage.setItem(JOB_KEY, JSON.stringify({ job, startedAt })) } catch { /* Polling still works without storage. */ }
  return pollBackground(api, job, stop, startedAt)
}

export interface BackgroundJobHandle {
  promise: Promise<BackgroundItem>
  controller: AbortController
  /** App applies resumed jobs; Photos only follows their progress. */
  resumed: boolean
}
let running: BackgroundJobHandle | null = null

function trackBackground(handle: BackgroundJobHandle): BackgroundJobHandle {
  running = handle
  const clear = () => { if (running === handle) running = null }
  handle.promise.then(clear, clear)
  return handle
}

export function resumeBackground(api: Api): BackgroundJobHandle | null {
  if (running) return null
  try {
    const stored = localStorage.getItem(JOB_KEY)
    if (!stored) return null
    const data = JSON.parse(stored)
    if (!data || typeof data.job !== 'string' || !data.job || typeof data.startedAt !== 'number' || !Number.isFinite(data.startedAt) || Date.now() - data.startedAt >= 330000 || data.startedAt > Date.now()) {
      clearStoredJob()
      return null
    }
    const controller = new AbortController()
    return trackBackground({ controller, resumed: true, promise: pollBackground(api, data.job, controller.signal, data.startedAt) })
  } catch {
    clearStoredJob()
    return null
  }
}

/** The image job in this page. It keeps polling after Settings closes, so the finished image still applies. */
export function backgroundJob(): BackgroundJobHandle | null {
  return running
}

export function startBackground(api: Api, prompt: string, tuned: boolean, size: BackgroundSize = 'landscape'): BackgroundJobHandle {
  if (running) return running
  const controller = new AbortController()
  return trackBackground({ controller, resumed: false, promise: generateBackground(api, prompt, tuned, size, controller.signal) })
}
