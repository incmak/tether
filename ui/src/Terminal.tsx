import { RetryButton } from './Retry'
import { useEffect, useRef, useState } from 'react'
import { SquareTerminal } from 'lucide-react'
import { copyText } from './clipboard'
import { Linked } from './Messages'
import { joinRows } from './terminalRows'
import { installTerminalIme } from './terminalIme'
import { dragSelect } from './terminalMouse'



































type TtydTerm = { cols: number; rows: number; options: { fontSize: number }; element?: HTMLElement; fit: () => void; onTitleChange: (listener: (title: string) => void) => unknown
  buffer?: { active: { viewportY: number; getLine: (row: number) => { isWrapped: boolean; translateToString: (trim: boolean) => string } | undefined } } }
const TERM_FONT = 13, TERM_MIN_FONT = 9

// Read this client's viewport. No terminal input, tmux setting, or shared pane changes.
// paneCols: the pane's own width (from fitPane). The terminal can be a column wider, so a full pane row would look short.
function terminalText(frame: HTMLIFrameElement, paneCols = 0): string | null {
  const term = (frame.contentWindow as (Window & { term?: TtydTerm }) | null)?.term
  const buffer = term?.buffer?.active
  if (!term || !buffer) return null
  const rows: { text: string; wrapped: boolean }[] = []
  for (let row = buffer.viewportY; row < buffer.viewportY + term.rows; row++) {
    const line = buffer.getLine(row)
    if (!line) break
    rows.push({ text: line.translateToString(true), wrapped: line.isWrapped })
  }
  return joinRows(rows, paneCols && paneCols < term.cols ? paneCols : term.cols)
}

// The pane's own width arrives in the terminal title ("tether-pane 80x24", from pane_view on the server).
// Make the font smaller until the pane fits the phone. Below 9 px, keep 9 px and scroll sideways.
function fitPane(frame: HTMLIFrameElement, box: HTMLElement, setWidth: (width: number | null) => void, paneCols: { current: number }, tries = 20) {
  const term = (frame.contentWindow as (Window & { term?: TtydTerm }) | null)?.term
  if (!term?.onTitleChange || !term.fit) {  // ttyd makes the terminal a moment after the page loads
    if (tries > 0 && frame.isConnected) setTimeout(() => fitPane(frame, box, setWidth, paneCols, tries - 1), 250)
    return
  }
  let cols = 0
  // grow: only a resized box may make the font bigger. A zoomed shell takes the terminal's width, so its title follows every
  // font change; if a title change could also grow the font, the two would chase each other (the 103x66 / 106x66 flicker).
  const fit = (grow: boolean) => {
    if (!cols) return
    term.fit()  // the box may have changed; the font stays
    const font = term.options.fontSize
    if (cols <= term.cols && (font === TERM_FONT || !grow)) return  // the pane fits at this font
    const inner = term.element?.clientWidth || frame.clientWidth
    const cell = inner / Math.max(1, term.cols) / font  // the width of one column for each px of font size
    const chrome = Math.max(0, frame.clientWidth - inner)
    const available = box.clientWidth - chrome
    const size = Math.floor(available / (cols * cell) * 2 + 0.01) / 2  // 0.01: float error must not drop a whole half step
    const next = size >= TERM_MIN_FONT ? Math.min(TERM_FONT, size) : TERM_MIN_FONT
    setWidth(size >= TERM_MIN_FONT ? null : Math.ceil(cols * cell * TERM_MIN_FONT + chrome + 4))
    if (next < font || (grow && next > font)) { term.options.fontSize = next; term.fit() }
  }
  term.onTitleChange(title => {
    const match = /^tether-pane (\d+)x\d+$/.exec(title)
    if (match && Number(match[1]) !== cols) { cols = paneCols.current = Number(match[1]); fit(false) }
  })
  new ResizeObserver(() => fit(true)).observe(box)
}

// ttyd exposes the terminal a moment after the iframe loads, in every view.
function attachDragSelect(frame: HTMLIFrameElement, paneCols: { current: number }, onCopied: () => void, tries = 20) {
  const win = frame.contentWindow as (Window & { term?: TtydTerm }) | null
  const term = win?.term
  if (!win || !term) {
    if (tries > 0 && frame.isConnected) setTimeout(() => attachDragSelect(frame, paneCols, onCopied, tries - 1), 250)
    return
  }
  dragSelect(win, term as unknown as Parameters<typeof dragSelect>[1], () => paneCols.current, onCopied)
  installTerminalIme(win, term)
  hideResizeOverlay(win.document)
}

// ttyd shows "106x66" in the middle of the terminal on every resize, and "✂" on every copy. Tether shows its own "Copied".
// ttyd builds that box with an inline style and does not expose its switch (-t disableResizeOverlay) to the page.
function hideResizeOverlay(doc: Document) {
  if (doc.getElementById('tether-no-overlay')) return
  const style = doc.createElement('style')
  style.id = 'tether-no-overlay'
  style.textContent = '.xterm > div[style*="xx-large"] { display: none !important; }'
  doc.head.append(style)
}

// The live terminal (ttyd through the server). Each tab has its own tmux window, so the view always shows the tab's pane, zoomed to fit.
// shell: Tether's own side shell (TerminalPanel). Its window holds only this pane, so the view zooms it to fit and shows no view switch.
// fixed: the window keeps its own size (btop needs a minimum), so the view never resizes it and scales the font instead.
export function LiveTerminal({ paneId, onChat, shell = false, fixed = false }: { paneId: string; onChat: () => void; shell?: boolean; fixed?: boolean }) {
  const [status, setStatus] = useState<'checking' | 'ok' | 'down'>('checking')
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [width, setWidth] = useState<number | null>(null)
  const view = 'pane'  // one pane per tmux window: no window view, no zoom switch
  const arg = fixed ? paneId : 'z' + paneId
  const box = useRef<HTMLDivElement>(null)
  const frame = useRef<HTMLIFrameElement>(null)
  const paneCols = useRef(0)
  const textBox = useRef<HTMLPreElement>(null)
  const [selection, setSelection] = useState<string | null>(null)
  const [hasSelection, setHasSelection] = useState(false)
  const [copyStatus, setCopyStatus] = useState('')
  const [dragCopied, setDragCopied] = useState(false)  // a drag in the live terminal copied its text
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (copyTimer.current !== null) clearTimeout(copyTimer.current) }, [])
  const [selectionFor, setSelectionFor] = useState({ arg, attempt })
  if (selectionFor.arg !== arg || selectionFor.attempt !== attempt) {
    setSelectionFor({ arg, attempt }); setSelection(null); setCopyStatus(''); setHasSelection(false)
  }
  const selectText = () => {
    if (selection !== null) { setSelection(null); setCopyStatus(''); return }
    const text = frame.current ? terminalText(frame.current, paneCols.current) : null
    if (text === null) { setCopyStatus('The terminal is still connecting. Try again.'); return }
    setSelection(text); setHasSelection(false); setCopyStatus('')
  }
  // The text selected inside the copy view, or '' when nothing there is selected.
  const pickedText = () => {
    const picked = window.getSelection(), box = textBox.current
    return picked && box && !picked.isCollapsed && box.contains(picked.anchorNode) && box.contains(picked.focusNode) ? picked.toString() : ''
  }
  useEffect(() => {
    if (selection === null) return
    const changed = () => setHasSelection(!!pickedText())
    document.addEventListener('selectionchange', changed)
    return () => document.removeEventListener('selectionchange', changed)
  }, [selection])
  const copySelection = async () => {
    if (selection === null) return
    const text = pickedText() || selection
    try { await copyText(text); setCopyStatus('Copied to clipboard.') }
    catch { setCopyStatus('Could not copy. Use the selection menu to copy the text.') }
  }
  useEffect(() => {
    let live = true
    fetch('/terminal/live/', { credentials: 'same-origin' }).then(response => { if (live) { setStatus(response.ok ? 'ok' : 'down'); setBusy(false) } }, () => { if (live) { setStatus('down'); setBusy(false) } })
    return () => { live = false }
  }, [attempt])
  if (status === 'ok') return <div className="live-terminal-wrap">
    <div className="terminal-bar">
      <button type="button" className={`terminal-zoom${selection !== null ? ' active' : ''}`} aria-pressed={selection !== null} onClick={selectText}>{selection !== null ? 'Back to terminal' : 'Select text'}</button>
      {selection !== null && <button type="button" className="terminal-zoom" onPointerDown={event => event.preventDefault()} onClick={() => void copySelection()}>{hasSelection ? 'Copy selection' : 'Copy all'}</button>}</div>
    {(selection !== null || copyStatus) && <p className="terminal-selection-hint" role="status">{copyStatus || 'Drag or long-press to select text. Links open in a new tab. This copy stays still until you return to the terminal.'}</p>}
    <div ref={box} className={`live-terminal-box${selection !== null ? ' selecting' : ''}`}><iframe ref={frame} key={`${attempt}:${arg}`} className="live-terminal" title="Live terminal" style={width ? { width } : undefined} inert={selection !== null}
      src={`/terminal/live/?arg=${encodeURIComponent(arg)}`} onLoad={event => {
        paneCols.current = 0
        if (box.current && (shell || view === 'pane')) fitPane(event.currentTarget, box.current, setWidth, paneCols)
        attachDragSelect(event.currentTarget, paneCols, () => {
          setDragCopied(true)
          if (copyTimer.current !== null) clearTimeout(copyTimer.current)
          copyTimer.current = setTimeout(() => setDragCopied(false), 2000)
        })
      }} />
      {/* A wrapped view: a long URL wraps on screen and still copies as one line. */}
      {selection !== null && <pre ref={textBox} className="terminal-select-text" tabIndex={0} aria-label="Terminal text"
        onClickCapture={event => { if (pickedText()) event.preventDefault() }}  // a drag that ends on a link selects; it does not open the link
        onPointerUp={event => { if (event.pointerType === 'mouse' && pickedText()) void copySelection() }}><Linked text={selection} /></pre>}</div>
    {/* An overlay, so the terminal does not move: a resize would clear the selection. */}
    <p className={`terminal-copied${dragCopied ? ' shown' : ''}`} role="status">{dragCopied ? 'Copied to clipboard' : ''}</p></div>
  return <div className="live-terminal-empty">{status === 'checking' ? <p role="status">Connecting to the terminal…</p> : <>
    <SquareTerminal size={28} strokeWidth={1.4} /><p><strong>The live terminal is not running.</strong></p><p>On the server, run <code>python3 ~/apps/mobile_tmux_public/deploy/install.py</code>.</p>
    <div>{!shell && <button type="button" className="secondary" onClick={onChat}>Back to chat</button>}<RetryButton className="primary" busy={busy} onRetry={() => { setBusy(true); setAttempt(attempt + 1) }} /></div></>}</div>
}

// Claude draws a text block and writes it to the transcript at almost the same moment, so most previews last under a second.
// Show one only after it stays for PREVIEW_HOLD_MS: a slow transcript write still shows, a flash does not.
