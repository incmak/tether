import { X } from 'lucide-react'
import { useModal } from './hooks'
import { IS_MAC, keyLabel } from './keys'
import { BACK_KEY, FORWARD_KEY } from './navKeys'

// Only keys that work in the desktop workspace. ⌘J and ⌘I come with the Needs you list.
const GROUPS = [
  { name: 'Move', items: [
    ['Back: close the top panel, or the previous chat or screen', BACK_KEY],
    ['Forward', FORWARD_KEY],
    ['Chats, Files or Terminals', '⌘1–3'],
    ['Search panes, commands and the backlog', '⌘K'],
    ['Search from anywhere', '⌥Space'],
    ['Next pane that needs you', '⌘J'],
    ['Focus slot 1 to 4', '⌥1–4'],
    ['Previous or next session', '⌥↑ ⌥↓'],
    ['Next pinned chat', '⌥]'],
    ['Previous pinned chat', '⌥['],
    ['Sidebar', '⌘B'],
    ['Needs you list', '⌘I'],
    ['Home', '⌥H'],
    ['Backlog', '⌥B'],
    ['Backlog as a full page', '⌥⇧B'],
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
    ['Send', '↵'],
    ['New line', '⇧↵'],
    ['Voice input on or off', '⌥V'],
    ['Copy the session name', '⌥⇧S'],
    ['Copy the Claude session name', '⌥⇧C'],
    ['Copy the tmux pane name', '⌥⇧P'],
    ['Copy the tmux session name', '⌥⇧T'],
    ['This list', '?'],
  ] },
] as const

/** The keyboard shortcuts sheet. `?` opens it when no text box has focus. */
export function Shortcuts({ onClose }: { onClose: () => void }) {
  const modal = useModal(true, onClose)
  return <dialog {...modal} className="sheet shortcuts-sheet" aria-labelledby="shortcuts-title">
    <div className="sheet-heading"><h2 id="shortcuts-title">Keyboard shortcuts</h2>{IS_MAC && <span className="shortcuts-note">⌘ is Ctrl on Windows and Linux</span>}<button type="button" className="icon-button" aria-label="Close" autoFocus onClick={onClose}><X /></button></div>
    <div className="shortcuts-groups">
      {GROUPS.map(group => <section key={group.name}>
        <h3>{group.name}</h3>
        <dl>{group.items.map(([label, key]) => <div key={key}><dt>{label}</dt><dd><kbd>{keyLabel(key)}</kbd></dd></div>)}</dl>
      </section>)}
    </div>
    <p className="hint">Mouse buttons 4 and 5 and a two-finger swipe on a Mac trackpad also go Back and Forward. Typing anywhere else moves to the message box. Drag a divider to resize. Double-click it to make the panes equal again.</p>
  </dialog>
}
