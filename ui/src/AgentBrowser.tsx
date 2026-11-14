import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Bot, ChevronDown, CircleSlash, Globe, Hand, KeyRound, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane, SignedBrowserAccess } from './api'
import type { BrowserAskRow } from './needs'
import { BrowserPanel } from './BrowserPanel'
import { useBackStep } from './backNav'
import './AgentBrowser.css'

// Agents (Playwright MCP, scripts) drive tabs in Tether's shared browser. See server/browser_cdp.py.
export interface AgentTab { id: string; url: string; title: string }
export interface AgentAsk { id: string; pane: string | null; text: string; at: number }
// Server shape (contract.md): `agents[].tabs` are target ids. The poll adds titles from `browser/tabs`.
interface AgentBrowserWire {
  enabled: boolean; available: boolean; reason: string
  agents: { pane: string | null; tabs: string[]; since: number }[]
  asks: AgentAsk[]
  // Newer servers (spec-s3): can Claude and Codex find the browser, and which running agents started before it was on.
  setup?: { claude: SetupState; codex: SetupState; claude_reason?: string; codex_reason?: string }
  stale?: string[]
}
type SetupState = 'ok' | 'missing' | 'none' | 'manual'
export interface AgentBrowserStatus extends Omit<AgentBrowserWire, 'agents'> {
  agents: { pane: string | null; tabs: AgentTab[]; since: number }[]
  // Signed-in browser: agents' requests waiting for Mak, the grants running now, and grants that ended
  // without Mak's End (server/signed_access.py).
  signed: Pick<SignedBrowserAccess, 'requests' | 'grants' | 'ended'>
}
const NO_SIGNED = { requests: [], grants: [], ended: [] }
const pickSigned = (access: SignedBrowserAccess) => ({ requests: access.requests ?? [], grants: access.grants ?? [], ended: access.ended ?? [] })

export const WATCH_EVENT = 'tether-browser-watch'
export const TAB_KEY = 'tether-browser-tab'
const POLL_MS = 4000

// One poll for the whole app, however many chats show the card.
let status: AgentBrowserStatus | null = null
let poller: { api: Api; timer: number } | null = null  // one chain of timers: the next poll waits for this one to end
const listeners = new Set<() => void>()
function emit() { listeners.forEach(listener => listener()) }
let known: Record<string, AgentTab> = {}
let signed: AgentBrowserStatus['signed'] = NO_SIGNED
async function accept(api: Api, wire: AgentBrowserWire | null, priority?: RequestPriority) {
  // Older servers answer 404: treat that as "no agent browser".
  if (!wire || !Array.isArray(wire.agents)) { status = null; emit(); return }
  const ids = wire.agents.flatMap(agent => agent.tabs)
  // Only ask for titles when agents have tabs: then the browser runs already, and `browser/tabs` does not start it.
  if (ids.length) {
    try {
      const rows = (await api<{ tabs: AgentTab[] }>('browser/tabs', undefined, undefined, undefined, undefined, priority)).tabs
      known = Object.fromEntries(rows.map(row => [row.id, { id: row.id, url: row.url, title: row.title }]))
    } catch { /* keep the old titles */ }
  }
  status = { ...wire, signed, agents: wire.agents.map(agent => ({ ...agent, tabs: agent.tabs.map(id => known[id] ?? { id, url: '', title: '' }) })) }
  emit()
}
// Background status: low fetch priority, so on a slow link it does not hold up chat requests (B67).
async function poll(api: Api) {
  if (document.visibilityState !== 'visible') return
  let wire: AgentBrowserWire | null = null
  try { wire = await api<AgentBrowserWire>('browser/agents', undefined, undefined, undefined, undefined, 'low') } catch { /* off or old server */ }
  // Does not start Chrome. Older servers answer 404: no requests.
  try { const access = await api<SignedBrowserAccess>('browser/signed/access', undefined, undefined, undefined, undefined, 'low'); signed = pickSigned(access) } catch { signed = NO_SIGNED }
  await accept(api, wire, 'low')
}
// The next poll starts POLL_MS after this one ends, so slow answers never stack up.
function run(current: { api: Api; timer: number }) {
  void poll(current.api).catch(() => {}).finally(() => { if (poller === current) current.timer = window.setTimeout(() => run(current), POLL_MS) })
}
function subscribe(api: Api) {
  return (listener: () => void) => {
    listeners.add(listener)
    if (!poller) {
      poller = { api, timer: 0 }
      run(poller)
    }
    return () => {
      listeners.delete(listener)
      if (!listeners.size && poller) { clearTimeout(poller.timer); poller = null }
    }
  }
}
const subscribers = new WeakMap<Api, (listener: () => void) => () => void>()
export function useAgentBrowser(api: Api) {
  let sub = subscribers.get(api)
  if (!sub) { sub = subscribe(api); subscribers.set(api, sub) }
  return useSyncExternalStore(sub, () => status)
}
async function answerSigned(api: Api, path: 'allow' | 'deny' | 'end' | 'dismiss', id: string) {
  const access = await api<SignedBrowserAccess>(`browser/signed/${path}`, { id })
  signed = pickSigned(access)
  if (status) { status = { ...status, signed }; emit() }
}
export async function changeAgentBrowser(api: Api, path: 'browser/agents' | 'browser/ask/dismiss' | 'browser/agents/setup', body: unknown) {
  await accept(api, await api<AgentBrowserWire>(path, body))
}

/** Open asks for the "Needs you" list, each with its agent's first tab. */
export function browserAsks(state: AgentBrowserStatus | null): BrowserAskRow[] {
  return (state?.asks ?? []).map(ask => ({ ...ask, tab: state?.agents.find(agent => agent.pane === ask.pane)?.tabs[0]?.id }))
}

// Chat names for the owner label in the browser panel, as the sidebar shows them. App.tsx keeps this current.
let labels: Record<string, string> = {}
const labelListeners = new Set<() => void>()
export function setPaneLabels(panes: { id: string; label: string }[]) {
  const next: Record<string, string> = {}
  for (const pane of panes) next[pane.id] = pane.label
  if (JSON.stringify(next) === JSON.stringify(labels)) return
  labels = next
  labelListeners.forEach(listener => listener())
}
const subscribeLabels = (listener: () => void) => { labelListeners.add(listener); return () => { labelListeners.delete(listener) } }
export function usePaneLabel(pane: string | null | undefined) {
  const all = useSyncExternalStore(subscribeLabels, () => labels)
  return pane ? all[pane] ?? null : null
}

/** Open the browser on an agent's tab: the desktop panel, or the phone sheet. App.tsx listens.
 * browser 'signed' or 'chrome': a tab Mak allowed an agent to use in the signed-in browser or in Mak's own Chrome. */
export function watchTab(tab: string, pane: string, browser: 'test' | 'signed' | 'chrome' = 'test') {
  try { localStorage.setItem(browser === 'test' ? TAB_KEY : `${TAB_KEY}-${browser}`, tab) } catch { /* private mode: the panel picks the first tab */ }
  window.dispatchEvent(new CustomEvent(WATCH_EVENT, { detail: { tab, pane, browser } }))
}

export function hostOf(url: string) {
  try { return new URL(url).host || url } catch { return url }
}

/** Status stack items for one chat: its agent's tabs, and a request for the person when there is one. */
export function agentBrowserItems(api: Api, state: AgentBrowserStatus | null, pane: string) {
  const tabs = state?.agents.filter(agent => agent.pane === pane).flatMap(agent => agent.tabs) ?? []
  const ask = state?.asks.find(item => item.pane === pane) ?? null
  const signedAsk = state?.signed.requests.find(item => item.pane === pane)
  const grant = state?.signed.grants.find(item => item.pane === pane)
  // Rows run oldest to newest: show the newest one.
  const ended = state?.signed.ended.filter(item => item.pane === pane).pop()
  const signedItems = signedAsk ? [{ id: 'browser-signed-ask', label: signedAsk.browser === 'chrome' ? 'Wants your Chrome' : 'Wants your signed-in browser', active: true, content: <SignedAskCard api={api} request={signedAsk} /> }]
    : grant ? [{ id: 'browser-signed', label: grant.browser === 'chrome' ? 'Using your Chrome' : 'Using your signed-in browser', active: true, content: <SignedGrantCard api={api} grant={grant} /> }]
    : ended ? [{ id: 'browser-signed-ended', label: 'Access ended', active: true, content: <SignedEndedCard api={api} ended={ended} /> }] : []
  if (!tabs.length && !ask) return signedItems
  const first = tabs.find(tab => tab.url && tab.url !== 'about:blank') ?? tabs[0]
  const label = ask ? 'Needs you in the browser' : first ? `Browsing ${hostOf(first.url) || 'a page'}` : 'Browser'
  return [...signedItems, { id: ask ? 'browser-ask' : 'browser', label, active: true, content: <AgentBrowserCard api={api} pane={pane} tabs={tabs} ask={ask} /> }]
}

type SignedRequest = AgentBrowserStatus['signed']['requests'][number]
type SignedGrant = AgentBrowserStatus['signed']['grants'][number]
type SignedEnded = AgentBrowserStatus['signed']['ended'][number]

/** An agent asks to drive one tab of the signed-in browser. It waits up to 2 minutes for this answer. */
function SignedAskCard({ api, request }: { api: Api; request: SignedRequest }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const name = usePaneLabel(request.pane) ?? request.agent
  const answer = async (path: 'allow' | 'deny') => {
    setBusy(true); setError('')
    try { await answerSigned(api, path, request.id) } catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  return <div className="agent-browser-card">
    <div className="agent-browser-ask signed-ask" role="alert">
      <KeyRound size={16} aria-hidden="true" />
      <div className="signed-ask-text">
        <p><strong>{name}</strong> wants to use {request.browser === 'chrome' ? 'your Chrome' : 'your signed-in browser'} for {request.minutes} min.</p>
        <p className="signed-ask-reason">“{request.reason}”</p>
        {request.url && <p className="signed-ask-meta">Opens {hostOf(request.url)}</p>}
        <p className="signed-ask-meta">It gets one new tab with your sign-ins{request.browser === 'chrome' ? ' in your Chrome' : ''}. It cannot see your other tabs, cookies or settings.</p>
        {request.browser === 'chrome' && <p className="signed-ask-meta"><strong>Chrome shows a debugging bar. Keep it open: closing it ends access.</strong></p>}
      </div>
      <div className="agent-browser-actions">
        <button type="button" className="primary signed-allow" disabled={busy} onClick={() => void answer('allow')}>Allow</button>
        <button type="button" className="secondary" disabled={busy} onClick={() => void answer('deny')}>Deny</button>
      </div>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}

function minutesLeft(expires: number, now: number) { return Math.max(0, Math.ceil((expires * 1000 - now) / 60000)) }

/** A running grant: watch the agent's tab, or end access now. The tab stays open for Mak after the end. */
function SignedGrantCard({ api, grant }: { api: Api; grant: SignedGrant }) {
  const [now, setNow] = useState(() => Date.now())
  const [error, setError] = useState('')
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(timer) }, [])
  const left = minutesLeft(grant.expires, now)
  return <div className="agent-browser-card">
    <ul className="agent-browser-tabs">
      <li>
        <KeyRound size={15} aria-hidden="true" />
        <span><strong>{grant.browser === 'chrome' ? 'Your Chrome tab' : 'Signed-in tab'}</strong><small>{left ? `${left} min left` : 'Ending'} · {grant.browser === 'chrome' ? 'Keep Chrome’s debugging bar open' : grant.reason}</small></span>
        <button type="button" className="secondary" onClick={() => watchTab(grant.tab, grant.pane, grant.browser === 'chrome' ? 'chrome' : 'signed')}>Watch</button>
        <button type="button" className="secondary" onClick={() => void answerSigned(api, 'end', grant.id).catch(failure => setError(errorText(failure)))}>End now</button>
      </li>
    </ul>
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}

function AgentBrowserCard({ api, pane, tabs, ask }: { api: Api; pane: string; tabs: AgentTab[]; ask: AgentAsk | null }) {
  const [error, setError] = useState('')
  const watchFirst = () => { if (tabs[0]) watchTab(tabs[0].id, pane) }
  return <div className="agent-browser-card">
    {ask && <div className="agent-browser-ask" role="alert">
      <Hand size={16} aria-hidden="true" />
      <p>{ask.text}</p>
      <div className="agent-browser-actions">
        {tabs.length > 0 && <button type="button" className="primary" onClick={watchFirst}>Open the page</button>}
        <button type="button" className="secondary" onClick={() => void changeAgentBrowser(api, 'browser/ask/dismiss', { id: ask.id }).catch(failure => setError(errorText(failure)))}>Done</button>
      </div>
    </div>}
    {tabs.length > 0 && <ul className="agent-browser-tabs">
      {tabs.map(tab => {
        // Chromium uses the URL as the title when a page has none.
        const title = tab.title && tab.title !== tab.url ? tab.title : ''
        return <li key={tab.id}>
        <Bot size={15} aria-hidden="true" />
        <span><strong>{title || hostOf(tab.url) || 'New tab'}</strong>{title && <small>{hostOf(tab.url)}</small>}</span>
        <button type="button" className="secondary" onClick={() => watchTab(tab.id, pane)}>Watch</button>
      </li>
      })}
    </ul>}
    {!tabs.length && ask && <p className="setting-note">The agent has no page open yet.</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}

/** Phone: the browser panel full screen, over the chat. */
// Phone: a sheet over the chat. Minimise keeps the browser's tabs and shows a round button; the stream stops meanwhile.
export function BrowserSheet({ api, pane, onClose }: { api: Api; pane: Pane; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const bubble = useRef<HTMLButtonElement>(null)
  const [small, setSmall] = useState(false)
  useBackStep(!small, onClose)
  useEffect(() => {
    const box = dialog.current
    if (!box) return
    if (!small && !box.open) box.showModal()
    if (small && box.open) { box.close(); bubble.current?.focus() }
  }, [small])
  // "Watch" on a chat card while minimised: open the sheet again.
  useEffect(() => {
    const onWatch = () => setSmall(false)
    window.addEventListener(WATCH_EVENT, onWatch)
    return () => window.removeEventListener(WATCH_EVENT, onWatch)
  }, [])
  return <>
    <dialog ref={dialog} className="browser-sheet" aria-label="Browser" onCancel={event => { event.preventDefault(); setSmall(true) }}
      onClick={event => { if (event.target === event.currentTarget && event.clientY < event.currentTarget.getBoundingClientRect().top) setSmall(true) }}>
      <header className="browser-sheet-head">
        <button type="button" className="icon-button" aria-label="Minimise the browser" title="Minimise" onClick={() => setSmall(true)}><ChevronDown /></button>
        <strong>Browser</strong>
        <button type="button" className="icon-button" aria-label="Close the browser" title="Close" onClick={onClose}><X /></button>
      </header>
      <BrowserPanel api={api} pane={pane} folder={null} active={!small} />
    </dialog>
    {small && <button ref={bubble} type="button" className="browser-bubble" aria-label="Open the browser" title="Browser" onClick={() => setSmall(false)}><Globe size={22} /></button>}
  </>
}

/** Settings › System: the switch and why it is off. */
export function AgentBrowserSettings({ api }: { api: Api }) {
  const state = useAgentBrowser(api)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!state) return null
  const connected = state.agents.length
  const setup = async (agent: 'claude' | 'codex') => {
    setBusy(true); setError('')
    try { await changeAgentBrowser(api, 'browser/agents/setup', { agent }) } catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  const note = !state.enabled ? 'Off. Agents start their own hidden browser.'
    : !state.available ? `Not available: ${state.reason || 'unknown reason'}.`
    : connected ? `On · ${connected} ${connected === 1 ? 'agent' : 'agents'} connected.` : 'On · Agents started from now on use it.'
  return <section className="settings-group" aria-labelledby="settings-agent-browser"><h3 id="settings-agent-browser">Shared browser</h3>
    {error && <p className="error" role="alert">{error}</p>}
    <label className="switch-row"><span><strong>Agents use Tether’s browser</strong><small>{note}</small></span>
      <input type="checkbox" role="switch" checked={state.enabled} disabled={busy} onChange={event => {
        setBusy(true); setError('')
        void changeAgentBrowser(api, 'browser/agents', { enabled: event.target.checked }).catch(failure => setError(errorText(failure))).finally(() => setBusy(false))
      }} /></label>
    {state.enabled && state.setup && (['claude', 'codex'] as const).filter(agent => state.setup![agent] !== 'none').map(agent => {
      const name = agent === 'claude' ? 'Claude Code' : 'Codex'
      const value = state.setup![agent]
      const note = value === 'ok' ? 'Ready.'
        : value === 'missing' ? (agent === 'claude' ? 'Claude does not know about the shared browser yet.' : 'Codex blocks the browser address for Playwright MCP.')
        : state.setup![`${agent}_reason` as const] || 'Change the settings file by hand.'
      return <div className="switch-row agent-browser-setup" key={agent}>
        <span><strong>{name}</strong><small>{note}</small></span>
        {value === 'missing' && <button type="button" className="secondary" disabled={busy} onClick={() => void setup(agent)}>Set up</button>}
      </div>
    })}
    {state.enabled && !!state.stale?.length && <StaleAgents panes={state.stale} />}
    <p className="setting-note">You can watch their tabs in the browser. Set up changes your Claude or Codex settings file and keeps a copy of the old file.</p>
  </section>
}

function StaleAgents({ panes }: { panes: string[] }) {
  const all = useSyncExternalStore(subscribeLabels, () => labels)
  const [open, setOpen] = useState(false)
  const names = panes.map(pane => all[pane] ?? pane)
  const one = names.length === 1
  return <div className="setting-note agent-browser-stale" role="status">
    <p>{one ? `${names[0]} started before this was on.` : `${names.length} agents started before this was on.`} {one ? 'Restart it to use the shared browser.' : 'Restart an agent to let it use the shared browser.'}</p>
    {!one && <button type="button" className="agent-browser-names" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? 'Hide names' : 'Show names'}</button>}
    {!one && open && <ul>{names.map((name, index) => <li key={panes[index]}>{name}</li>)}</ul>}
  </div>
}

/** A grant ended without Mak's End: say why. The agent gets the same reason (HTTP 410 or WebSocket close 4000). */
function SignedEndedCard({ api, ended }: { api: Api; ended: SignedEnded }) {
  const [error, setError] = useState('')
  return <div className="agent-browser-card">
    <ul className="agent-browser-tabs">
      <li className="ended">
        <CircleSlash size={15} aria-hidden="true" />
        <span><strong>{ended.browser === 'chrome' ? 'Your Chrome' : 'Signed-in browser'}</strong><small>{ended.message}</small></span>
        <button type="button" className="secondary" onClick={() => void answerSigned(api, 'dismiss', ended.id).catch(failure => setError(errorText(failure)))}>Dismiss</button>
      </li>
    </ul>
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
