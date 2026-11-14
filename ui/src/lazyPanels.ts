import { lazy } from 'react'

// Panels that render only after a tap: Files (desktop and phone), Share and Live mode. Each is its own chunk, so the
// first load on a slow network does not download them (B26). Vite's `vite:preloadError` covers a chunk that a deploy
// removed (staleChunk.ts).
export const loaders = {
  explorer: () => import('./Explorer'),
  explorerPhone: () => import('./ExplorerPhone'),
  share: () => import('./Share'),
  live: () => import('./Live'),
}

export const Explorer = lazy(() => loaders.explorer().then(module => ({ default: module.Explorer })))
export const ExplorerPhone = lazy(() => loaders.explorerPhone().then(module => ({ default: module.ExplorerPhone })))
export const Share = lazy(() => loaders.share().then(module => ({ default: module.Share })))
export const Live = lazy(() => loaders.live().then(module => ({ default: module.Live })))

type Idle = (run: () => void) => void
const whenIdle: Idle = run => {
  if (typeof requestIdleCallback === 'function') requestIdleCallback(run, { timeout: 10_000 })
  else setTimeout(run, 3_000)
}

/** After the first screen is up, prefetch the files of the screens this layout opens into the HTTP cache, one screen
 *  per idle slot, so a later tap is fast. The build lists the files in index.html (vite.config.ts panelManifest).
 *  A prefetch only warms the cache: it does not run Vite's import helper, so a failure is silent and the later tap loads
 *  the files as if there had been no prefetch. Live mode is not fetched: it is large and rarely opened.
 *  Save-Data turns this off. */
export function prefetchPanels(wide: boolean, idle: Idle = whenIdle, saveData = (navigator as { connection?: { saveData?: boolean } }).connection?.saveData === true, doc: Document = document) {
  if (saveData) return
  let files: Record<string, string[]>
  try { files = JSON.parse(doc.getElementById('panel-files')?.textContent ?? '{}') } catch { return }
  const queue = (wide ? ['explorer', 'share'] : ['explorerPhone', 'share']).map(key => files[key] ?? []).filter(urls => urls.length)
  const link = doc.createElement('link')
  const native = typeof link.relList?.supports === 'function' && link.relList.supports('prefetch')
  const next = () => {
    const urls = queue.shift()
    if (!urls) return
    idle(() => {
      for (const href of urls) {
        if (native) {
          const hint = doc.createElement('link')
          hint.rel = 'prefetch'
          // Vite loads chunks and their CSS in CORS mode (crossorigin). A hint in another mode is cached apart and the tap
          // downloads the file again.
          hint.crossOrigin = ''
          hint.href = href
          doc.head.append(hint)
        } else {
          // Safari has no rel=prefetch. A low-priority fetch fills the same HTTP cache (assets are immutable).
          void fetch(href, { priority: 'low', credentials: 'same-origin' } as RequestInit).catch(() => {})
        }
      }
      next()
    })
  }
  next()
}
