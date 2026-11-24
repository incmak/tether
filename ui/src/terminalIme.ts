type ImeTerm = {
  element?: HTMLElement
  input?: (data: string, wasUserInput: boolean) => void
  _core?: { coreService?: { triggerDataEvent?: (data: string, wasUserInput: boolean) => void } }
  onKey?: (listener: (event: { domEvent: KeyboardEvent }) => void) => unknown
}

export function imeDiff(sent: string, next: string): string {
  const before = Array.from(sent), after = Array.from(next)
  let prefix = 0
  while (prefix < before.length && before[prefix] === after[prefix]) prefix++
  return '\x7f'.repeat(before.length - prefix) + after.slice(prefix).join('').replace(/\r?\n/g, '\r')
}

export function installTerminalIme(win: Window, term: ImeTerm): void {
  const marked = win as Window & { __tetherIme?: boolean }
  // Android sends keyCode 229 for soft keyboard input, which takes xterm's broken textarea diff path.
  if (marked.__tetherIme || !/Android/i.test(win.navigator.userAgent)) return
  const textarea = term.element?.querySelector<HTMLTextAreaElement>('.xterm-helper-textarea')
  if (!textarea) return
  let send
  if (typeof term.input === 'function') send = term.input.bind(term)
  else {
    const core = term._core?.coreService
    if (typeof core?.triggerDataEvent === 'function') send = core.triggerDataEvent.bind(core)
  }
  if (!send) {
    console.warn('Android terminal IME fix is off. The ttyd/xterm input API changed (expected ttyd 1.7.4).')
    return
  }
  marked.__tetherIme = true

  // Chromium resets the caret after a commit when the helper still has its initial zero size.
  textarea.style.minWidth = '1px'
  textarea.style.minHeight = '1px'

  let sent = textarea.value
  let sentKeypress = false
  term.onKey?.(({ domEvent }) => {
    if (domEvent.type === 'keypress' && domEvent.target === textarea) sentKeypress = true
  })
  const isImeKey = (event: Event) => event.type === 'keydown' && (event as KeyboardEvent).keyCode === 229
  const capture = (event: Event) => {
    if (event.target !== textarea) return
    if (event.type === 'keydown') {
      sentKeypress = false
      if (!isImeKey(event)) return
    }
    event.stopImmediatePropagation()
    if (event.type !== 'input') return
    const next = textarea.value
    // xterm can send a hardware keypress before Chromium inserts the same character.
    if (!sentKeypress || (event as InputEvent).inputType !== 'insertText') {
      const data = imeDiff(sent, next)
      if (data) send(data, true)
    }
    sentKeypress = false
    sent = next
  }
  for (const type of ['compositionstart', 'compositionupdate', 'compositionend', 'input', 'keydown']) {
    win.addEventListener(type, capture, true)
  }

  const resync = (event: Event) => {
    if (event.target === textarea && !isImeKey(event)) sent = textarea.value
  }
  textarea.addEventListener('keyup', () => { sentKeypress = false })
  for (const type of ['keydown', 'paste', 'blur', 'focusout']) win.addEventListener(type, resync)
  // Enter and paste stop propagation in xterm, and blur does not bubble. Run after xterm on the target too.
  textarea.addEventListener('keydown', resync, true)
  textarea.addEventListener('paste', resync)
  textarea.addEventListener('blur', resync)
}
