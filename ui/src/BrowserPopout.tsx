import { useCallback, useEffect, useRef, useState } from 'react'
import { PanelRightOpen } from 'lucide-react'
import { ApiError, request, type Api } from './api'
import { setPaneLabels, WATCH_EVENT } from './AgentBrowser'
import { ATTACH_REFUSED, BrowserPanel } from './BrowserPanel'
import { BROWSER_WINDOW, type BrowserWindowMessage } from './BrowserWindow'
import './BrowserPopout.css'

const NO_CHAT = 'Open Tether to send this to a chat.'
type Target = Extract<BrowserWindowMessage, { type: 'target' }>

export default function BrowserPopout() {
  const [held, setHeld] = useState(false)
  const [target, setTarget] = useState<Target | null>(null)
  const [unauthorized, setUnauthorized] = useState(false)
  const [failure, setFailure] = useState('')
  const link = useRef<BroadcastChannel>(null)
  const replies = useRef(new Map<string, (error: string | null) => void>())
  const api: Api = useCallback(async (path, body, signal, method, timeout, priority) => {
    try { return await request(path, body, signal, method, timeout, priority) } catch (error) {
      if (error instanceof ApiError && error.status === 401) setUnauthorized(true)
      throw error
    }
  }, [])

  useEffect(() => {
    if (!navigator.locks || typeof BroadcastChannel === 'undefined') return
    const channel = new BroadcastChannel(BROWSER_WINDOW)
    const targets = new Map<string, Target>()
    const pending = replies.current
    const lifetime = new AbortController()
    let release: (() => void) | undefined
    link.current = channel
    channel.onmessage = (event: MessageEvent<BrowserWindowMessage>) => {
      const message = event.data
      switch (message.type) {
        case 'target': {
          if (message.focusedAt < 0) targets.delete(message.id)
          else targets.set(message.id, message)
          const latest = [...targets.values()].reduce<Target | null>(
            (best, item) => !best || item.focusedAt >= best.focusedAt ? item : best, null,
          )
          setTarget(latest)
          break
        }
        case 'labels': setPaneLabels(message.labels); break
        case 'dock': window.close(); break
        case 'focus': window.focus(); break
        case 'watch': {
          const { tab, pane, browser } = message
          window.dispatchEvent(new CustomEvent(WATCH_EVENT, { detail: { tab, pane, browser } }))
          window.focus()
          break
        }
        case 'attached': pending.get(message.req)?.(message.ok ? null : message.error ?? ATTACH_REFUSED); break
      }
    }
    void navigator.locks.request(BROWSER_WINDOW, { signal: lifetime.signal }, () => new Promise<void>(resolve => {
      release = resolve
      setHeld(true)
    })).catch(error => {
      if (lifetime.signal.aborted) return
      console.error('Cannot lock the browser window:', error)
      setFailure('The browser window did not open.')
    })
    return () => {
      lifetime.abort()
      release?.()
      for (const answer of pending.values()) answer(NO_CHAT)
      channel.close()
      link.current = null
    }
  }, [])
  // Announce after BrowserPanel has installed its Watch listeners.
  useEffect(() => {
    if (held) link.current?.postMessage({ type: 'hello' } satisfies BrowserWindowMessage)
  }, [held])
  const theme = target?.theme
  useEffect(() => {
    const root = document.documentElement
    if (theme && theme !== 'system') root.dataset.theme = theme
    else delete root.dataset.theme
  }, [theme])

  function attach(file: File) {
    const channel = link.current
    const pane = target?.pane
    if (!channel || !target || !pane) return Promise.resolve(NO_CHAT)
    const to = target.id
    const req = crypto.randomUUID()
    return new Promise<string | null>(resolve => {
      const answer = (error: string | null) => {
        window.clearTimeout(timer)
        replies.current.delete(req)
        resolve(error)
      }
      const timer = window.setTimeout(() => answer(NO_CHAT), 5000)
      replies.current.set(req, answer)
      channel.postMessage({ type: 'attach', req, to, pane, files: [file] } satisfies BrowserWindowMessage)
    })
  }
  function putBack() {
    link.current?.postMessage({ type: 'dock', to: target?.id } satisfies BrowserWindowMessage)
    window.close()
  }

  const unavailable = failure || (!navigator.locks || typeof BroadcastChannel === 'undefined' ? 'The browser window did not open.' : '')
  return <main className="browser-popout-page">
    <header className="browser-popout-head">
      <h1>Browser</h1>
      <span title={target?.pane ? `Send to chat goes to ${target.label}` : undefined}>{target?.pane ? <>Sends to <bdi>{target.label}</bdi></> : 'No Tether window'}</span>
      <button type="button" className="secondary" onClick={putBack}><PanelRightOpen aria-hidden="true" />Put back in Tether</button>
    </header>
    {unauthorized ? <p className="browser-popout-note" role="alert">Sign in to Tether in the main window, then reload this window.</p>
      : unavailable ? <p className="browser-popout-note" role="alert">{unavailable}</p>
      : held ? <BrowserPanel api={api} pane={{ id: target?.pane ?? '' }} folder={null} active onAttach={attach} />
      : <p className="browser-popout-note" role="status">Opening the browser window…</p>}
  </main>
}
