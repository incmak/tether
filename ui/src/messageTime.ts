// Time of the reply, as the CLI shows it: "9:36 PM" today, "Fri 9:36 PM" this week, else the date.
export function stamp(value?: string) {
  const at = value ? new Date(value) : null
  if (!at || Number.isNaN(at.getTime())) return ''
  const days = (Date.now() - at.getTime()) / 86400000
  const clock = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  if (at.toDateString() === new Date().toDateString()) return clock
  return days < 6 ? `${at.toLocaleDateString(undefined, { weekday: 'short' })} ${clock}` : `${at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} ${clock}`
}
