// Test-only entry. The responsive runner bundles this file separately from the production app.
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { CircleAlert } from 'lucide-react'
import type { Api, Ask, AskBridge, Attention, ChoiceState, Pane } from '../src/api'
import type { Session } from '../src/Sidebar'
import type { SidePin, WaitOrder } from '../src/prefs'
import type { Media } from '../src/ImageViewer'
import { ChoiceDock } from '../src/Dock'
import { AskForm } from '../src/ChoiceExtras'
import { ActionDeck } from '../src/ActionDeck'
import { AskChips, OldestButton, WaitingBox } from '../src/Attention'
import { ImageViewer } from '../src/ImageViewer'
import { PreviewDrawer } from '../src/PreviewDrawer'
import { NoticeStack } from '../src/Notices'
import { SidePins } from '../src/SidePins'
import { PaneActions, SessionActions } from '../src/Sheets'
import { OrganizeMenu } from '../src/Organize'
import { usePrefs } from '../src/prefs'
import { useQuickCreate } from '../src/QuickToast'
import '../src/index.css'
import '../src/App.css'

const query = new URLSearchParams(location.search)
const surface = query.get('surface') || 'choice'
const state = query.get('state') || 'ready'
const long = (query.get('word') || 'Responsive').repeat(10).slice(0, 80)
const error = `Request failed: ${long.repeat(3)}. ${'The fixture server could not finish this request. '.repeat(18)}`
const now = Math.floor(Date.now() / 1000)
const noop = () => {}
const done = async () => {}
const answer = async () => null
const pane = (index: number): Pane => ({ id: `%${index}`, tmux_session: '$1', label: `fixture:${index}`, agent: 'claude', dir: '/fixture/project', state: 'Idle', writable: true, thread: `thread-${index}`, approval: null, pane_name: `${long}${index}` })
const panes = Array.from({ length: 40 }, (_, at) => pane(at + 1))
const sessions: Session[] = [{ id: '$1', name: long, panes, agent: 'claude', state: 'Idle', dir: '/fixture/project', groups: [{ id: 'long-stack', name: long, project: '' }] }]
// All calls are local mocks. Even an unexpected call cannot reach a terminal or the real API.
const rejected: Api = async () => { throw new Error(error) }
const choice: ChoiceState = {
  selection: 'multi', focus: 0, filter: long, tabs: [{ label: long, active: true }, { label: `Second${long}`, active: false }],
  items: [{ id: 1, text: long, detail: long, selected: true, checked: true }, { id: 2, text: `Second${long}`, selected: false, checked: false }],
  more_above: 123, more_below: 99, hints: ['Space selects'],
}
const attention: Attention = { id: 'fixture-choice', reason: 'menu', menu_verified: 'a'.repeat(64), text: `Choose ${long}`, question: 'Choose how to continue.', choice,
  options: [{ id: 1, text: long, detail: long, selected: true }, { id: 2, text: `Second${long}`, detail: `Description${long}` }] }
const ask: AskBridge = { id: 'fixture-ask', nonce: 'fixture-nonce', expires: now + 3600, questions: [{ header: long, question: `How should we handle ${long}?`,
  options: [{ label: long, description: long }, { label: 'Use the second option', description: `Description${long}` }] }] }

function Choices() {
  return <ChoiceDock approval={null} attention={state === 'empty' ? { id: 'empty', reason: 'prompt', text: '' } : attention} agent="Claude" disabled={state === 'loading'} onChoose={done} onKey={noop} onPick={done} />
}

function Picker() {
  const picker: Attention = { id: 'fixture-picker', reason: 'picker', text: '', title: long, subtitle: `Choose ${long}`, choice,
    items: choice.items, more_above: 123, more_below: 123 }
  return <ChoiceDock approval={null} attention={picker} agent="Claude" disabled={state === 'loading'} onChoose={done} onKey={noop} onPick={done} />
}

function Waiting() {
  const [order, setOrder] = useState<WaitOrder>('oldest')
  const [selected, setSelected] = useState('%1')
  const items = state === 'empty' ? [] : Array.from({ length: 123 }, (_, at) => {
    const tab = pane(at + 1)
    const ask: Ask = { pane: tab.id, thread: tab.thread, since: now - 172800 - at, next: `${long}${at}`, kind: 'reply', opened: true, snoozed_until: null }
    return { pane: tab, ask }
  })
  const sort = { order, onOrder: setOrder }
  const open = (tab: Pane) => setSelected(tab.id)
  return <>
    <OldestButton items={items} sessions={sessions} selected={selected} compact sort={sort} onOpen={open} onDeck={noop} />
    <WaitingBox items={items} sessions={sessions} selected={selected} sort={sort} onOpen={open} onDeck={noop} />
    {items[0] && <AskChips api={rejected} pane={items[0].pane} ask={items[0].ask} onDone={noop} onUpdate={noop} />}
  </>
}

function Deck() {
  const [open, setOpen] = useState(true)
  const items = state === 'empty' ? [] : Array.from({ length: 123 }, (_, at) => {
    const tab = pane(at + 1)
    const ask: Ask = { pane: tab.id, thread: tab.thread, since: now - 7200 - at, next: `Please review ${long}.`, kind: 'reply', opened: false, snoozed_until: null }
    return { pane: tab, ask }
  })
  return open && <ActionDeck api={rejected} items={items} sessions={sessions} onOpen={noop} onClose={() => setOpen(false)} onUpdate={noop} />
}

const svg = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect width="1200" height="800" fill="#406f8f"/><circle cx="600" cy="400" r="240" fill="#e8c78a"/></svg>')}`
const images: Media[] = Array.from({ length: 123 }, (_, at) => ({ name: `${long}${at}.png`, url: `${svg}#image-${at}`, thumbnailUrl: svg }))
function Images() {
  const [image, setImage] = useState<Media | null>(state === 'error' ? { ...images[0], error } : images[61])
  return <ImageViewer image={image} items={images} onSelect={setImage} close={() => setImage(null)} onShow={noop} onDetails={noop} />
}

const previewApi: Api = async <T,>(path: string): Promise<T> => {
  if (state === 'loading') return new Promise<T>(() => {})
  if (state === 'error') throw new Error(error)
  if (path === 'preview') return { url: '/chat/fixture-frame.html' } as T
  throw new Error(`Unexpected fixture call: ${path}`)
}
function Preview() {
  const [open, setOpen] = useState(true)
  return <PreviewDrawer api={previewApi} session="%1" target={open ? { path: `/fixture/${long}.html`, title: long } : null} close={() => setOpen(false)} onSource={noop} />
}

function Notices() {
  const [visible, setVisible] = useState(true)
  return visible && <NoticeStack items={state === 'empty' ? [] : [{ id: 'fixture-notice', variant: 'warning', icon: <CircleAlert size={15} />, title: long, detail: error,
    more: <code className="notice-command">{long.repeat(5)}</code>, action: { label: 'Review', onClick: noop }, onDismiss: () => setVisible(false) }]} />
}

function Pins() {
  const [pins, setPins] = useState<SidePin[]>(state === 'empty' ? [] : panes.slice(0, 30).map(tab => ({ pane: tab.id, note: long })))
  const [selected, setSelected] = useState('%40')
  return <SidePins api={rejected} pins={pins} sessions={sessions} seen={{}} selected={selected} onOpen={tab => setSelected(tab.id)}
    onPin={id => setPins(old => [...old, { pane: id }])} onUnpin={id => setPins(old => old.filter(pin => pin.pane !== id))}
    onNote={(id, note) => setPins(old => old.map(pin => pin.pane === id ? { ...pin, note } : pin))}
    onMove={(id, to) => setPins(old => { const next = [...old], from = next.findIndex(pin => pin.pane === id), at = next.findIndex(pin => pin.pane === to); next.splice(at, 0, next.splice(from, 1)[0]); return next })} onUpdate={noop} />
}

const toastApi: Api = async <T,>(path: string, body?: unknown): Promise<T> => {
  if (state === 'loading') return new Promise<T>(() => {})
  if (state === 'error') throw new Error(error)
  if (path === 'quick-tab/route') { await new Promise(resolve => setTimeout(resolve, 3200)); return { place: { session: '$1', group: 'long-stack', sessionName: long, groupName: long } } as T }
  if (path === 'quick-tab/start') return { pane: '%1', name: long, agent: 'claude', place: { session: '$1', group: '', sessionName: long, groupName: '' } } as T
  if (path === 'projects/backlog') return body ? { text: `# Inbox\n- [ ] B123 ${long}\n`, version: '2', limit: 100000 } as T : { text: '# Inbox\n', version: '1', limit: 100000, next_id: 123 } as T
  throw new Error(`Unexpected fixture call: ${path}`)
}
function Toast() {
  const { create, toast } = useQuickCreate({ api: toastApi, agent: 'claude', sessions, onOpen: noop })
  return <><button type="button" className="secondary" data-fixture-action="toast-tab" onClick={() => create.onTab(long, { kind: state === 'suggested' ? 'auto' : 'default' })}>Start fixture tab</button>
    <button type="button" className="secondary" data-fixture-action="toast-backlog" onClick={() => create.onBacklog(long)}>Add fixture backlog item</button>{toast}</>
}

function PaneMenu() {
  const [open, setOpen] = useState(true)
  return open && <PaneActions api={rejected} session={sessions[0]} at={surface === 'pane-popover' ? { x: innerWidth - 16, y: 60 } : undefined} tabCount={panes.length} archived={false} pane={{ ...panes[0], group: 'long-stack' }} panes={panes} targets={sessions} onArchive={noop} onDone={noop} onClose={() => setOpen(false)} />
}
function SessionMenu() {
  const [open, setOpen] = useState(true)
  return open && <SessionActions api={rejected} session={{ ...sessions[0], dir: `/fixture/${long}` }} tabCount={123} pane="%1" pinned={false} archived={false} canMoveUp={false} canMoveDown={false} targets={sessions} onPin={noop} onArchive={noop} onMove={noop} onProject={noop} onDone={noop} onClose={() => setOpen(false)} />
}
const suggestApi: Api = async <T,>(): Promise<T> => ({ groups: [{ name: long, sessions: ['$1'] }, { name: `Second${long}`, sessions: ['$1'] }], source: 'llm' }) as T
function Organize() {
  const [prefs, update] = usePrefs()
  const [open, setOpen] = useState(true)
  return open && <OrganizeMenu api={suggestApi} sessions={sessions} prefs={prefs} update={update} onClose={() => setOpen(false)} />
}

export function ResponsiveChatFixture() {
  const width = Number(query.get('width')) || undefined
  let content
  switch (surface) {
    // App.tsx renders this status inline; keep the same native markup to test its CSS.
    case 'app-toast': content = <div className="toast app-toast" role="status">{error}</div>; break
    case 'pane-popover': content = <PaneMenu />; break
    case 'pane-menu': content = <PaneMenu />; break
    case 'session-menu': content = <SessionMenu />; break
    case 'organize': content = <Organize />; break
    case 'choice': content = <Choices />; break
    case 'picker': content = <Picker />; break
    case 'attention': content = <Waiting />; break
    case 'ask': content = <AskForm ask={ask} disabled={state === 'loading'} onAnswer={state === 'error' ? async () => error : answer} />; break
    case 'deck': content = <Deck />; break
    case 'image': content = <Images />; break
    case 'preview': content = <Preview />; break
    case 'notices': content = <Notices />; break
    case 'pins': content = <Pins />; break
    case 'toast': content = <Toast />; break
    default: throw new Error(`Unknown fixture surface: ${surface}`)
  }
  return <main className="chat-panel" data-fixture-surface={surface} style={{ width: width || '100%', maxWidth: '100%', height: '100dvh' }}>{surface === 'choice' || surface === 'picker' ? <div data-fixture-scroll style={{ minHeight: 0, height: '100%', overflow: 'auto' }}>{content}</div> : content}</main>
}

createRoot(document.getElementById('root')!).render(<ResponsiveChatFixture />)
