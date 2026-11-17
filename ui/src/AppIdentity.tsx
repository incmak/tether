import { useEffect, useRef, useState } from 'react'
import { errorText } from './api'
import type { Api } from './api'
import { useHost } from './host'
import { brandChanged } from './brand'

interface Color { id: string; label: string; hex: string }
type IconKind = 'color' | 'custom'
interface Identity { name: string; color: string; icon: IconKind; art: string | null; draft: string | null; job: string | null; colors: Color[] }
type Job = { state: 'running' } | { state: 'done' } | { state: 'failed'; error: string }
const NAME_LIMIT = 30
const PROMPT_LIMIT = 300
const JOB_LIMIT = 320000
const PREVIEW = '/mobile/api/app-identity/preview/'

/** The app icon as the home screen shows it, in the chosen colour. */
function AppIcon({ hex, size = 56 }: { hex: string; size?: number }) {
  return <svg viewBox="0 0 512 512" width={size} height={size} aria-hidden="true"><rect width="512" height="512" rx="112" fill={hex} /><path d="M150 150h212a40 40 0 0 1 40 40v120a40 40 0 0 1-40 40H236l-64 52v-52h-22a40 40 0 0 1-40-40V190a40 40 0 0 1 40-40Z" fill="#FBF7F1" /><path d="m180 214 40 36-40 36M248 286h84" fill="none" stroke={hex} strokeWidth="22" strokeLinecap="round" strokeLinejoin="round" /></svg>
}

/** A made icon, with its Tether badge, as the server will serve it. */
function ArtIcon({ which, stamp, size = 56, alt = '' }: { which: 'art' | 'draft'; stamp: string; size?: number; alt?: string }) {
  return <img className="app-identity-art" src={`${PREVIEW}${which}.png?v=${encodeURIComponent(stamp)}`} width={size} height={size} alt={alt} />
}

function wait(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, 2000)
    signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason) }, { once: true })
  })
}

/** Settings › Appearance › Installed app: the name and icon that this server gives its installed app. */
export function AppIdentitySettings({ api }: { api: Api }) {
  const host = useHost()
  const [saved, setSaved] = useState<Identity | null>(null)
  const [name, setName] = useState('')
  const [color, setColor] = useState('')
  const [icon, setIcon] = useState<IconKind>('color')
  const [prompt, setPrompt] = useState('')
  const [pending, setPending] = useState(true)
  const [making, setMaking] = useState<'' | 'ai' | 'upload'>('')
  const [error, setError] = useState('')
  const [artError, setArtError] = useState('')
  const [notice, setNotice] = useState('')
  const picker = useRef<HTMLInputElement>(null)
  const polling = useRef<AbortController | null>(null)
  // Aborts when Settings closes, so no request or poll starts after that.
  const life = useRef(new AbortController())

  function take(data: Identity, keepChoices = false) {
    setSaved(data)
    if (!keepChoices) { setName(data.name); setColor(data.color); setIcon(data.icon) }
  }

  async function follow(job: string) {
    polling.current?.abort()
    if (life.current.signal.aborted) return
    const controller = new AbortController()
    life.current.signal.addEventListener('abort', () => controller.abort(), { once: true })
    polling.current = controller
    const stop = AbortSignal.any([controller.signal, AbortSignal.timeout(JOB_LIMIT)])
    setMaking('ai'); setArtError('')
    try {
      for (;;) {
        await wait(stop)
        const result = await api<Job>(`app-identity/generate?job=${encodeURIComponent(job)}`, undefined, stop)
        if (result.state === 'failed') throw new Error(result.error)
        if (result.state === 'done') break
      }
      take(await api<Identity>('app-identity', undefined, stop), true)
    } catch (failure) {
      if (controller.signal.aborted) return
      setArtError(stop.aborted ? 'The icon took too long. Please try again.' : errorText(failure))
    } finally { if (polling.current === controller) { polling.current = null; setMaking('') } }
  }

  useEffect(() => {
    if (life.current.signal.aborted) life.current = new AbortController()  // React may mount twice in development
    const lifetime = life.current
    const controller = new AbortController()
    void api<Identity>('app-identity', undefined, controller.signal).then(data => {
      if (controller.signal.aborted) return
      take(data)
      if (data.job) void follow(data.job)
    }, failure => { if (!controller.signal.aborted) setError(errorText(failure)) }).finally(() => { if (!controller.signal.aborted) setPending(false) })
    return () => { controller.abort(); lifetime.abort() }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per api; follow() reads only stable values
  }, [api])

  const trimmed = name.split(/\s+/).filter(Boolean).join(' ')
  const changed = !!saved && (trimmed !== saved.name || color !== saved.color || icon !== saved.icon)
  const hex = saved?.colors.find(item => item.id === color)?.hex ?? '#C96442'
  const showArt = icon === 'custom' && saved?.art

  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (pending || !changed) return
    if (!trimmed) { setError('Type a name.'); return }
    if (icon === 'custom' && !saved?.art) { setError('Make or upload a picture first, then choose Use this icon.'); return }
    setPending(true); setError(''); setNotice('')
    try {
      const data = await api<Identity>('app-identity', { name: trimmed, color, icon })
      take(data)
      brandChanged(data.name)
      setNotice('Saved. New notifications show this icon now. A new install uses this name and icon. An app that is already installed changes within about a day, after you close it. To change it now, remove the app and install it again.')
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }

  async function generate() {
    const text = prompt.trim()
    if (!text) { setArtError('Describe the picture first.'); return }
    setMaking('ai'); setArtError(''); setNotice('')
    try {
      const { job } = await api<{ job: string }>('app-identity/generate', { prompt: text }, life.current.signal)
      await follow(job)
    } catch (failure) { if (!life.current.signal.aborted) { setArtError(errorText(failure)); setMaking('') } }
  }

  async function upload(file: File | undefined) {
    if (!file) return
    if (file.size > 12 * 1024 * 1024) { setArtError('Choose an image of 12 MB or less.'); return }
    setMaking('upload'); setArtError(''); setNotice('')
    try { take(await api<Identity>('app-identity/upload', file, life.current.signal, undefined, 60000), true) }
    catch (failure) { if (!life.current.signal.aborted) setArtError(errorText(failure)) } finally { if (!life.current.signal.aborted) setMaking('') }
  }

  async function decide(useDraft: boolean) {
    const draft = saved?.draft
    if (!draft) return
    setMaking('upload'); setArtError(''); setNotice('')
    try {
      // The draft revision this page shows: the server refuses with 409 if another device replaced it.
      const data = await api<Identity>('app-identity', useDraft ? { useDraft: true, draft } : { discardDraft: true, draft })
      setSaved(data)
      if (useDraft) {
        brandChanged(data.name)
        setIcon('custom')
        setNotice('Your picture is now the app icon. New notifications show it now. A new install uses it. An installed app changes within about a day, after you close it.')
      }
    } catch (failure) {
      setArtError(errorText(failure))
      // Show the draft that is there now.
      void api<Identity>('app-identity').then(data => take(data, true), () => {})
    } finally { setMaking('') }
  }

  return <section className="settings-group" aria-labelledby="settings-app-identity"><h3 id="settings-app-identity">Installed app</h3>
    <form className="app-identity" onSubmit={save}>
      <p id="app-identity-help" className="setting-note">Give each Tether server its own name and icon, so you can tell the installed apps and their notifications apart.</p>
      <div className="app-identity-preview" aria-hidden="true">{showArt ? <ArtIcon which="art" stamp={saved.art ?? ''} /> : <AppIcon hex={hex} />}<span>{trimmed || 'Tether'}</span></div>
      <label htmlFor="app-identity-name">App name</label>
      <input id="app-identity-name" value={name} maxLength={NAME_LIMIT} placeholder={host === 'the server' ? 'Tether' : host} autoComplete="off" spellCheck={false} aria-describedby="app-identity-help app-identity-length" disabled={pending && !saved} onChange={event => { setName(event.target.value); setNotice(''); setError('') }} />
      <p id="app-identity-length" className="setting-note">The home screen shows about 12 characters.</p>
      {saved && <fieldset className="choice app-identity-kind"><legend>Icon</legend>
        <label><input type="radio" name="app-identity-icon" value="color" checked={icon === 'color'} onChange={() => { setIcon('color'); setNotice(''); setError('') }} /> Tether icon in a colour</label>
        <label><input type="radio" name="app-identity-icon" value="custom" checked={icon === 'custom'} onChange={() => { setIcon('custom'); setNotice(''); setError('') }} /> My own picture, with a Tether badge</label>
      </fieldset>}
      {saved && icon === 'color' && <fieldset className="choice"><legend>Icon colour</legend><div className="preferences-colors">{saved.colors.map(item => <label key={item.id}><input type="radio" name="app-identity-color" value={item.id} checked={color === item.id} onChange={() => { setColor(item.id); setNotice('') }} /><span className="preferences-color" style={{ background: item.hex, borderColor: item.hex }} aria-hidden="true" /><span>{item.label}</span></label>)}</div></fieldset>}
      {saved && icon === 'custom' && <div className="app-identity-own" aria-busy={!!making}>
        <p id="app-identity-badge" className="setting-note">Each picture keeps a small Tether badge in the lower-right corner, so you can see that the app is Tether.</p>
        <label htmlFor="app-identity-prompt">Describe a picture for AI</label>
        <textarea id="app-identity-prompt" rows={2} value={prompt} maxLength={PROMPT_LIMIT} placeholder="For example: a lighthouse at night" aria-describedby="app-identity-badge" disabled={!!making} onChange={event => { setPrompt(event.target.value); setArtError('') }} />
        <div className="app-identity-actions">
          <button type="button" className="secondary" disabled={!!making || !prompt.trim()} onClick={() => void generate()}>{making === 'ai' ? 'Making the picture…' : 'Make with AI'}</button>
          <button type="button" className="secondary" disabled={!!making} onClick={() => picker.current?.click()}>{making === 'upload' ? 'Working…' : 'Upload a picture'}</button>
          <input ref={picker} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; void upload(file) }} />
        </div>
        {making === 'ai' && <p className="setting-note" role="status">Codex makes the picture. This takes about 1 minute. You can leave this page; the picture waits here for you.</p>}
        {artError && <p className="error" role="alert">{artError}</p>}
        {saved.draft && <div className="app-identity-draft">
          <ArtIcon which="draft" stamp={saved.draft} size={96} alt="New icon with the Tether badge" />
          <div className="app-identity-actions">
            <button type="button" className="primary" disabled={!!making} onClick={() => void decide(true)}>Use this icon</button>
            <button type="button" className="secondary" disabled={!!making} onClick={() => void decide(false)}>Discard</button>
          </div>
        </div>}
        {!saved.draft && !saved.art && !making && <p className="setting-note">No picture yet. Describe one for AI, or upload a PNG, JPEG or WebP.</p>}
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="setting-note" role="status">{notice}</p>}
      <button className="secondary" disabled={pending || !changed}>{pending && saved ? 'Saving…' : 'Save app name and icon'}</button>
    </form>
  </section>
}
