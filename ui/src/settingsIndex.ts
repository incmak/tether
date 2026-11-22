import { Bell, Bot, Brain, Clock, Globe, GitMerge, History, Info, Keyboard, Mic, Palette, PanelsTopLeft, Sparkles, SquareMousePointer } from 'lucide-react'

// One row per page in the Settings nav, in nav order. A small heading names each section above its first page.
// Ids are stable: Settings `start` and tests use them. A page's description is its subtitle and its phone index line.
export const SETTINGS_GROUPS = [
  { id: 'general', section: 'Tether', title: 'General', description: 'Time zone, default folder and restarts', icon: Globe },
  { id: 'appearance', section: 'Tether', title: 'Appearance', description: 'Theme, colors, text size, layout and app icon', icon: Palette },
  { id: 'workspace', section: 'Tether', title: 'Sidebar & chat', description: 'Session order, tab bar, replies and attention', icon: PanelsTopLeft },
  { id: 'voice', section: 'Tether', title: 'Voice', description: 'Microphone, read aloud and the assistant', icon: Mic },
  { id: 'notifications', section: 'Tether', title: 'Notifications', description: 'Push alerts on your devices', icon: Bell },
  { id: 'agents', section: 'Agents', title: 'Autonomy', description: 'How far agents go alone, setup and done tasks', icon: Bot },
  { id: 'helpers', section: 'Agents', title: 'Models & helpers', description: 'Main model, advisors, reviewers and Codex', icon: Sparkles },
  { id: 'gates', section: 'Agents', title: 'Commit & merge', description: 'The commit gate and auto-merge', icon: GitMerge },
  { id: 'upkeep', section: 'Agents', title: 'Learning & upkeep', description: 'Lessons, clean-up and the daily ops report', icon: Brain },
  { id: 'browser', section: 'Agents', title: 'Shared browser', description: 'The browser that agents use', icon: SquareMousePointer },
  { id: 'activity', section: 'More', title: 'Activity log', description: 'What each device and agent changed, and when', icon: History },
  { id: 'usage', section: 'More', title: 'Usage tracking', description: 'Coding time and which controls you use', icon: Clock },
  { id: 'shortcuts', section: 'More', title: 'Keyboard shortcuts', description: 'Every key, grouped by task', icon: Keyboard },
  { id: 'system', section: 'More', title: 'About & updates', description: 'Version, updates, resources and reload', icon: Info },
] as const
export type SettingsGroup = typeof SETTINGS_GROUPS[number]['id']
export interface SettingIndex { id: string; group: SettingsGroup; title: string; keywords: string }
export function matchesSetting(item: SettingIndex, query: string) {
  const group = SETTINGS_GROUPS.find(group => group.id === item.group)!
  const normalize = (text: string) => text.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLocaleLowerCase()
  const words = normalize(query).trim().split(/\s+/).filter(Boolean)
  const text = normalize(`${item.title} ${item.keywords} ${group.title}`)
  return words.every(word => text.includes(word))
}
