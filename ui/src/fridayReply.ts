export const REPLY_PART = 6000

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
