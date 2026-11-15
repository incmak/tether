import { useEffect, useRef, useState } from 'react'
import { Search, X } from 'lucide-react'
import { useModal } from './hooks'
import { IS_MAC, keyLabel } from './keys'
import { filterShortcuts, SHORTCUT_GROUPS } from './shortcutList'
import type { ShortcutGroup } from './shortcutList'

/** One group's keys as a list of label and key. */
export function ShortcutRows({ group }: { group: ShortcutGroup }) {
  return <dl className="shortcuts-rows">{group.items.map(([label, key]) => <div key={label}><dt>{label}</dt><dd><kbd>{keyLabel(key)}</kbd></dd></div>)}</dl>
}

/** The keyboard shortcuts sheet. ⌘/ opens it from anywhere; ? opens it when no text box has focus. */
export function Shortcuts({ onClose }: { onClose: () => void }) {
  const modal = useModal(true, onClose)
  // The control that had focus before the sheet opened. Read during the first render, before the sheet takes focus.
  const [opener] = useState(() => document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null)
  // The sheet unmounts without dialog.close(), so focus would land on <body>: give it back to the opener,
  // or to the dialog under the sheet (the backlog, Settings) when the opener is gone.
  useEffect(() => () => { requestAnimationFrame(() => {
    if (document.activeElement && document.activeElement !== document.body) return
    if (opener?.isConnected && !opener.closest('[inert]')) { opener.focus(); if (document.activeElement === opener) return }
    const under = [...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].pop()
    const target = under?.querySelector<HTMLElement>('[aria-current="true"], [autofocus], input:not([disabled]), button:not([disabled])')
    if (target) target.focus(); else under?.focus()
  }) }, [opener])
  const [filter, setFilter] = useState('')
  const find = useRef<HTMLInputElement>(null)
  // A phone keeps its keyboard closed: the cursor goes to Close there.
  useEffect(() => { if (window.matchMedia('(pointer: fine)').matches) find.current?.focus() }, [])
  const shown = filterShortcuts(SHORTCUT_GROUPS, filter)
  return <dialog {...modal} className="sheet shortcuts-sheet" aria-labelledby="shortcuts-title">
    <div className="sheet-heading"><h2 id="shortcuts-title">Keyboard shortcuts</h2>{IS_MAC && <span className="shortcuts-note">⌘ is Ctrl on Windows and Linux</span>}<button type="button" className="icon-button" aria-label="Close" autoFocus onClick={onClose}><X /></button></div>
    <label className="shortcuts-filter"><Search size={15} aria-hidden="true" />
      <input ref={find} type="search" value={filter} placeholder="Find a shortcut" aria-label="Find a shortcut" autoComplete="off" spellCheck={false}
        onChange={event => setFilter(event.target.value)}
        onKeyDown={event => { if (event.key === 'Escape' && filter) { event.preventDefault(); event.stopPropagation(); setFilter('') } }} />
    </label>
    <div className="shortcuts-groups">
      {shown.map(group => <section key={group.name}><h3>{group.name}</h3><ShortcutRows group={group} /></section>)}
    </div>
    {!shown.length && <p className="shortcuts-empty" role="status">No shortcut matches “{filter.trim()}”.</p>}
    <p className="hint">Mouse buttons 4 and 5 and a two-finger swipe on a Mac trackpad also go Back and Forward. Over a chat’s header, the swipe scrolls the tabs and stacks instead (Settings › Sessions & chat). Typing anywhere else moves to the message box. Drag a divider to resize. Double-click it to make the panes equal again. Settings › Keyboard shortcuts has this list too.</p>
  </dialog>
}
