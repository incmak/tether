import { useEffect, useState } from 'react'
import { errorText } from './api'
import type { Api } from './api'
import { useHost } from './host'

interface Color { id: string; label: string; hex: string }
interface Identity { name: string; color: string; colors: Color[] }
const NAME_LIMIT = 30

/** The app icon as the home screen shows it, in the chosen colour. */
function AppIcon({ hex, size = 56 }: { hex: string; size?: number }) {
  return <svg viewBox="0 0 512 512" width={size} height={size} aria-hidden="true"><rect width="512" height="512" rx="112" fill={hex} /><path d="M150 150h212a40 40 0 0 1 40 40v120a40 40 0 0 1-40 40H236l-64 52v-52h-22a40 40 0 0 1-40-40V190a40 40 0 0 1 40-40Z" fill="#FBF7F1" /><path d="m180 214 40 36-40 36M248 286h84" fill="none" stroke={hex} strokeWidth="22" strokeLinecap="round" strokeLinejoin="round" /></svg>
}

/** Settings › Appearance › Installed app: the name and icon colour that this server gives its installed app. */
export function AppIdentitySettings({ api }: { api: Api }) {
  const host = useHost()
  const [saved, setSaved] = useState<Identity | null>(null)
  const [name, setName] = useState('')
  const [color, setColor] = useState('')
  const [pending, setPending] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api<Identity>('app-identity', undefined, controller.signal).then(data => {
      if (controller.signal.aborted) return
      setSaved(data); setName(data.name); setColor(data.color)
    }, failure => { if (!controller.signal.aborted) setError(errorText(failure)) }).finally(() => { if (!controller.signal.aborted) setPending(false) })
    return () => controller.abort()
  }, [api])
  const trimmed = name.split(/\s+/).filter(Boolean).join(' ')
  const changed = !!saved && (trimmed !== saved.name || color !== saved.color)
  const hex = saved?.colors.find(item => item.id === color)?.hex ?? '#C96442'
  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (pending || !changed) return
    if (!trimmed) { setError('Type a name.'); return }
    setPending(true); setError(''); setNotice('')
    try {
      const data = await api<Identity>('app-identity', { name: trimmed, color })
      setSaved(data); setName(data.name); setColor(data.color)
      document.title = data.name
      setNotice('Saved. A new install uses this name and icon. An app that is already installed changes within about a day, after you close it. To change it now, remove the app and install it again.')
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  return <section className="settings-group" aria-labelledby="settings-app-identity"><h3 id="settings-app-identity">Installed app</h3>
    <form className="app-identity" onSubmit={save}>
      <p id="app-identity-help" className="setting-note">Give each Tether server its own name and icon colour, so you can tell the installed apps apart on your home screen.</p>
      <div className="app-identity-preview" aria-hidden="true"><AppIcon hex={hex} /><span>{trimmed || 'Tether'}</span></div>
      <label htmlFor="app-identity-name">App name</label>
      <input id="app-identity-name" value={name} maxLength={NAME_LIMIT} placeholder={host === 'the server' ? 'Tether' : host} autoComplete="off" spellCheck={false} aria-describedby="app-identity-help app-identity-length" disabled={pending && !saved} onChange={event => { setName(event.target.value); setNotice(''); setError('') }} />
      <p id="app-identity-length" className="setting-note">The home screen shows about 12 characters.</p>
      {saved && <fieldset className="choice"><legend>Icon colour</legend><div className="preferences-colors">{saved.colors.map(item => <label key={item.id}><input type="radio" name="app-identity-color" value={item.id} checked={color === item.id} onChange={() => { setColor(item.id); setNotice('') }} /><span className="preferences-color" style={{ background: item.hex, borderColor: item.hex }} aria-hidden="true" /><span>{item.label}</span></label>)}</div></fieldset>}
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="setting-note" role="status">{notice}</p>}
      <button className="secondary" disabled={pending || !changed}>{pending && saved ? 'Saving…' : 'Save app name and icon'}</button>
    </form>
  </section>
}
