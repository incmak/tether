// Tab names in agent replies open that tab. Agents name tabs in backticks (`B69 review`), in quotes ("Advisor: Triage")
// or by pane id (%71). Only a name that matches one live tab becomes a link; anything else stays plain text.
export interface TabLike { id: string; tmux_session: string; pane_name?: string; agent_name?: string }

const ID = /^%\d+$/

/** The tab a reply names, or null. A name shared by several tabs picks the one in the current session, else none. */
export function tabTarget<T extends TabLike>(text: string, panes: T[], current?: TabLike | null): T | null {
  const raw = text.trim().replace(/\s+tab$/i, '')
  if (!raw || raw.length > 80) return null
  if (ID.test(raw)) return panes.find(pane => pane.id === raw && pane.id !== current?.id) ?? null
  const wanted = raw.toLowerCase()
  // Only names a person or agent gave: the bare agent name ("Claude", "Shell") would match too many tabs.
  const found = panes.filter(pane => pane.id !== current?.id && [pane.pane_name, pane.agent_name]
    .some(name => !!name && name.trim().toLowerCase() === wanted))
  if (found.length === 1) return found[0]
  const here = found.filter(pane => current && pane.tmux_session === current.tmux_session)
  return here.length === 1 ? here[0] : null
}

type Node = { type: string; value?: string; url?: string; children?: Node[]; data?: { hProperties?: Record<string, string> } }
// "name", “name” and %71 in plain text. The quotes stay outside the link.
const QUOTED = /(["“])([^"“”\n]{1,80})(["”])|(?<![\w%])(%\d+)\b/g

/** Remark plugin: mark quoted text and pane ids as tab candidates. Link (MediaLink.tsx) links the ones that match a tab. */
export function remarkTabs() {
  const walk = (node: Node) => {
    if (!node.children || node.type === 'link' || node.type === 'linkReference') return
    node.children = node.children.flatMap(child => {
      if (child.type !== 'text' || !child.value) { walk(child); return [child] }
      const parts: Node[] = []
      let at = 0
      for (const match of child.value.matchAll(QUOTED)) {
        const start = match.index!
        const name = match[2] ?? match[4]
        const before = match[1] ?? ''
        if (start + before.length > at) parts.push({ type: 'text', value: child.value.slice(at, start + before.length) })
        parts.push({ type: 'link', url: '#', data: { hProperties: { 'data-tab': name } }, children: [{ type: 'text', value: name }] })
        at = start + match[0].length - (match[3] ?? '').length
      }
      if (!parts.length) return [child]
      if (at < child.value.length) parts.push({ type: 'text', value: child.value.slice(at) })
      return parts
    })
  }
  return (tree: Node) => walk(tree)
}
