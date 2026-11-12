import type { CatalogItem } from './api'

// Like Claude's own menu: the name or an alias (/new finds /clear), then words in the description (/account finds /status).
// -1 means no match. Separators are ignored, so /adddir finds /add-dir.
export const bare = (text: string) => text.toLowerCase().replace(/[-_:]/g, '')
export const rank = (item: CatalogItem, query: string) => {
  if (!query) return 0
  const name = item.name.toLowerCase()
  const key = bare(query)
  if (name.startsWith(query) || bare(name).startsWith(key)) return 0
  if (name.split(/[:-]/).some(word => word.startsWith(query))) return 1
  if (item.aliases?.some(alias => alias.startsWith(query) || bare(alias).startsWith(key))) return 2
  if (name.includes(query) || bare(name).includes(key)) return 3
  if (query.length >= 3 && item.description.toLowerCase().split(/[^a-z0-9]+/).some(word => word.startsWith(query))) return 4
  return -1
}

// Edit distance, for typos such as /imporove -> /improve.
export function distance(a: string, b: string) {
  let row = Array.from({ length: b.length + 1 }, (_, index) => index)
  for (let i = 1; i <= a.length; i++) {
    const next = [i]
    for (let j = 1; j <= b.length; j++) next[j] = Math.min(row[j] + 1, next[j - 1] + 1, row[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    row = next
  }
  return row[b.length]
}

export const named = (item: CatalogItem, name: string) => item.name.toLowerCase() === name || !!item.aliases?.some(alias => alias.toLowerCase() === name)
// A required argument (<path>) means the command needs more text before it runs.
export const needsMore = (item: CatalogItem) => /</.test(item.hint ?? '')
export const agentName: Record<string, string> = { claude: 'Claude', codex: 'Codex' }

// True when the text starts with a command this agent does not have. The agent would run its closest
// command for a partial name (Enter accepts the top match in its menu), or reply "Unknown command".
export function unknownCommand(text: string, agent: string, commands: CatalogItem[]) {
  const match = /^\/([^\s/]+)(?:\s|$)/.exec(text.trim())
  if (!match || !commands.length || (agent !== 'claude' && agent !== 'codex')) return false
  const name = match[1].toLowerCase()
  // Codex /ls becomes !ls on the server. MCP prompts (/mcp__server__name) are not in the list.
  if ((agent === 'codex' && name === 'ls') || name.includes('__')) return false
  return !commands.some(item => named(item, name))
}

// Up to four commands the user may have meant: close names first (prefix, alias, words), then small typos.
export function closest(typed: string, commands: CatalogItem[]) {
  const name = typed.toLowerCase()
  return commands
    .filter(item => !item.name.startsWith('$'))
    .map(item => { const at = rank(item, name); return { item, at: at >= 0 ? at : 10 + distance(name, item.name.toLowerCase()) } })
    .filter(entry => entry.at < 10 + Math.max(2, Math.floor(name.length / 3)))
    .sort((a, b) => a.at - b.at)
    .slice(0, 4)
    .map(entry => entry.item)
}
