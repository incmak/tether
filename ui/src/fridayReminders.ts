// Friday's reminder tools: add a spoken batch to Tether's reminders, and read the upcoming ones.
import { errorText, type Api } from './api'
import { fullLabel, PATH, type Listing, type Reminder } from './reminders'

const MAX = 5
const short = (text: string, max: number) => text.length > max ? text.slice(0, max - 1).trimEnd() + '…' : text

/** Adds each reminder in the spoken order. One bad time does not lose the rest; the result names each failure. */
export async function addReminders(api: Api, asked: unknown) {
  const items = Array.isArray(asked) ? asked.slice(0, MAX) : []
  if (!items.length) return { error: 'No reminders given.' }
  const set: { id: string; text: string; when: string }[] = [], failed: { text: string; error: string }[] = []
  for (const raw of items) {
    const one: Record<string, unknown> = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
    const text = String(one.text ?? '').trim(), when = String(one.when ?? '').trim()
    try {
      if (!text || !when) throw new Error('Each reminder needs text and a time.')
      const made = await api<{ reminder: Reminder }>(`${PATH}/add`, { text, when })
      set.push({ id: made.reminder.id, text: made.reminder.text, when: fullLabel(made.reminder.due) })
    } catch (error) {
      failed.push({ text: short(text, 80) || 'A reminder', error: errorText(error) })
    }
  }
  return set.length ? { ok: true, set, failed } : { error: failed.map(item => `${item.text}: ${item.error}`).join(' ') }
}

/** The reminders not yet done, soonest first, at most 10. */
export async function listReminders(api: Api) {
  const data = await api<Listing>(PATH)
  const open = data.reminders.filter(item => item.state !== 'done').sort((a, b) => a.due - b.due)
  return { count: open.length, items: open.slice(0, 10).map(item =>
    `${item.id} ${short(item.text, 120)}, ${fullLabel(item.due)}${item.state === 'due' ? ' (due now)' : ''}`) }
}
