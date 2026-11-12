import type { PipecatClient } from '@pipecat-ai/client-js'
import { DeviceVoice, type VoiceKind } from './deviceVoice'

const fridayPeerConnections = new Set<RTCPeerConnection>()
const statsCounters = ['packetsLost', 'packetsReceived', 'concealedSamples', 'totalSamplesReceived', 'concealmentEvents'] as const

function withTimeout<T>(work: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms)
    work.then(
      value => { clearTimeout(timer); resolve(value) },
      failure => { clearTimeout(timer); reject(failure) },
    )
  })
}

// Friday agent: one WebRTC call to the local voice worker (server/friday_agent.py, through /mobile/api/live/offer).
// The worker listens, thinks and speaks; this side plays its voice, runs the tools it asks for, and sends it chat events.
// Client messages are {t, d}: state (chats), event (chat news to speak), say (exact words), note (silent context).

export type AgentTurn = 'listening' | 'thinking' | 'speaking'
export type ToolResult = Record<string, unknown>
export type ToolHandler = (args: Record<string, unknown>) => Promise<ToolResult>
export interface AgentMetrics { turn: number; stop_to_first_audio: number; stt: number; llm_ttft: number; tts_ttfb: number }
export interface AgentEvent { id: string; chat: string; kind: 'approval' | 'question' | 'plan' | 'done' | 'error'; text: string; priority: 'now' | 'idle' }
// Kept small: the state goes into the LLM prompt on most turns (about 7 ms per token on this CPU).
export interface AgentChats { target: string; busy: string[]; needs: { chat: string; needs: string }[]; idle: number }
export interface AgentChatList { target: string; chats: string[]; sessions: string[] }

export interface AgentEvents {
  onTurn: (turn: AgentTurn) => void
  onUserText: (text: string, final: boolean) => void
  onBotText: (text: string) => void
  onMetrics?: (metrics: AgentMetrics) => void
  onVoice?: (kind: VoiceKind | null) => void
  onClosed: (reason: string) => void
  tools: Record<string, ToolHandler>
}

/** True when the worker answers its health check. */
export async function agentReady() {
  try {
    const response = await fetch('/mobile/api/live/agent', { credentials: 'same-origin', signal: AbortSignal.timeout(2500) })
    const data = await response.json() as { ok?: boolean; llm?: boolean }
    return response.ok && data.ok === true
  } catch { return false }
}

export class FridayAgent {
  turn: AgentTurn = 'listening'
  private client: PipecatClient | null = null
  private audio = new Audio()
  private isMuted = false
  private gated = false
  private closed = false
  private onPageHide = () => this.close('pagehide')
  private micLevel = 0
  private botLevel = 0
  private bot = ''
  private botSegments = new Map<number | string, string>()
  private events: AgentEvents
  private voice: DeviceVoice
  private peers = new Set<RTCPeerConnection>()
  private lastStats = new Map<RTCPeerConnection, { id: string; values: Record<string, number> }>()
  private statsTimer: ReturnType<typeof setInterval> | null = null
  private statsPending: Promise<void> = Promise.resolve()
  private statsStop: Promise<void> | null = null

  constructor(events: AgentEvents) {
    this.events = events
    this.audio.autoplay = true
    // The device speaks when it can; the server voice (Pocket) is the fallback.
    this.voice = new DeviceVoice({
      onStart: turn => { this.send('spoken', { turn, state: 'start' }); this.setTurn('speaking'); this.gate(this.voice.kind === 'system') },
      onEnd: turn => { this.gate(false); this.send('spoken', { turn, state: 'end' }); this.setTurn('listening') },
      // The reply so far was never heard: the server voice speaks it, and speaks the rest of the turn itself.
      onFail: reason => { console.warn('Friday device voice failed', reason); this.gate(false); this.voice.close(); this.mode(null, this.bot) },
      onKind: kind => this.mode(kind),
    })
  }

  /** Tell the server who speaks: this device (client) or Pocket (server). */
  private mode(kind: VoiceKind | null, replay = '') {
    this.send('voice', { mode: kind ? 'client' : 'server', ...(!kind && replay.trim() ? { replay: replay.trim() } : {}) })
    this.events.onVoice?.(kind)
  }

  /** The built-in voice gives the echo canceller no reference, so the mic is off while it speaks (no barge-in). */
  private gate(on: boolean) {
    if (this.gated === on) return
    this.gated = on
    this.client?.enableMic(!this.isMuted && !on)
  }

  get speaking() { return this.turn === 'speaking' }
  get muted() { return this.isMuted }
  set muted(value: boolean) {
    this.isMuted = value
    this.client?.enableMic(!value && !this.gated)
  }

  /** 0..1 for the orb: the voice while it speaks, else the mic. */
  level() {
    if (this.isMuted && this.turn !== 'speaking') return 0
    const bot = this.voice.kind ? this.voice.level() : this.botLevel
    return Math.min(1, (this.turn === 'speaking' ? bot : this.micLevel) * 3)
  }

  /** Connect the call. Mic denial stays a DOMException. */
  async open() {
    const [{ PipecatClient }, { SmallWebRTCTransport }] = await withTimeout(
      Promise.all([import('@pipecat-ai/client-js'), import('@pipecat-ai/small-webrtc-transport')]),
      15000, 'The voice files did not load.',
    ).catch(failure => {
      if (failure instanceof TypeError && (
        failure.message.includes('dynamically imported module') ||
        failure.message.includes('Importing a module script failed')
      )) {
        throw new Error('Tether was updated. Reload the page to use Friday.')
      }
      throw failure
    })
    if (this.closed) return
    const headers = new Headers({ 'X-Codex-Mobile': '1' })
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport({ webrtcRequestParams: { endpoint: '/mobile/api/live/offer', headers, timeout: 15000 } }),
      enableMic: !this.isMuted,
      enableCam: false,
      callbacks: {
        onTrackStarted: (track, participant) => {
          if (track.kind !== 'audio' || participant?.local) return
          this.audio.srcObject = new MediaStream([track])
          void this.audio.play().catch(() => {})
        },
        onLocalAudioLevel: level => { this.micLevel = level },
        onRemoteAudioLevel: level => { this.botLevel = level },
        onUserStartedSpeaking: () => this.setTurn('listening'),
        onUserStoppedSpeaking: () => this.setTurn('thinking'),
        // Messages sent before the data channel opens are dropped, so tell the server the voice mode again here.
        onBotReady: () => this.mode(this.voice.kind),
        onBotStartedSpeaking: () => { if (!this.voice.kind) { this.bot = ''; this.botSegments.clear() } this.setTurn('speaking') },
        onBotStoppedSpeaking: () => this.setTurn('listening'),
        onUserTranscript: data => this.events.onUserText(data.text, data.final),
        onBotOutput: data => {
          if (this.voice.kind) return  // The device shows each sentence as it gets it (say).
          // Pipecat reports a sentence before and after it speaks it, with one segment_id: keep it once.
          const progress = data.spoken_progress
          this.botSegments.set(data.segment_id ?? `text-${this.botSegments.size}`, progress ? `${progress.accumulated_text}${progress.remaining_text}` : data.text)
          this.bot = [...this.botSegments.values()].join(' ').trim(); this.events.onBotText(this.bot)
        },
        onServerMessage: (message: { t?: string; d?: unknown }) => {
          if (message?.t === 'metrics') this.events.onMetrics?.(message.d as AgentMetrics)
          else if (message?.t === 'ready') { if (this.voice.kind) this.mode(this.voice.kind) }
          else if (message?.t === 'say') { const d = message.d as { turn: number; seq: number; text: string }; this.bot = d.seq ? `${this.bot} ${d.text}` : d.text; this.events.onBotText(this.bot); this.voice.say(d.turn, d.seq, d.text) }
          else if (message?.t === 'say_end') { const d = message.d as { turn: number; count: number }; this.voice.end(d.turn, d.count) }
          else if (message?.t === 'stop') this.voice.stop()
          // The server heard no words (noise, echo, or a short overlap): no reply comes, so stop showing thinking.
          else if (message?.t === 'idle') { if (this.turn === 'thinking') this.setTurn('listening') }
        },
        onDisconnected: () => { void this.stopStats(); if (!this.closed) this.events.onClosed('Friday hung up.') },
        onError: message => { if (!this.closed) this.events.onClosed(String((message.data as { message?: string })?.message ?? 'Friday stopped.')) },
      },
    })
    this.client = client
    window.addEventListener('pagehide', this.onPageHide)
    for (const [name, handler] of Object.entries(this.events.tools)) {
      client.registerFunctionCallHandler(name, async call => {
        try { return await handler(call.arguments ?? {}) } catch (failure) { return { error: failure instanceof Error ? failure.message : String(failure) } }
      })
    }
    void this.voice.open().catch(failure => console.warn('Friday device voice unavailable', failure))
    try {
      await withTimeout(client.initDevices(), 10000, 'The microphone did not open. Close other apps that use it, then try again.')
      if (this.closed) return
      const OriginalPeerConnection = window.RTCPeerConnection
      const peers = this.peers
      window.RTCPeerConnection = class extends OriginalPeerConnection {
        constructor(configuration?: RTCConfiguration) {
          super(configuration)
          fridayPeerConnections.add(this)
          peers.add(this)
        }
      }
      try {
        await withTimeout(client.connect(), 20000, 'Friday did not answer.')
      } finally {
        window.RTCPeerConnection = OriginalPeerConnection
      }
    } catch (failure) {
      void this.stopStats()
      try {
        for (const track of Object.values(client.tracks().local)) track?.stop()
      } catch { /* Tracks may be unavailable before device initialization finishes. */ }
      try { void client.disconnect().catch(() => {}) } catch { /* Preserve the startup failure. */ }
      throw failure
    }
    if (!this.closed && !this.statsStop) this.statsTimer = setInterval(() => { void this.reportStats() }, 5000)
  }

  private reportStats() {
    this.statsPending = this.statsPending.then(async () => {
      for (const peer of this.peers) {
        try {
          const reports = await peer.getStats()
          const report = [...reports.values()].find(report => report.type === 'inbound-rtp' && report.kind === 'audio')
          if (report) {
            const last = this.lastStats.get(peer)
            const previous = last && last.id === report.id ? last.values : {}
            const values: Record<string, number> = {}
            const data: Record<string, number> = {}
            const round = (value: number) => Math.round(value * 1000) / 1000
            for (const field of [...statsCounters, 'jitterBufferDelay', 'jitterBufferEmittedCount']) {
              if (typeof report[field] === 'number' && Number.isFinite(report[field])) values[field] = report[field]
            }
            for (const field of statsCounters) {
              if (field in values) data[field] = round(values[field] - (previous[field] ?? 0))
            }
            // Jitter is a current measurement in seconds, rather than a cumulative counter.
            if (typeof report.jitter === 'number' && Number.isFinite(report.jitter)) data.jitter = round(report.jitter)
            if ('jitterBufferDelay' in values && 'jitterBufferEmittedCount' in values) {
              const emitted = values.jitterBufferEmittedCount - (previous.jitterBufferEmittedCount ?? 0)
              data.jitterBufferAvgMs = emitted > 0 ? round((values.jitterBufferDelay - (previous.jitterBufferDelay ?? 0)) / emitted * 1000) : 0
            }
            this.send('stats', data)
            this.lastStats.set(peer, { id: report.id, values })
          }
        } catch { /* Stats must never affect the call. */ }
      }
    }).catch(() => {})
    return this.statsPending
  }

  private stopStats() {
    if (this.statsTimer !== null) clearInterval(this.statsTimer)
    this.statsTimer = null
    this.statsStop ??= this.reportStats().finally(() => {
      for (const peer of this.peers) fridayPeerConnections.delete(peer)
      this.peers.clear()
      this.lastStats.clear()
    })
    return this.statsStop
  }

  close(reason = 'ended') {
    if (this.closed) return
    this.closed = true
    if (this.client) this.send('bye', { reason })
    window.removeEventListener('pagehide', this.onPageHide)
    this.audio.srcObject = null
    this.voice.close()
    const client = this.client
    void this.stopStats().finally(() => {
      void client?.disconnect().catch(() => {})
      if (this.client === client) this.client = null
    })
  }

  send(t: 'state' | 'event' | 'say' | 'note' | 'voice' | 'spoken' | 'interrupt' | 'stats' | 'bye', d: unknown) {
    try { this.client?.sendClientMessage(t, d) } catch { /* not connected yet */ }
  }
  state(chats: AgentChats) { this.send('state', chats) }
  event(event: AgentEvent) { this.send('event', event) }
  say(text: string) { this.send('say', { text }) }
  note(text: string) { this.send('note', { text }) }
  /** Stop Friday now. Phones on speaker often hide the user's voice from the server, so a tap must work too. */
  interrupt(source = 'tap', phase?: string) { this.send('interrupt', { source, ...(phase !== undefined ? { phase } : {}) }); this.voice.stop(); this.setTurn('listening') }

  private setTurn(turn: AgentTurn) {
    if (this.turn === turn) return
    this.turn = turn
    this.events.onTurn(turn)
  }
}
