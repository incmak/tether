import { useCallback, useState } from 'react'
import type { Api } from './api'
import { usePolling } from './hooks'
import type { Prefs } from './prefs'
import { Choice } from './SettingsControls'
import { USAGE_CLEARED } from './uiUsage'

// Settings › About & system › UI usage: the report from server/ui_usage.py. The log is uiUsage.ts.
interface Row { region: string; label: string; kind: string; depth: number; clicks: number; seen: number; rate: number; steps: number | null }
interface View { view: string; visits: number; steps: number | null; most: number | null; keyboard: number; from: string }
export interface UsageReport { enabled: boolean; days: number; device: string; visits: number; clicks: number; controls: number; ready: boolean; top: Row[]; demote: Row[]; promote: Row[]; unused: Row[]; views: View[] }
type Days = '7' | '30' | '90'
type Device = 'all' | 'phone' | 'wide'

const percent = (rate: number) => `${Math.round(rate * 100)}%`
const place = (row: Row) => row.depth === 0 ? row.region : `${row.region} (${row.depth === 1 ? 'menu' : `${row.depth} levels deep`})`

function List({ id, title, note, empty, rows, detail }: { id: string; title: string; note: string; empty: string; rows: Row[]; detail: (row: Row) => string }) {
  return <section className="ui-usage-list" aria-labelledby={id}>
    <h4 id={id}>{title}</h4>
    <p className="setting-note">{note}</p>
    {rows.length ? <ol>{rows.map(row => <li key={`${row.region}\n${row.label}\n${row.kind}`}><span><strong>{row.label}</strong><small>{place(row)}</small></span><span className="ui-usage-num">{detail(row)}</span></li>)}</ol>
      : <p className="ui-usage-empty">{empty}</p>}
  </section>
}

export function UiUsageSettings({ api, prefs, update }: { api: Api; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void }) {
  const [days, setDays] = useState<Days>('30')
  const [device, setDevice] = useState<Device>('all')
  const [confirming, setConfirming] = useState(false)
  const [notice, setNotice] = useState('')
  const fetchReport = useCallback((signal: AbortSignal) => api<UsageReport>(`ui-usage?days=${days}${device === 'all' ? '' : `&device=${device}`}`, undefined, signal), [api, days, device])
  const { data: report, error, refresh } = usePolling(fetchReport, 60000)
  async function clear() {
    try {
      const { gen } = await api<{ gen: string }>('ui-usage/clear', {})
      window.dispatchEvent(new CustomEvent(USAGE_CLEARED, { detail: gen }))
      setNotice('The UI usage log is empty now.')
      void refresh()
    } catch (failure) {
      setNotice(failure instanceof Error ? failure.message : 'Could not clear the log.')
    }
    setConfirming(false)
  }
  return <section className="settings-group ui-usage" aria-labelledby="settings-ui-usage"><h3 id="settings-ui-usage">UI usage</h3>
    <label className="switch-row"><span><strong>Log UI usage</strong><small>Tether notes which buttons, menu items and links you use, and which menus and dialogs you open. The log stays on this server for 90 days. It never has typed text. Names shown in lists, and words that are not in Tether's own labels, become “…”.</small><span className="preferences-scope">All devices</span></span><input type="checkbox" role="switch" checked={prefs.uiUsage} onChange={event => update(() => ({ uiUsage: event.target.checked }))} /></label>
    <Choice legend="Period" name="ui-usage-days" value={days} options={[['7', '7 days'], ['30', '30 days'], ['90', '90 days']]} onChange={setDays} />
    <Choice legend="Screen" name="ui-usage-device" value={device} options={[['all', 'All'], ['phone', 'Phone'], ['wide', 'Desktop']]} onChange={setDevice} />
    {error && <p className="error" role="alert">{error}</p>}
    {!report && !error && <p className="setting-note" role="status">Loading the report…</p>}
    {report && <>
      <p className="setting-note" role="status">{report.visits} {report.visits === 1 ? 'visit' : 'visits'}, {report.clicks} {report.clicks === 1 ? 'click' : 'clicks'}, {report.controls} controls seen.{!report.ready && ' Suggestions start after 5 visits.'}</p>
      {report.ready && <>
        <List id="ui-usage-demote" title="Rarely used on the main screen" note="These are always on screen but you almost never use them. Move them into a menu or a More button." empty="Every control on the main screen gets used." rows={report.demote} detail={row => `${percent(row.rate)} of ${row.seen} visits`} />
        <List id="ui-usage-promote" title="Often used, but hidden" note="You open a menu or dialog to reach these often, or it takes many clicks. Put them on the main screen." empty="Nothing hidden is used often." rows={report.promote} detail={row => `${row.clicks} clicks${row.steps != null ? ` · ${row.steps} to reach` : ''}`} />
        <List id="ui-usage-unused" title="Never used" note="These were on screen in a menu or dialog you opened, but you never used them. Remove them or move them down." empty="You used every control you saw." rows={report.unused} detail={row => `seen in ${row.seen} visits`} />
      </>}
      <List id="ui-usage-top" title="Most used" note="The controls you use most." empty="No clicks yet." rows={report.top} detail={row => `${row.clicks} clicks`} />
      <section className="ui-usage-list" aria-labelledby="ui-usage-views">
        <h4 id="ui-usage-views">Clicks to reach each view</h4>
        <p className="setting-note">The usual number of clicks from the main screen to each menu or dialog. Views that take 3 or more clicks are hard to reach.</p>
        {report.views.length ? <ol>{report.views.map(view => <li key={view.view}><span><strong>{view.view}{view.steps != null && view.steps >= 3 && <span className="ui-usage-deep"> · hard to reach</span>}</strong><small>{view.from ? `Mostly from ${view.from}` : 'By keyboard'}</small></span><span className="ui-usage-num">{view.steps != null ? `${view.steps} ${view.steps === 1 ? 'click' : 'clicks'}` : 'keys'} · {view.visits}×{view.keyboard ? ` · ${view.keyboard} by keys` : ''}</span></li>)}</ol>
          : <p className="ui-usage-empty">No views opened yet.</p>}
      </section>
    </>}
    <div className="ui-usage-actions">
      {confirming ? <><span>Remove the whole log?</span><button type="button" className="danger-text" onClick={() => void clear()}>Remove</button><button type="button" className="secondary" onClick={() => setConfirming(false)}>Cancel</button></>
        : <button type="button" className="secondary" disabled={!report?.visits} onClick={() => { setNotice(''); setConfirming(true) }}>Clear the log</button>}
    </div>
    {notice && <p className="setting-note" role="status">{notice}</p>}
  </section>
}
