import { keyLabel } from './keys'
import { BACK_KEY, FORWARD_KEY } from './navKeys'

export interface ShortcutGroup { name: string; items: readonly (readonly [label: string, key: string])[] }

// Only keys that work. ⌘J and ⌘I come with the Needs you list. Settings › Keyboard shortcuts shows the same groups.
export const SHORTCUT_GROUPS: readonly ShortcutGroup[] = [
  { name: 'Move', items: [
    ['Back', BACK_KEY],
    ['Forward', FORWARD_KEY],
    ['Chats, Files or Terminals', '⌘1–3'],
    ['Next pane that needs you', '⌘J'],
    ['Focus slot 1 to 4', '⌥1–4'],
    ['Previous or next session', '⌥↑ ⌥↓'],
    ['Next pinned chat', '⌥]'],
    ['Previous pinned chat', '⌥['],
    ['Sidebar', '⌘B'],
    ['Needs you list', '⌘I'],
    ['Home', '⌥H'],
  ] },
  { name: 'Search', items: [
    ['Search panes, commands and the backlog', '⌘K'],
    // The Claude desktop app, Raycast and Alfred can take ⌥Space for the whole Mac; ⌘K always reaches Tether.
    ['Search, also (when no other Mac app uses the key)', '⌥Space'],
    // Global Chrome command (extension/README.md). Chrome suggests only ⌘⇧ plus a digit; ⌘Space stays with Spotlight.
    ['Search from any app (Tether Chrome extension). Set another key at chrome://extensions/shortcuts', '⌘⇧9'],
    ['Move through the results', '↑ ↓'],
    ['Open the result', '↵'],
    ['Choose where a new tab starts', '→ or Tab'],
    ['Close the search', 'Esc'],
  ] },
  { name: 'Backlog', items: [
    ['Backlog', '⌥B'],
    ['Backlog as a full page', '⌥⇧B'],
    ['Search the backlog', '/'],
    ['Search the backlog page', '⌘F'],
    ['Clear the search', 'Esc'],
    ['Open the item’s details (full page)', '↵'],
    ['Next or previous item', '↑ ↓ J K'],
    ['Close the details', 'Esc'],
  ] },
  { name: 'Layout', items: [
    ['Split right', '⌘\\'],
    ['Grid of four', '⌘⇧\\'],
    ['Zoom or restore the focused pane', '⌥Z'],
    ['Close the focused slot', '⌥X'],
    ['Terminal view of the focused pane', '⌥T'],
    ['Terminal panel', '⌘.'],
    ['Side chat', '⌘;'],
    ['New session', '⌥N'],
    ['Make a layout', '⌥L'],
    ['Restore the default layout', '⌥⇧L'],
  ] },
  { name: 'Act', items: [
    ['Undo the last tab, session or stack step', '⌘Z'],
    ['Redo it', '⌘⇧Z'],
    ['Send', '↵'],
    ['New line', '⇧↵'],
    ['Voice input on or off', '⌥V'],
    ['Copy the session name', '⌥⇧S'],
    ['Copy the Claude session name', '⌥⇧C'],
    ['Copy the tmux pane name', '⌥⇧P'],
    ['Copy the tmux session name', '⌥⇧T'],
    ['Keyboard shortcuts', '⌘/'],
    ['Keyboard shortcuts, when no text box has focus', '?'],
  ] },
]

const words = (text: string) => text.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase()
/** The groups with only the rows that have every word of the filter, in the label, the key or the group name. */
export function filterShortcuts(groups: readonly ShortcutGroup[], filter: string): ShortcutGroup[] {
  const wanted = words(filter).trim().split(/\s+/).filter(Boolean)
  if (!wanted.length) return [...groups]
  return groups.map(group => ({ ...group, items: group.items.filter(([label, key]) => {
    const text = words(`${group.name} ${label} ${key} ${keyLabel(key)}`)
    return wanted.every(word => text.includes(word))
  }) })).filter(group => group.items.length > 0)
}
