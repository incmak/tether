// A deploy replaces the hashed files in dist/. A page loaded before the deploy then asks for a file that is gone, and the
// lazy import fails. Vite reports this as `vite:preloadError` (https://vite.dev/guide/build#load-error-handling).
// Reload once to get the new files. A second failure within a minute is a real fault: let it reach CrashScreen.

const KEY = 'tether.stale-chunk-reload'
const WINDOW_MS = 60_000

export function shouldReload(last: string | null, now: number) {
  const at = Number(last)
  return !Number.isFinite(at) || at <= 0 || now - at > WINDOW_MS
}

export function reloadOnStaleChunk() {
  window.addEventListener('vite:preloadError', event => {
    // Without storage a reload could loop, so the error goes to CrashScreen, which offers Reload.
    try {
      if (!shouldReload(sessionStorage.getItem(KEY), Date.now())) return
      sessionStorage.setItem(KEY, String(Date.now()))
    } catch { return }
    event.preventDefault()
    location.reload()
  })
}
