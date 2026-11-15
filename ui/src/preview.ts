import { createContext } from 'react'
import type { ChatMessage, ToolArtifact } from './api'

/** A page to show in the preview drawer. path: the local HTML file. remote: the claude.ai link of a published artifact. */
export interface PreviewTarget { path: string; title: string; remote?: string }

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
