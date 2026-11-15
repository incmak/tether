import { Loading } from './Skeleton'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, Approval, AskBridge, Attention, ChatData, Choice, DialogAction, Pane, PromptAction } from './api'
import { ChoiceDock } from './Dock'
import { usePolling } from './hooks'
import { agentNames, agoSeconds, paneTitle } from './Sidebar'

interface Props {
  api: Api
  pane: Pane
  id: string
  focus: boolean
  onOpen: () => void
  onSuccess: (answered: boolean) => void
}

export function QuickReply({ api, pane, id, focus, onOpen, onSuccess }: Props) {
  const fetcher = useCallback((signal: AbortSignal) => api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=1`, undefined, signal), [api, pane.id])
  const { data, error, refresh } = usePolling(fetcher, 1500)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [sendError, setSendError] = useState('')
  const writing = useRef(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const focusOnOpen = useRef(focus)
  const title = paneTitle(pane)
  const agent = agentNames[pane.agent]
  const approval = (data?.approval as Approval | null) ?? null
  const attention = data?.attention ?? null
  const ownAnswer = !approval && attention?.reason === 'menu' ? attention.options?.find(option => option.free) : undefined
  const reply = !!data && !data.plan && ((!approval && !attention) || !!ownAnswer)
  const disabled = busy || pane.writable === false || data?.writable === false

  useEffect(() => {
    if (!data || error || !focusOnOpen.current) return
    focusOnOpen.current = false
    if (matchMedia('(pointer: fine)').matches) input.current?.focus()
  }, [data, error])
  useLayoutEffect(() => {
    const box = input.current
    if (!box) return
    box.style.height = 'auto'
    const style = getComputedStyle(box)
    const max = parseFloat(style.lineHeight) * 4 + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
    box.style.height = `${Math.min(box.scrollHeight, max)}px`
  }, [text, reply, error])

  async function write(path: string, body: object, answered: boolean) {
    if (!data || data.plan || error || disabled || writing.current) return false
    writing.current = true
    setBusy(true); setSendError('')
    try {
      await api(path, { session: pane.id, thread: data.thread ?? null, ...body })
      onSuccess(answered)
      return true
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        setSendError('The chat changed. Check it, then try again.')
        await refresh()
      } else setSendError(errorText(failure))
      return false
    } finally { writing.current = false; setBusy(false) }
  }
  // A key from a read-only panel's footer, by action id. It does not answer anything, so the pin stays open.
  async function dialogAction(attention: Attention, action: DialogAction) {
    if (!data || disabled || writing.current) return null
    writing.current = true
    setBusy(true)
    try { await api('dialog', { session: pane.id, thread: data.thread ?? null, title: attention.dialog?.title, ...action }); return null }
    catch (failure) { return errorText(failure) }
    finally { writing.current = false; setBusy(false); await refresh() }
  }
  async function promptAction(attention: Attention, action: PromptAction) {
    if (!data || disabled || writing.current) return null
    writing.current = true
    setBusy(true)
    try {
      await api('prompt-action', { session: pane.id, thread: data.thread ?? null, promptId: attention.promptId, expectedRevision: attention.revision, actionId: action.id, requestId: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}` })
      return null
    } catch (failure) { return errorText(failure) }
    finally { writing.current = false; setBusy(false); await refresh() }
  }
  // A bridged Claude question: the answers go to the waiting hook as data. null gives the question back to the terminal.
  // The phone person is using the form: keep the question here (the server extends the deadline). Returns the new deadline.
  async function askEngage(ask: AskBridge) {
    try { return (await api<{ expires: number }>('ask-engage', { session: pane.id, id: ask.id, nonce: ask.nonce })).expires } catch { return null }
  }
  async function askAnswer(ask: AskBridge, answers: Record<string, string | string[]> | null) {
    if (!data || disabled || writing.current) return null
    writing.current = true
    setBusy(true)
    try {
      await api('ask-answer', { session: pane.id, id: ask.id, nonce: ask.nonce, answers, requestId: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}` })
      return null
    } catch (failure) { return errorText(failure) }
    finally { writing.current = false; setBusy(false); await refresh() }
  }
  async function choose(kind: 'approval' | 'menu', option: Choice, approval: Approval | null, feedback?: string) {
    if (kind === 'approval' && approval?.id) await write('approval', { approval: approval.id, choice: option.id, feedback }, true)
    else await write('menu', { choice: option.id, label: option.text }, true)
  }
  const send = () => {
    if (!text.trim()) return
    if (ownAnswer) void write('menu', { choice: ownAnswer.id, answer: text }, true)
    else void write('message', { message: text }, false)
  }
  const lastTurn = pane.finished ?? (data?.messages.length ? Date.parse(data.messages[data.messages.length - 1].time) / 1000 : 0)
  const ago = lastTurn ? agoSeconds(lastTurn) : 'now'
  const context = approval ? `${agent} needs your permission` : data?.state === 'Working' || data?.busy ? 'Working'
    : ago === 'now' ? 'Last turn just now' : `Last turn ${ago} ago`

  return <div id={id} className="side-pin-quick" aria-busy={busy}>
    <fieldset className="side-pin-quick-controls" disabled={disabled}>
      {error ? <p role="alert">{error}</p> : !data ? <Loading />
        : data.plan ? <div className="side-pin-plan"><p>{agent} wants you to review a plan.</p><button type="button" className="primary" disabled={disabled} onClick={onOpen}>Open chat</button></div>
        : <>
          {(approval || attention) && <ChoiceDock key={approval?.id ?? attention?.id ?? 'choice'} approval={approval} attention={attention} plan={data.plan} agent={agent} disabled={disabled}
            onChoose={choose} onKey={key => { void write('key', { key }, true) }} onAction={action => promptAction(attention!, action)} onDialog={action => dialogAction(attention!, action)} onAsk={askAnswer} onAskEngage={askEngage} onType={(text, loginUrl) => write('type', { text, enter: true, ...loginUrl ? { login_url: loginUrl } : {} }, true)} onFocusReply={() => input.current?.focus()} />}
          {reply && <form className="side-pin-reply-field" onSubmit={event => { event.preventDefault(); send() }}>
            <textarea ref={input} rows={1} maxLength={4000} placeholder={`Message ${title}`} aria-label={`Message ${title}`} value={text} disabled={disabled}
              onChange={event => setText(event.target.value)} onKeyDown={event => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.stopPropagation(); send() }
              }} />
            <button type="submit" className="side-pin-send" aria-label="Send" disabled={disabled || !text.trim()}><ArrowUp size={16} /></button>
          </form>}
        </>}
      <footer className="side-pin-quick-foot"><span>{data && !approval && !attention ? context : ''}</span><button type="button" disabled={disabled} onClick={onOpen}>Open chat</button></footer>
    </fieldset>
    {data?.login_result && <p role={data.login_result.phase === 'failed' ? 'alert' : 'status'}>{data.login_result.message}</p>}
    {sendError && <p className="side-pin-reply-error" role="alert">{sendError}</p>}
  </div>
}
