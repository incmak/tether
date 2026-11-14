import { Component, type ErrorInfo, type ReactNode } from 'react'
import { copyText } from './clipboard'

// The last line of defence. Without it, any render error unmounts the whole app and leaves a blank page
// with no way out but a manual reload. This shows what broke, with Reload and Copy details.

function crashDetails(error: unknown, componentStack = '') {
  const head = error instanceof Error ? `${error.name}: ${error.message}` : String(error)
  const stack = error instanceof Error && error.stack ? error.stack : ''
  return [head, stack && stack !== head ? stack : '', componentStack.trim() && `Component stack:\n${componentStack.trim()}`,
    `Page: ${location.href}`, `Browser: ${navigator.userAgent}`].filter(Boolean).join('\n\n')
}

type State = { error: unknown, details: string, copied: boolean }

export class CrashScreen extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, details: '', copied: false }

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error ?? new Error('Unknown error'), details: crashDetails(error) }
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    this.setState({ details: crashDetails(error, info.componentStack ?? '') })
    console.error('Tether stopped:', error, info.componentStack)
  }

  copy = async () => {
    try { await copyText(this.state.details); this.setState({ copied: true }) } catch { /* the details stay selectable below */ }
  }

  render() {
    const { error, details, copied } = this.state
    if (!error) return this.props.children
    const message = error instanceof Error ? error.message : String(error)
    return <main className="signin crash" role="alert">
      <h1>Tether stopped</h1>
      <p>Part of the page failed to load. Your sessions keep running on the server.<br />Reload to open Tether again.</p>
      <div className="crash-actions">
        <button type="button" className="primary" onClick={() => location.reload()}>Reload</button>
        <button type="button" className="secondary" onClick={() => void this.copy()}>{copied ? 'Copied' : 'Copy details'}</button>
      </div>
      <details className="crash-details">
        <summary>{message || 'Error details'}</summary>
        <pre>{details}</pre>
      </details>
    </main>
  }
}
