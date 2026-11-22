import { fmtDate, fmtTime, sameDay } from './zone'

// Time of the reply, as the CLI shows it: "9:36 PM" today, "Fri 9:36 PM" this week, else the date.
export function stamp(value?: string) {
  const at = value ? Date.parse(value) : NaN
  if (Number.isNaN(at)) return ''
  const now = Date.now()
  const clock = fmtTime(at, { hour: 'numeric', minute: '2-digit' })
  if (sameDay(at, now)) return clock
  return now - at < 6 * 86400000
    ? `${fmtDate(at, { weekday: 'short' })} ${clock}`
    : `${fmtDate(at, { day: 'numeric', month: 'short' })} ${clock}`
}
