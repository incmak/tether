import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowLeft, ArrowUpRight, ChevronRight, Search, X } from 'lucide-react'
import type { Api } from './api'
import type { Prefs } from './prefs'
import { useModal } from './hooks'
import { SCHEMES, schemeById } from './schemes'
import { canSpeak } from './speech'
import { playChime, prepareChime } from './sound'
import { GlobalCodexLevel } from './CodexLevel'
import { GlobalAutonomy, ReviewerSettings } from './Autonomy'
import { AutonomySetup } from './AutonomySetup'
import { AgentBrowserSettings } from './AgentBrowser'
import { Choice, Notifications, SessionSettings, SystemSettings } from './SettingsControls'
import { matchesSetting, SETTINGS_GROUPS } from './settingsIndex'
import type { SettingIndex, SettingsGroup } from './settingsIndex'
import { keyLabel } from './keys'
import { DigestSettings } from './Attention'
import { LayoutPicker, StylePicker } from './LookSettings'
import './Settings.css'

type Scope = 'All devices' | 'This device' | 'This server'
interface Setting extends SettingIndex { content: ReactNode }
function Toggle({ title, note, checked, onChange, disabled = false, scope = 'All devices' }: { title: string; note: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean; scope?: Scope }) {
  return <label className="switch-row"><span><strong>{title}</strong><small>{note}</small><span className="preferences-scope">{scope}</span></span><input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} /></label>
}
function Field({ children, scope }: { children: ReactNode; scope: Scope }) {
  return <div className="preferences-field">{children}<span className="preferences-scope">{scope}</span></div>
}

export function Settings({ api, prefs, update, onReload, onClose, start }: { api: Api; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void; onReload: () => void; onClose: () => void; start?: 'layout-maker' }) {
  const modal = useModal(true, onClose)
  const [selected, setSelected] = useState<SettingsGroup | null>(start === 'layout-maker' ? 'appearance' : null)
  const [query, setQuery] = useState('')
  const search = useRef<HTMLInputElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const item = (id: string, group: SettingsGroup, title: string, keywords: string, content: ReactNode): Setting => ({ id, group, title, keywords, content })
  const items: Setting[] = [
    item('theme', 'appearance', 'Theme', 'light dark automatic system', <Field scope="All devices"><Choice legend="Theme" name="theme" value={prefs.theme} options={[["system", "System"], ["light", "Light"], ["dark", "Dark"]]} onChange={theme => update(() => ({ theme }))} /></Field>),
    item('colors', 'appearance', 'Colors', `color colour scheme palette ${SCHEMES.map(s => `${s.id} ${s.name}`).join(' ')}`, <Field scope="All devices"><fieldset className="choice"><legend>Colors</legend><div className="preferences-colors">{SCHEMES.map(scheme => <label key={scheme.id} title={scheme.name}><input type="radio" name="scheme" value={scheme.id} checked={schemeById(prefs.scheme).id === scheme.id} onChange={() => update(() => ({ scheme: scheme.id }))} /><span className="preferences-color" style={{ background: scheme[prefs.theme === 'light' ? 'light' : 'dark'].bg, borderColor: scheme[prefs.theme === 'light' ? 'light' : 'dark'].accent }} aria-hidden="true" /><span>{scheme.name}</span></label>)}</div></fieldset></Field>),
    item('layout', 'appearance', 'Layout', 'layout grid panels islands vs code vscode zed status column custom json my look make generate prompt import export preview', <Field scope="This device"><LayoutPicker api={api} prefs={prefs} update={update} onClose={onClose} focusMaker={start === 'layout-maker'} /></Field>),
    item('style', 'appearance', 'Style', 'glass flat blur translucent frosted backdrop ember graphite dusk tint colour color photo wallpaper image ai generate upload fill', <Field scope="This device"><StylePicker api={api} prefs={prefs} update={update} /></Field>),
    item('bubbles', 'appearance', 'Bubbles on every layout', 'agents bubble pinned pins side floating always show layout working hide', <Toggle title="Bubbles on every layout" note="Keep the agents bubble on top of every layout and style. Pinned bubbles always show." scope="This device" checked={prefs.agentBubble} onChange={agentBubble => update(() => ({ agentBubble }))} />),
    item('text-size', 'appearance', 'Text size', 'font small large readability zoom', <Field scope="This device"><Choice legend="Text size" name="text-size" value={prefs.textSize} options={[["small", "Small"], ["default", "Default"], ["large", "Large"]]} onChange={textSize => update(() => ({ textSize }))} /></Field>),
    item('folder', 'workspace', 'Default folder', 'working directory path home new session start projects', <Field scope="This server"><SessionSettings api={api} /></Field>),
    item('sort', 'workspace', 'Session order', 'sidebar sort smart manual recent', <Field scope="All devices"><Choice legend="Session order" name="sort" value={prefs.sort} options={[["smart", "Smart"], ["recent", "Recent"], ["name", "Name"], ["manual", "Manual"]]} onChange={sort => update(() => ({ sort }))} /><p className="setting-note">{prefs.sort === 'smart' ? 'Needs you, then Done, Working, and most recent.' : prefs.sort === 'recent' ? 'Needs you, then the latest agent activity.' : prefs.sort === 'name' ? 'Needs you, then A to Z.' : 'Use Reorder in the sidebar to arrange sessions.'}</p></Field>),
    item('paths', 'workspace', 'Folder paths', 'sidebar location directory', <Toggle title="Folder paths" note="Show the folder under each session name." checked={prefs.showPaths} onChange={showPaths => update(() => ({ showPaths }))} />),
    item('terms', 'workspace', 'tmux words', 'pane split tab terminology ctrl-b', <Toggle title="tmux words" note="Use pane, split and Ctrl-b in place of Tether’s terms." checked={prefs.tmuxWords} onChange={tmuxWords => update(() => ({ tmuxWords }))} />),
    item('preview', 'workspace', 'Live reply preview', 'claude response stream messages', <Toggle title="Live reply preview" note="Show Claude’s reply as it arrives." checked={prefs.liveReplyPreview} onChange={liveReplyPreview => update(() => ({ liveReplyPreview }))} />),
    item('attention', 'workspace', 'Attention mode', 'waiting on you needs you ask oldest handled snooze remind tomorrow digest daily summary', <>
      <Toggle title="Attention mode" note="Keep a tab in “Waiting on you” until you answer it, mark it handled or snooze it." checked={prefs.attention} onChange={attention => update(() => ({ attention }))} />
      {prefs.attention && <DigestSettings api={api} />}
    </>),
    item('auto-resume', 'workspace', 'Reopen tabs after a restart', 'auto resume restore restart tmux reboot', <Toggle title="Reopen tabs after a restart" note="Bring back open Claude and Codex chats when tmux or the machine restarts." scope="This server" checked={prefs.autoResume} onChange={autoResume => update(() => ({ autoResume }))} />),
    item('microphone', 'voice', 'Voice button', 'mic microphone dictate speech input keyboard', <Toggle title="Voice button" note="Show the microphone in the message box." checked={!prefs.voiceOff} onChange={on => update(() => ({ voiceOff: !on }))} />),
    item('chime', 'voice', 'Start sound', 'mic microphone ready chime volume', <Toggle title="Start sound" note="Play a chime when the microphone is ready." checked={prefs.voiceSound && !prefs.voiceOff} disabled={prefs.voiceOff} onChange={voiceSound => update(() => ({ voiceSound }))} />),
    ...(canSpeak ? [
      item('read-aloud', 'voice', 'Read replies aloud', 'speaker speech audio tts auto play', <Toggle title="Read replies aloud" note="Read each completed reply on this device." scope="This device" checked={prefs.readAloud} onChange={readAloud => update(() => ({ readAloud }))} />),
      item('hands-free', 'voice', 'Hands-free', 'automatic mic voice conversation silence pause handsfree', <Toggle title="Hands-free" note="Start the mic after speech. Pause two seconds to send." scope="This device" checked={prefs.readAloud && prefs.handsFree && !prefs.voiceOff} disabled={!prefs.readAloud || prefs.voiceOff} onChange={handsFree => update(() => ({ handsFree }))} />),
    ] : []),
    item('sound-test', 'voice', 'Test the sound', 'chime play volume audio', <div className="switch-row"><span><strong>Test the sound</strong><small>Uses this device’s media volume.</small></span><button type="button" className="secondary" onClick={() => { prepareChime(); void playChime() }}>Play</button></div>),
    item('notifications', 'voice', 'Push notifications', 'alerts permissions approvals urgent important background delivery sound silent test', <section className="preferences-nested"><h3>Notifications</h3><Notifications api={api} /></section>),
    item('main-model', 'agents', 'Main AI model', 'main model ai claude codex haiku local suggest groups helper', <Field scope="All devices"><Choice legend="Main AI model" name="main-model" value={prefs.mainModel} options={[["auto", "Automatic"], ["claude", "Claude"], ["codex", "Codex"], ["none", "Local only"]]} onChange={mainModel => update(() => ({ mainModel }))} /><p className="setting-note">{prefs.mainModel === 'auto' ? 'Suggest groups uses the first installed agent: Claude, then Codex. It sends session, folder and tab names to that agent. With no agent, it uses the local model.' : prefs.mainModel === 'claude' ? 'Suggest groups uses Claude Haiku. It sends session, folder and tab names to Claude.' : prefs.mainModel === 'codex' ? 'Suggest groups uses Codex at low effort. It sends session, folder and tab names to Codex.' : 'Suggest groups uses the local model on this server, then folders.'}</p></Field>),
    item('autonomy-setup', 'agents', 'Setup', 'setup install hook teams briskbot urgent ping notification approve', <Field scope="This server"><AutonomySetup api={api} /></Field>),
    item('autonomy', 'agents', 'Autonomy level', 'automation autopilot full auto ask questions check-in guided trusted level urgent', <Field scope="This server"><GlobalAutonomy api={api} /></Field>),
    item('reviewers', 'agents', 'Reviewers', 'review reviewer critical normal light model effort claude codex fable opus', <Field scope="This server"><ReviewerSettings api={api} /></Field>),
    item('codex', 'agents', 'Codex helper', 'claude delegation level model effort fast runs limits', <Field scope="This server"><GlobalCodexLevel api={api} /></Field>),
    item('browser', 'agents', 'Shared browser', 'agents chrome playwright setup connection claude codex', <Field scope="This server"><AgentBrowserSettings api={api} /></Field>),
    item('system', 'system', 'Updates and resources', 'version release reload refresh cpu memory host processes btop monitor', <Field scope="This server"><SystemSettings api={api} onReload={onReload} /></Field>),
  ]
  const searching = !!query.trim()
  const visible = searching ? items.filter(item => matchesSetting(item, query)) : items.filter(item => item.group === selected)
  const group = SETTINGS_GROUPS.find(group => group.id === selected)
  function choose(id: SettingsGroup) { setSelected(id); setQuery(''); body.current?.scrollTo({ top: 0 }) }
  function quick(value: string) { setQuery(value); search.current?.focus() }
  const navigation = <nav className="preferences-nav" aria-label="Settings categories">{SETTINGS_GROUPS.map(({ id, title, description, icon: Icon }) => <button type="button" key={id} aria-current={selected === id && !searching ? 'page' : undefined} onClick={() => choose(id)}><Icon size={19} strokeWidth={1.6} /><span><strong>{title}</strong><small>{description}</small></span><ChevronRight size={16} /></button>)}</nav>
  return <dialog {...modal} className={`sheet preferences ${selected || searching ? 'preferences-detail' : ''}`} aria-labelledby="settings-title" onKeyDown={event => {
    if (event.key === 'Tab') {
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], [tabindex]')].filter(element => element.tabIndex >= 0 && !element.matches(':disabled') && element.getClientRects().length > 0)
      const stops = controls.filter(element => {
        if (!(element instanceof HTMLInputElement) || element.type !== 'radio' || !element.name) return true
        const group = controls.filter((other): other is HTMLInputElement => other instanceof HTMLInputElement && other.type === 'radio' && other.name === element.name)
        return element === (group.find(radio => radio.checked) ?? group[0])
      })
      const first = stops[0], last = stops[stops.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    if (event.key === 'Escape' && query) { event.preventDefault(); event.stopPropagation(); setQuery(''); search.current?.focus() }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); event.stopPropagation(); search.current?.focus() }
  }}>
    <header className="preferences-header"><div><span className="preferences-eyebrow">Tether</span><h2 id="settings-title">Settings</h2></div><button type="button" className="icon-button" aria-label="Close settings" onClick={onClose}><X size={22} /></button></header>
    <div className="preferences-search"><Search size={18} aria-hidden="true" /><input ref={search} type="search" aria-label="Search settings" placeholder="Search settings…" value={query} autoComplete="off" onChange={event => setQuery(event.target.value)} />{query && <button type="button" className="icon-button" aria-label="Clear search" onClick={() => { setQuery(''); search.current?.focus() }}><X size={16} /></button>}<kbd aria-hidden="true">{keyLabel('⌘K')}</kbd></div>
    <div className="preferences-layout"><aside>{navigation}<p>Changes save automatically.<br />Folder changes use Save folder.</p></aside><div ref={body} className="preferences-content">
      {searching ? <><div className="preferences-content-heading"><span className="preferences-eyebrow">Search results</span><h3>{visible.length} {visible.length === 1 ? 'match' : 'matches'}</h3><p role="status">Results for “{query.trim()}”</p></div>{!visible.length && <div className="preferences-empty"><Search size={24} /><h4>No matching settings</h4><p>Try “folder”, “notifications”, “font” or “browser”.</p><button type="button" className="secondary" onClick={() => { setQuery(''); search.current?.focus() }}>Clear search</button></div>}</> : group ? <div className="preferences-content-heading"><button type="button" className="preferences-back" onClick={() => setSelected(null)}><ArrowLeft size={16} />All settings</button><h3>{group.title}</h3><p>{group.description}</p></div> : <div className="preferences-overview"><span className="preferences-eyebrow">Your workspace, your way</span><h3>Make Tether fit your work.</h3><p>Choose a category, or search for a setting.</p><div className="preferences-mobile-index">{navigation}</div><div className="preferences-quick"><h4>Quick access</h4>{[['Default folder', 'default folder'], ['Theme', 'theme'], ['Notifications', 'push notifications']].map(([label, query]) => <button type="button" key={label} onClick={() => quick(query)}>{label}<ArrowUpRight size={15} /></button>)}</div></div>}
      <div className="preferences-list">{visible.map((item, index) => <section key={item.id} className="preferences-entry" aria-label={item.title}>{searching && (index === 0 || visible[index - 1].group !== item.group) && <button type="button" className="preferences-result-group" onClick={() => choose(item.group)}>{SETTINGS_GROUPS.find(group => group.id === item.group)!.title}<ChevronRight size={13} /></button>}{item.content}</section>)}</div>
    </div></div>
  </dialog>
}
