import { useEffect, useRef, useState } from 'react'
import { Code2, ExternalLink, Maximize2, Minimize2, Monitor, RotateCw, Smartphone, X } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { RetryButton } from './Retry'
import { Spinner } from './Skeleton'
import type { PreviewTarget } from './preview'
import './PreviewDrawer.css'

// The page runs scripts in an opaque origin: it cannot read Tether's cookies, storage or API (server/previews.py).
const SANDBOX = 'allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads'
// A dev server needs its origin: without allow-same-origin the frame's origin is opaque, so the browser sends no
// preview cookie (every request gets 401), and the app has no storage and no hot reload. That origin is the dev
// server's own (another port), never Tether's, so the page still cannot read Tether's pages, storage or API, and it
// cannot reach this frame element to lift the sandbox. Tether's cookies are HttpOnly (server/dev_previews.py).
const DEV_SANDBOX = `${SANDBOX} allow-same-origin`
// A frame that reports "ended" this soon after it got a fresh link did not keep the preview cookie.
const COOKIE_CHECK_MS = 8000
const HINT_DELAY_MS = 2500

type Link = { key: string; url?: string; outside?: string; pool?: number; at?: number; error?: string; servers?: DevServerRow[] }
type DevServerRow = { port: number; name: string; folder: string }
type DevState = { servers: DevServerRow[]; previews: { port: number; hint: string | null }[] }

/** A page an agent made, in a drawer: a Claude artifact's local copy, an HTML file, or a dev server on the agent's
 * machine. Phone: a sheet that leaves the top of the chat in view, with a full-screen switch. Wider screens: a
 * full-screen modal with a phone-width switch.
 * onSource: show the page's HTML as text, read from the preview link (it also works for scratchpad pages). */
export function PreviewDrawer({ api, session, target, close, onSource }: { api: Api; session: string; target: PreviewTarget | null; close: () => void; onSource?: (path: string, url: string) => void }) {
  const modal = useModal(!!target, close)
  // The person may pick another dev server from the list in the error state. The choice ends with the target.
  const [picked, setPicked] = useState<{ target: PreviewTarget; port: number } | null>(null)
  const dev = !target?.dev ? null : picked?.target === target ? { port: picked.port, path: '/' } : target.dev
  const devPort = dev?.port, devPath = dev?.path
  const key = target ? `${session}\n${target.path}\n${devPort ?? ''}` : ''
  const [link, setLink] = useState<Link | null>(null)
  const [round, setRound] = useState(0)
  const [loaded, setLoaded] = useState('')
  const [full, setFull] = useState(false)
  const [narrow, setNarrow] = useState(false)
  const [hint, setHint] = useState<{ key: string; kind: string } | null>(null)
  const frameRef = useRef<HTMLIFrameElement>(null)
  useEffect(() => {
    if (!target) return
    const abort = new AbortController()
    const fail = (failure: unknown, servers?: DevServerRow[]) => { if (!abort.signal.aborted) setLink({ key, error: errorText(failure), servers }) }
    if (devPort === undefined) {
      api<{ url: string }>('preview', { session, path: target.path }, abort.signal).then(result => setLink({ key, url: result.url }), fail)
    } else {
      // Each preview port is its own origin on this host (server/dev_previews.py). Both links hold a one-time token.
      const origin = `${location.protocol}//${location.hostname}:`
      api<{ port: number; frame: string; browser: string }>('dev-preview', { port: devPort, path: devPath }, abort.signal)
        .then(result => setLink({ key, url: `${origin}${result.port}${result.frame}`, outside: `${origin}${result.port}${result.browser}`, pool: result.port, at: Date.now() }),
          // The agent's link may name a port that changed (Vite takes 5174 when 5173 is busy): offer what runs now.
          failure => api<DevState>('dev-preview', undefined, abort.signal).then(state => fail(failure, state.servers.filter(row => row.port !== devPort)), () => fail(failure)))
    }
    return () => abort.abort()
  }, [api, session, target, key, round, devPort, devPath])
  const current = link?.key === key ? link : null
  const frame = !current?.url ? '' : dev ? current.url : `${current.url}#${round}`
  const pool = current?.pool, issued = current?.at ?? 0
  useEffect(() => {
    if (!pool || !frame) return
    // The preview port answers 401 with a page that posts this message when the link or its cookie is no longer good.
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frameRef.current?.contentWindow || (event.data as { tetherPreview?: string } | null)?.tetherPreview !== 'expired') return
      setLink(old => old?.key !== key || old.error ? old : { ...old, url: undefined, error: Date.now() - issued < COOKIE_CHECK_MS
        ? 'This browser did not keep the preview’s cookie, so the page cannot load here. Use Open in browser.'
        : 'This preview has ended. Open it again.' })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [pool, frame, key, issued])
  const shown = !!frame && loaded === frame
  useEffect(() => {
    if (!pool || !shown) return
    // Next.js may refuse dev requests from an origin it does not know. The proxy sees that; ask once the page has run.
    const abort = new AbortController()
    const timer = window.setTimeout(() => {
      api<DevState>('dev-preview', undefined, abort.signal).then(state => {
        const kind = state.previews.find(row => row.port === pool)?.hint
        if (kind) setHint({ key, kind })
      }, () => { /* the hint is optional */ })
    }, HINT_DELAY_MS)
    return () => { window.clearTimeout(timer); abort.abort() }
  }, [api, pool, shown, key])
  const remote = target?.remote
  const outside = remote ?? (current?.outside || (current?.url ? new URL(current.url, location.href).href : ''))
  const name = target?.path.split('/').pop() || target?.path || ''
  const reload = () => { setLoaded(''); setRound(value => value + 1) }
  const retry = () => { setLink(null); setRound(value => value + 1) }
  const pick = (port: number) => { if (target) { setLink(null); setLoaded(''); setPicked({ target, port }) } }
  return <dialog {...modal} className={['sheet', 'preview-drawer', full && 'full'].filter(Boolean).join(' ')} aria-labelledby="preview-title">{target && <>
    <div className="preview-grip" aria-hidden="true" />
    <div className="preview-head">
      <h2 id="preview-title" className="preview-name">{dev ? `localhost:${dev.port}` : target.title}</h2>
      <p className="preview-sub" title={target.path}>{remote ? 'Claude artifact · local copy' : dev ? 'Dev server · live' : <bdi>{name}</bdi>}</p>
      <div className="preview-actions">
        <button type="button" className="icon-button preview-wide-only" aria-pressed={narrow} aria-label="Phone width" title="Phone width" onClick={() => setNarrow(!narrow)}>{narrow ? <Monitor size={18} /> : <Smartphone size={18} />}</button>
        {onSource && !dev && <button type="button" className="icon-button" aria-label="View source" title="View source" disabled={!current?.url} onClick={() => current?.url && onSource(target.path, current.url)}><Code2 size={18} /></button>}
        <button type="button" className="icon-button" aria-label="Reload" title="Reload" disabled={!current?.url} onClick={reload}><RotateCw size={18} /></button>
        <button type="button" className="icon-button preview-phone-only" aria-pressed={full} aria-label="Full screen" title={full ? 'Leave full screen' : 'Full screen'} onClick={() => setFull(!full)}>{full ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button>
        {outside && <a className="icon-button" href={outside} target="_blank" rel="noreferrer" aria-label={remote ? 'Open on claude.ai' : 'Open in browser'} title={remote ? 'Open on claude.ai' : 'Open in browser'}><ExternalLink size={18} /></a>}
      </div>
      <button type="button" className="icon-button preview-close" aria-label="Close" onClick={close}><X size={20} /></button>
    </div>
    {hint?.key === key && hint.kind === 'next' && !current?.error && <p className="preview-hint" role="status">Next.js refused a dev request. Add <code>allowedDevOrigins: ['{location.hostname}']</code> to next.config.js, then restart the dev server.</p>}
    <div className={narrow ? 'preview-body narrow' : 'preview-body'}>
      {current?.error ? <div className="preview-error" role="alert">
        <p>{current.error}</p>
        {remote && <p>The published page is still on claude.ai.</p>}
        <div className="preview-error-actions">
          <RetryButton label={dev && current.outside ? 'Open again' : 'Try again'} onRetry={retry} />
          {remote && <a className="secondary" href={remote} target="_blank" rel="noreferrer"><ExternalLink size={16} aria-hidden="true" />Open on claude.ai</a>}
          {dev && current.outside && <a className="secondary" href={current.outside} target="_blank" rel="noreferrer"><ExternalLink size={16} aria-hidden="true" />Open in browser</a>}
        </div>
        {!!current.servers?.length && <div className="preview-servers">
          <h3 id="preview-servers-title">Dev servers that run now</h3>
          <ul aria-labelledby="preview-servers-title">{current.servers.map(row => <li key={row.port}>
            <button type="button" className="secondary" onClick={() => pick(row.port)}>
              <span>localhost:{row.port}</span>
              {(row.name || row.folder) && <small>{[row.name, row.folder].filter(Boolean).join(' · ')}</small>}
            </button>
          </li>)}</ul>
        </div>}
      </div> : <>
        {frame && <iframe ref={frameRef} key={frame} src={frame} title={dev ? `localhost:${dev.port}` : target.title} sandbox={dev ? DEV_SANDBOX : SANDBOX} referrerPolicy="no-referrer" onLoad={() => setLoaded(frame)} />}
        {(!frame || loaded !== frame) && <div className="preview-loading" role="status"><Spinner />{dev ? 'Connecting to the dev server…' : 'Loading the page…'}</div>}
      </>}
    </div>
  </>}</dialog>
}
