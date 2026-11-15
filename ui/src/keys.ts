export const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
/** Key labels use Mac symbols. Off a Mac, ⌘ shows as Ctrl, ⌥ as Alt and ⇧ as Shift. */
export const keyLabel = (key: string) => IS_MAC ? key : key.replace(/⌘/g, 'Ctrl ').replace(/⌥/g, 'Alt ').replace(/⇧/g, 'Shift ').replace(/ {2,}/g, ' ').trim()

/** The focused slot's terminal view: the same button as in the slot header. */
export function toggleTerminal() {
  document.querySelector<HTMLButtonElement>('.slot.focused .terminal-toggle')?.click()
}

/** Voice input on or off in the focused slot's message box (⌥V). Returns false when no mic button can take it. */
export function toggleVoice() {
  const pick = (scope: string) => [...document.querySelectorAll<HTMLButtonElement>(`${scope} form.composer .mic`)].find(button => button.offsetParent && !button.disabled)
  const button = pick('.slot.focused') ?? pick('')
  button?.click()
  return !!button
}
