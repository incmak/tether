import { needsYou } from './Sidebar'

// New session is the session mode of NewTask.tsx. This file keeps the small state badge the chat header uses.

export function State({ state }: { state: string }) {
  return <span className="pane-state"><span className={`state-dot ${needsYou(state) ? 'approval' : state.toLowerCase()}`} />{state}</span>
}
