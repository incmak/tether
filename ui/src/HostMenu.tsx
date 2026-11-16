import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronsUpDown, Plus, X } from 'lucide-react'
import type { Host } from './prefs'
import { knowHosts, openHost } from './HostView'
import './HostMenu.css'

const MAX_HOSTS = 20  // Match the 'hosts' checks in server/prefs_sync.py.
type Reach = 'checking' | 'on' | 'off'

/** The origin of a typed address, or an error. "mac.example.com/chat/" gives "https://mac.example.com". */
export function hostOrigin(text: string): { origin: string } | { error: string } {
  const typed = text.trim()
  if (!typed) return { error: 'Type the address of the other Tether.' }
  let url: URL
  try { url = new URL(/^[a-z][a-z\d+.-]*:\/\//i.test(typed) ? typed : 'https://' + typed) } catch { return { error: 'That address is not valid.' } }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return { error: 'Use an https:// or http:// address.' }
  if (url.username || url.password || !url.hostname) return { error: 'That address is not valid.' }
  if (url.origin.length > 200) return { error: 'That address is too long.' }
  return { origin: url.origin }
}

/** The name to show: what was typed, else the first part of the host name. */
export function hostName(text: string, origin: string) {
  const hostname = new URL(origin).hostname
  // An IP address stays whole; a name like mac.example.com gives "mac".
  const printable = [...text].filter(char => char.charCodeAt(0) >= 32 && char.charCodeAt(0) !== 127).join('')
  const name = printable.trim() || (/^[\d.]+$|:/.test(hostname) ? hostname : hostname.split('.')[0])
  return name.slice(0, 40)
}

/** Does the other Tether answer? An opaque no-cors reply still proves the server is up. */
async function reach(origin: string): Promise<Reach> {
  try {
    await fetch(origin + '/chat/icon.svg', { mode: 'no-cors', cache: 'no-store', signal: AbortSignal.timeout(4000) })
    return 'on'
  } catch { return 'off' }
}

const REACH_LABEL: Record<Reach, string> = { checking: 'Checking', on: 'Online', off: 'Not reachable' }

/** The footer's host name. A tap lists the other Tether servers; each opens inside this app (see HostView). */
export function HostMenu({ host, hosts, onChange }: { host: string; hosts: Host[]; onChange: (hosts: Host[]) => void }) {
  const [open, setOpen] = useState(false)
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [address, setAddress] = useState('')
  const [error, setError] = useState('')
  const [status, setStatus] = useState<Record<string, Reach>>({})
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const menuId = useId()
  // The menu opens in a portal: a layout can clip the sidebar (rounded or glass panels), which would cut the menu.
  const [at, setAt] = useState<{ left: number; bottom: number; parent: HTMLElement } | null>(null)
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const box = trigger.current?.getBoundingClientRect()
      if (box) setAt({ left: Math.max(8, Math.min(box.left - 2, window.innerWidth - 308)), bottom: window.innerHeight - box.top + 6, parent: trigger.current?.closest('dialog') ?? document.body })
    }
    place()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open])

  useEffect(() => { knowHosts(hosts) }, [hosts])

  const close = (focus = false) => { setOpen(false); setAdding(false); setError(''); if (focus) trigger.current?.focus() }

  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) close() }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true) } }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape, true)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape, true) }
  }, [open])

  // Check every host each time the menu opens.
  const urls = hosts.map(item => item.url).join(' ')
  const [previousCheck, setPreviousCheck] = useState({ open, urls })
  if (open !== previousCheck.open || urls !== previousCheck.urls) {
    setPreviousCheck({ open, urls })
    if (open && urls) setStatus(Object.fromEntries(urls.split(' ').map(url => [url, 'checking' as Reach])))
  }
  useEffect(() => {
    if (!open || !urls) return
    let live = true
    const list = urls.split(' ')
    for (const url of list) void reach(url).then(result => { if (live) setStatus(current => ({ ...current, [url]: result })) })
    return () => { live = false }
  }, [open, urls])

  const add = (event: React.FormEvent) => {
    event.preventDefault()
    const parsed = hostOrigin(address)
    if ('error' in parsed) { setError(parsed.error); return }
    if (parsed.origin === location.origin) { setError('That is this Tether.'); return }
    if (hosts.some(item => item.url === parsed.origin)) { setError('That host is already in the list.'); return }
    if (hosts.length >= MAX_HOSTS) { setError(`The list holds ${MAX_HOSTS} hosts. Remove one first.`); return }
    onChange([...hosts, { name: hostName(name, parsed.origin), url: parsed.origin }])
    setName(''); setAddress(''); setError(''); setAdding(false)
  }

  return <div className="host-menu-root" ref={root}>
    <button ref={trigger} type="button" className="host-switch" aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={() => open ? close() : setOpen(true)}>
      <span className="state-dot" aria-hidden="true" /><span className="host-switch-name">{host}</span><ChevronsUpDown size={14} aria-hidden="true" />
    </button>
    {open && at && createPortal(<div ref={menu} className="host-menu" id={menuId} role="group" aria-label="Tether hosts" style={{ left: at.left, bottom: at.bottom }}>
      <div className="host-row current"><span className="state-dot done" aria-hidden="true" /><span className="host-text"><span className="host-name">{host}</span><span className="host-address">This Tether</span></span><Check size={16} aria-label="Open now" /></div>
      {hosts.map(item => <div className="host-row" key={item.url}>
        <a className="host-open" href={item.url + '/chat/'} target="_blank" rel="noopener noreferrer" onClick={event => {
          // A plain tap opens the host inside this app. Ctrl, Shift or a middle click still opens a browser tab.
          if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); openHost(item) }
          close()
        }}>
          <span className={`state-dot ${status[item.url] === 'on' ? 'done' : status[item.url] === 'off' ? 'idle' : ''}`} aria-hidden="true" />
          <span className="host-text"><span className="host-name">{item.name}</span><span className="host-address">{REACH_LABEL[status[item.url] ?? 'checking']} · {new URL(item.url).host}</span></span>
        </a>
        <button type="button" className="icon-button host-remove" aria-label={`Remove ${item.name}`} title="Remove from the list" onClick={() => onChange(hosts.filter(other => other.url !== item.url))}><X size={15} /></button>
      </div>)}
      {!hosts.length && !adding && <p className="host-note">Add your other Tether servers. Each one opens inside this app.</p>}
      {adding
        ? <form className="host-form" onSubmit={add} noValidate>
          <label><span>Address</span><input type="url" inputMode="url" autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder="https://mac.example.com" value={address} onChange={event => { setAddress(event.target.value); setError('') }} autoFocus /></label>
          <label><span>Name <span className="host-optional">(optional)</span></span><input type="text" maxLength={40} placeholder="MacBook Pro" value={name} onChange={event => setName(event.target.value)} /></label>
          {error && <p className="host-error" role="alert">{error}</p>}
          <div className="host-form-actions"><button type="button" className="ghost" onClick={() => { setAdding(false); setError('') }}>Cancel</button><button type="submit" className="primary">Add host</button></div>
        </form>
        : <button type="button" className="host-add" onClick={() => setAdding(true)}><Plus size={16} aria-hidden="true" />Add a host</button>}
    </div>, at.parent)}
  </div>
}
