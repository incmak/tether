import { ArrowLeft, ArrowRight, AudioLines, Folder, Home, Images, MessageCircle, SquareTerminal } from 'lucide-react'
import { ASSISTANT } from './assistant'
import { keyLabel } from './keys'
import { BACK_KEY, FORWARD_KEY } from './navKeys'

export type RailNav = { back: boolean; forward: boolean; onBack: () => void; onForward: () => void }

export type Screen = 'home' | 'chats' | 'files' | 'terminals' | 'share'
const SCREENS: { id: Screen; label: string; key: string; icon: typeof Folder }[] = [
  { id: 'home', label: 'Home', key: '⌥H', icon: Home },
  { id: 'chats', label: 'Chats', key: '⌘1', icon: MessageCircle },
  { id: 'files', label: 'Files', key: '⌘2', icon: Folder },
  { id: 'terminals', label: 'Terminals', key: '⌘3', icon: SquareTerminal },
  { id: 'share', label: 'Share', key: '⌘4', icon: Images },
]

/**
 * The desktop left rail: Back and Forward, then Home, Chats, Files, Terminals and Share. The phone does not show it.
 * waiting: how many agents need you. nav: whether Back and Forward have a step to go to inside Tether.
 */
export function Rail({ screen, waiting = 0, onScreen, onLive, nav }: { screen: Screen; waiting?: number; onScreen: (screen: Screen) => void; onLive?: () => void; nav?: RailNav }) {
  return <nav className="app-rail" aria-label="Tether">
    <span className="app-rail-mark" aria-hidden="true"><SquareTerminal size={16} /></span>
    {nav && <div className="app-rail-history" role="group" aria-label="History">
      {/* aria-disabled, not disabled: the buttons keep focus and their tooltip when there is nothing to go to. */}
      <button type="button" className="app-rail-step" aria-label="Back" aria-disabled={!nav.back} title={`Back  ${keyLabel(BACK_KEY)}`} onClick={() => { if (nav.back) nav.onBack() }}><ArrowLeft size={18} aria-hidden="true" /></button>
      <button type="button" className="app-rail-step" aria-label="Forward" aria-disabled={!nav.forward} title={`Forward  ${keyLabel(FORWARD_KEY)}`} onClick={() => { if (nav.forward) nav.onForward() }}><ArrowRight size={18} aria-hidden="true" /></button>
    </div>}
    {SCREENS.map(({ id, label, key, icon: Icon }) => <button key={id} type="button" className="app-rail-button" aria-label={id === 'home' && waiting ? `${label}, ${waiting} need you` : label} title={`${label}  ${keyLabel(key)}`}
      aria-current={screen === id ? 'page' : undefined} onClick={() => onScreen(id)}><Icon size={20} aria-hidden="true" />{id === 'home' && waiting > 0 && <span className="inbox-badge" aria-hidden="true">{waiting > 99 ? '99+' : waiting}</span>}</button>)}
    {onLive && <button type="button" className="app-rail-button app-rail-live" aria-label={`${ASSISTANT} Live mode`} title={`${ASSISTANT} Live mode: talk hands-free`} onClick={onLive}><AudioLines size={20} aria-hidden="true" /></button>}
  </nav>
}
