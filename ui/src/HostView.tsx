import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { ArrowLeft, ExternalLink } from 'lucide-react'
import type { Host } from './prefs'
import { useHost } from './host'
import { useBackStep } from './backNav'
import './HostView.css'

/* Another Tether, shown inside this app instead of in a browser tab.
   An installed app (PWA) opens a link outside its own address in a browser tab. A frame keeps the other server
   inside this app. Its sign-in cookie reaches the frame when both servers are on one site, for example two Tailscale
   Serve names in one tailnet (Tailscale's domain is on the Public Suffix List, so each tailnet is its own site).
   For any other server, "Open in the browser" stays one tap away.

   Messages between the two pages (window.postMessage). The parent takes them only from its own frame's window and
   from that host's origin:
     framed -> parent  {type: 'tether-host-ready'}               the framed Tether started
     framed -> parent  {type: 'tether-host-open', url: origin}  a tap in its host menu: the parent opens that host
                                                                 if it is in the parent's own list, or comes home
                                                                 when url is the parent's origin */

const READY = 'tether-host-ready'
const OPEN = 'tether-host-open'
const SLOW_MS = 10000

type Shown = { host: Host; id: number }
let current: Shown | null = null
let opened = 0
let known: Host[] = []
const listeners = new Set<() => void>()
const emit = () => listeners.forEach(listener => listener())

/** True when this page runs inside another Tether's frame. */
export const framed = typeof window !== 'undefined' && window.parent !== window

/** Open a host from the list inside this app. Inside a frame, ask the parent Tether to switch instead. */
export function openHost(host: Host) {
  if (framed) { window.parent.postMessage({ type: OPEN, url: host.url }, '*'); return }
  if (current?.host.url === host.url) return
  current = { host, id: ++opened }
  emit()
}

/** Come back to this Tether. Focus goes back to the host menu's button. */
function closeHost() {
  if (!current) return
  current = null
  emit()
  requestAnimationFrame(() => [...document.querySelectorAll<HTMLElement>('.host-switch')].find(button => button.getClientRects().length)?.focus())
}

/** The hosts this page may open when a framed Tether asks. HostMenu keeps it up to date. */
export function knowHosts(hosts: Host[]) { known = hosts }

function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
const snapshot = () => current

/** The view itself. It renders nothing until a host is opened. */
export function HostView() {
  const home = useHost()
  const shown = useSyncExternalStore(subscribe, snapshot)
  const [ready, setReady] = useState(0)  // the id of the frame that said it started
  const [slow, setSlow] = useState(0)    // the id of the frame that took more than SLOW_MS
  const back = useRef<HTMLButtonElement>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const frame = useRef<HTMLIFrameElement>(null)
  // One history step: the phone's Back button comes home. The framed Tether adds its own entries after it, so Back
  // first steps back inside that Tether, as in any app. backNav steps over those entries when the view closes.
  useBackStep(!!shown, closeHost, { frame: true })

  useEffect(() => {
    // Inside another Tether: say so once this app has started.
    if (framed) window.parent.postMessage({ type: READY }, '*')
    const message = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; url?: unknown } | null
      const at = current
      if (!data || typeof data !== 'object' || !at || event.origin !== at.host.url || event.source !== frame.current?.contentWindow) return
      if (data.type === READY) setReady(at.id)
      if (data.type === OPEN && typeof data.url === 'string') {
        if (data.url === location.origin) closeHost()
        else { const next = known.find(item => item.url === data.url); if (next) openHost(next) }
      }
    }
    window.addEventListener('message', message)
    return () => window.removeEventListener('message', message)
  }, [])

  // A modal dialog: it sits above every other layer, the phone drawer too, and the app behind it takes no focus or taps.
  useEffect(() => {
    const box = dialog.current
    if (!shown || !box) return
    if (!box.open) box.showModal()
    back.current?.focus()
    const timer = setTimeout(() => setSlow(shown.id), SLOW_MS)
    return () => clearTimeout(timer)
  }, [shown])

  if (!shown) return null
  const { host, id } = shown
  const page = host.url + '/chat/'
  return createPortal(<dialog ref={dialog} className="host-view" aria-label={host.name} onCancel={event => { event.preventDefault(); closeHost() }}>
    <header className="host-view-bar">
      <button ref={back} type="button" className="host-view-back" aria-label={`Back to ${home}`} onClick={closeHost}><ArrowLeft size={18} aria-hidden="true" /><span>{home}</span></button>
      <span className="host-view-name" title={new URL(host.url).host}>{host.name}</span>
      <a className="icon-button host-view-out" href={page} target="_blank" rel="noopener noreferrer" aria-label={`Open ${host.name} in the browser`} title="Open in the browser"><ExternalLink size={17} aria-hidden="true" /></a>
    </header>
    {slow === id && ready !== id && <p className="host-view-note" role="status">
      If {host.name} does not show, open it in the browser. A server on another site, or an older Tether, may need its own sign-in there.
      <a href={page} target="_blank" rel="noopener noreferrer">Open {host.name}</a>
    </p>}
    <iframe ref={frame} key={id} className="host-view-frame" src={page} title={host.name}
      allow="microphone; camera; clipboard-read; clipboard-write; fullscreen; screen-wake-lock; autoplay"
      referrerPolicy="strict-origin" />
  </dialog>, document.body)
}
