import { useLayoutEffect } from 'react'

const TEXT_INPUT = new Set(['text', 'search', 'email', 'password', 'tel', 'url', 'number'])

function editable(element: Element | null) {
  if (element instanceof HTMLInputElement) return !element.readOnly && TEXT_INPUT.has(element.type)
  if (element instanceof HTMLTextAreaElement) return !element.readOnly
  return element instanceof HTMLElement && (element.isContentEditable || element.tagName === 'IFRAME')
}

// Keep the keyboard state in sync with focus and the measured viewport.
export function useKeyboardState() {
  useLayoutEffect(() => {
    const root = document.documentElement, view = window.visualViewport
    const baselines = new Map<number, number>()
    let disposed = false, stale = false, blurTimer = 0

    function check() {
      if (disposed) return
      const width = Math.round(view?.width ?? innerWidth), height = view?.height ?? innerHeight
      // A height drop with editable focus opens the keyboard. On blur, wait for the viewport to grow.
      // If the viewport stays small, clear the keyboard state after the blur timer.
      // At a new width with the keyboard open, use the screen height until it closes.
      // With the keyboard closed and no editable focus, the baseline follows the measured height.
      // With editable focus, the closed-keyboard baseline can only grow.
      const open = root.hasAttribute('data-keyboard'), known = baselines.get(width)
      const baseline = Math.max(height, known ?? (open ? screen.availHeight : height))
      const keyboard = height < baseline - 150 && (editable(document.activeElement) || (open && !stale))
      root.toggleAttribute('data-keyboard', keyboard)
      if (!keyboard) baselines.set(width, editable(document.activeElement) ? Math.max(height, known ?? height) : height)
      stale = false
    }

    const afterFocus = () => {
      queueMicrotask(check)
      clearTimeout(blurTimer)
      blurTimer = window.setTimeout(() => { if (!editable(document.activeElement)) { stale = true; check() } }, 700)
    }
    check()
    window.addEventListener('resize', check)
    view?.addEventListener('resize', check)
    document.addEventListener('focusin', check)
    document.addEventListener('focusout', afterFocus)
    return () => {
      disposed = true
      clearTimeout(blurTimer)
      window.removeEventListener('resize', check)
      view?.removeEventListener('resize', check)
      document.removeEventListener('focusin', check)
      document.removeEventListener('focusout', afterFocus)
      root.removeAttribute('data-keyboard')
    }
  }, [])
}
