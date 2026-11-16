import { useEffect, useRef, useState } from 'react'
import type { DragEvent } from 'react'

// Drag files from the desktop onto a chat to attach them. The chat panel finds the files here, then sends them to its
// composer through the 'tether-attach' event, the same path as the browser panel's "Send to chat".

// A drag that starts on this page (a tab, a slot header, an image in a message) is not a file drop, even when the
// browser lists 'Files' for it. Chrome does that for a dragged <img>. guardFileDrops marks such drags with this type.
// The mark travels with the drag, so a cancelled drag or a lost dragend leaves nothing behind.
const PAGE_DRAG = 'application/x-tether-page-drag'

/** True for files dragged in from outside the page. */
export function isFileDrag(types: readonly string[] | DOMStringList): boolean {
  const list = Array.from(types as ArrayLike<string>)
  return list.includes('Files') && !list.includes(PAGE_DRAG)
}

/** The files of a drop, without folders. A folder arrives as an empty File that no upload can read. */
export function droppedFiles(data: Pick<DataTransfer, 'files' | 'items'>): { files: File[]; folders: string[] } {
  const files = Array.from(data.files), folders: string[] = []
  const items = Array.from(data.items ?? []).filter(item => item.kind === 'file')
  // items and files list the same entries in the same order. Without the entry API, keep every file.
  if (items.length !== files.length) return { files, folders }
  const kept = files.filter((file, index) => {
    const entry = items[index].webkitGetAsEntry?.()
    if (entry?.isDirectory) { folders.push(file.name || entry.name); return false }
    return true
  })
  return { files: kept, folders }
}

/** Why a chat cannot take files now, or '' when it can. Mirrors canAttach in Composer.tsx. */
export function dropRefusal(pane: { agent: string; writable: boolean }, draft: string, loaded: boolean): string {
  if (pane.agent === 'shell') return 'A terminal tab does not take files. Drop them on an agent chat.'
  if (!pane.writable) return 'This chat is read-only, so it does not take files.'
  if (!loaded) return 'Wait for the chat to load, then drop the files again.'
  if (draft.startsWith('!')) return 'Leave shell mode (remove the “!”) to attach files.'
  return ''
}

/** What to say when a drop held folders. */
export function folderNote(folders: string[]): string {
  if (!folders.length) return ''
  const what = folders.length === 1 ? `“${folders[0]}” is a folder` : `${folders.length} items are folders`
  return `${what}, so ${folders.length === 1 ? 'it was' : 'they were'} not attached. Drop the files inside instead.`
}

/** Send files to the composer of one pane. False when no composer took them. */
export function sendToComposer(pane: string, files: File[]): boolean {
  return !window.dispatchEvent(new CustomEvent('tether-attach', { detail: { pane, files }, cancelable: true }))
}

let guarded = false
/**
 * A file dropped where nothing takes it must not open in the tab and replace Tether. Mark drags that start on the
 * page, so isFileDrag can tell them apart from files from the desktop.
 */
export function guardFileDrops(target: Window = window) {
  if (guarded) return
  guarded = true
  target.addEventListener('dragstart', event => { try { event.dataTransfer?.setData(PAGE_DRAG, '1') } catch { /* read-only data */ } }, true)
  target.addEventListener('dragover', event => {
    if (event.defaultPrevented || !event.dataTransfer || !isFileDrag(event.dataTransfer.types)) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'none'
  })
  target.addEventListener('drop', event => {
    if (!event.defaultPrevented && event.dataTransfer && isFileDrag(event.dataTransfer.types)) event.preventDefault()
  })
}

/**
 * Drop handlers for one chat panel. dropping is null with no file drag, '' when the chat takes the files, else the
 * reason it does not (shown on the panel while the files hover).
 */
export function useFileDrop(pane: string, refusal: string, notice: (text: string) => void) {
  const [dropping, setDropping] = useState<string | null>(null)
  // dragover repeats while files hover (the HTML spec allows a gap of up to 550 ms). When it stops for 1 s (the drag left, or the browser cancelled the drop without a
  // dragleave), the target goes away.
  const idle = useRef(0)
  const hide = () => { clearTimeout(idle.current); setDropping(null) }
  useEffect(() => () => clearTimeout(idle.current), [])
  const fileDrop = {
    onDragOver: (event: DragEvent) => {
      if (!isFileDrag(event.dataTransfer.types)) return
      // A drop zone inside the chat (Files, Share) took this drag: show its target, not the chat's.
      if (event.defaultPrevented) { if (dropping !== null) hide(); return }
      event.preventDefault()
      event.dataTransfer.dropEffect = refusal ? 'none' : 'copy'
      if (dropping !== refusal) setDropping(refusal)
      clearTimeout(idle.current)
      idle.current = window.setTimeout(() => setDropping(null), 1000)
    },
    onDragLeave: (event: DragEvent) => {
      const box = event.currentTarget.getBoundingClientRect(), to = event.relatedTarget as Node | null
      // Safari and some drags give no relatedTarget: then the pointer position tells if the drag left the panel.
      const within = to ? event.currentTarget.contains(to) : event.clientX > box.left && event.clientX < box.right && event.clientY > box.top && event.clientY < box.bottom
      if (!within) hide()
    },
    onDrop: (event: DragEvent) => {
      hide()
      if (event.defaultPrevented || !isFileDrag(event.dataTransfer.types)) return
      event.preventDefault()
      if (refusal) { notice(refusal); return }
      const { files, folders } = droppedFiles(event.dataTransfer)
      if (files.length && !sendToComposer(pane, files)) { notice('This chat cannot take files right now. Try the + button.'); return }
      if (folders.length) notice(folderNote(folders))
    },
  }
  return { dropping, fileDrop }
}
