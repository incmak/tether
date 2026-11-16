import { Bell, Bot, FolderOpen, Keyboard, Palette, SlidersHorizontal } from 'lucide-react'

export const SETTINGS_GROUPS = [
  { id: 'appearance', title: 'Appearance', description: 'Layout, style, theme, colors, text size and app icon', icon: Palette },
  { id: 'workspace', title: 'Sessions & chat', description: 'Default folder, sidebar and chat behavior', icon: FolderOpen },
  { id: 'voice', title: 'Voice & alerts', description: 'Microphone, read aloud and notifications', icon: Bell },
  { id: 'agents', title: 'Agents & browser', description: 'Autonomy, reviewers, Codex helper and the shared browser', icon: Bot },
  { id: 'shortcuts', title: 'Keyboard shortcuts', description: 'Every key, grouped by task', icon: Keyboard },
  { id: 'system', title: 'About & system', description: 'Updates, resources and reload', icon: SlidersHorizontal },
] as const
export type SettingsGroup = typeof SETTINGS_GROUPS[number]['id']
export interface SettingIndex { id: string; group: SettingsGroup; title: string; keywords: string }
export function matchesSetting(item: SettingIndex, query: string) {
  const group = SETTINGS_GROUPS.find(group => group.id === item.group)!
  const normalize = (text: string) => text.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase()
  const words = normalize(query).trim().split(/\s+/).filter(Boolean)
  const text = normalize(`${item.title} ${item.keywords} ${group.title}`)
  return words.every(word => text.includes(word))
}
