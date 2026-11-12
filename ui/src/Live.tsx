import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, AudioLines, Check, ChevronDown, GripHorizontal, Maximize2, Mic, MicOff, Minimize2, Minus, PhoneOff, Square } from 'lucide-react'
import { errorText, SESSION_NAME, type Api, type Approval, type ChatData, type Choice, type Pane, type SessionsData } from './api'
import type { Command } from './CommandMenu'
import { agentNames, groupSessions, paneTitle } from './Sidebar'
import { agentReady, FridayAgent, type AgentChats, type AgentEvent, type ToolHandler } from './fridayAgent'
import { fridayChatList } from './fridaySessions'
import { fridayChatFolder } from './fridayFolders'
import { LiveVoice, sentences } from './liveVoice'
import { useBackStep } from './backNav'
import { useFloatingWindow, type Edge, type FloatMode, type Point, type Rect } from './floatingWindow'
import { bloomFromBubble, foldToBubble, openFrom, popBubble, settle } from './liveMotion'
import { speakable } from './speech'
import { BACKLOG_PATH } from './backlogNews'
import { parseBacklog, type Item } from './backlogItems'
import { itemPrompt } from './backlogSend'
import { reportVoice } from './voiceLog'
import './Live.css'

// Live mode: talk to the agents hands-free. The orb listens, the words go to the target pane,
// and the reply comes back as one or two spoken sentences. Colors come from the Settings color scheme.

export const ASSISTANT = 'Friday'
/** The tmux session that holds the chats Friday creates, one tab each. */
const FRIDAY_SESSION = 'Friday chats'

// Like a phone call: the mic stays open and each pause sends the phrase. 'paused' is the person's own pause button.
// 'thinking' is the Friday agent's gap between the end of speech and its voice.
type Phase = 'starting' | 'idle' | 'paused' | 'listening' | 'converting' | 'thinking' | 'sending' | 'speaking'
interface Watch { lastId: string | null; since: number; name: string }
interface Brief { speech: string }

const STATUS: Record<Phase, string> = { starting: 'Starting', idle: 'Tap to talk', paused: 'Paused', listening: 'Listening', converting: 'Converting', thinking: 'Thinking', sending: 'Sending', speaking: 'Speaking' }
const PANELS: Record<string, string> = { preview: 'browser', browser: 'browser', terminal: 'terminal', changes: 'changes', diff: 'changes', file: 'files', files: 'files', sidebar: 'sidebar', backlog: 'backlog' }

const approvalOf = (pane: Pane) => {
  const approval = pane.approval as Approval | null
  return approval?.id && approval.options?.length ? approval : null
}
// The floating window opens at the bottom right on a wide screen, and across the bottom on a phone, above the tab bar.
const BUBBLE = { w: 92, h: 56 }
const EDGES: Edge[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']
const firstRect = (): Rect => {
  const vw = window.innerWidth, vh = window.innerHeight
  if (vw < 600) { const h = Math.min(520, Math.round(vh * 0.62)); return { x: 8, y: Math.max(8, vh - h - 80), w: vw - 16, h } }
  const w = Math.min(400, vw - 32), h = Math.min(600, vh - 48)
  return { x: vw - w - 24, y: vh - h - 24, w, h }
}
const name = (pane: Pane) => `${paneTitle(pane)} · ${agentNames[pane.agent]}`
const lastReply = (data: ChatData) => [...data.messages].reverse().find(item => item.role === 'assistant' && !item.kind && item.text.trim()) ?? null
const shortText = (text: string, limit: number) => { const plain = speakable(text); return plain.length > limit ? `${plain.slice(0, limit).replace(/\s+\S*$/, '')}…` : plain }
const yesOption = (approval: Approval) => approval.options!.find(option => !option.free && !/^no\b/i.test(option.text)) ?? approval.options![0]
const noOption = (approval: Approval) => { const options = approval.options!; return options.find(option => /^no\b/i.test(option.text)) ?? options[options.length - 1] }
/** Speech has no hyphens: "weekly reporting" must match "weekly-reporting". */
const spokenForm = (text: string) => text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()
const spokenName = (wanted: string) => spokenForm(wanted).replace(/^the /, '').replace(/ (pane|chat|tab|session)$/, '')
const inSession = (agents: Pane[], name: string) => agents.filter(pane => spokenForm(pane.tmux_session) === name)
/** A chat by its spoken name: exact title first, then a part of it, then the agent name, then a tmux session with one chat. */
const findPane = (agents: Pane[], wanted: string) => {
  const name = spokenName(wanted)
  if (!name) return undefined
  const session = inSession(agents, name)
  return agents.find(pane => spokenForm(paneTitle(pane)) === name) ?? agents.find(pane => spokenForm(paneTitle(pane)).includes(name) || pane.agent === name)
    ?? (session.length === 1 ? session[0] : undefined)
}
/** A name that means a tmux session with several chats: Friday must ask which one, and must not guess. */
const sessionChoice = (agents: Pane[], wanted: string) => {
  const name = spokenName(wanted)
  const session = name && !findPane(agents, wanted) ? inSession(agents, name) : []
  return session.length ? { error: `${session[0].tmux_session} is a tmux session with ${session.length} chats: ${session.map(paneTitle).join(', ')}. Ask which one.` } : null
}
const chatState = (pane: Pane) => approvalOf(pane) ? 'needs approval' : pane.needs ? `needs ${pane.needs.kind}` : pane.state.toLowerCase()
const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export function Live({ api, panes, selected, commands, onClose, onBacklog, reveal = 0, from = null }: { api: Api; panes: Pane[]; selected?: Pane; commands: Command[]; onClose: () => void; onBacklog?: () => void; reveal?: number; from?: DOMRect | null }) {
  const { ref: attachWindow, mode, setMode, moving, bubble, windowStyle, bubbleStyle, move, resize, bubbleHandlers } = useFloatingWindow('tether.live-window.v1', { minW: 300, minH: 360, bubble: BUBBLE, initial: firstRect, initialMode: 'window' })
  const root = useRef<HTMLDivElement>(null)
  const bubbleButton = useRef<HTMLButtonElement>(null)
  // Motion between window, bubble and full screen. The fold plays first, then the mode changes; the layout
  // effect below plays the arrival before the browser paints the new mode.
  const folding = useRef<Animation | null>(null)
  const folded = useRef(false)
  const focusBubble = useRef(false)
  function shrink(focus = false) {
    if (mode === 'bubble' || folding.current) return
    const fold = foldToBubble(root.current, bubble, BUBBLE)
    let fallback = 0, over = false
    const done = () => {
      if (over) return
      over = true
      window.clearTimeout(fallback)
      folded.current = !!fold
      focusBubble.current = focus
      setMode('bubble')
    }
    if (!fold) { done(); return }
    folding.current = fold
    fallback = window.setTimeout(done, 500)  // a hidden tab may never finish the animation
    fold.finished.then(done, () => window.clearTimeout(fallback))
  }
  const shown = useRef<{ mode: FloatMode | null; bubble: Point }>({ mode: null, bubble })
  const openedFrom = useRef(from)  // the button that opened Friday; only the first open grows out of it
  useLayoutEffect(() => {
    const el = root.current, prev = shown.current
    shown.current = { mode, bubble }
    if (prev.mode === null) { if (openedFrom.current && mode !== 'bubble') openFrom(el, openedFrom.current); else settle(el, true); return }
    if (prev.mode === mode) return  // The drag hook owns bubble snap motion.
    folding.current?.cancel(); folding.current = null
    if (mode === 'bubble') { popBubble(el, folded.current); folded.current = false; if (focusBubble.current) bubbleButton.current?.focus(); focusBubble.current = false }
    else if (prev.mode === 'bubble') bloomFromBubble(el, bubble, BUBBLE)
    else settle(el)
  }, [mode, bubble, moving])
  const agents = panes.filter(pane => pane.agent !== 'shell')
  const [targetId, setTargetId] = useState(() => (selected && selected.agent !== 'shell' ? selected : agents[0])?.id ?? '')
  const target = agents.find(pane => pane.id === targetId)
  const asking = agents.filter(approvalOf)
  const approvalPane = target && approvalOf(target) ? target : asking[0]
  const approval = approvalPane ? approvalOf(approvalPane) : null
  const [phase, setPhase] = useState<Phase>('starting')
  const [said, setSaid] = useState('')
  const [reply, setReply] = useState({ who: ASSISTANT, text: '' })
  const [muted, setMuted] = useState(false)
  const [awake, setAwake] = useState(false)
  const [problem, setProblem] = useState('')
  const [menu, setMenu] = useState(false)
  const [watching, setWatching] = useState(0)
  const voice = useRef<LiveVoice | null>(null)
  const agent = useRef<FridayAgent | null>(null)  // set when the local Friday worker runs; else `voice` does the old phrase loop
  const [agentOn, setAgentOn] = useState(false)
  const [voiceKind, setVoiceKind] = useState<'kokoro' | 'system' | null>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const lit = useRef<HTMLElement>(null)  // the light in the Interrupt pill, or in the bubble
  const words = useRef<HTMLDivElement>(null)
  // Keep the newest words in view when a long reply scrolls.
  useEffect(() => { const box = words.current; if (box) box.scrollTop = box.scrollHeight }, [said, reply.text, problem, approval?.id])
  const talkButton = useRef<HTMLButtonElement>(null)
  const phaseNow = useRef<Phase>('starting')
  const talking = useRef(false)  // the person is mid-phrase: hold announcements until they pause
  const queue = useRef<{ who: string; text: string }[]>([])
  const watches = useRef(new Map<string, Watch>())
  const announced = useRef(new Set<string>())
  const live = useRef({ agents, panes, target, approval, approvalPane, commands, api, onClose, onBacklog })
  useEffect(() => { live.current = { agents, panes, target, approval, approvalPane, commands, api, onClose, onBacklog } })
  const go = (next: Phase) => { phaseNow.current = next; setPhase(next) }

  // Every step reads the latest panes and commands through `live`, so the voice callbacks never go stale.
  const flow = useRef({
    async say(who: string, text: string) {
      if (!text.trim()) return
      if (agent.current) { setReply({ who, text }); agent.current.say(text); return }
      if (!voice.current) return
      setReply({ who, text })
      go('speaking')
      const done = await voice.current.speak(sentences(text))
      if (done) flow.current.next()
    },
    /** Speak now, unless the person is talking or a reply is on its way; then after it. */
    announce(who: string, text: string, news?: Omit<AgentEvent, 'chat' | 'text'>) {
      if (agent.current) { agent.current.event({ id: news?.id ?? `${who}-${Date.now()}`, kind: news?.kind ?? 'done', priority: news?.priority ?? 'idle', chat: who, text }); return }
      const phase = phaseNow.current
      if (phase === 'listening' && !talking.current) voice.current?.cancelListening()
      else if (phase !== 'idle' && phase !== 'paused') { queue.current.push({ who, text }); return }
      void flow.current.say(who, text)
    },
    /** After speech: the next queued line, else listen again. */
    next() {
      const line = queue.current.shift()
      if (line) { void flow.current.say(line.who, line.text); return }
      flow.current.listen(true)
    },
    /** Open the mic for the next phrase. The tone marks "your turn" after Friday speaks. */
    listen(tone = false) {
      if (agent.current) { go(agent.current.muted ? 'paused' : 'listening'); return }
      const engine = voice.current
      talking.current = false
      if (engine?.listening) { go('listening'); return }
      if (engine?.listen()) { if (tone) engine.cue('listen'); go('listening') }
      else go(engine?.muted ? 'paused' : 'idle')
    },
    /** Nothing to say: the next queued line, else keep listening. */
    rest() {
      if (queue.current.length) flow.current.next()
      else flow.current.listen()
    },
    async phrase(audio: Blob) {
      go('converting')
      try {
        const response = await fetch('/mobile/api/transcribe', { method: 'POST', body: audio, credentials: 'same-origin', signal: AbortSignal.timeout(60000), headers: { 'X-Codex-Mobile': '1', 'X-Recording': `live-${Date.now()}`, 'X-Live-Phrase': '1', 'Content-Type': audio.type } })
        const data = await response.json().catch(() => ({})) as { text?: string; error?: string }
        if (!response.ok) throw new Error(data.error || 'Voice-to-text failed.')
        const text = (data.text ?? '').trim()
        if (!text) { flow.current.rest(); return }
        setSaid(text)
        setReply(old => ({ ...old, text: '' }))
        setProblem('')
        await flow.current.act(text)
      } catch (failure) {
        setProblem(errorText(failure))
        flow.current.rest()
      }
    },
    /** Commands Live mode runs itself; anything else goes to the target pane. */
    async act(text: string) {
      const { agents, target, approval, approvalPane } = live.current
      const words = text.toLowerCase().replace(/[.!?,]/g, '').trim()
      if (/^(stop|cancel|quiet|be quiet|never ?mind|shh+)$/.test(words)) { flow.current.rest(); return }
      if (/^(end|exit|close|stop) (live|live mode|session)$|^(goodbye|bye)\b/.test(words)) { live.current.onClose(); return }
      if (/\b(what'?s|what is) (running|happening|going on)\b|^(status|brief me|catch me up|briefing)$/.test(words)) { await flow.current.brief(); return }
      if (approval && approvalPane && /^(approve|approved|yes|yeah|yep|allow|go ahead|do it|accept|ok(ay)?)\b/.test(words)) {
        await flow.current.choose(approvalPane, approval, approval.options!.find(option => !option.free && !/^no\b/i.test(option.text)) ?? approval.options![0], 'Approved.')
        return
      }
      if (approval && approvalPane && /^(deny|denied|no|nope|reject|don'?t|do not|decline)\b/.test(words)) {
        const options = approval.options!
        await flow.current.choose(approvalPane, approval, options.find(option => /^no\b/i.test(option.text)) ?? options[options.length - 1], 'Denied.')
        return
      }
      const panel = words.match(/\b(open|show|close|hide)\b.*?\b(preview|browser|terminal|changes|diff|files?|sidebar|backlog)\b/)
      if (panel) { flow.current.panel(panel[1], PANELS[panel[2]]); return }
      const pick = words.match(/^(switch|talk|go|change|send it) to (.+)$/)
      if (pick) {
        const wanted = pick[2].replace(/^the /, '').replace(/ (pane|chat|tab)$/, '')
        const found = agents.find(pane => paneTitle(pane).toLowerCase() === wanted) ?? agents.find(pane => paneTitle(pane).toLowerCase().includes(wanted) || pane.agent === wanted)
        if (found) { setTargetId(found.id); await flow.current.say(ASSISTANT, `Now talking to ${paneTitle(found)}.`) }
        else await flow.current.say(ASSISTANT, `I can't find a chat called ${wanted}.`)
        return
      }
      if (!target) { await flow.current.say(ASSISTANT, 'Pick a chat first.'); return }
      await flow.current.send(target, text)
    },
    /** Type the words into the pane, then watch it for the reply. */
    async deliver(pane: Pane, text: string) {
      const before = await live.current.api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=4`).catch(() => null)
      await live.current.api('message', { session: pane.id, thread: before?.thread ?? pane.thread ?? null, message: text })
      watches.current.set(pane.id, { lastId: before ? lastReply(before)?.id ?? null : null, since: Date.now(), name: paneTitle(pane) })
      setWatching(watches.current.size)
    },
    async send(pane: Pane, text: string) {
      go('sending')
      try {
        await flow.current.deliver(pane, text)
        voice.current?.cue('sent')
        setReply({ who: name(pane), text: '' })
        flow.current.listenOrWork()
      } catch (failure) {
        await flow.current.say(ASSISTANT, `That did not send. ${errorText(failure)}`)
      }
    },
    /** After a send: keep the floor open for the next command while the agent works. */
    listenOrWork() { flow.current.rest() },
    async answer(pane: Pane, approval: Approval, option: Choice) {
      await live.current.api('approval', { session: pane.id, thread: pane.thread ?? null, approval: approval.id, choice: option.id })
      if (!watches.current.has(pane.id)) watches.current.set(pane.id, { lastId: null, since: Date.now(), name: paneTitle(pane) })
      setWatching(watches.current.size)
    },
    async choose(pane: Pane, approval: Approval, option: Choice, spoken: string) {
      go('sending')
      try {
        await flow.current.answer(pane, approval, option)
        if (agent.current) { agent.current.note(`You ${spoken === 'Approved.' ? 'approved' : 'denied'} the request in ${paneTitle(pane)}.`); flow.current.listen(); return }
        await flow.current.say(ASSISTANT, spoken)
      } catch (failure) { await flow.current.say(ASSISTANT, `That did not go through. ${errorText(failure)}`) }
    },
    /** Show or hide a panel. Returns what happened, in words. */
    toggle(open: boolean, kind: string) {
      const { commands, onBacklog: showBacklog } = live.current
      if (kind === 'backlog') {
        if (!open) return { ok: false, words: 'Close the backlog on the screen, so no typing is lost.' }
        if (!showBacklog) return { ok: false, words: 'The backlog cannot open here.' }
        showBacklog()
        return { ok: true, words: 'Opened the backlog.' }
      }
      if (!commands.length) return { ok: false, words: 'Panels work on the desktop only.' }
      const label = kind === 'sidebar' ? 'the sidebar' : `the ${kind === 'browser' ? 'browser' : kind}`
      const command = commands.find(item => item.label.toLowerCase().includes(label) && item.label.startsWith(open ? 'Show' : 'Hide'))
      const other = commands.find(item => item.label.toLowerCase().includes(label))
      const spokenName = kind === 'browser' ? 'preview' : kind
      if (command) { command.run(); return { ok: true, words: `${open ? 'Opened' : 'Closed'} the ${spokenName}.` } }
      if (other) return { ok: true, words: `The ${spokenName} is already ${open ? 'open' : 'closed'}.` }
      return { ok: false, words: `There is no ${spokenName} panel for this chat.` }
    },
    panel(verb: string, kind: string) {
      void flow.current.say(ASSISTANT, flow.current.toggle(verb === 'open' || verb === 'show', kind).words)
    },
    /** A new tab in the "Friday chats" session with an agent in it; the first prompt goes in when the agent is up. */
    async create(wanted: string, kind: 'claude' | 'codex', prompt: string, project?: string) {
      const folder = fridayChatFolder(live.current.panes, live.current.target, project, FRIDAY_SESSION)
      if ('error' in folder) throw new Error(folder.error)
      const { api } = live.current
      const clean = wanted.replace(/[^A-Za-z0-9 _-]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 40).replace(/[ ]+$/, '') || `${kind} ${new Date().toTimeString().slice(0, 5).replace(':', '')}`
      if (!SESSION_NAME.test(clean)) throw new Error('Use letters, numbers, spaces or hyphens for the chat name.')
      // Friday's chats live together in one tmux session, one tab each. Other sessions stay as they are.
      const home = (await api<SessionsData>('sessions')).sessions.find(item => item.tmux_session === FRIDAY_SESSION)
      let id: string
      if (home) id = (await api<{ pane: string }>('split', { session: home.id, direction: 'right', cwd: folder.dir })).pane
      else {
        const made = await api<{ session: string; name: string }>('tmux-sessions', { name: FRIDAY_SESSION, cwd: folder.dir })
        let first: Pane | undefined
        for (let tries = 0; tries < 20 && !first; tries++) {
          first = (await api<SessionsData>('sessions')).sessions.find(item => item.label === made.name)
          if (!first) await wait(300)
        }
        if (!first) throw new Error(`The session ${made.name} started, but its pane is not visible yet.`)
        id = first.id
      }
      await api('pane-name', { pane: id, name: clean })
      await api('start', { session: id, thread: null, agent: kind })
      const made = { name: clean }
      if (prompt.trim()) void (async () => {
        // The agent takes a few seconds to start; type the prompt once the pane shows it.
        for (let tries = 0; tries < 40; tries++) {
          await wait(750)
          const ready = (await api<SessionsData>('sessions').catch(() => null))?.sessions.find(item => item.id === id && item.agent === kind)
          if (ready) { await flow.current.deliver(ready, prompt).catch(failure => flow.current.announce(ASSISTANT, `The prompt did not reach ${made.name}. ${errorText(failure)}`, { id: `new-${id}`, kind: 'error', priority: 'idle' })); return }
        }
        flow.current.announce(ASSISTANT, `${made.name} did not start in time, so the prompt was not sent.`, { id: `new-${id}`, kind: 'error', priority: 'idle' })
      })()
      setTargetId(id)
      return { chat: made.name, folder: folder.dir }
    },
    async brief() {
      const { agents } = live.current
      const data = await live.current.api<Brief>('live/brief').catch(() => null)
      const working = agents.filter(pane => pane.state === 'Working').map(paneTitle)
      const waiting = agents.filter(approvalOf).map(paneTitle)
      const local = !agents.length ? 'No chats are open.'
        : [waiting.length ? `${waiting.join(', ')} ${waiting.length > 1 ? 'need' : 'needs'} your OK.` : '', working.length ? `${working.join(', ')} ${working.length > 1 ? 'are' : 'is'} working.` : '',
          !waiting.length && !working.length ? `All ${agents.length} chats are idle.` : ''].filter(Boolean).join(' ')
      await flow.current.say(ASSISTANT, data?.speech || local)
    },
  })

  // The tools Friday's brain calls. They run here, with the page's own sign-in, and reply in short facts.
  const tools = useRef<Record<string, ToolHandler>>({
    list_chats: async ({ session }) => ({ ...chatList(typeof session === 'string' && session.trim() ? session : undefined) }),
    send_to_chat: async ({ chat, prompt }) => {
      const choice = sessionChoice(live.current.agents, String(chat ?? ''))
      if (choice) return choice
      const pane = findPane(live.current.agents, String(chat ?? '')) ?? (chat ? undefined : live.current.target)
      if (!pane) return { error: `No chat called ${chat}.` }
      if (!String(prompt ?? '').trim()) return { error: 'The prompt is empty.' }
      await flow.current.deliver(pane, String(prompt))
      return { ok: true, chat: paneTitle(pane) }
    },
    answer_approval: async ({ chat, decision }) => {
      const choice = sessionChoice(live.current.agents, String(chat ?? ''))
      if (choice) return choice
      const pane = findPane(live.current.agents, String(chat ?? '')) ?? live.current.approvalPane
      const approval = pane && approvalOf(pane)
      if (!pane || !approval) return { error: 'No chat is waiting for approval.' }
      await flow.current.answer(pane, approval, decision === 'approve' ? yesOption(approval) : noOption(approval))
      return { ok: true, chat: paneTitle(pane), decision: decision === 'approve' ? 'approve' : 'deny' }
    },
    read_last_reply: async ({ chat }) => {
      const choice = sessionChoice(live.current.agents, String(chat ?? ''))
      if (choice) return choice
      const pane = findPane(live.current.agents, String(chat ?? '')) ?? live.current.target
      if (!pane) return { error: `No chat called ${chat}.` }
      const data = await live.current.api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=6`)
      const message = lastReply(data)
      return { chat: paneTitle(pane), text: message ? shortText(message.text, 1500) : 'No reply yet.' }
    },
    new_chat: async ({ name: wanted, agent: kind, prompt, project }) => {
      const made = await flow.current.create(String(wanted ?? ''), kind === 'codex' ? 'codex' : 'claude', String(prompt ?? ''), String(project ?? ''))
      return { ok: true, chat: made.chat, folder: made.folder }
    },
    switch_chat: async ({ chat }) => {
      const choice = sessionChoice(live.current.agents, String(chat ?? ''))
      if (choice) return choice
      const pane = findPane(live.current.agents, String(chat ?? ''))
      if (!pane) return { error: `No chat called ${chat}.` }
      setTargetId(pane.id)
      return { ok: true, chat: paneTitle(pane) }
    },
    open_panel: async ({ panel, open }) => {
      const kind = PANELS[String(panel ?? '').toLowerCase()]
      if (!kind) return { error: `No panel called ${panel}.` }
      const done = flow.current.toggle(open !== false, kind)
      return done.ok ? { ok: true } : { error: done.words }
    },
    backlog_list: async () => {
      const open = await openItems()
      return { count: open.length, items: open.slice(0, 10).map(item => `${item.ref ?? ''} ${shortText(item.text, 140)}${item.status?.kind === 'doing' ? ' (in progress)' : ''}`.trim()) }
    },
    backlog_add: async ({ text }) => {
      if (!String(text ?? '').trim()) return { error: 'The item is empty.' }
      const made = await live.current.api<{ ref: string }>(`${BACKLOG_PATH}-add`, { text: String(text) })
      return { ok: true, id: made.ref }
    },
    backlog_send: async ({ item: wanted, chat }) => {
      const choice = sessionChoice(live.current.agents, String(chat ?? ''))
      if (choice) return choice
      const pane = findPane(live.current.agents, String(chat ?? ''))
      if (!pane || (pane.agent !== 'claude' && pane.agent !== 'codex')) return { error: `No Claude or Codex chat called ${chat}.` }
      const data = await live.current.api<{ text: string; command?: string }>(BACKLOG_PATH)
      const open = parseBacklog(data.text).filter((row): row is Item => row.kind === 'item' && !row.done)
      const words = String(wanted ?? '').trim()
      const ref = /^#?b\s*(\d+)$/i.exec(words)
      const found = ref ? open.filter(item => item.ref === `B${ref[1]}`) : open.filter(item => item.text.toLowerCase().includes(words.toLowerCase()))
      if (!words || !found.length) return { error: `No open backlog item matches ${wanted}.` }
      if (found.length > 1) return { error: `Several items match: ${found.slice(0, 3).map(item => item.ref).join(', ')}. Ask which one.` }
      const item = found[0]
      await flow.current.deliver(pane, itemPrompt(item, data.command))
      const who = [pane.agent, `pane ${pane.id}`, pane.thread && `chat ${pane.thread}`].filter(Boolean).join(' · ')
      await live.current.api(`${BACKLOG_PATH}-start`, { item: item.ref, who }).catch(() => null)
      return { ok: true, item: item.ref, chat: paneTitle(pane) }
    },
  })
  /** The open backlog items, in list order. */
  async function openItems() {
    const data = await live.current.api<{ text: string }>(BACKLOG_PATH)
    return parseBacklog(data.text).filter((row): row is Item => row.kind === 'item' && !row.done)
  }
  /** The chats as Friday sees them: name, agent, state, and what each needs from you. */
  function needsOf(pane: Pane) { return pane.needs?.text ?? (approvalOf(pane) ? shortText(approvalOf(pane)!.text, 140) : '') }
  /** Prompt state: only what matters now. Names of idle chats come from list_chats. */
  function chats(): AgentChats {
    const { agents, target } = live.current
    const needs = agents.filter(pane => needsOf(pane)).map(pane => ({ chat: paneTitle(pane), needs: needsOf(pane) }))
    const busy = agents.filter(pane => !needsOf(pane) && chatState(pane) !== 'idle').map(pane => paneTitle(pane))
    return { target: target ? paneTitle(target) : '', busy, needs, idle: agents.length - busy.length - needs.length }
  }
  /** Every chat, one short line each, with the sidebar's session name. */
  function chatList(session?: string) {
    const { agents, panes, target } = live.current
    return fridayChatList(agents.map(pane => ({
      session: pane.tmux_session, chat: paneTitle(pane), agent: agentNames[pane.agent],
      state: needsOf(pane) ? 'needs you' : chatState(pane), busy: !needsOf(pane) && chatState(pane) !== 'idle',
    })), groupSessions(panes), target ? paneTitle(target) : '', session)
  }

  // Start: the Friday agent when its worker runs, else the phrase loop. Then a short briefing.
  useEffect(() => {
    let gone = false
    const blocked = (failure: unknown) => failure instanceof DOMException && failure.name === 'NotAllowedError'
    const classic = () => {
      const engine = new LiveVoice({
      onPhrase: audio => { talking.current = false; void flow.current.phrase(audio) },
      onSpeech: () => { talking.current = true },
      onBargeIn: () => { engine.stopSpeaking(); flow.current.listen() },
      })
      voice.current = engine
      engine.open().then(() => { if (!gone) void flow.current.brief() }, failure => {
        if (gone) return
        setProblem(blocked(failure) ? 'The microphone is blocked. Allow it in the browser, then start Live mode again.' : errorText(failure))
        go('idle')
      })
    }
    void agentReady().then(async ready => {
      if (gone) return
      if (!ready) { classic(); return }
      const friday = new FridayAgent({
        tools: tools.current,
        onTurn: turn => {
          if (turn === 'listening') flow.current.listen()
          else go(turn)
        },
        onUserText: (text, final) => { if (final) { setSaid(text); setReply(old => ({ ...old, text: '' })); setProblem('') } },
        onBotText: text => setReply({ who: ASSISTANT, text }),
        onMetrics: metrics => console.debug('Friday turn', metrics),
        onVoice: kind => { if (!gone) setVoiceKind(kind) },
        onClosed: reason => {
          reportVoice({ kind: 'friday', event: 'closed', detail: String(reason) })
          if (gone || agent.current !== friday) return
          agent.current = null
          setAgentOn(false)
          friday.close('transport')
          setProblem(`${reason} Using the basic voice instead.`)
          classic()
        },
      })
      agent.current = friday
      setAgentOn(true)
      try {
        await friday.open()
        if (gone) return
        // No spoken briefing: Friday has the chat state and answers when asked.
        friday.state(chats())
        go('listening')
      } catch (failure) {
        reportVoice({ kind: 'friday', event: 'start-failed', detail: errorText(failure) })
        if (gone || agent.current !== friday) return  // onClosed already fell back
        agent.current = null
        setAgentOn(false)
        friday.close('start-failed')
        if (blocked(failure)) { setProblem('The microphone is blocked. Allow it in the browser, then start Live mode again.'); go('idle'); return }
        console.error('Friday could not start', failure)
        setProblem(`Friday could not start: ${errorText(failure)} Using the basic voice instead.`)
        classic()
      }
    })
    return () => { gone = true; agent.current?.close('ended'); agent.current = null; voice.current?.close(); voice.current = null }
  }, [])

  // Friday hears the chat list whenever it changes, so it can answer "what's running" from facts.
  const chatsKey = JSON.stringify([agents.map(pane => [pane.id, paneTitle(pane), pane.state, approvalOf(pane)?.id, pane.needs?.text]), target?.id])
  useEffect(() => { if (agentOn) agent.current?.state(chats()) }, [agentOn, chatsKey])

  // Keep the screen on. The browser drops the lock when the page hides, so ask again when it shows.
  useEffect(() => {
    let lock: WakeLockSentinel | null = null
    let gone = false
    const ask = () => {
      if (gone || document.visibilityState !== 'visible' || !('wakeLock' in navigator)) return
      navigator.wakeLock.request('screen').then(sentinel => {
        if (gone) { void sentinel.release(); return }
        lock = sentinel
        setAwake(true)
        sentinel.addEventListener('release', () => setAwake(false))
      }, () => setAwake(false))
    }
    ask()
    document.addEventListener('visibilitychange', ask)
    return () => { gone = true; document.removeEventListener('visibilitychange', ask); void lock?.release() }
  }, [])

  // New approvals on any chat: say them once.
  useEffect(() => {
    for (const pane of asking) {
      const item = approvalOf(pane)!
      if (announced.current.has(item.id!)) continue
      announced.current.add(item.id!)
      if (phaseNow.current === 'starting') continue  // the briefing covers it
      if (agent.current) flow.current.announce(paneTitle(pane), shortText(item.text, 200), { id: item.id!, kind: 'approval', priority: 'now' })
      else flow.current.announce(ASSISTANT, `${paneTitle(pane)} needs your OK: ${shortText(item.text, 140)} Say approve or deny.`)
    }
  })

  // Watch each chat we sent to. When it stops working with a new reply, say a short version of it.
  useEffect(() => {
    if (!watching) return
    const timer = window.setInterval(() => {
      for (const [id, watch] of watches.current) {
        void api<ChatData>(`chat?session=${encodeURIComponent(id)}&limit=6`).then(async data => {
          if (!watches.current.has(id)) return
          const message = lastReply(data)
          const done = !data.busy && data.state !== 'Working' && Date.now() - watch.since > 1500
          if (Date.now() - watch.since > 15 * 60000) { watches.current.delete(id); setWatching(watches.current.size); return }
          if (!done || !message || message.id === watch.lastId || data.approval) return
          watches.current.delete(id)
          setWatching(watches.current.size)
          // Friday's own brain shortens the reply; the old loop asks the server for a summary.
          if (agent.current) { flow.current.announce(watch.name, shortText(message.text, 600), { id: message.id, kind: 'done', priority: 'idle' }); return }
          const result = await api<{ summary: string }>('live/summary', { text: message.text, pane: watch.name }).catch(() => null)
          const summary = result?.summary || sentences(message.text).slice(0, 2).join(' ')
          flow.current.announce(watch.name, summary)
        }, () => undefined)
      }
    }, 2500)
    return () => window.clearInterval(timer)
  }, [api, watching])

  // The voice shape, the window's glow and the lit pill all follow the voice level. Colors follow the Settings scheme.
  useEffect(() => {
    const element = canvas.current
    const context = element?.getContext('2d')
    if (!element || !context) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const probe = document.createElement('canvas').getContext('2d')!
    const rgb = (token: string, fallback: string) => {
      probe.fillStyle = fallback
      probe.fillStyle = getComputedStyle(element).getPropertyValue(token).trim() || fallback
      const value = probe.fillStyle
      const hex = value.startsWith('#') ? parseInt(value.slice(1), 16) : 0
      return value.startsWith('#') ? [hex >> 16 & 255, hex >> 8 & 255, hex & 255] : (value.match(/\d+/g) ?? ['200', '100', '66']).slice(0, 3).map(Number)
    }
    // The agent's color: the accent's hue turned half way round, a little lighter, so it reads on both themes.
    const opposite = ([r, g, b]: number[]) => {
      const max = Math.max(r, g, b) / 255, min = Math.min(r, g, b) / 255, light = (max + min) / 2, delta = max - min
      const sat = delta ? delta / (1 - Math.abs(2 * light - 1)) : 0
      let hue = !delta ? 0 : max === r / 255 ? ((g - b) / 255 / delta) % 6 : max === g / 255 ? (b - r) / 255 / delta + 2 : (r - g) / 255 / delta + 4
      hue = (hue * 60 + 360 + 180) % 360
      const l = Math.min(0.68, Math.max(0.5, light)), s2 = Math.max(0.35, Math.min(0.55, sat))
      const c = (1 - Math.abs(2 * l - 1)) * s2, x = c * (1 - Math.abs((hue / 60) % 2 - 1)), m = l - c / 2
      const [r1, g1, b1] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x]
      return [r1, g1, b1].map(v => Math.round((v + m) * 255))
    }
    let width = 0, height = 0, smooth = 0, frame = 0
    let aurora: HTMLElement | null = null
    // Layout size, not getBoundingClientRect: that shrinks with the open animation's scale, and a resize observer does not fire again when it ends.
    const size = () => { const ratio = Math.min(devicePixelRatio || 1, 3); width = element.width = Math.round(element.clientWidth * ratio); height = element.height = Math.round(element.clientHeight * ratio) }
    // The orb changes size when words appear. Resize in the draw loop: a resize here would blank the canvas until the next frame.
    let stale = false
    const observer = new ResizeObserver(() => { stale = true })
    observer.observe(element)
    size()
    const color = { you: [0, 0, 0], agent: [0, 0, 0], ask: [0, 0, 0], light: true }
    const read = () => {
      color.you = rgb('--accent', '#c96442'); color.agent = opposite(color.you); color.ask = rgb('--attention', '#e8a33d')
      element.closest<HTMLElement>('.live')?.style.setProperty('--live-agent', `rgb(${color.agent.join(',')})`)
      const [r, g, b] = rgb('--bg', '#ffffff'); color.light = (r * 0.299 + g * 0.587 + b * 0.114) > 140
      const shell = element.closest<HTMLElement>('.live')
      if (shell) shell.dataset.tone = color.light ? 'light' : 'dark'
    }
    read()
    const scheme = new MutationObserver(read)
    scheme.observe(document.documentElement, { attributes: true, attributeFilter: ['style', 'data-theme', 'class'] })
    const current = [...color.you], second = [...color.agent]
    const mix = (from: number[], to: number[], k: number) => from.map((v, i) => Math.round(v + (to[i] - v) * k))
    const rgba = (c: number[], alpha: number) => `rgba(${c.join(',')},${alpha})`
    const glowSpot = (x: number, y: number, r: number, tint: number[], alpha: number) => {
      const fill = context.createRadialGradient(x, y, 0, x, y, r)
      fill.addColorStop(0, rgba(tint, alpha))
      fill.addColorStop(1, rgba(tint, 0))
      context.fillStyle = fill
      context.fillRect(x - r, y - r, r * 2, r * 2)
    }
    /** A solid sphere with a clean edge. Inside it, soft lights drift; only gradients, so no layer shows an edge. */
    const sphere = (cx: number, cy: number, r: number, t: number, amp: number, tint: number[], other: number[]) => {
      context.beginPath()
      for (let i = 0; i <= 120; i++) {
        const a = i / 120 * Math.PI * 2
        const d = 1 + amp * (Math.sin(a * 2 + t * 0.9) * 0.6 + Math.sin(a * 3 - t * 1.2 + 1.7) * 0.4)
        const x = cx + Math.cos(a) * r * d, y = cy + Math.sin(a) * r * d
        if (i) context.lineTo(x, y); else context.moveTo(x, y)
      }
      context.closePath()
      // A dark accent (light themes) would read as a heavy ball, so lift it first.
      const base = color.light ? mix(tint, [255, 255, 255], 0.22) : tint
      const body = context.createRadialGradient(cx - r * 0.25, cy - r * 0.35, 0, cx, cy, r * 1.05)
      body.addColorStop(0, rgba(mix(base, [255, 255, 255], 0.3), 1))
      body.addColorStop(0.6, rgba(base, 1))
      body.addColorStop(1, rgba(mix(base, [0, 0, 0], 0.16), 1))
      context.fillStyle = body
      context.fill()
      context.save()
      context.clip()
      glowSpot(cx + Math.cos(t * 0.6) * r * 0.5, cy + Math.sin(t * 0.8) * r * 0.3 + r * 0.4, r * 0.9, mix(other, base, 0.45), color.light ? 0.3 : 0.5)
      glowSpot(cx - Math.sin(t * 0.5) * r * 0.5, cy + Math.cos(t * 0.7) * r * 0.2 - r * 0.2, r * 0.85, mix(base, [255, 255, 255], 0.55), 0.3)
      context.restore()
    }
    const draw = (now: number) => {
      const t = now / 1000
      const state = phaseNow.current
      const level = agent.current?.level() ?? voice.current?.level() ?? 0
      let wanted = 0.06
      if (state === 'listening') wanted = Math.max(0.12, level)
      else if (state === 'speaking') wanted = Math.max(0.15, level)
      else if (state === 'converting' || state === 'thinking' || state === 'sending') wanted = 0.22
      else if (state === 'paused') wanted = 0.02
      if (state === 'listening' && watches.current.size) wanted = Math.max(wanted, 0.1 + 0.04 * Math.sin(t * 2))
      smooth += (wanted - smooth) * (reduced ? 1 : 0.18)
      // Set the light's own transform: a CSS variable on an ancestor would restyle the whole Live screen each frame.
      aurora ??= element.closest('.live')?.querySelector<HTMLElement>('.live-aurora') ?? null
      // A slow breath keeps the light alive in silence. It stays still for reduced motion.
      const glow = reduced ? 0.25 : Math.min(0.6, smooth) + 0.025 * Math.sin(t * 1.6)
      const dim = state === 'paused' || state === 'idle' || state === 'starting'
      if (aurora) {
        aurora.style.transform = `scaleY(${(0.76 + glow * 0.8).toFixed(3)})`
        aurora.style.opacity = dim ? '0.35' : Math.min(1, 0.72 + glow * 0.6).toFixed(3)
      }
      if (lit.current) {
        lit.current.style.transform = `scaleY(${(0.9 + glow * 0.4).toFixed(3)})`
        lit.current.style.opacity = dim ? '0.4' : '1'
      }
      if (stale) { stale = false; size() }
      // The bubble hides the canvas. With no size there is nothing to draw, and a negative radius would throw.
      if (!width || !height) { frame = requestAnimationFrame(draw); return }
      const [first, next] = state === 'speaking' ? [color.agent, color.you] : live.current.approval ? [color.ask, color.you] : [color.you, color.agent]
      for (let i = 0; i < 3; i++) { current[i] += (first[i] - current[i]) * 0.06; second[i] += (next[i] - second[i]) * 0.06 }
      context.clearRect(0, 0, width, height)
      // Everything must end inside the canvas: a gradient cut by the edge shows as a hard square.
      // Largest reach: 0.5 × 1.25 (level) × 1.08 (wobble) ≈ 0.68 of the half width; the halo stops at the edge.
      const cx = width / 2, cy = height / 2, half = Math.min(width, height) / 2
      const tt = reduced ? 0 : t * (state === 'converting' ? 2.6 : state === 'paused' ? 0.4 : 1)
      const r = half * 0.5 * (1 + (reduced ? 0 : Math.min(1, smooth) * 0.25))
      const amp = reduced ? 0.01 : 0.012 + Math.min(1, smooth) * 0.04
      const one = current.map(Math.round), two = second.map(Math.round)
      const reach = Math.min(r * 2, half)
      const halo = context.createRadialGradient(cx, cy, r * 0.8, cx, cy, reach)
      halo.addColorStop(0, rgba(one, 0.2 + smooth * 0.3))
      halo.addColorStop(1, rgba(one, 0))
      context.fillStyle = halo
      context.beginPath(); context.arc(cx, cy, reach, 0, Math.PI * 2); context.fill()
      sphere(cx, cy, r, tt, amp, one, two)
      if ((state === 'converting' || state === 'thinking' || state === 'sending') && !reduced) {
        context.lineWidth = Math.max(2, width / 180)
        context.strokeStyle = rgba(one, 0.8)
        context.lineCap = 'round'
        context.beginPath(); context.arc(cx, cy, Math.min(r * 1.3, half - context.lineWidth), t * 4, t * 4 + 1.1); context.stroke()
      }
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => { cancelAnimationFrame(frame); observer.disconnect(); scheme.disconnect() }
  }, [])

  function talk() {
    if (agent.current) {  // Friday hears all the time: this only stops its reply. The mic button mutes.
      if (phase === 'speaking' || phase === 'thinking') agent.current.interrupt('tap', phase)
      // A tap while Friday thinks drops the answer it was about to give, so say so.
      if (phase === 'thinking') setReply({ who: ASSISTANT, text: 'Stopped. Say it again.' })
      return
    }
    const engine = voice.current
    if (!engine || phase === 'starting') return
    if (phase === 'speaking') { engine.stopSpeaking(); flow.current.listen(); return }
    if (phase === 'listening') { engine.finishListening(); return }
    if (phase === 'paused') { togglePause(); return }
    if (phase === 'idle') flow.current.listen(true)
  }
  /** Pause: the mic stops; Friday still reads replies and approvals. Resume: listen again. */
  function togglePause() {
    const friday = agent.current
    if (friday) { friday.muted = !muted; setMuted(friday.muted); go(friday.muted ? 'paused' : 'listening'); return }
    const engine = voice.current
    if (!engine) return
    engine.muted = !muted
    setMuted(engine.muted)
    if (engine.muted) { engine.cancelListening(); if (phaseNow.current === 'listening') go('paused') }
    else if (phaseNow.current === 'paused' || phaseNow.current === 'idle') flow.current.listen(true)
  }
  function tapApproval(yes: boolean) {
    if (!approval || !approvalPane) return
    const option = yes ? yesOption(approval) : noOption(approval)
    voice.current?.stopSpeaking()
    void flow.current.choose(approvalPane, approval, option, yes ? 'Approved.' : 'Denied.')
  }

  // Space talks. Esc steps down: full screen to window, window to bubble. Only End stops the call.
  // Outside full screen Tether stays usable, so the keys work only while focus is inside Friday.
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const target = event.target as Element
      if (mode !== 'full' && !root.current?.contains(target)) return
      if (event.key === 'Escape') { event.preventDefault(); if (menu) setMenu(false); else if (mode === 'full') setMode('window'); else if (mode === 'window') shrink(true) }
      else if (event.key === ' ' && mode !== 'bubble' && !target.closest('input, textarea, button, [role="menu"]')) { event.preventDefault(); talkButton.current?.click() }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [menu, mode, setMode])
  // The rail button or the command menu asks again while the call runs: bring the window back.
  // The bubble button goes away when the window opens: move focus into the window, not to the page.
  const lastMode = useRef(mode)
  useEffect(() => { if (lastMode.current === 'bubble' && mode !== 'bubble') root.current?.focus(); lastMode.current = mode }, [mode])
  // Back shrinks Friday to its bubble, like a call app. Only End stops the call.
  useBackStep(mode !== 'bubble', () => shrink())
  const revealed = useRef(reveal)
  useEffect(() => { if (reveal !== revealed.current) { revealed.current = reveal; if (mode === 'bubble') setMode('window') } }, [reveal, mode, setMode])
  useEffect(() => { talkButton.current?.focus() }, [])

  const working = agents.filter(pane => pane.state === 'Working').length
  const status = STATUS[phase]
  const talkLabel = agentOn ? 'Interrupt' : phase === 'speaking' ? 'Interrupt' : phase === 'listening' ? 'Send now' : phase === 'paused' ? 'Resume' : 'Talk'
  const picker = agents.map(pane => <button key={pane.id} type="button" role="menuitemradio" aria-checked={pane.id === targetId} className="live-pane" data-state={approvalOf(pane) ? 'asking' : pane.state === 'Working' ? 'working' : 'idle'} onClick={() => { setTargetId(pane.id); setMenu(false) }}>
    <i aria-hidden="true" /><span><b>{paneTitle(pane)}</b><small>{agentNames[pane.agent]}{approvalOf(pane) ? ' · needs your OK' : pane.state === 'Working' ? ' · working' : ''}</small></span><Check size={15} className="live-check" aria-hidden="true" />
  </button>)

  const asks = !!(approval && approvalPane)
  const interruptable = phase === 'speaking' || phase === 'thinking'
  const voiceInfo = voiceKind === 'kokoro' ? 'Voice: this device' : voiceKind === 'system' ? 'Voice: device basic' : 'Voice: server'
  const bubbleLabel = `Open ${ASSISTANT}: ${status}${asks ? ', a chat needs your OK' : ''}`
  return <div ref={el => { root.current = el; attachWindow(el) }} className={`live live-${mode}${moving ? ' moving' : ''}`} style={mode === 'window' ? windowStyle : mode === 'bubble' ? bubbleStyle : undefined}
    tabIndex={-1} role="dialog" aria-modal={mode === 'full'} aria-label={`${ASSISTANT} Live mode`} data-phase={phase} data-asking={asks}>
    {mode === 'bubble' && <button ref={bubbleButton} type="button" className="live-bubble-hit" aria-label={bubbleLabel} title={bubbleLabel} {...bubbleHandlers}><i ref={lit} className="live-lit" aria-hidden="true" /></button>}
    {mode === 'window' && EDGES.map(edge => <span key={edge} className={`live-edge live-edge-${edge}`} aria-hidden="true" {...resize(edge)} />)}
    {mode === 'window' && <button type="button" className="live-grip-resize" aria-label="Resize Friday window. Use the arrow keys." title="Drag to resize" {...resize('se')} />}
    <aside className="live-rail" aria-label="Commands go to">
      <h2>Commands go to</h2>
      <div role="menu" aria-label="Send voice commands to">{picker}</div>
      <p className="live-note">{agentOn ? 'Just talk. Friday sends your requests to the chats, answers questions, and tells you when a chat finishes or needs you.' : 'Just talk: each pause sends what you said. Say “switch to” and a chat name to change it, or “what\'s running” for a briefing.'}</p>
    </aside>
    <section className="live-stage" inert={mode === 'bubble'} {...(mode === 'window' ? { onPointerDown: move.onPointerDown, onPointerMove: move.onPointerMove, onPointerUp: move.onPointerUp, onPointerCancel: move.onPointerCancel } : {})}>
      <div className="live-aurora" aria-hidden="true" />
      <header className="live-top">
        {mode === 'window' && <button type="button" className="live-icon live-grip" data-float-grip aria-label="Move Friday window. Use the arrow keys." title="Drag to move" onKeyDown={move.onKeyDown}><GripHorizontal size={16} aria-hidden="true" /></button>}
        <span className="live-target-wrap">
          <button type="button" className="live-target" aria-haspopup="menu" aria-expanded={menu} aria-label={target ? `Choose chat: ${name(target)}` : 'Choose chat'} onClick={() => setMenu(open => !open)}>
            {target ? <><b>{paneTitle(target)}</b><span>{agentNames[target.agent]}</span></> : <b>No chat</b>}<ChevronDown size={15} aria-hidden="true" />
          </button>
          {menu && <div className="live-popover" role="menu" aria-label="Send voice commands to"><h3>Commands go to</h3>{picker.length ? picker : <p className="live-note">No agent chats are open.</p>}</div>}
        </span>
        <span className="live-info" title={awake ? 'The screen stays on while Live mode runs.' : 'This browser can turn the screen off.'}>{awake ? 'Screen on' : 'Screen may sleep'}</span>
        {agentOn && <span className="live-info" title={voiceKind ? 'This device speaks Friday’s replies, so a busy server cannot break the voice.' : 'The server speaks Friday’s replies.'}>{voiceInfo}</span>}
        <span className="live-window-buttons">
          <button type="button" className="live-icon" aria-label="Shrink Friday to a bubble" title="Shrink to a bubble (Esc). The call stays on." onClick={() => shrink()}><Minus size={18} aria-hidden="true" /></button>
          {mode === 'full'
            ? <button type="button" className="live-icon" aria-label="Show Friday in a window" title="Window" onClick={() => setMode('window')}><Minimize2 size={16} aria-hidden="true" /></button>
            : <button type="button" className="live-icon" aria-label="Show Friday full screen" title="Full screen" onClick={() => setMode('full')}><Maximize2 size={16} aria-hidden="true" /></button>}
        </span>
      </header>

      <div className="live-center">
        <div className="live-orb">
          <canvas ref={canvas} aria-hidden="true" onClick={talk} />
        </div>
        <div className="live-status" aria-live="polite" title={`${awake ? 'Screen on' : 'Screen may sleep'}${agentOn ? ` · ${voiceInfo}` : ''}`}>{status}{working ? <span> · {working} working</span> : null}</div>
        <div ref={words} className="live-words" data-no-drag aria-live="polite">
          {said && <p className="live-said" data-small={!!reply.text}>{said}</p>}
          {reply.text && <p className="live-reply" data-long={reply.text.length > 110}>{reply.who !== ASSISTANT && <span className="live-who">{reply.who}</span>}{reply.text}</p>}
          {problem && <p className="live-problem" role="alert">{problem}</p>}
        </div>
      </div>

      {approval && approvalPane && <div className="live-approval" data-no-drag role="group" aria-label={`${paneTitle(approvalPane)} needs your OK`}>
        <h4>Needs your OK <span>{name(approvalPane)}</span></h4>
        <p>{shortText(approval.text, 220)}</p>
        <small>Say approve or deny, or tap.</small>
        <div className="live-row"><button type="button" className="live-deny" onClick={() => tapApproval(false)}>Deny</button><button type="button" className="live-approve" onClick={() => tapApproval(true)}>Approve</button></div>
      </div>}

      <div className="live-controls">
        <div className="live-ctl"><button type="button" className="live-round" aria-pressed={muted} aria-label={agentOn ? 'Mute microphone' : muted ? 'Resume listening' : 'Pause listening'} onClick={togglePause} disabled={phase === 'starting'}>{muted ? <MicOff size={22} aria-hidden="true" /> : <Mic size={22} aria-hidden="true" />}</button><span>{agentOn ? (muted ? 'Unmute' : 'Mute') : muted ? 'Paused' : 'Pause'}</span></div>
        <div className="live-ctl live-talk"><button ref={talkButton} type="button" className="live-pill" aria-label={talkLabel} onClick={talk} disabled={agentOn ? !interruptable : phase === 'starting'}>
          {mode !== 'bubble' && <i ref={lit} className="live-lit" aria-hidden="true" />}
          {phase === 'speaking' || (agentOn && interruptable) ? <Square size={20} fill="currentColor" aria-hidden="true" /> : agentOn ? null : phase === 'listening' ? <ArrowUp size={24} aria-hidden="true" /> : phase === 'paused' ? <Mic size={22} aria-hidden="true" /> : <AudioLines size={24} aria-hidden="true" />}
        </button><span>{talkLabel} <kbd>Space</kbd></span></div>
        <div className="live-ctl live-end"><button type="button" className="live-round" aria-label="End Live mode" onClick={onClose}><PhoneOff size={22} aria-hidden="true" /></button><span>End</span></div>
      </div>
    </section>
  </div>
}
