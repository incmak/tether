import { useEffect, useState } from 'react'
import { AppWindow, Copy, Download } from 'lucide-react'
import { ApiError, errorText, reach } from './api'
import type { Api, BrowserExtensionDevice, BrowserExtensionDevices, BrowserExtensionPair } from './api'

function ago(seconds: number, now: number) {
  const minutes = Math.round((now / 1000 - seconds) / 60)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`
}

const chromeVersion = (agent: string) => /Chrome\/(\d+)/.exec(agent)?.[1] ?? ''

/** The extension as a zip of its committed files under one tether-extension/ folder (server/extension_zip.py). */
const EXTENSION_ZIP = '/mobile/api/browser/extension/download'
const FOLDER = 'tether-extension'

/** The file name from a Content-Disposition header: filename* (RFC 6266) first, then filename. */
function savedName(header: string | null) {
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header ?? '')?.[1]
  if (star) { try { return decodeURIComponent(star.trim()) } catch { /* a bad escape: use the plain name */ } }
  return /filename="([^"]+)"/i.exec(header ?? '')?.[1] ?? ''
}

/** Fetch the zip first, so a refusal (signed out, server error) shows in the panel instead of saving a JSON file. */
async function saveExtension() {
  const response = await reach(EXTENSION_ZIP, { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(30000) })
  if (!response.ok) {
    if (response.status === 401) throw new ApiError(401, 'Your sign-in ended. Reload Tether, sign in, and download again.')
    const data = await response.json().catch(() => ({}))
    throw new ApiError(response.status, data.error || `The download failed (${response.status}). Try again.`)
  }
  // A proxy or captive portal can answer 200 with a sign-in page: never save that as the zip.
  if (!/^application\/zip\s*(;|$)/i.test(response.headers.get('Content-Type') || '')) {
    throw new ApiError(response.status, 'The server did not send a zip file. Reload Tether, sign in, and download again.')
  }
  const blob = await response.blob()
  const name = savedName(response.headers.get('Content-Disposition')) || `${FOLDER}.zip`
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url; link.download = name; link.rel = 'noopener'
  document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 60000)
  return name
}

/** Your Chrome with no open tab: pair the Tether extension (server/mac_browser.py) and manage paired browsers.
 * Tabs appear here only after Mak taps Allow on an agent's request. */
export function MacPairing({ api, connected }: { api: Api; connected: boolean }) {
  const [devices, setDevices] = useState<BrowserExtensionDevice[] | null>(null)
  const [code, setCode] = useState<BrowserExtensionPair | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState('')
  const [downloading, setDownloading] = useState(false)
  const [saved, setSaved] = useState('')
  const [downloadError, setDownloadError] = useState('')

  useEffect(() => {
    let stop = false
    const load = async () => {
      try { const rows = await api<BrowserExtensionDevice[]>('browser/extension/devices'); if (!stop) setDevices(rows) } catch (failure) { if (!stop) setError(errorText(failure)) }
    }
    void load()
    const timer = window.setInterval(() => { setNow(Date.now()); void load() }, 5000)
    return () => { stop = true; clearInterval(timer) }
  }, [api])

  const left = code ? Math.max(0, Math.ceil((code.expires * 1000 - now) / 60000)) : 0
  const live = code && code.expires * 1000 > now ? code : null
  const origin = window.location.origin

  async function pair() {
    setBusy(true); setError('')
    try { setCode(await api<BrowserExtensionPair>('browser/extension/pair', {})); setNow(Date.now()) } catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  async function revoke(id: string) {
    setError('')
    try { setDevices((await api<BrowserExtensionDevices>('browser/extension/revoke', { id })).devices) } catch (failure) { setError(errorText(failure)) }
  }
  async function download(event: React.MouseEvent<HTMLAnchorElement>) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return  // the browser's own link actions
    event.preventDefault()
    if (downloading) return
    setDownloading(true); setDownloadError(''); setSaved('')
    try { setSaved(await saveExtension()) } catch (failure) { setDownloadError(errorText(failure)) } finally { setDownloading(false) }
  }
  // The result shows under the link, where the eye is after the click.
  const downloadLink = <>
    <a className="mac-pairing-download" href={EXTENSION_ZIP} download aria-busy={downloading || undefined}
      aria-disabled={downloading || undefined} onClick={event => void download(event)}>
      <Download size={15} aria-hidden="true" />{downloading ? 'Downloading…' : 'Download extension (.zip)'}</a>
    <span className="mac-pairing-download-note" role="status">{saved ? `Downloaded ${saved}.` : ''}</span>
    {downloadError && <span className="mac-pairing-download-error" role="alert">{downloadError}</span>}
  </>

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(text); window.setTimeout(() => setCopied(''), 1500) } catch { /* no clipboard access: the text stays selectable */ }
  }

  return <div className="mac-pairing">
    <AppWindow size={22} aria-hidden="true" className="mac-pairing-icon" />
    <h3>{connected ? 'Your Chrome is connected' : 'Connect your Chrome'}</h3>
    <p className="mac-pairing-lead">{connected
      ? 'No tab is open. When an agent asks for your browser, tap Allow. Its tab shows here.'
      : 'Agents can then use a tab in your own Chrome, after you tap Allow.'}</p>

    {!connected && <ol className="mac-pairing-steps">
      <li>On your computer, download the Tether extension.{downloadLink}</li>
      <li>Unzip it. Keep the <code>{FOLDER}</code> folder: Chrome runs the extension from it.</li>
      <li>In Chrome, open <code>chrome://extensions</code> and turn on Developer mode.</li>
      <li>Click Load unpacked and pick the <code>{FOLDER}</code> folder, not the .zip file.</li>
      <li>Open the extension’s options. Enter this address and a pairing code.</li>
      <li>Click Allow when Chrome asks for access.</li>
    </ol>}

    <div className="mac-pairing-fields">
      <div className="mac-pairing-field"><span>Address</span><code>{origin}</code>
        <button type="button" className="browser-icon" aria-label="Copy the address" onClick={() => void copy(origin)}><Copy size={15} /></button></div>
      {live ? <div className="mac-pairing-field"><span>Code</span><code className="mac-pairing-code">{live.code}</code>
        <button type="button" className="browser-icon" aria-label="Copy the code" onClick={() => void copy(live.code)}><Copy size={15} /></button></div> : null}
    </div>
    <p className="mac-pairing-meta" role="status">{copied ? 'Copied' : live ? `The code works once. It expires in ${left} min.` : ''}</p>
    <button type="button" className={connected ? 'secondary' : 'primary'} disabled={busy} onClick={() => void pair()}>{live ? 'New code' : connected ? 'Pair another browser' : 'Get a pairing code'}</button>

    {devices && devices.length > 0 && <ul className="mac-pairing-devices" aria-label="Paired browsers">
      {devices.map(device => <li key={device.id}>
        <span className={device.connected ? 'mac-dot on' : 'mac-dot'} aria-hidden="true" />
        <span className="mac-pairing-name"><strong>{device.name}</strong><small>{device.connected ? 'Connected' : `Last seen ${ago(device.lastSeen, now)}`}{chromeVersion(device.chrome) ? ` · Chrome ${chromeVersion(device.chrome)}` : ''}</small></span>
        <button type="button" className="browser-text-button" onClick={() => void revoke(device.id)}>Remove</button>
      </li>)}
    </ul>}
    {connected && <div className="mac-pairing-update"><p>For another computer or an update:</p>{downloadLink}
      <p>To update, replace the files in the <code>{FOLDER}</code> folder, then click Reload on <code>chrome://extensions</code>.</p></div>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
