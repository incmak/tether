import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { AudioLines, Ellipsis, Folder, Globe, Home, Images, MessageCircle, SquareTerminal } from 'lucide-react'
import { ASSISTANT } from './assistant'
import type { Screen } from './Rail'
import { toggleButton } from './phoneScreen'
import './ExplorerPhone.css'

// The phone's bottom tab bar: Home | Chats | Files | Terminals | More. The desktop shows the left rail instead.
// Chats and Terminals are the chat panel and its terminal view; Files is the phone explorer.
// No tab is ever disabled: with no chat open, Terminals shows an empty state with the way to a chat (Home.tsx NoTerminal).
// The bar holds five items at most (Material 3 navigation bar: three to five destinations). Share, Browser and the
// assistant open from the More menu, two taps away.
// Actions such as Undo live in the toast, the chat header's ⋯ menu and the command menu.

const TABS: { id: Screen; label: string; icon: typeof Folder }[] = [
  { id: 'home', label: 'Home', icon: Home },
  { id: 'chats', label: 'Chats', icon: MessageCircle },
  { id: 'files', label: 'Files', icon: Folder },
  { id: 'terminals', label: 'Terminals', icon: SquareTerminal },
]
const MORE_MENU = 'phone-more-menu'

/** browser: opens the shared browser full screen. busy = an agent has tabs open, asking = an agent needs the person there. */
export interface PhoneBrowser { onOpen: () => void; busy: boolean; asking: boolean }

export function PhoneTabs({ screen, waiting = 0, onScreen, setScreen, onLive, browser }: { screen: Screen; waiting?: number; onScreen: (screen: Screen) => void; setScreen: (screen: Screen) => void; onLive?: (event?: { currentTarget: Element | null }) => void; browser?: PhoneBrowser }) {
  const [more, setMore] = useState(false)
  const moreButton = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  // The chat header has its own terminal button, and each pane keeps its own view. Keep the tab in step with what shows.
  useEffect(() => {
    if (screen === 'home' || screen === 'files' || screen === 'share') return
    const shell = document.querySelector('.app-shell')
    if (!shell) return
    const sync = () => {
      const toggle = toggleButton()
      const shown: Screen = toggle?.getAttribute('aria-pressed') === 'true' ? 'terminals' : 'chats'
      if (toggle && shown !== screen) setScreen(shown)
    }
    const observer = new MutationObserver(sync)
    observer.observe(shell, { subtree: true, childList: true, attributes: true, attributeFilter: ['aria-pressed'] })
    return () => observer.disconnect()
  }, [screen, setScreen])
  // The menu opens with its first row in focus, so the arrow keys and a screen reader start inside it.
  useEffect(() => { if (more) menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus() }, [more])

  const browserState = browser?.asking ? 'an agent needs you' : browser?.busy ? 'agents are using it' : ''
  // Focus goes back to More first: a dialog that the row opens (Browser, the assistant) returns focus there when it closes.
  const choose = (run: () => void) => { setMore(false); moreButton.current?.focus(); run() }
  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const rows = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])]
    const at = rows.indexOf(document.activeElement as HTMLButtonElement)
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setMore(false); moreButton.current?.focus(); return }
    // Tab leaves the menu: it continues from More, so focus does not fall to the top of the page.
    if (event.key === 'Tab') { moreButton.current?.focus(); setMore(false); return }
    const next = event.key === 'ArrowDown' ? rows[(at + 1) % rows.length] : event.key === 'ArrowUp' ? rows[(at - 1 + rows.length) % rows.length]
      : event.key === 'Home' ? rows[0] : event.key === 'End' ? rows[rows.length - 1] : undefined
    if (next) { event.preventDefault(); next.focus() }
  }

  return <nav className="phone-tabbar" aria-label="Tether">
    {TABS.map(({ id, label, icon: Icon }) => <button key={id} type="button" aria-current={screen === id ? 'page' : undefined}
      aria-label={id === 'home' && waiting ? `${label}, ${waiting} need you` : undefined} onClick={() => { setMore(false); onScreen(id) }}><span className="phone-tab-icon"><Icon size={22} aria-hidden="true" />{id === 'home' && waiting > 0 && <span className="phone-tab-dot asking" aria-hidden="true" />}</span><span>{label}</span></button>)}
    {/* More is current while one of its pages shows. Its dot and name carry the browser's state, as the Browser tab did. */}
    <button ref={moreButton} type="button" className="phone-tab-more" data-ui="More" aria-haspopup="menu" aria-expanded={more} aria-controls={more ? MORE_MENU : undefined}
      aria-current={screen === 'share' ? 'page' : undefined} aria-label={browserState ? `More, Browser: ${browserState}` : undefined} onClick={() => setMore(open => !open)}>
      <span className="phone-tab-icon"><Ellipsis size={22} aria-hidden="true" />{browser?.asking && <span className="phone-tab-dot asking" aria-hidden="true" />}</span>
      <span>More</span>
    </button>
    {more && <>
      <div className="phone-more-backdrop" aria-hidden="true" onClick={() => setMore(false)} />
      <div ref={menu} id={MORE_MENU} className="phone-more-menu" role="menu" aria-label="More" onKeyDown={onMenuKey}>
        <button type="button" role="menuitem" aria-current={screen === 'share' ? 'page' : undefined} onClick={() => choose(() => onScreen('share'))}>
          <span className="phone-tab-icon"><Images size={22} aria-hidden="true" /></span><span>Share</span>
        </button>
        {browser && <button type="button" role="menuitem" className="phone-tab-browser" data-ui="Browser" aria-label={browserState ? `Browser, ${browserState}` : undefined} onClick={() => choose(browser.onOpen)}>
          <span className="phone-tab-icon"><Globe size={22} aria-hidden="true" />{browserState && <span className={`phone-tab-dot${browser.asking ? ' asking' : ''}`} aria-hidden="true" />}</span>
          <span>Browser</span>{browserState && <small>{browser.asking ? 'An agent needs you' : 'Agents are using it'}</small>}
        </button>}
        {/* The assistant's panel grows from the More tab. */}
        {onLive && <button type="button" role="menuitem" onClick={() => choose(() => onLive({ currentTarget: moreButton.current }))}>
          <span className="phone-tab-icon"><AudioLines size={22} aria-hidden="true" /></span><span>{ASSISTANT}</span>
        </button>}
      </div>
    </>}
  </nav>
}
