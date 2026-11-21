import { useMemo, useState } from 'react'
import { SelectMenu } from './SelectMenu'
import type { MenuOption } from './SelectMenu'
import { deviceZone, fmtTime, offsetLabel, useTetherZone, zoneList } from './zone'

const spoken = (zone: string) => zone.replace(/_/g, ' ')

/** Settings › General › Time zone: the one zone for all of Tether (synced pref `timeZone`, server default Asia/Kolkata). */
export function TimeZoneSetting({ onChange }: { onChange: (zone: string) => void }) {
  const zone = useTetherZone()
  const [opened] = useState(() => Date.now())
  const device = deviceZone()
  const options = useMemo<MenuOption[]>(() => {
    const names = zoneList()
    if (!names.includes(zone)) names.unshift(zone)
    return names.map(name => { const offset = offsetLabel(name); return { value: name, label: spoken(name), detail: offset, search: `${name} ${spoken(name)} ${offset}` } })
  }, [zone])
  return <fieldset className="choice time-zone-setting">
    <legend>Time zone</legend>
    <SelectMenu id="time-zone" label="Time zone" value={zone} options={options} onChange={onChange} triggerLabel={`${spoken(zone)} · ${offsetLabel(zone, opened)}`} searchLabel="Search time zones" emptyText="No time zone matches." />
    <p className="setting-note">Reminders, the daily summary, the daily ops report and every time Tether shows use this zone. It is {fmtTime(opened, { hour: 'numeric', minute: '2-digit' })} there now.</p>
    {device && device !== zone && <button type="button" className="secondary time-zone-device" onClick={() => onChange(device)}>Use this device’s zone ({spoken(device)})</button>}
  </fieldset>
}
