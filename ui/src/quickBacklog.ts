// The search menu's "Add to the backlog" row: one new inbox item, saved with the same version check as the Backlog sheet.
import { ApiError } from './api'
import type { Api } from './api'
import { BACKLOG_PATH } from './backlogNews'
import { addItem, keptLength, parseBacklog, removeItem, withIds } from './backlogItems'
import type { Change, Item } from './backlogItems'

interface Saved { text: string; version: string; limit: number; next_id?: number }
const RETRIES = 3

/** Apply a change to the newest backlog. A 409 means someone saved first: read the file again and apply it there.
 * Returns the saved file and the text it was saved over, so a caller can tell its own change from others. */
async function save(api: Api, change: Change): Promise<{ saved: Saved; base: string }> {
  let base = await api<Saved>(BACKLOG_PATH)
  for (let attempt = 0; attempt < RETRIES; attempt += 1) {
    const value = change(base.text)
    if (value === null) throw new Error('That item was changed elsewhere, so the backlog was not changed.')
    const numbered = withIds(value, base.next_id)
    if (keptLength(numbered) > base.limit) throw new Error(`The backlog is full (${base.limit.toLocaleString()} characters). Delete some items first.`)
    try {
      return { saved: await api<Saved>(BACKLOG_PATH, { text: numbered, version: base.version }), base: base.text }
    } catch (failure) {
      if (!(failure instanceof ApiError && failure.status === 409)) throw failure
      base = await api<Saved>(BACKLOG_PATH)
    }
  }
  throw new Error('The backlog keeps changing. Try again.')
}

const items = (text: string) => parseBacklog(text).filter((row): row is Item => row.kind === 'item')

/** The item this save added: an inbox line with this text whose ID was not in the file before. */
export function addedItem(before: string, after: string, text: string): Item | undefined {
  const old = new Set(items(before).map(item => item.ref).filter(Boolean))
  const line = text.trim().replace(/\s*\n\s*/g, ' ')
  return items(after).find(item => !item.done && item.ref && !old.has(item.ref) && item.text === line)
}

/** Add the text as a new inbox item. Returns its ID (B12) and an undo that removes that exact line again. */
export async function addToBacklog(api: Api, text: string) {
  // Compare with the exact text this save replaced: an equal item another client added earlier is already in it.
  const { saved, base } = await save(api, addItem(text))
  const item = addedItem(base, saved.text, text)
  const undo = async () => {
    if (!item) throw new Error('The new item was not found.')
    const fresh = await api<Saved>(BACKLOG_PATH)
    const now = items(fresh.text).find(row => row.ref === item.ref)
    if (!now) return
    if (now.raw !== item.raw || now.details.length) throw new Error(`${item.ref} changed since it was added. Edit it in the backlog.`)
    await save(api, removeItem(now.raw, now.line))
  }
  return { ref: item?.ref ?? '', undo: item ? undo : undefined }
}
