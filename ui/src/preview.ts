import { createContext } from 'react'
import type { ChatMessage, ToolArtifact } from './api'

/** A page to show in the preview drawer. path: the local HTML file. remote: the claude.ai link of a published artifact.
 * manage: no page; the drawer lists the dev servers that run now, to preview or stop (the chat's ... menu). */
export interface PreviewTarget { path: string; title: string; remote?: string; dev?: DevServer; manage?: boolean }
/** The drawer's "Dev servers" view. Each open makes a new object, so the view starts fresh. */
export const devServersTarget = (): PreviewTarget => ({ path: 'dev-servers', title: 'Dev servers', manage: true })
/** A dev server on the agent's machine: its port, and the path to open on it. */
export interface DevServer { port: number; path: string }

/** Chat.tsx provides this: open a page in the drawer, find the local file of a claude.ai link. */
export interface PreviewActions {
  open: (target: PreviewTarget) => void
  artifactFor: (url: string) => ToolArtifact | undefined
}
export const PreviewOpener = createContext<PreviewActions | null>(null)

const ARTIFACT_URL = /^https:\/\/claude\.ai\/(?:code\/)?artifact\/[\w-]{6,80}$/
/** A claude.ai artifact link, without a query or fragment, or null. */
export function artifactUrl(href: string) {
  const bare = href.trim().split(/[?#]/)[0].replace(/\/$/, '')
  return ARTIFACT_URL.test(bare) ? bare : null
}


/** The newest artifact for each claude.ai link in a chat: a page published again keeps its link. */
export function artifactsOf(messages: ChatMessage[]) {
  const found = new Map<string, ToolArtifact>()
  const walk = (list: ChatMessage[]) => {
    for (const message of list) {
      for (const tool of message.tools ?? []) if (tool.artifact) found.set(tool.artifact.url, tool.artifact)
      if (message.members) walk(message.members)
    }
  }
  walk(messages)
  return found
}

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '0.0.0.0', '[::1]'])
const BARE_LOCAL = /^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d{2,5}(?:[/?#]\S*)?$/
/** A link to a dev server on the agent's machine (http://localhost:5173/app), which the phone cannot open itself: the
 *  preview drawer opens it through Tether (server/dev_previews.py). null for other links, and for a link with no port
 *  or a system port (Tether forwards ports above 1024 only). bare: also read "localhost:5173", as agents write it in code. */
export function localUrl(href: string, bare = false): PreviewTarget | null {
  const text = href.trim()
  if (/\s/.test(text) || text.length > 2048) return null
  let url: URL
  try { url = new URL(bare && BARE_LOCAL.test(text) ? `http://${text}` : text) } catch { return null }
  const port = Number(url.port)
  if (!/^https?:$/.test(url.protocol) || !LOCAL_HOSTS.has(url.hostname) || url.username || url.password || port <= 1024) return null
  const path = url.pathname + url.search + url.hash
  return { path: `localhost:${port}${path === '/' ? '' : path}`, title: `localhost:${port}`, dev: { port, path } }
}
