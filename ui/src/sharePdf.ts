// PDF pages for Share, drawn in the browser with pdf.js. Loaded only when a PDF is shown, so the main bundle stays small.
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist'

let lib: Promise<typeof import('pdfjs-dist/legacy/build/pdf.mjs')> | null = null
function pdfjs() {
  // Use the builds that include polyfills for browsers without the latest JavaScript APIs.
  lib ??= Promise.all([import('pdfjs-dist/legacy/build/pdf.mjs'), import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url')]).then(([module, worker]) => {
    module.GlobalWorkerOptions.workerSrc = worker.default
    return module
  })
  return lib
}

/** Opens a PDF by URL. Share answers Range requests, so pdf.js reads only the parts it needs. */
export async function openPdf(url: string) {
  const { getDocument } = await pdfjs()
  return getDocument({ url, withCredentials: true, enableXfa: false, rangeChunkSize: 256 * 1024 }).promise
}

/** Draws one page at `width` CSS pixels into `canvas`. Returns a cancel function for the running draw. */
export function drawPage(page: PDFPageProxy, canvas: HTMLCanvasElement, width: number, ratio = window.devicePixelRatio || 1) {
  const base = page.getViewport({ scale: 1 })
  const viewport = page.getViewport({ scale: (width / base.width) * ratio })
  canvas.width = Math.floor(viewport.width); canvas.height = Math.floor(viewport.height)
  const task = page.render({ canvas, viewport })
  return { done: task.promise, cancel: () => task.cancel() }
}

/** Page 1 as a JPEG, longest side 480 px, for a Share tile. */
export async function pdfThumb(url: string) {
  let doc: PDFDocumentProxy | null = null
  try {
    doc = await openPdf(url)
    const page = await doc.getPage(1)
    const base = page.getViewport({ scale: 1 })
    const canvas = document.createElement('canvas')
    const width = base.width >= base.height ? 480 : 480 * base.width / base.height
    const context = canvas.getContext('2d')
    await drawPage(page, canvas, width, 1).done
    // pdf.js leaves transparent pages transparent; JPEG would turn them black.
    if (context) { context.globalCompositeOperation = 'destination-over'; context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height) }
    return await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.8))
  } catch {
    return null
  } finally {
    void doc?.loadingTask.destroy()
  }
}
