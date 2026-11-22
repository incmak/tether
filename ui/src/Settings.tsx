import { errorText } from './api'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { ReactNode } from 'react'
import { ArrowLeft, ChevronRight, Search, X } from 'lucide-react'
import type { Api } from './api'
import type { Prefs } from './prefs'
import { useModal } from './hooks'
import { SCHEMES, schemeById } from './schemes'
import { canReadAloud } from './speech'
import { playChime, prepareChime } from './sound'
import { GlobalCodexLevel } from './CodexLevel'
import { GlobalCommitGate } from './CommitGate'
import { GlobalClaudeAdvisor } from './ClaudeAdvisor'
import { AutoMergeSettings } from './AutoMerge'
import { GlobalAutonomy } from './Autonomy'
import { LevelUndoProvider } from './LevelUndo'
import { HelperSettings } from './Helpers'
import { LearningSettings } from './Learning'
import { CleanUpSettings, OpsSettings } from './Upkeep'
import { AutonomySetup } from './AutonomySetup'
import { AgentBrowserSettings } from './AgentBrowser'
import { Choice, Notifications, SessionSettings, SystemSettings } from './SettingsControls'
import { matchesSetting, SETTINGS_GROUPS } from './settingsIndex'
import type { SettingIndex, SettingsGroup } from './settingsIndex'
import { keyLabel } from './keys'
import { ShortcutRows } from './Shortcuts'
import { SHORTCUT_GROUPS } from './shortcutList'
import { DigestSettings, StaleRuleSetting } from './Attention'
import { LayoutPicker, StylePicker } from './LookSettings'
import { ActivitySettings } from './ActivityTime'
import { UiUsageSettings } from './UiUsage'
import { AppIdentitySettings } from './AppIdentity'
import { ActivityLog } from './ActivityLog'
import { TimeZoneSetting } from './TimeZoneSetting'
import './Settings.css'
import { ASSISTANT } from './assistant'

type Scope = 'All devices' | 'This device' | 'This server'
interface Setting extends SettingIndex { content: ReactNode }
function Toggle({ title, note, checked, onChange, disabled = false, scope = 'All devices' }: { title: string; note: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean; scope?: Scope }) {
  return <label className="switch-row"><span><strong>{title}</strong><small>{note}</small><span className="preferences-scope">{scope}</span></span><input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} /></label>
}
function Field({ children, scope }: { children: ReactNode; scope: Scope }) {
  return <div className="preferences-field">{children}<span className="preferences-scope">{scope}</span></div>
}

// Desktop shows the nav beside a page; a phone shows the nav as an index, then one page. Settings.css uses the same width.
const WIDE = '(min-width: 800px)'
function subscribeWide(listener: () => void) {
  const query = matchMedia(WIDE)
  query.addEventListener('change', listener)
  return () => query.removeEventListener('change', listener)
}
const useWide = () => useSyncExternalStore(subscribeWide, () => matchMedia(WIDE).matches)
const SECTIONS = [...new Set(SETTINGS_GROUPS.map(group => group.section))]

const TASK_AUTO_CLOSE = 'Close done tasks after 24 hours untouched'

function TaskAutoClose({ api }: { api: Api }) {
  const [checked, setChecked] = useState<boolean>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  type Result = { prefs: { task_auto_close?: boolean } }
  useEffect(() => {
    const controller = new AbortController()
    api<Result>('prefs', undefined, controller.signal).then(
      result => { if (!controller.signal.aborted) setChecked(result.prefs.task_auto_close === true) },
      failure => { if (!controller.signal.aborted) setError(errorText(failure)) },
    )
    return () => controller.abort()
  }, [api, attempt])
  async function change(value: boolean) {
    setPending(true)
    setError('')
    try {
      const result = await api<Result>('prefs', { changes: { task_auto_close: value } })
      setChecked(result.prefs.task_auto_close === true)
    } catch (failure) { setError(errorText(failure)) }
    finally { setPending(false) }
  }
  return <>
    <Toggle title={TASK_AUTO_CLOSE} note="Close the task and its helpers after 24 hours without activity or a visit. Tasks with a tab that cannot be undone, such as Antigravity, stay open." scope="This server"
      checked={checked ?? false} disabled={checked === undefined || pending} onChange={value => void change(value)} />
    {checked === undefined && !error && <p className="setting-note" role="status">Loading setting…</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {checked === undefined && error && <button type="button" className="secondary" onClick={() => { setError(''); setAttempt(attempt + 1) }}>Try again</button>}
  </>
}

export function Settings({ api, prefs, update, rejectedZone, onReload, onClose, start }: { api: Api; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void; rejectedZone?: string; onReload: () => void; onClose: () => void; start?: 'layout-maker' | 'notifications' }) {
  const modal = useModal(true, onClose, undefined, { view: 'settings' })
  const wide = useWide()
  const [selected, setSelected] = useState<SettingsGroup | null>(start === 'layout-maker' ? 'appearance' : start === 'notifications' ? 'notifications' : null)
  // The group to give focus back to when the phone returns to the index.
  const returnTo = useRef<SettingsGroup | null>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const [query, setQuery] = useState('')
  const search = useRef<HTMLInputElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const item = (id: string, group: SettingsGroup, title: string, keywords: string, content: ReactNode): Setting => ({ id, group, title, keywords, content })
  const items: Setting[] = [
    item('time-zone', 'general', 'Time zone', 'time zone timezone tz clock hours daytime reminders digest report ist utc gmt offset region city', <Field scope="All devices"><TimeZoneSetting rejectedZone={rejectedZone} onChange={timeZone => update(() => ({ timeZone }))} /></Field>),
    item('folder', 'general', 'Default folder', 'working directory path home new session start projects', <Field scope="This server"><SessionSettings api={api} /></Field>),
    item('auto-resume', 'general', 'Reopen tabs after a restart', 'auto resume restore restart tmux reboot', <Toggle title="Reopen tabs after a restart" note="Bring back open Claude and Codex chats when tmux or the machine restarts." scope="This server" checked={prefs.autoResume} onChange={autoResume => update(() => ({ autoResume }))} />),
    item('theme', 'appearance', 'Theme', 'light dark automatic system', <Field scope="All devices"><Choice legend="Theme" name="theme" value={prefs.theme} options={[["system", "System"], ["light", "Light"], ["dark", "Dark"]]} onChange={theme => update(() => ({ theme }))} /></Field>),
    item('colors', 'appearance', 'Colors', `color colour scheme palette ${SCHEMES.map(s => `${s.id} ${s.name}`).join(' ')}`, <Field scope="All devices"><fieldset className="choice"><legend>Colors</legend><div className="preferences-colors">{SCHEMES.map(scheme => <label key={scheme.id} title={scheme.name}><input type="radio" name="scheme" value={scheme.id} checked={schemeById(prefs.scheme).id === scheme.id} onChange={() => update(() => ({ scheme: scheme.id }))} /><span className="preferences-color" style={{ background: scheme[prefs.theme === 'light' ? 'light' : 'dark'].bg, borderColor: scheme[prefs.theme === 'light' ? 'light' : 'dark'].accent }} aria-hidden="true" /><span>{scheme.name}</span></label>)}</div></fieldset></Field>),
    item('text-size', 'appearance', 'Text size', 'font small large readability zoom', <Field scope="This device"><Choice legend="Text size" name="text-size" value={prefs.textSize} options={[["small", "Small"], ["default", "Default"], ["large", "Large"]]} onChange={textSize => update(() => ({ textSize }))} /></Field>),
    item('layout', 'appearance', 'Layout', 'layout grid panels islands vs code vscode zed status column custom json my look make generate prompt import export preview', <Field scope="This device"><LayoutPicker api={api} prefs={prefs} update={update} onClose={onClose} focusMaker={start === 'layout-maker'} /></Field>),
    item('style', 'appearance', 'Style', 'glass flat blur translucent frosted backdrop ember graphite dusk tint colour color photo wallpaper image ai generate upload fill', <Field scope="This device"><StylePicker api={api} prefs={prefs} update={update} /></Field>),
    item('bubbles', 'appearance', 'Bubbles on every layout', 'agents bubble pinned pins side floating always show layout working hide', <Toggle title="Bubbles on every layout" note="Keep the agents bubble on top of every layout and style. Pinned bubbles always show." scope="This device" checked={prefs.agentBubble} onChange={agentBubble => update(() => ({ agentBubble }))} />),
    item('app-identity', 'appearance', 'Installed app name and icon', 'app name icon colour color home screen pwa install installed launcher two servers hosts tell apart manifest', <Field scope="This server"><AppIdentitySettings api={api} /></Field>),
    item('sort', 'workspace', 'Session order', 'sidebar sort smart manual recent', <Field scope="All devices"><Choice legend="Session order" name="sort" value={prefs.sort} options={[["smart", "Smart"], ["recent", "Recent"], ["name", "Name"], ["manual", "Manual"]]} onChange={sort => update(() => ({ sort }))} /><p className="setting-note">{prefs.sort === 'smart' ? 'Needs you, then Done, Working, and most recent.' : prefs.sort === 'recent' ? 'Needs you, then the latest agent activity.' : prefs.sort === 'name' ? 'Needs you, then A to Z.' : 'Use Reorder in the sidebar to arrange sessions.'}</p></Field>),
    item('paths', 'workspace', 'Folder paths', 'sidebar location directory', <Toggle title="Folder paths" note="Show the folder under each session name." checked={prefs.showPaths} onChange={showPaths => update(() => ({ showPaths }))} />),
    item('terms', 'workspace', 'tmux words', 'pane split tab terminology ctrl-b', <Toggle title="tmux words" note="Use pane, split and Ctrl-b in place of Tether’s terms." checked={prefs.tmuxWords} onChange={tmuxWords => update(() => ({ tmuxWords }))} />),
    item('header-swipe', 'workspace', 'Swipe on the tab bar scrolls it', 'trackpad two finger swipe gesture header tab bar stacks scroll back forward history mac', <Toggle title="Swipe on the tab bar scrolls it" note="On a trackpad, swipe with two fingers over a chat’s tabs or stacks, or just above them, to scroll that row. The swipe does not go Back or Forward anywhere in the chat’s header. Off: it goes Back or Forward, as on the rest of the page." scope="This device" checked={prefs.headerSwipe} onChange={headerSwipe => update(() => ({ headerSwipe }))} />),
    item('preview', 'workspace', 'Live reply preview', 'claude response stream messages', <Toggle title="Live reply preview" note="Show Claude’s reply as it arrives." checked={prefs.liveReplyPreview} onChange={liveReplyPreview => update(() => ({ liveReplyPreview }))} />),
    item('attention', 'workspace', 'Attention mode', 'waiting on you needs you ask oldest handled snooze remind tomorrow digest daily summary later stale automation', <>
      <Toggle title="Attention mode" note="Keep a tab in “Waiting on you” until you answer it, mark it handled or snooze it." checked={prefs.attention} onChange={attention => update(() => ({ attention }))} />
      {prefs.attention && <DigestSettings api={api} />}
      {prefs.attention && <StaleRuleSetting api={api} />}
    </>),
    item('microphone', 'voice', 'Voice button', 'mic microphone dictate speech input keyboard', <Toggle title="Voice button" note="Show the microphone in the message box." checked={!prefs.voiceOff} onChange={on => update(() => ({ voiceOff: !on }))} />),
    item('chime', 'voice', 'Start sound', 'mic microphone ready chime volume', <Toggle title="Start sound" note="Play a chime when the microphone is ready." checked={prefs.voiceSound && !prefs.voiceOff} disabled={prefs.voiceOff} onChange={voiceSound => update(() => ({ voiceSound }))} />),
    item('sound-test', 'voice', 'Test the sound', 'chime play volume audio', <div className="switch-row"><span><strong>Test the sound</strong><small>Uses this device’s media volume.</small></span><button type="button" className="secondary" onClick={() => { prepareChime(); void playChime() }}>Play</button></div>),
    ...(canReadAloud ? [
      item('read-aloud', 'voice', 'Read replies aloud', 'speaker speech audio tts auto play', <Toggle title="Read replies aloud" note="Read each completed reply on this device." scope="This device" checked={prefs.readAloud} onChange={readAloud => update(() => ({ readAloud }))} />),
      item('hands-free', 'voice', 'Hands-free', 'automatic mic voice conversation silence pause handsfree', <Toggle title="Hands-free" note="Start the mic after speech. Pause two seconds to send." scope="This device" checked={prefs.readAloud && prefs.handsFree && !prefs.voiceOff} disabled={!prefs.readAloud || prefs.voiceOff} onChange={handsFree => update(() => ({ handsFree }))} />),
    ] : []),
    item('friday-acts', 'voice', `${ASSISTANT} can act in Tether`, 'friday live assistant voice call act agency open show switch go to chat session tab panel screen window navigate', <Toggle title={`${ASSISTANT} can act in Tether`} note={`When you ask, ${ASSISTANT} opens chats, sessions and panels on your screen. Off: ${ASSISTANT} leaves your screen as it is.`} checked={prefs.fridayActs} onChange={fridayActs => update(() => ({ fridayActs }))} />),
    item('notifications', 'notifications', 'Push notifications', 'alerts permissions approvals urgent important background delivery sound silent test', <section className="preferences-nested"><Notifications api={api} /></section>),
    item('autonomy', 'agents', 'Autonomy level', 'automation autopilot full auto ask questions check-in guided trusted level urgent', <Field scope="This server"><GlobalAutonomy api={api} /></Field>),
    item('autonomy-setup', 'agents', 'Setup', 'setup install hook teams briskbot urgent ping notification approve', <Field scope="This server"><AutonomySetup api={api} /></Field>),
    item('task-auto-close', 'agents', TASK_AUTO_CLOSE, 'done tasks auto close 24 hours tabs untouched', <TaskAutoClose api={api} />),
    item('main-model', 'helpers', 'Main AI model', 'main model ai claude codex haiku local suggest groups helper tab name description brief', <Field scope="All devices"><Choice legend="Main AI model" name="main-model" value={prefs.mainModel} options={[["auto", "Automatic"], ["claude", "Claude"], ["codex", "Codex"], ["none", "Local only"]]} onChange={mainModel => update(() => ({ mainModel }))} /><p className="setting-note">{prefs.mainModel === 'auto' ? 'Suggest groups uses the first installed agent: Claude, then Codex. It sends session, folder and tab names to that agent. With no agent, it uses the local model.' : prefs.mainModel === 'claude' ? 'Suggest groups uses Claude Haiku. It sends session, folder and tab names to Claude.' : prefs.mainModel === 'codex' ? 'Suggest groups uses Codex at low effort. It sends session, folder and tab names to Codex.' : 'Suggest groups uses the local model on this server, then folders.'} {prefs.mainModel === 'none' ? 'AI tab names and descriptions are off.' : 'AI tab names and descriptions use the tab’s own agent: Claude Haiku, or Codex at low effort. They send the first prompts and replies of that tab.'}</p></Field>),
    item('advisor', 'helpers', 'Advisor and reviewers', 'advisor reviewers review reviewer second opinion consult never guess decide critical normal light latest model effort claude codex fable opus gpt', <Field scope="This server"><HelperSettings api={api} /></Field>),
    item('claude-advisor', 'helpers', 'Claude Code advisor', 'advisor tool advisormodel opus fable consult stronger model second opinion credits', <Field scope="This server"><GlobalClaudeAdvisor api={api} /></Field>),
    item('codex', 'helpers', 'Codex helper', 'claude delegation level model effort fast runs limits', <Field scope="This server"><GlobalCodexLevel api={api} /></Field>),
    item('commit-gate', 'gates', 'Commit gate', 'commit gate verified trailer stamp verification observe enforce block false block rate git', <Field scope="This server"><GlobalCommitGate api={api} /></Field>),
    item('auto-merge', 'gates', 'Auto-merge', 'auto merge automerge branch main worker review approved trust ladder observe enforce revert risk', <Field scope="This server"><AutoMergeSettings api={api} /></Field>),
    item('learning', 'upkeep', 'Learning from feedback', 'learn learning lessons feedback self-learning reward punishment correct mistake memory remember improve', <Field scope="This server"><LearningSettings api={api} /></Field>),
    item('cleanup', 'upkeep', 'Clean up lessons and notes', 'clean cleanup prune stale old lessons notes remove delete undo weekly tidy', <Field scope="This server"><CleanUpSettings api={api} /></Field>),
    item('ops-report', 'upkeep', 'Daily ops report', 'ops operations postmortem daily report verified stamps reverts paused reviews lesson candidates proposed haiku tokens schedule', <Field scope="This server"><OpsSettings api={api} /></Field>),
    item('browser', 'browser', 'Shared browser', 'agents chrome playwright setup connection claude codex', <Field scope="This server"><AgentBrowserSettings api={api} /></Field>),
    item('activity-log', 'activity', 'Activity log', 'audit log history trail who changed what when record recorded device tab agent actions undo codex level autonomy reviewers advisor passkey backlog lessons reviews approvals verdict', <ActivityLog api={api} />),
    item('activity-time', 'usage', 'Activity time', 'metrics analytics coding time focus hours tracking stats dashboard agents publish export wakatime', <ActivitySettings api={api} prefs={prefs} update={update} />),
    item('ui-usage', 'usage', 'UI usage', 'telemetry metrics analytics clicks usage menu buttons least most used navigation ux report log', <UiUsageSettings api={api} prefs={prefs} update={update} />),
    // The same list as the ⌘/ sheet. A search here finds a key by its action, like "split" or "backlog".
    ...SHORTCUT_GROUPS.map(keys => item(`keys-${keys.name.toLowerCase()}`, 'shortcuts', `${keys.name} keys`, `keyboard shortcut hotkey key ${keys.items.map(([label, key]) => `${label} ${key} ${keyLabel(key)}`).join(' ')}`,
      <section className="preferences-nested"><h3>{keys.name}</h3><ShortcutRows group={keys} /></section>)),
    item('system', 'system', 'Updates and resources', 'version release reload refresh cpu memory host processes btop monitor', <Field scope="This server"><SystemSettings api={api} onReload={onReload} /></Field>),
  ]
  const searching = !!query.trim()
  // Desktop always shows a page: General until you choose another. A phone starts on the index.
  const current = selected ?? (wide ? 'general' : null)
  const visible = searching ? items.filter(item => matchesSetting(item, query)) : items.filter(item => item.group === current)
  const group = SETTINGS_GROUPS.find(group => group.id === current)
  // Opened from /chat/?go=notifications: show the notification switch, not the top of its group.
  useEffect(() => { if (start === 'notifications') body.current?.querySelector('[data-setting="notifications"]')?.scrollIntoView({ block: 'start' }) }, [start])
  // When the control you used leaves the screen (a phone index row, or a search result), focus goes to the new page's
  // title, and Back on a phone returns it to the row.
  // A deep link on a phone opens straight on a page, so it starts there too.
  const focusHeading = useRef(!!start && !wide)
  useEffect(() => {
    if (!focusHeading.current) return
    focusHeading.current = false
    heading.current?.focus({ preventScroll: true })
  }, [selected, query])
  // A resize can hide the control that has focus (the desktop nav or the phone index): focus goes to the page title,
  // or on the phone index to the current row.
  useEffect(() => {
    const active = document.activeElement
    // getClientRects: a hidden control has none (checkVisibility needs Safari 17.4).
    if (active instanceof HTMLElement && active !== document.body && active.getClientRects().length) return
    if (heading.current) heading.current.focus()
    else body.current?.querySelector<HTMLElement>(`.preferences-index [data-group="${returnTo.current ?? 'general'}"]`)?.focus()
  }, [wide])
  useEffect(() => {
    if (!returnTo.current || selected) return
    body.current?.querySelector<HTMLElement>(`[data-group="${returnTo.current}"]`)?.focus()
    returnTo.current = null
  }, [selected])
  function choose(id: SettingsGroup, fromResult = false) { focusHeading.current = fromResult || !wide; setSelected(id); setQuery(''); body.current?.scrollTo({ top: 0 }) }
  function back() { returnTo.current = current; setSelected(null); body.current?.scrollTo({ top: 0 }) }
  const navigation = (place: 'side' | 'index') => <nav className="preferences-nav" aria-label="Settings categories">{SECTIONS.map(section => <div key={section} role="group" aria-labelledby={`settings-${place}-${section}`}>
    <h3 id={`settings-${place}-${section}`} className="preferences-section">{section}</h3>
    {SETTINGS_GROUPS.filter(group => group.section === section).map(({ id, title, description, icon: Icon }) => <button type="button" key={id} data-group={id} aria-current={current === id && !searching ? 'page' : undefined} onClick={() => choose(id)}><Icon size={18} strokeWidth={1.6} aria-hidden="true" /><span><strong>{title}</strong><small>{description}</small></span><ChevronRight size={16} aria-hidden="true" /></button>)}
  </div>)}</nav>
  return <dialog {...modal} className={`sheet preferences ${current || searching ? 'preferences-detail' : ''}`} aria-labelledby="settings-title" onKeyDown={event => {
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
    <LevelUndoProvider>
    <header className="preferences-header"><div className="preferences-title"><h2 id="settings-title">Settings</h2><p>Changes save at once, except fields with a Save button.</p></div>
      <div className="preferences-search"><Search size={18} aria-hidden="true" /><input ref={search} type="search" aria-label="Search settings" placeholder="Search settings…" value={query} autoComplete="off" onChange={event => setQuery(event.target.value)} />{query && <button type="button" className="icon-button" aria-label="Clear search" onClick={() => { setQuery(''); search.current?.focus() }}><X size={16} /></button>}<kbd aria-hidden="true">{keyLabel('⌘K')}</kbd></div>
      <button type="button" className="icon-button preferences-close" aria-label="Close settings" onClick={onClose}><X size={22} /></button></header>
    <div className="preferences-layout"><aside>{navigation('side')}</aside><div ref={body} className="preferences-content">
      {searching ? <><div className="preferences-content-heading"><span className="preferences-eyebrow">Search results</span><h3>{visible.length} {visible.length === 1 ? 'match' : 'matches'}</h3><p role="status">Results for “{query.trim()}”</p></div>{!visible.length && <div className="preferences-empty"><Search size={24} aria-hidden="true" /><h4>No matching settings</h4><p>Try “folder”, “notifications”, “font” or “browser”.</p><button type="button" className="secondary" onClick={() => { setQuery(''); search.current?.focus() }}>Clear search</button></div>}</>
        : group ? <div className="preferences-content-heading"><button type="button" className="preferences-back" onClick={back}><ArrowLeft size={16} aria-hidden="true" />All settings</button><h3 ref={heading} tabIndex={-1}>{group.title}</h3><p>{group.description}</p></div>
        : <div className="preferences-index">{navigation('index')}</div>}
      <div className="preferences-list">{visible.map((item, index) => <section key={item.id} className="preferences-entry" aria-label={item.title} data-setting={item.id}>{searching && (index === 0 || visible[index - 1].group !== item.group) && <button type="button" className="preferences-result-group" onClick={() => choose(item.group, true)}>{SETTINGS_GROUPS.find(group => group.id === item.group)!.title}<ChevronRight size={13} aria-hidden="true" /></button>}{item.content}</section>)}</div>
    </div></div>
    </LevelUndoProvider>
  </dialog>
}
