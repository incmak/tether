import { useCallback, useEffect, useRef, useState } from 'react'
import type { Pane } from './api'
import { ImageViewer } from './ImageViewer'
import type { Media } from './ImageViewer'
import { mediaPath, saveFile } from './MediaLink'
import { TextViewer } from './TextViewer'
import type { TextFile } from './TextViewer'
import { OfficeViewer } from './OfficeViewer'
import type { OfficeFile } from './OfficeViewer'
import { useHost } from './host'

export type { PanelProps } from './panels'

/** A load in flight this long is taken as stuck: the next tick aborts it and starts again. */
const STALLED_MS = 30_000
/** Run `load` now and every `ms` while the panel is active and the page is visible. A tick waits for a load in flight,
 * so a slow network still finishes one; only a stuck load is aborted. */
export function usePanelPoll(load: (signal: AbortSignal) => Promise<void>, ms: number, active: boolean) {
  const latest = useRef(load)
  useEffect(() => { latest.current = load })
  useEffect(() => {
    if (!active) return
    let controller = new AbortController()
    let timer = 0, started: number | null = null
    const tick = () => {
      window.clearTimeout(timer)
      if (document.visibilityState === 'visible' && (started === null || Date.now() - started > STALLED_MS)) {
        controller.abort()
        const own = controller = new AbortController()
        started = Date.now()
        void latest.current(own.signal).catch(() => {}).finally(() => { if (controller === own) started = null })
      }
      timer = window.setTimeout(tick, ms)
    }
    tick()
    const visible = () => { if (document.visibilityState === 'visible') tick() }
    document.addEventListener('visibilitychange', visible)
    return () => { window.clearTimeout(timer); controller.abort(); document.removeEventListener('visibilitychange', visible) }
  }, [active, ms, load])
}

/** The query that names a panel's folder: the folder when the frame passes one, and the pane so the server can fall back to the tab's folder. */
export const folderQuery = (pane: Pane, folder: string | null) => `session=${encodeURIComponent(pane.id)}${folder ? `&folder=${encodeURIComponent(folder)}` : ''}`

/** Open a file the way chat does: text and Office files in their viewers, media in the media viewer, PDFs in a new tab. GET /mobile/api/file decides what may open. */
export function useFileOpener(pane: Pane) {
  const host = useHost()
  const [media, setMedia] = useState<Media | null>(null)
  const [text, setText] = useState<TextFile | null>(null)
  const [office, setOffice] = useState<OfficeFile | null>(null)
  const open = useCallback(async (path: string) => {
    const url = `/mobile/api/file?session=${encodeURIComponent(pane.id)}&path=${encodeURIComponent(path)}`
    const name = path.split('/').pop() || path
    const kind = mediaPath(path)?.kind
    if (!kind) { setMedia({ name, url, error: 'Tether opens files with an extension only.' }); return }
    if (kind === 'text') { setText({ path, url }); return }
    try {
      const check = await fetch(url, { headers: { Range: 'bytes=0-0' } })
      if (!check.ok) { const body = await check.json().catch(() => null) as { error?: string } | null; setMedia({ name, url, error: body?.error ?? 'Tether cannot open this file.' }); return }
      void check.body?.cancel()
    } catch { setMedia({ name, url, error: `Cannot reach ${host}.` }); return }
    if (kind === 'office') setOffice({ path, url })
    else if (kind === 'file') saveFile(url, name)
    else if (kind === 'pdf') window.open(url, '_blank', 'noopener')
    else setMedia({ name, url, kind })
  }, [pane.id, host])
  const viewers = <>
    <ImageViewer image={media} close={() => setMedia(null)} />
    <TextViewer file={text} close={() => setText(null)} />
    <OfficeViewer file={office} close={() => setOffice(null)} />
  </>
  return [open, viewers] as const
}
