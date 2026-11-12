// Guided setup for autonomy (server: autonomy_setup.py) and the answers a person saved for a project (decisions.py).
// Each setup step shows the exact change first; Tether writes the file only after the person approves it.
import { useCallback, useState } from 'react'
import { KeyRound, Trash2 } from 'lucide-react'
import { addPasskey, passkeysWork, removePasskey, withApproval } from './approval'
import type { ApprovalAction, ApprovalStatus } from './approval'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import { Loading } from './Skeleton'
import { SelectMenu } from './SelectMenu'
import './AutonomySetup.css'

type HookState = { state: 'ok' | 'missing' | 'none' | 'manual'; path: string; entry: Record<string, unknown>; reason?: string }
type PingState = {
  state: 'on' | 'off'; preset: string | null; fields: Record<string, string>; base_url: string
  presets: { id: string; name: string; available: boolean }[]
}
interface SetupState { hook: HookState; ping: PingState; test?: { ok: boolean; code: number | null; detail: string } }
export interface SavedAnswer { id: string; date: string; question: string; answer: string }

const SPACES = ['team', 'qa', 'release', 'bots']

/** Settings › Agents: the two setup steps autonomy needs, each applied only after approval. */
export function AutonomySetup({ api }: { api: Api }) {
  const fetcher = useCallback((signal: AbortSignal) => api<SetupState>('autonomy/setup', undefined, signal), [api])
  const { data, error: loadError, refresh } = usePolling(fetcher, 30000)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const keysFetcher = useCallback((signal: AbortSignal) => api<ApprovalStatus>('passkeys', undefined, signal), [api])
  const { data: keys, refresh: refreshKeys } = usePolling(keysFetcher, 30000)
  const ready = !!keys?.here
  async function apply(body: Record<string, unknown>, done: string) {
    setBusy(true); setError(''); setStatus('')
    const gate: Record<string, ApprovalAction> = { hook: 'setup-hook', ping: 'setup-ping' }
    try {
      const action = gate[String(body.action)]
      const next = action ? await withApproval<SetupState>(api, 'autonomy/setup', action, body) : await api<SetupState>('autonomy/setup', body)
      if (next.test) setStatus(next.test.ok ? 'Test sent. Check Teams.' : `The test failed${next.test.code === null ? '' : ` (exit ${next.test.code})`}: ${next.test.detail || 'no detail'}`)
      else setStatus(done)
      await refresh()
      return true
    } catch (failure) { setError(errorText(failure)); return false }
    finally { setBusy(false) }
  }
  async function keyAction(work: () => Promise<unknown>, done: string) {
    setBusy(true); setError(''); setStatus('')
    try { await work(); setStatus(done); await refreshKeys() }
    catch (failure) { setError(errorText(failure)) }
    finally { setBusy(false) }
  }
  return <section className="settings-group" aria-labelledby="settings-autonomy-setup"><h3 id="settings-autonomy-setup">Setup</h3>
    {!data || !keys ? loadError ? <p className="setting-note" role="alert">{loadError}</p> : <Loading className="setting-note" /> : <>
      <p className="setting-note">Each step shows its change first. You approve it with your passkey, so an agent cannot approve it for you. Tether keeps a copy of the old file.</p>
      <PasskeyStep keys={keys} busy={busy}
        onAdd={() => keyAction(() => addPasskey(api, deviceName(), ready), 'Passkey added. Changes now need your approval.')}
        onRemove={id => keyAction(() => removePasskey(api, id), 'Passkey removed.')} />
      <HookStep hook={data.hook} busy={busy} ready={ready} onApprove={() => apply({ action: 'hook' }, 'Done. New prompts get the level rules.')} />
      <PingStep ping={data.ping} busy={busy} ready={ready} apply={apply} />
      {error && <p className="error" role="alert">{error}</p>}
      {status && <p className="setting-note setup-status" role="status">{status}</p>}
    </>}
  </section>
}

function HookStep({ hook, busy, ready, onApprove }: { hook: HookState; busy: boolean; ready: boolean; onApprove: () => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  const note = hook.state === 'ok' ? 'Ready. Each Claude prompt gets the level and reviewer rules.'
    : hook.state === 'missing' ? 'Not set up. Claude does not get the level rules yet.'
    : hook.state === 'none' ? 'Claude Code is not installed on this server.'
    : hook.reason || 'Change the settings file by hand.'
  const preview = JSON.stringify(hook.entry, null, 2)
  return <div className="setup-step">
    <div className="switch-row">
      <span><strong>Level rules for Claude</strong><small>{note}</small></span>
      {hook.state === 'missing' && !open && <button type="button" className="secondary" disabled={busy} onClick={() => setOpen(true)}>Review change</button>}
    </div>
    {hook.state === 'missing' && open && <div className="setup-review">
      <p>Tether adds this hook under <code>UserPromptSubmit</code> in <code>{hook.path}</code>. Other hooks stay as they are.</p>
      <pre tabIndex={0} aria-label="The change">{preview}</pre>
      {!ready && <p className="setup-need-key">Add a passkey above first.</p>}
      <div className="sheet-buttons">
        <button type="button" className="secondary" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
        <button type="button" className="primary" disabled={busy || !ready} onClick={() => void onApprove().then(ok => { if (ok) setOpen(false) })}><KeyRound size={15} aria-hidden="true" />Approve and apply</button>
      </div>
    </div>}
  </div>
}

function PingStep({ ping, busy, ready, apply }: { ping: PingState; busy: boolean; ready: boolean; apply: (body: Record<string, unknown>, done: string) => Promise<boolean> }) {
  const preset = ping.presets.find(item => item.id === 'briskbot')
  const [open, setOpen] = useState(false)
  const [mention, setMention] = useState(ping.fields.mention ?? '')
  const [space, setSpace] = useState(ping.fields.space ?? 'team')
  const valid = /^[A-Za-z0-9._@-]{1,64}$/.test(mention.trim())
  const on = ping.state === 'on'
  const note = on ? `On. ${preset?.name ?? 'The bot'} posts in ${ping.fields.space ?? 'the channel'} and mentions @${ping.fields.mention ?? ''}.`
    : preset?.available ? 'Off. Urgent asks show only in Tether.'
    : 'Off. BriskBot is not installed on this server. For another service, edit urgent-ping.json in the Tether config folder.'
  const editing = open && preset?.available
  return <div className="setup-step">
    <div className="switch-row">
      <span><strong>Urgent pings in Teams</strong><small>{note}</small></span>
      {!editing && preset?.available && (on
        ? <span className="setup-buttons">
            <button type="button" className="secondary" disabled={busy} onClick={() => void apply({ action: 'ping-test' }, '')}>Send test</button>
            <button type="button" className="secondary" disabled={busy} onClick={() => void apply({ action: 'ping-off' }, 'Urgent pings are off.')}>Turn off</button>
          </span>
        : <button type="button" className="secondary" disabled={busy} onClick={() => setOpen(true)}>Set up</button>)}
    </div>
    {editing && <form className="setup-review" onSubmit={event => {
      event.preventDefault()
      if (valid) void apply({ action: 'ping', preset: 'briskbot', fields: { mention: mention.trim(), space }, base_url: location.origin }, 'Urgent pings are on. Send a test to check.').then(ok => { if (ok) setOpen(false) })
    }}>
      <div className="project-size-row">
        <label htmlFor="ping-mention">Mention</label>
        <input id="ping-mention" type="text" autoComplete="off" autoCapitalize="off" spellCheck={false} maxLength={64} placeholder="Your BriskBot name" value={mention} disabled={busy} onChange={event => setMention(event.target.value)} />
      </div>
      <div className="project-size-row">
        <label htmlFor="ping-space">Channel</label>
        <SelectMenu id="ping-space" value={space} onChange={setSpace} options={SPACES.map(value => ({ value, label: value }))} />
      </div>
      <p>When an agent marks a question urgent, BriskBot posts it in <strong>{space}</strong> and mentions <strong>@{mention.trim() || '…'}</strong>, with a link to the tab. The limit is 1 ping per tab every 10 minutes and 6 per hour. Tether saves this in <code>urgent-ping.json</code>.</p>
      {!ready && <p className="setup-need-key">Add a passkey above first.</p>}
      <div className="sheet-buttons">
        <button type="button" className="secondary" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
        <button type="submit" className="primary" disabled={busy || !valid || !ready}><KeyRound size={15} aria-hidden="true" />Approve and turn on</button>
      </div>
    </form>}
  </div>
}

function deviceName() {
  const phone = /Android|iPhone|iPad|Mobi/i.test(navigator.userAgent)
  const where = /iPhone|iPad/.test(navigator.userAgent) ? 'iPhone' : /Android/.test(navigator.userAgent) ? 'Android' : /Mac/.test(navigator.userAgent) ? 'Mac' : /Windows/.test(navigator.userAgent) ? 'Windows' : phone ? 'Phone' : 'Computer'
  return `${where} · ${new Date().toLocaleDateString()}`
}

/** The passkeys that approve changes. Agents can use Tether's token; they cannot pass Face ID, a fingerprint or a PIN. */
function PasskeyStep({ keys, busy, onAdd, onRemove }: { keys: ApprovalStatus; busy: boolean; onAdd: () => void; onRemove: (id: string) => void }) {
  const works = passkeysWork()
  const note = !works ? 'This browser cannot use passkeys here. Open Tether over HTTPS.'
    : keys.here ? `${keys.here} ${keys.here === 1 ? 'passkey approves' : 'passkeys approve'} changes on this address.`
    : keys.passkeys.length ? 'Your passkeys work on another Tether address. Open Tether there to approve changes or add passkeys.'
    : 'No passkey yet. Add one now: the first passkey needs no approval, so add it before anyone else does.'
  return <div className="setup-step">
    <div className="switch-row">
      <span><strong>Passkey approval</strong><small>{note}</small></span>
      {works && (keys.here > 0 || !keys.passkeys.length) && <button type="button" className="secondary" disabled={busy} onClick={onAdd}>{keys.here ? 'Add another' : 'Add passkey'}</button>}
    </div>
    {!!keys.passkeys.length && <ul className="project-list setup-keys">{keys.passkeys.map(item => <li key={item.id}>
      <span className="project-item"><strong>{item.name}</strong><small>Added {new Date(item.created).toLocaleDateString()}{item.last_used ? ` · last used ${new Date(item.last_used).toLocaleDateString()}` : ''}</small></span>
      <button type="button" className="icon-button" aria-label={`Remove the passkey ${item.name}`} disabled={busy || !works} onClick={() => onRemove(item.id)}><Trash2 size={16} aria-hidden="true" /></button>
    </li>)}</ul>}
  </div>
}

/** The Project sheet: answers saved with "Remember for this project". Agents at level 3 and above reuse them. */
export function SavedAnswers({ api, project, answers, onChange }: { api: Api; project: string; answers: SavedAnswer[]; onChange: () => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  async function remove(id: string) {
    setBusy(id); setError('')
    try { await api('autonomy/decisions', { project, delete: id }); onChange() }
    catch (failure) { setError(errorText(failure)) }
    finally { setBusy('') }
  }
  return <section className="settings-group" aria-labelledby="project-answers"><h3 id="project-answers">Saved answers</h3>
    <p className="setting-note">Agents at autonomy level 3 and above reuse these answers in this project, and nowhere else.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {answers.length ? <ul className="project-list saved-answers">{answers.map(item => <li key={item.id}>
      <span className="project-item"><small>{item.date} · {item.question}</small><strong>{item.answer}</strong></span>
      <button type="button" className="icon-button" aria-label={`Delete the saved answer: ${item.answer}`} disabled={!!busy} onClick={() => void remove(item.id)}><Trash2 size={16} aria-hidden="true" /></button>
    </li>)}</ul> : <p className="hint">No saved answers. To save one, tick “Remember for this project” when you answer in Waiting on you.</p>}
  </section>
}
