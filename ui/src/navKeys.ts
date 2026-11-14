import { IS_MAC } from './keys'

/** Shortcut labels for Back and Forward: ⌘[ ⌘] on a Mac (Safari and Chrome use them), Alt+← Alt+→ elsewhere. */
export const BACK_KEY = IS_MAC ? '⌘[' : '⌥←'
export const FORWARD_KEY = IS_MAC ? '⌘]' : '⌥→'

type KeyInfo = Pick<KeyboardEvent, 'code' | 'key' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'target'>

/**
 * 'back' or 'forward' for Tether's Back and Forward keys, else null. On a Mac, Alt+← moves by word, so only ⌘[ and ⌘]
 * count there. Keys pressed in a text field, a select, an editable element or the terminal stay with that element.
 */
export function isNavKey(event: KeyInfo, mac = IS_MAC): 'back' | 'forward' | null {
  if (event.shiftKey || event.ctrlKey) return null
  const target = event.target as Element | null
  if (target?.closest?.('input, textarea, select, [contenteditable=""], [contenteditable="true"], .xterm')) return null
  if (mac) {
    if (!event.metaKey || event.altKey) return null
    return event.code === 'BracketLeft' ? 'back' : event.code === 'BracketRight' ? 'forward' : null
  }
  if (!event.altKey || event.metaKey) return null
  return event.key === 'ArrowLeft' ? 'back' : event.key === 'ArrowRight' ? 'forward' : null
}
