import { chatMessages, type ChatData, type ChatMessage } from './api'

export const REPLY_WINDOWS = [20, 200, 2000] as const
export const REPLY_PART = 6000

export const isReply = (message: ChatMessage) => message.role === 'assistant' && !message.kind && !!message.text.trim()

export async function findLastReply(fetchChat: (limit: number) => Promise<ChatData>): Promise<{ message: ChatMessage | null; searchedAll: boolean }> {
  for (const limit of REPLY_WINDOWS) {
    const data = await fetchChat(limit)
    const message = [...chatMessages(data)].reverse().find(isReply) ?? null
    const searchedAll = !data.earlier
    if (message || searchedAll) return { message, searchedAll }
  }
  return { message: null, searchedAll: false }
}

export function replyPage(text: string, replyId: string, offset?: number, previousId?: string) {
  const newReply = previousId !== undefined && previousId !== replyId
  return { ...replyPart(text, newReply ? 0 : offset), reply_id: replyId, ...(newReply ? { new_reply: true } : {}) }
}

function splitsSurrogate(text: string, index: number) {
  const before = text.charCodeAt(index - 1)
  const after = text.charCodeAt(index)
  return before >= 0xd800 && before <= 0xdbff && after >= 0xdc00 && after <= 0xdfff
}

export function replyPart(text: string, offset = 0, limit = REPLY_PART) {
  const total_chars = text.length
  offset = Math.min(total_chars, Math.max(0, Math.trunc(offset) || 0))
  if (splitsSurrogate(text, offset)) offset--

  let end = Math.min(total_chars, offset + Math.max(0, Math.trunc(limit) || 0))
  if (end > offset && end < total_chars) {
    if (!/\s/.test(text[end]) && !/\s/.test(text[end - 1])) {
      const space = text.slice(offset, end).search(/\s\S*$/)
      if (space >= 0) end = offset + space + 1
    }
    if (splitsSurrogate(text, end)) end--
  }

  const part = { text: text.slice(offset, end), total_chars, offset, truncated: end < total_chars }
  return part.truncated ? { ...part, next_offset: end } : part
}
