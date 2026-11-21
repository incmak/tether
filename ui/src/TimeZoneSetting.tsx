import { useEffect, useMemo, useState } from 'react'
import { SelectMenu } from './SelectMenu'
import type { MenuOption } from './SelectMenu'
import { deviceZone, fmtTime, offsetLabel, useTetherZone, zoneList } from './zone'

const spoken = (zone: string) => zone.replace(/_/g, ' ')

/** Settings › General › Time zone: the one zone for all of Tether (synced pref `timeZone`, server default Asia/Kolkata). */
export function TimeZoneSetting({ onChange, rejectedZone }: { onChange: (zone: string) => void; rejectedZone?: string }) {
  const zone = useTetherZone()
  const [clock, setClock] = useState(() => Date.now())
  // Keep "It is … there now" current while the page stays open.
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 15_000)
    return () => window.clearInterval(timer)
  }, [])
  const device = deviceZone()
  // The device button goes away once it applies, so give focus to the zone menu, which now names the new zone.
  const applyDevice = () => { onChange(device); document.getElementById('time-zone')?.focus() }
  const options = useMemo<MenuOption[]>(() => {
    const names = zoneList()
    if (!names.includes(zone)) names.unshift(zone)
    return names.map(name => { const offset = offsetLabel(name); return { value: name, label: spoken(name), detail: offset, search: `${name} ${spoken(name)} ${offset}` } })
  }, [zone])
  return <fieldset className="choice time-zone-setting">
    <legend>Time zone</legend>
    <SelectMenu id="time-zone" label="Time zone" value={zone} options={options} onChange={onChange} triggerLabel={`${spoken(zone)} · ${offsetLabel(zone, clock)}`} searchLabel="Search time zones" emptyText="No time zone matches." />
    <div role="status">{rejectedZone && <p className="setting-note">Tether could not use {spoken(rejectedZone)} on the server. It still uses {spoken(zone)}.</p>}</div>
    <p className="setting-note">Reminders, the daily summary, the daily ops report and every time Tether shows use this zone. It is {fmtTime(clock, { hour: 'numeric', minute: '2-digit' })} there now.</p>
    {device && device !== zone && <button type="button" className="secondary time-zone-device" onClick={applyDevice}>Use this device’s zone ({spoken(device)})</button>}
  </fieldset>
}
