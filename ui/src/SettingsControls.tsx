import { Bone } from './Skeleton'
import { useCallback, useEffect, useState } from 'react'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import { HostResources } from './HostResources'

type Delivery = 'sound' | 'silent' | 'off'
type Priority = 'urgent' | 'important' | 'low'
interface PushConfig { supported: boolean; publicKey: string; delivery?: Record<Priority, Delivery> }
const PRIORITIES: [Priority, string][] = [['urgent', 'Urgent — approvals, questions, errors'], ['important', 'Important — tasks you started'], ['low', 'Low — background updates']]

export function Choice<T extends string>({ legend, name, value, options, onChange }: { legend: string; name: string; value: T; options: [T, string][]; onChange: (value: T) => void }) {
  return <fieldset className="choice"><legend>{legend}</legend><div className="segmented">
    {options.map(([key, label]) => <label key={key}><input type="radio" name={name} value={key} checked={value === key} onChange={() => onChange(key)} /><span>{label}</span></label>)}
  </div></fieldset>
}

// Push subscription is browser-specific; delivery settings apply to all subscribed devices.
export function Notifications({ api }: { api: Api }) {
  const [notice, onNotice] = useState('')
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => onNotice(''), 5000); return () => clearTimeout(timer) }, [notice])
  const supported = 'Notification' in window && 'serviceWorker' in navigator && 'PushManager' in window
  const [state, setState] = useState(supported ? Notification.permission === 'denied' ? 'blocked' : 'off' : 'unsupported')
  const [pending, setPending] = useState(supported)
  const [publicKey, setPublicKey] = useState('')
  const [delivery, setDelivery] = useState<Record<Priority, Delivery>>({ urgent: 'sound', important: 'sound', low: 'off' })
  useEffect(() => {
    if (!supported) return
    let live = true
    Promise.all([api<PushConfig>('push/config'), navigator.serviceWorker.ready.then(registration => registration.pushManager.getSubscription())])
      .then(([config, subscription]) => {
        if (!live) return
        setPublicKey(config.publicKey || '')
        if (config.delivery) setDelivery(config.delivery)
        setState(!config.supported ? 'unsupported' : Notification.permission === 'denied' ? 'blocked' : subscription ? 'on' : 'off')
      }, failure => { if (live) onNotice(errorText(failure)) })
      .finally(() => { if (live) setPending(false) })
    return () => { live = false }
  }, [api, supported, onNotice])
  async function toggle() {
    setPending(true)
    try {
      // Request permission from the tap, before any network work.
      if (state !== 'on' && await Notification.requestPermission() !== 'granted') {
        setState(Notification.permission === 'denied' ? 'blocked' : 'off'); return
      }
      const registration = await navigator.serviceWorker.ready
      const existing = await registration.pushManager.getSubscription()
      if (state === 'on') {
        if (existing) {
          await api('push/unsubscribe', { endpoint: existing.endpoint })
          if (!await existing.unsubscribe()) throw new Error('Could not turn notifications off. Please try again.')
        }
        setState('off')
      } else {
        const key = atob(publicKey.replace(/-/g, '+').replace(/_/g, '/'))
        const subscription = existing ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: Uint8Array.from(key, char => char.charCodeAt(0)) })
        try { await api('push/subscribe', { subscription: subscription.toJSON() }) }
        catch (failure) { if (!existing) await subscription.unsubscribe(); throw failure }
        setState('on')
      }
    } catch (failure) { onNotice(errorText(failure)) } finally { setPending(false) }
  }
  async function changeDelivery(priority: Priority, value: Delivery) {
    setPending(true)
    try {
      const result = await api<{ delivery: Record<Priority, Delivery> }>('push/settings', { delivery: { [priority]: value } })
      setDelivery(result.delivery)
    } catch (failure) { onNotice(errorText(failure)) } finally { setPending(false) }
  }
  async function test() {
    setPending(true)
    try {
      const result = await api<{ delivered: number }>('push/test', {})
      onNotice(result.delivered ? `Test sent to ${result.delivered} subscription${result.delivered === 1 ? '' : 's'}.` : 'No test notifications were delivered.')
    } catch (failure) { onNotice(errorText(failure)) } finally { setPending(false) }
  }
  const label = state === 'unsupported' ? 'Unsupported' : state === 'blocked' ? 'Blocked. Allow notifications for this site in the browser or app settings.' : state === 'on' ? 'On' : 'Off'
  return <>
    <label className="switch-row"><span><strong>Push notifications</strong><small>{pending ? 'Please wait…' : label}</small></span><input type="checkbox" role="switch" checked={state === 'on'} disabled={pending || state === 'unsupported' || state === 'blocked' || (!publicKey && state !== 'on')} onChange={() => void toggle()} /></label>
    {notice && <div className="toast" role="status">{notice}</div>}
    {state === 'on' && <div className="notification-delivery">
      <p className="setting-note">Applies to all subscribed devices.</p>
      {PRIORITIES.map(([priority, label]) => <fieldset key={priority} disabled={pending}>
        <legend>{label}</legend><div className="segmented">
          {(['sound', 'silent', 'off'] as const).map(value => <label key={value}><input type="radio" name={`notify-${priority}`} value={value} checked={delivery[priority] === value} onChange={() => void changeDelivery(priority, value)} /><span>{value === 'sound' ? 'Sound' : value === 'silent' ? 'Silent' : 'Off'}</span></label>)}
        </div>
      </fieldset>)}
    </div>}
    {state === 'on' && <div className="switch-row"><span><strong>Test notifications</strong><small>Send a notification to subscribed devices.</small></span><button type="button" className="secondary" disabled={pending} onClick={() => void test()}>Send test</button></div>}
  </>
}

export function SessionSettings({ api }: { api: Api }) {
  const [folder, setFolder] = useState('')
  const [saved, setSaved] = useState('')
  const [pending, setPending] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api<{ prefs: { sessionFolder?: string } }>('prefs', undefined, controller.signal).then(data => {
      if (!controller.signal.aborted) { setFolder(data.prefs.sessionFolder || ''); setSaved(data.prefs.sessionFolder || '') }
    }, failure => { if (!controller.signal.aborted) setError(errorText(failure)) }).finally(() => { if (!controller.signal.aborted) setPending(false) })
    return () => controller.abort()
  }, [api])
  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (pending) return
    setPending(true); setError(''); setNotice('')
    const value = folder.trim()
    try {
      await api('prefs', { changes: { sessionFolder: value } })
      setFolder(value); setSaved(value); setNotice('Saved. New sessions use this folder.')
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  return <section className="settings-group" aria-labelledby="settings-sessions"><h3 id="settings-sessions">New sessions</h3>
    <form className="session-folder-setting" onSubmit={save}>
      <label htmlFor="default-session-folder">Default folder</label>
      <p id="session-folder-help" className="setting-note">Use an absolute path or ~/. Leave blank to use your home folder. Applies to new sessions on this server.</p>
      <input id="default-session-folder" value={folder} maxLength={1024} placeholder="~/projects" autoCapitalize="none" autoCorrect="off" spellCheck={false} autoComplete="off" aria-describedby="session-folder-help" disabled={pending} onChange={event => { setFolder(event.target.value); setNotice('') }} />
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="setting-note" role="status">{notice}</p>}
      <button className="secondary" disabled={pending || folder === saved}>{pending ? 'Please wait…' : 'Save folder'}</button>
    </form>
  </section>
}

interface UpdateStatus { current: string; latest: string | null; available: boolean; checking: boolean; mode: string; checked_at: number | null; url: string }
export function SystemSettings({ api, onReload }: { api: Api; onReload: () => void }) {
  const [host, setHost] = useState<{ resources: boolean; terminal: boolean } | null>(null)
  const fetchStatus = useCallback((signal: AbortSignal) => api<UpdateStatus>('update', undefined, signal), [api])
  const { data: status, error: updateError } = usePolling(fetchStatus, 5000)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    void api<{ resources: boolean; terminal: boolean }>('host').then(data => { if (live) setHost(data) }, failure => { if (live) setError(errorText(failure)) })
    return () => { live = false }
  }, [api])
  return <>
    {host?.resources && <HostResources api={api} terminal={host.terminal} />}
    <section className="settings-group" aria-labelledby="settings-system"><h3 id="settings-system">Tether</h3>
      {(error || updateError) && <p className="error" role="alert">{error || updateError}</p>}
      <div className="switch-row"><span><strong>Updates</strong><small>{status ? `Version ${status.current} · ${status.mode === 'off' ? 'Checks off' : status.checking ? 'Checking…' : status.available ? `${status.latest} available` : status.latest ? 'Up to date' : 'No release data'}` : <><Bone w="12em" /><span className="sk-label" role="status">Checking the version…</span></>}</small></span>{status?.available && status.url.startsWith('https://') && <a className="secondary" href={status.url} target="_blank" rel="noreferrer">Release notes</a>}</div>
      <div className="switch-row"><span><strong>Reload Tether</strong><small>Unsent drafts stay.</small></span><button type="button" className="secondary" onClick={onReload}>Reload</button></div>
    </section>
  </>
}
