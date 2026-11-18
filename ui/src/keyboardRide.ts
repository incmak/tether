import { useLayoutEffect } from 'react'

const TEXT_INPUT = new Set(['text', 'search', 'email', 'password', 'tel', 'url', 'number'])

function editable(element: Element | null) {
  if (element instanceof HTMLInputElement) return !element.readOnly && TEXT_INPUT.has(element.type)
  if (element instanceof HTMLTextAreaElement) return !element.readOnly
  return element instanceof HTMLElement && (element.isContentEditable || element.tagName === 'IFRAME')
}

export function useKeyboardRide(shell: HTMLElement | null) {
  useLayoutEffect(() => {
    if (!shell) return
    const root = document.documentElement, view = window.visualViewport
    const reduced = matchMedia('(prefers-reduced-motion: reduce)')
    const baselines = new Map<number, number>()
    const target = () => ({ H: root.clientHeight, bottom: parseFloat(getComputedStyle(shell).bottom) })
    let settled = target()
    let ride: { animation: Animation; H: number; from: number; to: number } | undefined
    let disposed = false

    function check() {
      if (!shell || disposed) return
      const width = Math.round(view?.width ?? innerWidth), height = view?.height ?? innerHeight
      const baseline = Math.max(height, baselines.get(width) ?? height)
      baselines.set(width, baseline)
      root.toggleAttribute('data-keyboard', editable(document.activeElement) && height < baseline - 150)

      let edge = settled.H - settled.bottom
      if (ride) {
        // Use the old viewport and the eased progress to recover the edge before the resize.
        const { animation, H, from, to } = ride
        const progress = animation.effect?.getComputedTiming().progress ?? (animation.playState === 'finished' ? 1 : 0)
        edge = H - (from + (to - from) * progress)
        animation.cancel()
        ride = undefined
      }
      settled = target()
      const { H, bottom } = settled
      if (!shell.classList.contains('phone-tabs') || reduced.matches || typeof shell.animate !== 'function' || Math.abs(H - bottom - edge) < 1) return
      const from = H - edge
      const animation = shell.animate({ bottom: [`${from}px`, `${bottom}px`] }, { duration: 285, easing: 'cubic-bezier(.2, 0, 0, 1)' })
      ride = { animation, H, from, to: bottom }
      animation.onfinish = () => { if (ride?.animation === animation) ride = undefined }
    }

    const afterFocus = () => queueMicrotask(check)
    check()
    window.addEventListener('resize', check)
    view?.addEventListener('resize', check)
    document.addEventListener('focusin', check)
    document.addEventListener('focusout', afterFocus)
    reduced.addEventListener('change', check)
    return () => {
      disposed = true
      window.removeEventListener('resize', check)
      view?.removeEventListener('resize', check)
      document.removeEventListener('focusin', check)
      document.removeEventListener('focusout', afterFocus)
      reduced.removeEventListener('change', check)
      ride?.animation.cancel()
      root.removeAttribute('data-keyboard')
    }
  }, [shell])
}
