import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { ApiError, errorText } from './api'
import type { Api, Approval, Choice } from './api'
import type { InboxItem } from './needs'

export async function sendAnswer(api: Api, item: InboxItem, option: Choice) {
  const { pane } = item
  const approval = pane.approval as Approval | null
  if (item.kind === 'approval' && approval?.id) await api('approval', { session: pane.id, thread: pane.thread, approval: approval.id, choice: option.id })
  else {
    const revision = pane.attention?.menu_verified
    if (revision) await api('menu', { session: pane.id, thread: pane.thread, choice: option.id, label: option.text, revision })
  }
}

export function answerError(error: unknown) {
  return error instanceof ApiError && error.status === 409 ? 'This pane changed. Open it to check.' : errorText(error)
}

export function optionForKey(code: string, item: InboxItem): Choice | undefined {
  if (code === 'KeyY') return item.yes
  if (code === 'KeyN') return item.no
  const digit = /^(?:Digit|Numpad)([1-3])$/.exec(code)
  return digit ? item.menu?.find(choice => choice.id === Number(digit[1])) : undefined
}

export function isAnswerKey(code: string) {
  return /^Key[YN]$|^(?:Digit|Numpad)[1-3]$/.test(code)
}

interface UseAnswersProps {
  api: Api
  items: InboxItem[]
  active: boolean
  onUpdate: () => void
  focusRow: (id: string) => void
}

export function useAnswers({ api, items, active, onUpdate, focusRow }: UseAnswersProps) {
  const [busy, setBusy] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  // After an answer, focus moves to the row that was below it, so Y, N and the digits keep working down the list.
  const refocus = useRef<string | null>(null)
  const ids = items.map(item => item.pane.id).join(',')
  useEffect(() => {
    setErrors(current => { const kept = Object.entries(current).filter(([id]) => ids.split(',').includes(id)); return kept.length === Object.keys(current).length ? current : Object.fromEntries(kept) })
    const id = refocus.current
    if (!id || !active) return
    if (!document.activeElement || document.activeElement === document.body || !document.activeElement.isConnected) focusRow(id)
    refocus.current = null
  }, [ids, active, focusRow])

  const fail = (paneId: string, error: unknown) => {
    setErrors(current => ({ ...current, [paneId]: errorText(error) }))
  }

  const answer = async (item: InboxItem, option: Choice) => {
    const { pane } = item
    if (busy) return
    const at = items.indexOf(item)
    refocus.current = (items[at + 1] ?? items[at - 1])?.pane.id ?? null
    setBusy(pane.id)
    setErrors(current => Object.fromEntries(Object.entries(current).filter(([id]) => id !== pane.id)))
    try {
      await sendAnswer(api, item, option)
      onUpdate()
    } catch (error) {
      refocus.current = null
      setErrors(current => ({ ...current, [pane.id]: answerError(error) }))
      onUpdate()
    } finally { setBusy(null) }
  }

  const onAnswerKey = (event: KeyboardEvent, item: InboxItem) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
    const option = optionForKey(event.code, item)
    // Keep the key out of the message box even when this row has no such answer.
    if (isAnswerKey(event.code)) { event.preventDefault(); event.stopPropagation() }
    if (option) void answer(item, option)
  }
  return { busy, errors, answer, fail, onAnswerKey }
}

