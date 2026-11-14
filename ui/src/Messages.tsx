import { terminalText } from './terminalText'
import { shellCommand } from './shellCommand'
import { copyText } from './clipboard'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Blocks, RefreshCw, Clock3, Layers, Check, Copy, Share2, ChevronRight, X, MessageSquare, Info, ShieldAlert, Square, Volume2, Eye, ClipboardPlus } from 'lucide-react'
import { MessagePrimitive } from '@assistant-ui/react'
import type { ThreadMessageLike } from '@assistant-ui/react'
import { MarkdownTextPrimitive } from '@assistant-ui/react-markdown'
import remarkGfm from 'remark-gfm'
import type { Api, ChatMessage } from './api'
import { ToolGroup } from './ToolGroup'
import { SentContent, sentContent } from './SentImages'
import { InlineCode, Link, remarkPaths } from './MediaLink'
import { remarkTabs } from './tabLinks'
import { canSpeak, speak, stopSpeaking, useSpeaking } from './speech'
import type { ToolSummary } from './api'















const URL_PATTERN = /https?:\/\/[^\s<>"'`]+/g
export function Linked({ text }: { text: string }) {
  const parts: React.ReactNode[] = []
  let last = 0
  for (const match of text.matchAll(URL_PATTERN)) {
    const url = match[0].replace(/[).,;:!?\]}]+$/, ''), at = match.index ?? 0
    parts.push(text.slice(last, at), <a key={at} href={url} target="_blank" rel="noreferrer">{url}</a>)
    last = at + url.length
  }
  parts.push(text.slice(last))
  return <>{parts}</>
}
// A terminal panel wraps on a phone. Each line keeps its indent as a hanging indent, so wrapped parts line up under their line.
export function PanelText({ text }: { text: string }) {
  return <>{text.split('\n').map((line, index) => {
    const indent = line.length - line.trimStart().length
    return <div key={index} className="panel-line" style={{ paddingLeft: `${Math.min(indent, 12) + 2}ch` }}>{line.trim() ? <Linked text={line.trim()} /> : '\u00a0'}</div>
  })}</>
}
// A read-only terminal panel (Claude's /usage): keep its columns and bars, scroll sideways on a narrow phone.
export function PanelPre({ text }: { text: string }) {
  // The tab row and "Esc to cancel" repeat the card's own title and Close button. Bars shrink to 40% so the numbers fit a phone.
  const lines = text.split('\n').filter(line => !/^\s*Settings\s+Status\s+Config\b|^\s*Esc to (?:cancel|close)\s*$/.test(line))
    .map(line => line.replace(/[█▉▊▋▌▍▎▏]+/g, bar => '█'.repeat(Math.max(1, Math.round(bar.length * .4)))))
  const indent = Math.min(...lines.filter(line => line.trim()).map(line => line.length - line.trimStart().length))
  return <pre className="panel-pre" tabIndex={0} aria-label="Panel text">{lines.map(line => line.slice(indent).replace(/(\S) {4,}/g, '$1   ')).join('\n').trim()}</pre>
}
// A "!" command's result that Tether ran itself (shell_log.py): "~/dir $ cmd", the output, then "[exit 2]", "[stopped after 120 s]" or "→ ~/new".
// Only these carry a prompt and an exit status; an agent's own "!" output is the output alone.
function shellParts(text: string) {
  const lines = text.split('\n')
  const prompt = /^(\S.*?) \$ (.*)$/.exec(lines[0] ?? '')
  if (!prompt) return null
  let end = lines.length, exit = 0, stopped = '', moved = ''
  for (;;) {
    const line = lines[end - 1] ?? ''
    if (end > 1 && line.startsWith('→ ')) { moved = line.slice(2); end-- }
    else if (end > 1 && /^\[exit \d+\]$/.test(line)) { exit = Number(line.slice(6, -1)); end-- }
    else if (end > 1 && /^\[stopped after \d+ s\]$/.test(line)) { stopped = line.slice(1, -1); end-- }
    else break
  }
  return { folder: prompt[1], command: prompt[2], output: lines.slice(1, end).join('\n'), exit, stopped, moved }
}
function TerminalSnapshot({ text, shell }: { text: string; shell: boolean }) {
  const ref = useRef<HTMLPreElement>(null)
  useLayoutEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight }, [text])
  text = terminalText(text)
  const parts = shell ? shellParts(text) : null
  if (parts) return <div className="terminal-block shell-run">
    <div className="shell-run-head"><code className="shell-run-prompt"><span className="shell-folder">{parts.folder}</span> $ {parts.command}</code>
      <span className={`exit-chip ${parts.stopped || parts.exit ? 'failed' : 'ok'}`}>{parts.stopped ? 'Stopped' : parts.exit ? `Exit ${parts.exit}` : 'Exit 0'}</span></div>
    {parts.output && <pre ref={ref} className="terminal-snapshot" tabIndex={0} aria-label="Command output"><code><Linked text={parts.output} /></code></pre>}
    {(parts.moved || parts.stopped) && <p className="shell-run-note">{parts.stopped ? `${parts.stopped[0].toUpperCase()}${parts.stopped.slice(1)}. ` : ''}{parts.moved && `Now in ${parts.moved}`}</p>}
    <CopyButton text={parts.output || text} label="Copy command output" className="copy-chip" />
  </div>
  return <div className="terminal-block">
    <pre ref={ref} className="terminal-snapshot" tabIndex={0} aria-label="Terminal output"><code><Linked text={text} /></code></pre>
    <CopyButton text={text} label="Copy terminal output" className="copy-chip" />
  </div>
}
// A watcher that expired with nothing to report, re-armed by the agent, shows as one quiet line. The notice, the re-arm
// and the agent's short reply stay one tap away. foldWatchCycles (watchCycles.ts) decides what counts as quiet.
export type WatchRun = { members: ChatMessage[]; watches: string[]; cycles: number; rearmed: boolean }
const watchName = (label: string) => label.split(':')[0].trim() || label
function WatchGroup({ id, run, time, api, session }: { id: string; run: WatchRun; time?: string; api: Api; session: string }) {
  const names = [...new Set(run.watches.map(watchName))]
  const what = run.cycles > 1 ? `${run.cycles} quiet cycles · ${names.join(', ')}` : `${names[0] ?? 'Watch'} · no events · ${run.rearmed ? 're-armed' : 'ended'}`
  const when = stamp(time)
  return <details className="watch-row" data-msg-id={id}>
    <summary><RefreshCw size={13} aria-hidden="true" /><span className="watch-what">{what}</span>{when && <time dateTime={time}>{run.cycles > 1 ? `last ${when}` : when}</time>}<ChevronRight className="chevron" size={14} aria-hidden="true" /></summary>
    <div className="watch-members">{run.members.map(item => item.kind
      ? <Message key={item.id} api={api} session={session} actions={false} id={item.id} role={item.role} kind={item.kind} title={item.title} text={item.text} count={item.count} time={item.time} tools={item.tools} />
      : <div key={item.id} className="watch-reply" data-msg-id={item.id}>{item.text.split(/\n\s*\n/).map((part, at) => <p key={at}><Linked text={part.trim()} /></p>)}<MessageActions text={item.text} time={item.time} /></div>)}</div>
  </details>
}
export function Message({ id, role, kind, title, text, count, time, tools, api, session, actions = true, images, imageKey, live, runningShown, watch }: { watch?: WatchRun; live?: boolean; runningShown?: boolean; images?: number; imageKey?: string; actions?: boolean; id: string; role: string; kind?: unknown; title?: string; text: string; count?: unknown; time?: string; tools?: ToolSummary[]; api: Api; session: string }) {
  if (kind === 'watch' && watch) return <WatchGroup id={id} run={watch} time={time} api={api} session={session} />
  // A line Claude Code prints between turns: a hook that blocked it, a "while you were away" recap, a notice.
  if (kind === 'notice' && title) {
    const icon = title.endsWith('hook blocked') ? <ShieldAlert size={15} /> : title === 'While you were away' ? <Clock3 size={15} /> : <Info size={15} />
    if (text.includes('\n') || text.length > 240) return <details className="tool-row compact-row notice-row" data-msg-id={id}><summary>{icon}{title}<ChevronRight size={15} /></summary><pre><Linked text={text} /></pre></details>
    return <p className="notice-line" data-msg-id={id}>{icon}<span><strong>{title}</strong> · <Linked text={text} /></span></p>
  }
  // A message from another Claude session, or a subagent's final report: Claude's input, but not the user's words.
  if (kind === 'peer' && title) {
    const report = title === 'Subagent report'
    return <details className="peer-message" data-msg-id={id} open={!report}>
      <summary>{report ? <Blocks size={15} /> : <MessageSquare size={15} />}<strong>{title}</strong>{report && <span className="peer-preview">{text.split('\n').find(line => line.trim())?.replace(/^#+\s*/, '')}</span>}<ChevronRight size={15} /></summary>
      <MessagePrimitive.Root className="assistant-message"><MessagePrimitive.Parts components={{ Text: Markdown }} /><MessageActions text={text} time={time} /></MessagePrimitive.Root>
    </details>
  }
  if (kind === 'actions') {
    return <ToolGroup id={id} text={text} count={count} tools={tools} api={api} session={session} live={live} runningShown={runningShown} />
  }
  if (kind === 'terminal') return <div data-msg-id={id}><TerminalSnapshot text={text} shell={id.startsWith('shell:')} /></div>
  const shell = role === 'user' && !kind ? shellCommand(text) : null
  if (shell) return <div className="terminal-block shell-run" data-msg-id={id} role="group" aria-label="Shell command result">
    <div className="shell-run-head"><code className="shell-run-prompt"><span className="shell-sigil" aria-hidden="true">$</span> {shell.command}</code><span className={`exit-chip ${shell.exit ? 'failed' : 'ok'}`}>Exit {shell.exit}</span></div>
    {shell.output && <pre className="terminal-snapshot" tabIndex={0} aria-label="Command output"><code>{shell.output}</code></pre>}
    <CopyButton text={shell.output || shell.command} label="Copy command output" className="copy-chip" />
  </div>
  // A "!" line the person sent: the agent ran it in its terminal. Show it as the prompt line of the output below it.
  const command = role === 'user' && !kind ? /^!\s*(\S[\s\S]*)$/.exec(text) : null
  if (command) return <div className="shell-command" data-msg-id={id} role="group" aria-label="Shell command"><span className="shell-sigil" aria-hidden="true">$</span><code>{command[1]}</code></div>
  // Tether's review loop types these into a tab: a review request to the reviewer, or its feedback to the worker.
  const review = role === 'user' && !kind ? parseReview(text) : null
  if (review) return <ReviewCard id={id} review={review} />
  // Claude puts the whole summary in a user message after /compact or an auto-compact. Fold it like a tool row.
  if (role === 'user' && text.startsWith('This session is being continued from a previous conversation')) {
    return <details className="tool-row compact-row" data-msg-id={id}><summary><Layers size={15} />Conversation compacted<ChevronRight size={15} /></summary><pre>{text}</pre></details>
  }
  const sent = role === 'user' && !kind ? sentContent(text, session, images, imageKey) : null
  if (sent?.shots.length) return <MessagePrimitive.Root className="user-message" data-msg-id={id}><SentContent {...sent} /></MessagePrimitive.Root>
  return <MessagePrimitive.Root className={role === 'user' ? 'user-message' : 'assistant-message'} data-msg-id={id}>
    {role === 'user' ? <MessagePrimitive.Parts /> : <><MessagePrimitive.Parts components={{ Text: Markdown }} />{actions && <MessageActions text={text} time={time} />}</>}
  </MessagePrimitive.Root>
}
type Review = { feedback: boolean; tab: string; agent?: string; name?: string; reply: string; transcript?: string; last?: boolean }
const REVIEW_REQUEST = /^\[Tether review\] The (\S+) tab “([^”]*)” \((%\d+)\) finished a turn\.[\s\S]*?Its last reply:\s*([\s\S]*?)\s*Transcript: (\S*)( This is the last round: your reply stays here\.)?\s*$/
const REVIEW_FEEDBACK = /^\[Tether review\] Feedback from the reviewer tab \((%\d+)\):\s*([\s\S]*?)\s*Fix what it raises, then stop and wait\.\s*$/
function parseReview(text: string): Review | null {
  const request = REVIEW_REQUEST.exec(text)
  if (request) return { feedback: false, agent: request[1], name: request[2], tab: request[3], reply: request[4], transcript: request[5], last: !!request[6] }
  const feedback = REVIEW_FEEDBACK.exec(text)
  return feedback ? { feedback: true, tab: feedback[1], reply: feedback[2] } : null
}
// A request starts folded: its body is the other tab's reply, which that tab already shows. Feedback starts open: it is what this tab acts on.
function ReviewCard({ id, review }: { id: string; review: Review }) {
  const preview = review.reply.split('\n').find(line => line.trim())?.replace(/^#+\s*/, '')
  return <details className="peer-message review-message" data-msg-id={id} open={review.feedback}>
    <summary><Eye size={15} /><strong>{review.feedback ? `Review from ${review.tab}` : `Review request · ${review.name || review.tab}`}</strong>{review.last && <span className="badge">Last round</span>}<span className="peer-preview">{preview}</span><ChevronRight size={15} /></summary>
    <p className="review-meta">{review.feedback
      ? 'Tether sent the reviewer tab’s reply here. This tab fixes what it raises.'
      : `The ${review.agent} tab ${review.tab} finished a turn. Reply APPROVED, or list what is wrong. Tether sends your reply to that tab.`}</p>
    <div className="review-quote"><Linked text={review.reply} /></div>
    {review.transcript && <p className="review-meta review-path" title={review.transcript}>{review.transcript}</p>}
  </details>
}
// One piece of a message that a long press can copy without any drag of the selection handles.
const COPY_BLOCK = 'p, li, td, th, pre, h1, h2, h3, h4, h5, h6'
// On a touch screen, a long press selects text in place. While a selection is in a message, this offers its paragraph and the whole message.
// onBacklog adds the selected text to the backlog. With a mouse, its button shows by the selection once the drag ends.
export function SelectionCopy({ messages, onBacklog }: { messages: { id: string; text: string }[]; onBacklog?: (text: string) => void }) {
  const [id, setId] = useState('')
  const [block, setBlock] = useState<HTMLElement | null>(null)
  const [at, setAt] = useState<{ x: number; y: number } | null>(null)
  const [coarse] = useState(() => window.matchMedia('(pointer: coarse)').matches)
  const mouse = !coarse && !!onBacklog
  useEffect(() => {
    if (!coarse && !mouse) return
    let timer = 0, down = false
    const elementOf = (node: Node | null | undefined) => node instanceof Element ? node : node?.parentElement
    const inMessage = (element: Element | null | undefined) => element?.closest('.assistant-message, .user-message, .peer-message')?.closest('[data-msg-id]')
    const selected = () => { const selection = window.getSelection(); return selection && !selection.isCollapsed ? selection : null }
    const changed = () => {
      clearTimeout(timer)
      const selection = selected()
      const anchor = elementOf(selection?.anchorNode)
      const next = inMessage(anchor)?.getAttribute('data-msg-id')
      if (!coarse) {
        // With a mouse, the button waits for the drag to end, and stays by the end of the selection.
        if (!next || down || !selection?.toString().trim()) { setId(''); setAt(null); return }
        const box = selection.getRangeAt(selection.rangeCount - 1).getBoundingClientRect()
        setId(next); setAt({ x: box.right, y: box.bottom })
        return
      }
      // A tap on a button can clear the selection just before its click arrives, so the buttons stay a moment.
      if (!next) { timer = window.setTimeout(() => { setId(''); setBlock(null) }, 300); return }
      const found = anchor?.closest<HTMLElement>(COPY_BLOCK) ?? null
      setId(next)
      setBlock(found && elementOf(selection?.focusNode)?.closest(COPY_BLOCK) === found ? found : null)
    }
    // A stray tap on the text keeps the selection. Links and buttons still work, and X clears it.
    const pressed = (event: MouseEvent) => {
      const target = event.target as Element
      if (coarse) { if (selected() && inMessage(target) && !target.closest('a, button, summary, input, textarea, label')) event.preventDefault(); return }
      if (event.button === 0 && !target.closest('.selection-backlog')) down = true
    }
    const released = () => { if (!down) return; down = false; timer = window.setTimeout(changed, 0) }
    const scrolled = () => { if (!coarse && selected()) changed() }
    document.addEventListener('selectionchange', changed)
    document.addEventListener('mousedown', pressed, true)
    document.addEventListener('mouseup', released, true)
    document.addEventListener('scroll', scrolled, true)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('selectionchange', changed); document.removeEventListener('mousedown', pressed, true)
      document.removeEventListener('mouseup', released, true); document.removeEventListener('scroll', scrolled, true)
    }
  }, [coarse, mouse])
  const text = messages.find(message => message.id === id)?.text
  if (!text) return null
  const addSelected = onBacklog && (() => { const chosen = window.getSelection()?.toString().trim(); setAt(null); if (chosen) onBacklog(chosen) })
  if (!coarse) {
    if (!at || !addSelected) return null
    const style = { left: Math.max(8, Math.min(at.x - 70, window.innerWidth - 168)), top: Math.min(at.y + 8, window.innerHeight - 44) }
    return <button type="button" className="selection-backlog copy-chip" style={style} onMouseDown={event => event.preventDefault()} onClick={addSelected}>
      <ClipboardPlus size={15} aria-hidden="true" /><span>Add to backlog</span>
    </button>
  }
  return <div className="selection-copy" role="group" aria-label="Copy selected message" onMouseDown={event => event.preventDefault()}>
    {/* Three chips and X must fit a 360px phone: with Backlog there, the copy icon says "Copy". */}
    {block?.isConnected && <CopyButton text={() => visibleText(block)} label="Copy paragraph" shown={addSelected && 'Paragraph'} className="copy-chip" wide />}
    <CopyButton text={text} label="Copy message" shown={addSelected && 'Message'} className="copy-chip" wide />
    {addSelected && <button type="button" className="copy-chip" title="Add the selected text to the backlog" onClick={addSelected}><ClipboardPlus size={16} aria-hidden="true" /><span>Backlog</span></button>}
    <button type="button" className="icon-button" aria-label="Clear selection" title="Clear selection" onClick={() => window.getSelection()?.removeAllRanges()}><X size={16} /></button>
  </div>
}
// Copy with a short "Copied" confirmation. The Clipboard API needs HTTPS, which Tailscale Serve gives us.
export function CopyButton({ text, label, className = 'icon-button', wide = false, shown }: { text: string | (() => string); label: string; className?: string; wide?: boolean; shown?: string }) {
  const [done, setDone] = useState<'' | 'ok' | 'fail'>('')
  useEffect(() => { if (!done) return; const timer = setTimeout(() => setDone(''), 1500); return () => clearTimeout(timer) }, [done])
  return <button type="button" className={className} aria-label={done === 'ok' ? 'Copied' : label} title={label}
    onClick={() => copyText(typeof text === 'string' ? text : text()).then(() => setDone('ok'), () => setDone('fail'))}>
    {done === 'ok' ? <Check size={16} /> : <Copy size={16} />}{className !== 'icon-button' && <span>{done === 'ok' ? 'Copied' : done === 'fail' ? 'Failed' : wide ? shown ?? label : 'Copy'}</span>}
  </button>
}
// Time of the reply, as the CLI shows it: "9:36 PM" today, "Fri 9:36 PM" this week, else the date.
function stamp(value?: string) {
  const at = value ? new Date(value) : null
  if (!at || Number.isNaN(at.getTime())) return ''
  const days = (Date.now() - at.getTime()) / 86400000
  const clock = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  if (at.toDateString() === new Date().toDateString()) return clock
  return days < 6 ? `${at.toLocaleDateString(undefined, { weekday: 'short' })} ${clock}` : `${at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} ${clock}`
}
function MessageActions({ text, time }: { text: string; time?: string }) {
  const canShare = typeof navigator.share === 'function'
  const when = stamp(time)
  return <div className="message-actions">
    <CopyButton text={text} label="Copy message" />
    <SpeakButton text={text} />
    {canShare && <button type="button" className="icon-button" aria-label="Share message" title="Share message" onClick={() => navigator.share({ text }).catch(() => {})}><Share2 size={16} /></button>}
    {when && <time className="message-time" dateTime={time}>{when}</time>}
  </div>
}
function SpeakButton({ text }: { text: string }) {
  // The text is the key, so the button also shows a reply that is read aloud by itself at the end of a turn.
  const reading = useSpeaking() === text
  if (!canSpeak) return null
  return <button type="button" className="icon-button" aria-label={reading ? 'Stop reading' : 'Read aloud'} title={reading ? 'Stop reading' : 'Read aloud'} aria-pressed={reading} onClick={() => reading ? stopSpeaking() : speak(text, text)}>{reading ? <Square size={14} /> : <Volume2 size={16} />}</button>
}
function CodeHeader({ language, code }: { language: string | undefined; code: string }) {
  return <div className="code-header"><span>{language || 'code'}</span><CopyButton text={code} label="Copy code" className="code-copy" /></div>
}
// The text as it reads on screen, without Copy controls or the sublists of a list item. innerText needs the element on the page.
function visibleText(element: HTMLElement) {
  const copy = element.cloneNode(true) as HTMLElement
  copy.querySelectorAll(element.matches('li') ? 'button, .code-header, ul, ol' : 'button, .code-header').forEach(item => item.remove())
  copy.style.cssText = 'position:fixed;left:-9999px;top:0;width:600px'
  document.body.append(copy)
  const text = copy.innerText.trim()
  copy.remove()
  return text
}
// Agents put text to paste somewhere else in a quote, so a quote gets its own Copy, like a code block.
function QuoteBlock(props: React.ComponentProps<'blockquote'>) {
  const quote = useRef<HTMLQuoteElement>(null)
  return <div className="quote-block"><blockquote ref={quote} {...props} /><CopyButton text={() => quote.current ? visibleText(quote.current) : ''} label="Copy quote" className="copy-chip" /></div>
}
export function Markdown() { return <MarkdownTextPrimitive remarkPlugins={[remarkGfm, remarkPaths, remarkTabs]} smooth={false} components={{ CodeHeader, code: InlineCode, a: Link, blockquote: QuoteBlock }} /> }
export const convertMessage = (message: ChatMessage): ThreadMessageLike => ({
  id: message.id, role: message.role, content: message.text,
  metadata: { custom: { kind: message.kind, title: message.title, count: message.count, tools: message.tools, text: message.text, time: message.time, images: message.images, imageKey: message.image_key,
    watch: message.kind === 'watch' ? { members: message.members ?? [], watches: message.watches ?? [], cycles: message.cycles ?? 1, rearmed: !!message.rearmed } : undefined } },
})

// Phone photos are large and carry location data: re-draw JPEG/HEIC photos (this drops the metadata) and shrink big images.
