import { terminalText } from './terminalText'
import { shortFolder } from './paneState'
import { keyLabel } from './keys'
import { history, loadHistory, remember } from './history'
import { chatTitle } from './Sidebar'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { placeAttachLayer } from './attachLayer'
import { ArrowUp, Blocks, Camera, CornerDownLeft, FolderOpen, Smartphone, ImagePlus, Mic, Paperclip, Plus, Square, X, AtSign, Slash } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { takeShared, doneShared, releaseShared } from './shared'
import { useBackStep } from './backNav'
import { canPickFolder, forgetFolder, latestScreenshot } from './screenshot'
import { Suggestions } from './Suggest'
import type { SuggestHandle } from './Suggest'
import { ImageViewer } from './ImageViewer'
import { useDictation } from './useDictation'
import { useHost } from './host'
import { PasteChip } from './Pasted'
import { inlinePaste, joinPastes, longPaste, pasteOf } from './pasteText'
import type { Paste } from './pasteText'












async function prepareFile(file: File): Promise<{ blob: Blob; name: string }> {
  const kind = file.type
  if (!kind.startsWith('image/') || kind === 'image/gif' || kind === 'image/svg+xml') return { blob: file, name: file.name || 'file' }
  const isPng = kind === 'image/png'
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
    const scale = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height))
    if (isPng && scale === 1 && file.size <= 4 * 1024 * 1024) { bitmap.close(); return { blob: file, name: file.name || 'screenshot.png' } }
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale)
    canvas.getContext('2d')?.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    bitmap.close()
    const type = isPng ? 'image/png' : 'image/jpeg'
    const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, type, 0.9))
    if (!blob) throw new Error('encode failed')
    return { blob, name: `${(file.name || 'photo').replace(/\.[^.]*$/, '') || 'photo'}${isPng ? '.png' : '.jpg'}` }
  } catch { return { blob: file, name: file.name || 'file' } }
}
export async function uploadFile(file: File, session: string, thread: string | null): Promise<{ path: string; name: string }> {
  const { blob, name } = await prepareFile(file)
  const response = await fetch('/mobile/api/upload', { method: 'POST', body: blob, credentials: 'same-origin', headers: {
    'X-Codex-Mobile': '1', 'X-Session': session, 'X-Thread': thread ?? '', 'X-Filename': encodeURIComponent(name), 'Content-Type': blob.type || 'application/octet-stream' } })
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Sign in again before you attach files.')
  const data = await response.json() as { path?: string; name?: string; error?: string }
  if (!response.ok || !data.path) throw new Error(data.error || 'Could not attach that file.')
  return { path: data.path, name: name }
}

interface ComposerProps { onTerminalEscape?: () => void; api: Api; pane: Pane; thread: string | null; disabled: boolean; onSend: (text: string) => Promise<boolean>; error: string; errorAction?: React.ReactNode; draft: string; setDraft: (text: string) => void; working: boolean; onStop: () => void; micOk: boolean; hint?: string; chime: boolean; options?: React.ReactNode; onTools: () => void; handsFree: boolean; listenRequest: number; sent?: string[] }
// With no accept list, Chrome on Android opens the system app chooser (Camera, Camcorder, Media picker).
// showOpenFilePicker (Chrome 132+ on Android) opens the Files app directly, with every file type.
// Older Android Chrome falls back to a list of document types, which also skips the chooser.
const ANDROID = typeof navigator !== 'undefined' && /Android/i.test(navigator.userAgent)
const FILES_ACCEPT = ANDROID ? 'application/*,text/*' : undefined
type OpenPicker = (options: { multiple: boolean }) => Promise<{ getFile(): Promise<File> }[]>
const openPicker = () => ANDROID ? (window as unknown as { showOpenFilePicker?: OpenPicker }).showOpenFilePicker : undefined
export function Composer({ onTerminalEscape, api, pane, thread, disabled, onSend, error, errorAction, draft, setDraft, working, onStop, micOk, chime, options, onTools, hint, handsFree, listenRequest, sent = [] }: ComposerProps) {
  const host = useHost()
  const [menu, setMenu] = useState(false)
  useBackStep(menu, () => setMenu(false))
  const hintId = useId()
  const camera = useRef<HTMLInputElement>(null)
  const photos = useRef<HTMLInputElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!menu) return
    const dismiss = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(false) }
    document.addEventListener('click', dismiss)
    return () => document.removeEventListener('click', dismiss)
  }, [menu])
  // The sheet sits in the top layer, above every chat overlay on every layout (attachLayer.ts).
  const layer = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const box = layer.current, anchor = menuRef.current
    if (!menu || !box || !anchor) return
    const place = () => placeAttachLayer(box, anchor, innerHeight)
    place()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [menu])
  // Start a "/" or "@" suggestion from the menu: "/" only works at the start, "@" goes after the text.
  function insertTrigger(mark: '@' | '/') {
    setMenu(false)
    setDraft(mark === '/' ? '/' + draft.replace(/^\//, '') : draft + (draft && !/\s$/.test(draft) ? ' ' : '') + '@')
    requestAnimationFrame(() => { const box = textarea.current; if (box) { box.focus(); box.setSelectionRange(mark === '/' ? 1 : box.value.length, mark === '/' ? 1 : box.value.length) } })
  }
  // Gboard's image history cannot reach a web text box, so read the newest file in the Screenshots folder instead.
  const [hasFolder, setHasFolder] = useState(() => { try { return localStorage.getItem('tether-screenshot-folder') === '1' } catch { return false } })
  const [files, setFiles] = useState<{ name: string; path: string; url?: string }[]>([])
  const [viewing, setViewing] = useState<{ name: string; url: string } | null>(null)
  const [uploading, setUploading] = useState('')
  const picker = useRef<HTMLInputElement>(null)
  const suggest = useRef<SuggestHandle>(null)
  // A line that starts with "!" runs as a shell command in the agent's terminal on the server. The box shows it as a terminal prompt.
  const shellMode = pane.agent !== 'shell' && draft.startsWith('!')
  const terminalInput = shellMode || pane.agent === 'shell'
  const canAttach = pane.agent !== 'shell' && pane.writable && !shellMode
  // Long pastes wait as badges above the box. Tap one to read it in place.
  const [pastes, setPastes] = useState<Paste[]>([])
  const [openPaste, setOpenPaste] = useState<number | null>(null)
  const [attachmentContext, setAttachmentContext] = useState({ pane: pane.id, thread })
  if (attachmentContext.pane !== pane.id || attachmentContext.thread !== thread) {
    setAttachmentContext({ pane: pane.id, thread }); setFiles([]); setPastes([]); setOpenPaste(null)
  }
  const [sending, setSending] = useState(false)
  const form = useRef<HTMLFormElement>(null)
  const dictation = useDictation({ text: draft, setText: setDraft, chime, handsFree,
    onHandsFreeDone: () => form.current?.requestSubmit(), onFinished: () => textarea.current?.focus() })
  const { listening, transcribing, seconds, error: speechError, setError: setSpeechError, canRecord, toggle: toggleMic } = dictation
  // Upload files in order and return how many worked. onSaved runs after each good upload.
  async function attach(list: FileList | File[], onSaved?: (index: number) => void): Promise<number> {
    const chosen = Array.from(list)
    if (!chosen.length) return 0
    setSpeechError('')
    let count = 0
    try {
      for (const [index, file] of chosen.entries()) {
        setUploading(`Uploading ${chosen.length > 1 ? `${index + 1}/${chosen.length} ` : ''}${file.name || 'file'}…`)
        const saved = await uploadFile(file, pane.id, thread)
        setFiles(current => [...current, { ...saved, url: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined }])
        onSaved?.(index); count++
      }
    } catch (failure) { setSpeechError(errorText(failure)) } finally { setUploading('') }
    textarea.current?.focus()
    return count
  }
  async function attachLatest() {
    setMenu(false)
    if (!canPickFolder()) { photos.current?.click(); return }
    try {
      const file = await latestScreenshot()
      if (!file) return
      try { localStorage.setItem('tether-screenshot-folder', '1') } catch { /* private mode */ }
      setHasFolder(true)
      await attach([file])
    } catch (failure) { setSpeechError(errorText(failure)) }
  }
  const filesFallback = useRef(false)
  async function pickFiles() {
    setMenu(false)
    const open = openPicker()
    if (!open || filesFallback.current) { picker.current?.click(); return }
    // Step names show where it stops: the phone gives no other trace, and nothing reaches the server.
    let step = 'open the Files app'
    try {
      const handles = await open.call(window, { multiple: true })
      step = `read ${handles.length} file(s)`
      const chosen = await Promise.all(handles.map(handle => handle.getFile()))
      if (!chosen.length) throw new Error('No file came back')
      await attach(chosen)
    } catch (failure) {
      const name = (failure as Error)?.name || 'Error'
      // Cancel and failure both look like AbortError on some phones, so say it and use the plain picker next time.
      filesFallback.current = true
      setSpeechError(`Files could not ${step} (${name}: ${(failure as Error)?.message || errorText(failure)}). Tap Files again to use the other picker.`)
    }
  }
  async function changeFolder() {
    await forgetFolder()
    try { localStorage.removeItem('tether-screenshot-folder') } catch { /* private mode */ }
    setHasFolder(false)
    await attachLatest()
  }
  // Attach files shared from another app (Gallery → Share → Tether) once a session that takes files is open.
  // Wait until the chat has loaded (disabled is false), so the upload carries the current thread.
  useEffect(() => { if (canAttach && !disabled) void takeShared().then(async shared => { if (!shared.length) return; const saved = await attach(shared.map(item => item.file), index => void doneShared(shared[index])); releaseShared(shared.slice(saved)) }) }, [canAttach, disabled, pane.id, thread]) // eslint-disable-line react-hooks/exhaustive-deps
  // Images from a side panel (the browser panel's "Send to chat") arrive as a 'tether-attach' event for this pane.
  // Cancel the event so the panel knows a composer took the files.
  useEffect(() => {
    const take = (event: Event) => { const detail = (event as CustomEvent<{ pane: string; files: File[] }>).detail; if (detail?.pane !== pane.id || !canAttach || disabled) return; event.preventDefault(); void attach(detail.files) }
    window.addEventListener('tether-attach', take)
    return () => window.removeEventListener('tether-attach', take)
  }, [canAttach, disabled, pane.id, thread]) // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    const input = textarea.current
    if (input) { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 180)}px` }
  }, [draft])
  // Hands-free: a read-aloud reply asks for the mic (listenRequest). A pause after speech stops it and sends.
  const dictationRef = useRef(dictation)
  useLayoutEffect(() => { dictationRef.current = dictation })
  useEffect(() => { if (listenRequest && !dictationRef.current.active() && micOk) void dictationRef.current.toggle() }, [listenRequest, micOk])
  // own: text to send instead of the box (a command picked with Enter). force: skip the unknown-command check.
  async function submit(event?: React.FormEvent, own?: string, force = false) {
    event?.preventDefault()
    const waiting = own === undefined ? dictation.finalText() : null
    const typed = own ?? draft
    if (disabled || sending || uploading || (!typed.trim() && !files.length && !pastes.length && !waiting) || (shellMode && !typed.slice(1).trim())) return
    if (!force && !waiting && suggest.current?.check(typed)) return
    dictation.stop()
    const typing = document.activeElement === textarea.current
    setSending(true)
    try {
      const body = waiting ? (await waiting).trim() : typed.trim()
      const text = [...files.map(file => file.path), joinPastes(body, pastes)].filter(Boolean).join(' ')
      if (text && await onSend(text)) { if (body) remember(historyKind, body); browse.current = null; setDraft(''); setFiles([]); setPastes([]); setOpenPaste(null); if (coarse?.matches) navigator.vibrate?.(10) }
    } finally { setSending(false); if (typing) requestAnimationFrame(() => textarea.current?.focus()) }
  }
  // Up on the first line goes to the previous sent prompt; Down on the last line goes forward, then back to the draft.
  // The list: this session's own prompts from the chat (any device, older ones too), then recent sends from this browser
  // that the chat does not show (slash commands, shell lines).
  const historyKind = pane.id
  const browse = useRef<{ at: number; list: string[]; draft: string } | null>(null)
  function walk(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.nativeEvent.isComposing) return false
    const box = event.currentTarget, start = box.selectionStart, end = box.selectionEnd
    if (start !== end || box.readOnly) return false
    if (event.key === 'ArrowUp' && !draft.slice(0, start).includes('\n')) {
      const state = browse.current ?? { at: -1, list: history(sent, loadHistory(historyKind)), draft }
      const at = state.at === -1 ? state.list.length - 1 : state.at - 1
      if (at < 0 || !state.list.length) return false
      browse.current = { ...state, at }
      setDraft(state.list[at])
      requestAnimationFrame(() => textarea.current?.setSelectionRange(0, 0))
      return true
    }
    if (event.key === 'ArrowDown' && browse.current && !draft.slice(end).includes('\n')) {
      const state = browse.current, at = state.at + 1
      const text = at < state.list.length ? state.list[at] : state.draft
      browse.current = at < state.list.length ? { ...state, at } : null
      setDraft(text)
      requestAnimationFrame(() => textarea.current?.setSelectionRange(text.length, text.length))
      return true
    }
    return false
  }
  // A tap on a button in the composer keeps the focus in the text box. Without this, on a phone the tap first closes
  // the keyboard, the layout jumps, and the tap misses the button. On a desktop the box keeps the caret.
  function keepFocus(event: React.MouseEvent) {
    if (document.activeElement === textarea.current && event.target !== textarea.current && (event.target as HTMLElement).closest('button')) event.preventDefault()
  }
  function terminalKey(key: string) {
    const box = textarea.current
    if (!box || disabled || sending || box.readOnly) return
    box.focus()
    if (key === 'Escape') { onTerminalEscape?.(); return }
    if (key === 'C-c') { onStop(); return }
    if (key === 'ArrowUp' || key === 'ArrowDown') {
      box.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); return
    }
    const start = box.selectionStart, end = box.selectionEnd
    if (key === 'Tab') {
      setDraft(draft.slice(0, start) + '\t' + draft.slice(end))
      requestAnimationFrame(() => box.setSelectionRange(start + 1, start + 1))
    } else {
      const at = key === 'ArrowLeft' ? Math.max(0, start - (start === end ? 1 : 0)) : Math.min(draft.length, end + (start === end ? 1 : 0))
      box.setSelectionRange(at, at)
    }
  }
  return <div className="composer-area">{pane.agent === 'shell' && <div className="shell-key-strip" role="group" aria-label="Terminal keys" onMouseDown={keepFocus}>
    {([['Escape', 'esc', 'Send Escape to terminal'], ['Tab', 'tab', 'Insert tab'], ['C-c', 'ctrl+c', 'Stop command'], ['ArrowUp', '↑', 'Previous command'], ['ArrowDown', '↓', 'Next command'], ['ArrowLeft', '←', 'Move cursor left'], ['ArrowRight', '→', 'Move cursor right']] as const).map(([key, label, title]) => <button key={key} type="button" title={title} aria-label={title} disabled={disabled || sending || listening || transcribing} onClick={() => terminalKey(key)}>{label}</button>)}
  </div>}<Suggestions ref={suggest} api={api} pane={pane} draft={draft} setDraft={setDraft} focus={() => textarea.current?.focus()} run={text => { setDraft(text); void submit(undefined, text, true) }} sendAnyway={() => void submit(undefined, undefined, true)} /><form className={shellMode ? 'composer shell-mode' : 'composer'} ref={form} onSubmit={submit} onMouseDown={keepFocus}>
    {files.length + pastes.length > 0 && <ul className="attachments" aria-label="Attached files and pasted text">{files.map((file, index) => <li key={file.path} className="chip">{file.url ? <button type="button" className="chip-view" aria-label={`View ${file.name}`} onClick={() => setViewing({ name: file.name, url: file.url! })}><img src={file.url} alt="" /><span>{file.name}</span></button> : <><Paperclip size={13} /><span>{file.name}</span></>}<button type="button" aria-label={`Remove ${file.name}`} onClick={() => setFiles(current => current.filter((_, at) => at !== index))}><X size={14} /></button></li>)}{pastes.map(paste => <PasteChip key={paste.id} paste={paste} open={openPaste === paste.id} onToggle={() => setOpenPaste(openPaste === paste.id ? null : paste.id)} onEdit={() => { setDraft(inlinePaste(draft, paste)); setPastes(current => current.filter(item => item.id !== paste.id)); setOpenPaste(null); textarea.current?.focus() }} onRemove={() => { setPastes(current => current.filter(item => item.id !== paste.id)); textarea.current?.focus() }} />)}</ul>}
    <ImageViewer image={viewing} close={() => setViewing(null)} items={files.flatMap(file => file.url ? [{ name: file.name, url: file.url }] : [])} onSelect={setViewing} />
    <div className="composer-input">{pane.agent === 'shell' && <span className="terminal-sigil" aria-hidden="true">$</span>}{shellMode && <span className="shell-prompt" title={pane.dir ? `Runs in ${pane.dir}` : undefined}><span className="shell-folder">{shortFolder(pane.dir)}</span><span aria-hidden="true">$</span></span>}
    <textarea className={terminalInput ? 'shell-input' : undefined} onKeyDown={event => {
      if (browse.current && walk(event)) event.preventDefault()
      else if (suggest.current?.key(event)) event.preventDefault()
      else if (walk(event)) event.preventDefault()
      else if (shellMode && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setDraft(draft.slice(1)) }
      else if (shellMode && event.key === 'Backspace' && !draft.slice(1).trim() && event.currentTarget.selectionEnd <= 1) { event.preventDefault(); setDraft(draft.slice(1)) }
      else if (sendsOnEnter(event)) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} onPaste={event => { if (canAttach && event.clipboardData.files.length) { event.preventDefault(); void attach(event.clipboardData.files); return }
      const raw = event.clipboardData.getData('text/plain'), pasted = terminalText(raw), box = event.currentTarget
      if (canAttach && !box.readOnly && longPaste(pasted)) { event.preventDefault(); setPastes(current => [...current, pasteOf(pasted, !draft.slice(0, box.selectionStart).trim())]) }
      else if (!box.readOnly && pasted !== raw) { event.preventDefault(); const start = box.selectionStart, end = box.selectionEnd; setDraft(draft.slice(0, start) + pasted + draft.slice(end)); requestAnimationFrame(() => { box.setSelectionRange(start + pasted.length, start + pasted.length) }) } }} ref={textarea} aria-label={pane.agent === 'shell' ? 'Run a command' : shellMode ? 'Shell command' : `Message ${chatTitle(pane)}`} aria-describedby={shellMode ? hintId : undefined} spellCheck={!terminalInput} autoCapitalize={terminalInput ? 'none' : undefined} autoCorrect={terminalInput ? 'off' : undefined} placeholder={hint ?? (pane.agent === 'shell' ? 'Run a command' : `Message ${chatTitle(pane)}`)} rows={1} value={draft} readOnly={listening || transcribing || sending} disabled={disabled} onChange={event => setDraft(event.target.value)} /></div>
    <div className="composer-controls"><div>{canAttach && <div className="attach-container" ref={menuRef}>
      <input ref={picker} type="file" multiple accept={FILES_ACCEPT} hidden onChange={event => { const chosen = Array.from(event.target.files ?? []); event.target.value = ''; void attach(chosen) }} />
      <input ref={photos} type="file" multiple accept="image/*,video/*" hidden onChange={event => { const chosen = Array.from(event.target.files ?? []); event.target.value = ''; void attach(chosen) }} />
      <input ref={camera} type="file" accept="image/*" capture="environment" hidden onChange={event => { const chosen = Array.from(event.target.files ?? []); event.target.value = ''; void attach(chosen) }} />
      <button type="button" className="icon-button attach" aria-label="Add photos and files" aria-expanded={menu} disabled={disabled || sending || !!uploading} onClick={() => setMenu(!menu)}><Plus size={21} /></button>
      {menu && <div className="attach-layer" popover="manual" ref={layer}><div className="attach-backdrop" aria-hidden="true" onClick={() => setMenu(false)} /><div className="attach-menu" role="menu" aria-label="Add to message" onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setMenu(false) } }}>
        <span className="attach-grip" aria-hidden="true" />
        <div className="attach-tiles">
          <button role="menuitem" type="button" onClick={() => { setMenu(false); photos.current?.click() }}><span><ImagePlus size={24} /></span>Gallery</button>
          <button role="menuitem" type="button" onClick={() => { setMenu(false); camera.current?.click() }}><span><Camera size={24} /></span>Camera</button>
          <button role="menuitem" type="button" onClick={() => void pickFiles()}><span><Paperclip size={24} /></span>Files</button>
        </div>
        <button role="menuitem" type="button" onClick={() => void attachLatest()}><Smartphone size={18} />Latest screenshot</button>
        {hasFolder && <button role="menuitem" type="button" onClick={() => void changeFolder()}><FolderOpen size={18} />Change screenshot folder</button>}
        {pane.agent !== 'shell' && <>
          <button role="menuitem" type="button" onClick={() => insertTrigger('@')}><AtSign size={18} />Mention a file</button>
          <button role="menuitem" type="button" onClick={() => insertTrigger('/')}><Slash size={18} />Commands & skills</button>
          <button role="menuitem" type="button" onClick={() => { setMenu(false); onTools() }}><Blocks size={18} />Plugins & MCP</button>
        </>}
      </div></div>}
    </div>}{!uploading && !listening && !transcribing && options}<span className="composer-status" aria-live="polite">{uploading || (listening ? `Recording ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : transcribing ? 'Converting…' : pane.agent === 'shell' ? 'Terminal input' : shellMode ? 'Shell mode' : '')}</span>{!draft && !files.length && !pastes.length && pane.agent !== 'shell' && !listening && !transcribing && !uploading && <span className="composer-keys" aria-hidden="true"><span><kbd>↵</kbd> send</span><span><kbd>⇧↵</kbd> new line</span><span><kbd>/</kbd> commands</span><span><kbd>!</kbd> shell</span></span>}</div><div>
      {micOk && canRecord && (transcribing
        ? <button type="button" className="icon-button mic active" aria-label="Stop converting and keep the live text" onClick={() => dictation.cancelConverting()}><X size={19} /></button>
        : <button type="button" className={`icon-button mic ${listening ? 'active' : ''}`} aria-label={listening ? 'Stop recording' : 'Start voice input'} aria-pressed={listening} aria-keyshortcuts="Alt+V" title={`${listening ? 'Stop recording' : 'Voice input'} (${keyLabel('⌥V')})`} disabled={disabled || sending} onClick={() => void toggleMic()}>{listening ? <Square size={18} /> : <Mic size={20} />}</button>)}
      {working && !draft.trim() && !files.length && !pastes.length
        ? <button type="button" className="primary send-button stop-button" aria-label={pane.agent === 'shell' ? 'Stop command (Ctrl+C)' : 'Stop response (Esc)'} disabled={disabled} onClick={onStop}><Square size={15} fill="currentColor" /></button>
        : shellMode
          ? <button className="primary send-button run-button" aria-label="Run command" disabled={disabled || sending || !!uploading || !draft.slice(1).trim()}><CornerDownLeft size={16} />Run</button>
          : <button className="primary send-button" aria-label={pane.agent === 'shell' ? 'Run command' : 'Send message'} disabled={disabled || sending || !!uploading || (!draft.trim() && !files.length && !pastes.length)}><ArrowUp size={21} /></button>}
    </div></div>
  </form>{shellMode && <p className="shell-hint" id={hintId}>Runs on {host} · Esc or Backspace on empty leaves shell mode</p>}{(error || speechError) && <p className="composer-error error" role="alert">{error || speechError}{error && errorAction}</p>}</div>
}

// Desktop (a mouse or trackpad): Enter sends, Shift+Enter adds a line. Phone soft keyboard: Enter adds a line, as before.
// Ctrl/Cmd+Enter sends everywhere. An open IME composition (Chinese, Japanese input) keeps its Enter.
const desktop = typeof matchMedia === 'function' ? matchMedia('(hover: hover) and (pointer: fine)') : null
const coarse = typeof matchMedia === 'function' ? matchMedia('(pointer: coarse)') : null  // a phone: a light tap on send
function sendsOnEnter(event: React.KeyboardEvent) {
  if (event.key !== 'Enter' || event.nativeEvent.isComposing || event.shiftKey || event.altKey) return false
  return event.ctrlKey || event.metaKey || !!desktop?.matches
}
