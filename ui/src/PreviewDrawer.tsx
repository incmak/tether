import { useEffect, useState } from 'react'
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

type Link = { key: string; url?: string; error?: string }

/** A page an agent made, in a drawer: a Claude artifact's local copy or an HTML file. Phone: a sheet that leaves the top
 * of the chat in view, with a full-screen switch. Wider screens: a full-screen modal with a phone-width switch.
 * onSource: show the page's HTML as text, read from the preview link (it also works for scratchpad pages). */
export function PreviewDrawer({ api, session, target, close, onSource }: { api: Api; session: string; target: PreviewTarget | null; close: () => void; onSource?: (path: string, url: string) => void }) {
  const modal = useModal(!!target, close)
  const key = target ? `${session}\n${target.path}` : ''
  const [link, setLink] = useState<Link | null>(null)
  const [round, setRound] = useState(0)
  const [loaded, setLoaded] = useState('')
  const [full, setFull] = useState(false)
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    if (!target) return
    const abort = new AbortController()
    api<{ url: string }>('preview', { session, path: target.path }, abort.signal)
      .then(result => setLink({ key, url: result.url }), failure => { if (!abort.signal.aborted) setLink({ key, error: errorText(failure) }) })
    return () => abort.abort()
  }, [api, session, target, key, round])
  const current = link?.key === key ? link : null
  const frame = current?.url ? `${current.url}#${round}` : ''
  const remote = target?.remote
  const outside = remote ?? (current?.url ? new URL(current.url, location.href).href : '')
  const name = target?.path.split('/').pop() || target?.path || ''
  const reload = () => { setLoaded(''); setRound(value => value + 1) }
  return <dialog {...modal} className={['sheet', 'preview-drawer', full && 'full'].filter(Boolean).join(' ')} aria-labelledby="preview-title">{target && <>
    <div className="preview-grip" aria-hidden="true" />
    <div className="preview-head">
      <h2 id="preview-title" className="preview-name">{target.title}</h2>
      <p className="preview-sub" title={target.path}>{remote ? 'Claude artifact · local copy' : <bdi>{name}</bdi>}</p>
      <div className="preview-actions">
        <button type="button" className="icon-button preview-wide-only" aria-pressed={narrow} aria-label="Phone width" title="Phone width" onClick={() => setNarrow(!narrow)}>{narrow ? <Monitor size={18} /> : <Smartphone size={18} />}</button>
        {onSource && <button type="button" className="icon-button" aria-label="View source" title="View source" disabled={!current?.url} onClick={() => current?.url && onSource(target.path, current.url)}><Code2 size={18} /></button>}
        <button type="button" className="icon-button" aria-label="Reload" title="Reload" disabled={!current?.url} onClick={reload}><RotateCw size={18} /></button>
        <button type="button" className="icon-button preview-phone-only" aria-pressed={full} aria-label="Full screen" title={full ? 'Leave full screen' : 'Full screen'} onClick={() => setFull(!full)}>{full ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button>
        {outside && <a className="icon-button" href={outside} target="_blank" rel="noreferrer" aria-label={remote ? 'Open on claude.ai' : 'Open in browser'} title={remote ? 'Open on claude.ai' : 'Open in browser'}><ExternalLink size={18} /></a>}
      </div>
      <button type="button" className="icon-button preview-close" aria-label="Close" onClick={close}><X size={20} /></button>
    </div>
    <div className={narrow ? 'preview-body narrow' : 'preview-body'}>
      {current?.error ? <div className="preview-error" role="alert">
        <p>{current.error}</p>
        {remote && <p>The published page is still on claude.ai.</p>}
        <div className="preview-error-actions">
          <RetryButton label="Try again" onRetry={() => { setLink(null); setRound(value => value + 1) }} />
          {remote && <a className="secondary" href={remote} target="_blank" rel="noreferrer"><ExternalLink size={16} aria-hidden="true" />Open on claude.ai</a>}
        </div>
      </div> : <>
        {frame && <iframe key={frame} src={frame} title={target.title} sandbox={SANDBOX} referrerPolicy="no-referrer" onLoad={() => setLoaded(frame)} />}
        {(!frame || loaded !== frame) && <div className="preview-loading" role="status"><Spinner />Loading the page…</div>}
      </>}
    </div>
  </>}</dialog>
}
