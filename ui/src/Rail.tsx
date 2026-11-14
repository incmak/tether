import { AudioLines, Folder, Home, Images, MessageCircle, SquareTerminal } from 'lucide-react'
import { ASSISTANT } from './assistant'
import { keyLabel } from './keys'

export type Screen = 'home' | 'chats' | 'files' | 'terminals' | 'share'
const SCREENS: { id: Screen; label: string; key: string; icon: typeof Folder }[] = [
  { id: 'home', label: 'Home', key: '⌥H', icon: Home },
  { id: 'chats', label: 'Chats', key: '⌘1', icon: MessageCircle },
  { id: 'files', label: 'Files', key: '⌘2', icon: Folder },
  { id: 'terminals', label: 'Terminals', key: '⌘3', icon: SquareTerminal },
  { id: 'share', label: 'Share', key: '⌘4', icon: Images },
]

/** The desktop left rail: Home, Chats, Files, Terminals and Share. The phone does not show it. waiting: how many agents need you. */
export function Rail({ screen, waiting = 0, onScreen, onLive }: { screen: Screen; waiting?: number; onScreen: (screen: Screen) => void; onLive?: () => void }) {
  return <nav className="app-rail" aria-label="Tether">
    <span className="app-rail-mark" aria-hidden="true"><SquareTerminal size={16} /></span>
    {SCREENS.map(({ id, label, key, icon: Icon }) => <button key={id} type="button" className="app-rail-button" aria-label={id === 'home' && waiting ? `${label}, ${waiting} need you` : label} title={`${label}  ${keyLabel(key)}`}
      aria-current={screen === id ? 'page' : undefined} onClick={() => onScreen(id)}><Icon size={20} aria-hidden="true" />{id === 'home' && waiting > 0 && <span className="inbox-badge" aria-hidden="true">{waiting > 99 ? '99+' : waiting}</span>}</button>)}
    {onLive && <button type="button" className="app-rail-button app-rail-live" aria-label={`${ASSISTANT} Live mode`} title={`${ASSISTANT} Live mode: talk hands-free`} onClick={onLive}><AudioLines size={20} aria-hidden="true" /></button>}
  </nav>
}
