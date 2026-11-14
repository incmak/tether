import { Spinner } from './Skeleton'
import { VoiceField } from './VoiceField'
import { useEffect, useRef, useState } from 'react'
import { ApiError, type Api } from './api'
import type { Prefs } from './prefs'
import { useModal } from './hooks'
import { BUILT_INS, CUSTOM_PREFIX, DEFAULT_LOOK, MANIFEST_VERSION, deleteLayout, generateLayout, layoutData, lookKey, lookLayout, saveLayout, themeLabel, useCustomLayouts, validateLayout } from './layouts'
import type { Backdrop, Layout, Look, Surface } from './layouts'
import { LayoutPreviews } from './LayoutPreview'
import { startTrial } from './lookTrial'
import { backgroundJob, deleteBackground, listBackgrounds, startBackground, uploadBackground, type BackgroundItem } from './backgrounds'
import { applyPhoto } from './photoLook'
import { Choice } from './SettingsControls'
import { keyLabel } from './keys'

// Settings › Appearance: Layout and Style (layouts.ts). A layout with its own colours replaces the user's colours, so
// Tether first offers to save the current look as "My look", which then comes back in one click.
// New layouts (made from a prompt, imported or updated) always show a preview on a desktop, a tablet and a phone first.
// "Save and try" closes Settings and goes back after 10 seconds unless the user keeps it (lookTrial.ts).

type Update = (change: (current: Prefs) => Partial<Prefs>) => void
const SWATCHES = ['--bg', '--surface', '--text', '--accent', '--attention']
const message = (error: unknown) => error instanceof Error ? error.message : String(error)
const aborted = (error: unknown) => error instanceof DOMException && error.name === 'AbortError'

function SaveLook({ next, onSave, onSkip, onCancel }: { next: Layout; onSave: () => void; onSkip: () => void; onCancel: () => void }) {
  const modal = useModal(true, onCancel)
  const [colours] = useState(() => { const style = getComputedStyle(document.documentElement); return SWATCHES.map(name => style.getPropertyValue(name).trim()) })
  return <dialog {...modal} onCancel={event => { event.stopPropagation(); modal.onCancel(event) }} className="sheet look-save" aria-labelledby="look-save-title">
    <h2 id="look-save-title">Save your current look first?</h2>
    <p>“{next.name}” brings its own colours ({themeLabel(next)}). They replace your current colours. Save your current layout and colours as “My look” to come back to them in one click.</p>
    <div className="look-swatches" aria-hidden="true">{colours.map((colour, at) => <span key={SWATCHES[at]} style={{ background: colour }} />)}</div>
    <div className="sheet-buttons look-save-buttons">
      <button type="button" className="secondary" onClick={onCancel}>Cancel</button>
      <button type="button" className="secondary" onClick={onSkip}>Don’t save</button>
      <button type="button" className="primary" onClick={onSave}>Save as “My look”, then apply</button>
    </div>
  </dialog>
}

/** A button that asks once ("Delete it?") and acts on a second press within 4 seconds. */
function ConfirmButton({ label, confirm, onConfirm, className = 'secondary', title, disabled }: { label: string; confirm: string; onConfirm: () => void; className?: string; title?: string; disabled?: boolean }) {
  const [asking, setAsking] = useState(false)
  useEffect(() => { if (!asking) return; const timer = setTimeout(() => setAsking(false), 4000); return () => clearTimeout(timer) }, [asking])
  return <button type="button" disabled={disabled} title={title} aria-label={title && !asking ? title : undefined} className={`${className}${asking ? ' look-confirm' : ''}`} onClick={() => { if (asking) { setAsking(false); onConfirm() } else setAsking(true) }}>{asking ? confirm : label}</button>
}

function download(layout: Layout) {
  const blob = new Blob([JSON.stringify(layoutData(layout), null, 2) + '\n'], { type: 'application/json' })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = `${layout.id}.json`
  link.click()
  setTimeout(() => URL.revokeObjectURL(link.href), 1000)
}

interface Draft { layout: Layout; notes: string; from: 'prompt' | 'import' | 'update'; replaces?: string }

/** Make a layout from a short wish, or show an imported or updated one, with the 3-device preview. */
function LayoutMaker({ api, draft, setDraft, focus, current, onSaved, onTry }: {
  api: Api; draft: Draft | null; setDraft: (draft: Draft | null) => void; focus: boolean; current: Layout
  onSaved: () => void; onTry: (layout: Layout) => void
}) {
  const [prompt, setPrompt] = useState('')
  const [voiceBusy, setVoiceBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState<'' | 'save' | 'try'>('')
  const job = useRef<AbortController | null>(null)
  const box = useRef<HTMLTextAreaElement>(null)
  // The settings dialog focuses its own Close button on open, so the box takes focus just after that.
  useEffect(() => {
    if (!focus) return
    const timer = setTimeout(() => { box.current?.focus(); box.current?.scrollIntoView({ block: 'center' }) }, 80)
    return () => clearTimeout(timer)
  }, [focus])
  useEffect(() => () => job.current?.abort(), [])

  const make = async () => {
    const wish = prompt.trim()
    if (!wish || busy || voiceBusy) return
    setBusy(true); setError(''); setConflict('')
    const controller = new AbortController()
    job.current = controller
    try {
      // A second wish changes the draft; the first starts from the layout on screen.
      const result = await generateLayout(api, wish, draft?.layout ?? current, controller.signal)
      setDraft({ layout: result.layout, notes: result.notes, from: 'prompt' })
    } catch (failure) { if (!aborted(failure)) setError(message(failure)) }
    finally { if (job.current === controller) job.current = null; setBusy(false) }
  }
  const save = async (andTry: boolean, overwrite = false) => {
    if (!draft) return
    setError(''); setConflict('')
    try {
      const { layout } = await saveLayout(api, draft.layout, overwrite || draft.replaces === draft.layout.id)
      const clean = validateLayout(layout, layout.id)
      setDraft(null); setPrompt(''); onSaved()
      if (andTry) onTry(clean)
    } catch (failure) {
      const taken = failure instanceof ApiError && failure.status === 409
      if (taken) setConflict(andTry ? 'try' : 'save')
      setError(taken ? 'You already have a layout with this id.' : message(failure))
    }
  }
  const setPhone = (keep: boolean) => { if (draft) setDraft({ ...draft, layout: { ...draft.layout, phone: keep ? 'keep' : undefined } }) }

  return <section className="look-maker" aria-labelledby="look-maker-title">
    <h4 id="look-maker-title">Make a layout <kbd>{keyLabel('⌥L')}</kbd></h4>
    <p className="setting-note">Describe it in a few words. Tether adds the full instructions, builds it from Tether’s panels, and shows it on each device before anything changes.</p>
    <form className="look-maker-form" onSubmit={event => { event.preventDefault(); void make() }}>
      <VoiceField multiline label="layout description" value={prompt} setValue={setPrompt} fieldRef={box} onBusyChange={setVoiceBusy}
        field={{ className: 'look-textarea', rows: 2, maxLength: 2000, disabled: busy, 'aria-label': 'Describe the layout',
          placeholder: draft ? 'Change it, for example: darker, sessions list on the right' : 'For example: like Telegram, each session a person with an avatar',
          onKeyDown: event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void make() } } }} />
      <div className="look-maker-buttons">
        {busy ? <>
          <span className="look-busy" role="status"><Spinner />Making the layout. This takes about a minute.</span>
          <button type="button" className="secondary" onClick={() => job.current?.abort()}>Stop</button>
        </> : <button type="submit" className="primary" disabled={voiceBusy || !prompt.trim()}>{draft ? 'Change the draft' : 'Make layout'}</button>}
      </div>
    </form>
    {error && <p className="setting-note look-error" role="alert">{error}{conflict && draft && <> <button type="button" className="link-button" onClick={() => void save(conflict === 'try', true)}>Replace it</button></>}</p>}
    {draft && <div className="look-draft">
      <div className="look-draft-head">
        <strong>{draft.layout.name}</strong>
        <span className="look-draft-kind">{draft.from === 'import' ? 'Imported' : draft.from === 'update' ? 'Updated' : 'Draft'}, not saved</span>
      </div>
      {(draft.notes || draft.layout.description) && <p className="setting-note">{draft.notes || draft.layout.description}</p>}
      <LayoutPreviews layout={draft.layout} />
      <label className="look-check"><input type="checkbox" checked={draft.layout.phone === 'keep'} onChange={event => setPhone(event.target.checked)} />Desktop and tablet only: the phone keeps its own look</label>
      <div className="look-draft-buttons">
        <button type="button" className="secondary" onClick={() => { setDraft(null); setError('') }}>Discard</button>
        <button type="button" className="secondary" onClick={() => void save(false)}>Save</button>
        <button type="button" className="primary" onClick={() => void save(true)}>Save and try</button>
      </div>
      <p className="setting-note">Save and try closes Settings. The layout goes back after 10 seconds unless you choose Keep.</p>
    </div>}
  </section>
}

export function LayoutPicker({ api, prefs, update, onClose, focusMaker = false }: { api: Api; prefs: Prefs; update: Update; onClose?: () => void; focusMaker?: boolean }) {
  const [version, setVersion] = useState(0)
  const { custom, error } = useCustomLayouts(api, true, version)
  const [asking, setAsking] = useState<Layout | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [listError, setListError] = useState('')
  const [updating, setUpdating] = useState('')
  const importer = useRef<HTMLInputElement>(null)
  const look = prefs.look ?? DEFAULT_LOOK
  const shown = lookLayout(look)
  const current = lookKey(shown)
  const layouts = [...BUILT_INS, ...custom?.layouts ?? []]
  // The saved copy of a custom layout stays in the list while its file is gone or refused.
  if (current.startsWith(CUSTOM_PREFIX) && !layouts.some(layout => lookKey(layout) === current)) layouts.push(shown)

  // Glass settings (tint, blur, fill, photo) belong to the device, so a new layout keeps them.
  const lookFor = (base: Look | undefined, layout: Layout): Look => ({ ...base ?? DEFAULT_LOOK, layout: lookKey(layout), surface: layout.style.surface, custom: lookKey(layout).startsWith(CUSTOM_PREFIX) ? layout : undefined })
  const apply = (layout: Layout, save: boolean) => {
    setAsking(null)
    update(state => ({ look: lookFor(state.look, layout), ...save ? { myLook: { ...state.look ?? DEFAULT_LOOK, scheme: state.scheme } } : {} }))
  }
  const choose = (layout: Layout) => {
    if (lookKey(layout) === current) return
    // Ask only while the user's own colours show and no look is saved yet.
    if (layout.theme && !shown.theme && !prefs.myLook) setAsking(layout)
    else apply(layout, false)
  }
  // Save and try: the user's own colours are saved as My look first (if none is saved), so they never get lost.
  const tryLayout = (layout: Layout) => {
    startTrial(prefs.look ?? null, 'trial', layout.name)
    update(state => ({ look: lookFor(state.look, layout), ...layout.theme && !lookLayout(state.look).theme && !state.myLook ? { myLook: { ...state.look ?? DEFAULT_LOOK, scheme: state.scheme } } : {} }))
    onClose?.()
  }
  const restore = () => {
    const saved = prefs.myLook
    if (!saved) return
    const { scheme, ...rest } = saved
    update(() => ({ look: rest, scheme }))
  }
  const importFile = async (file: File) => {
    setListError('')
    try {
      if (file.size > 64000) throw new Error('the file is too big for a layout.')
      const data: unknown = JSON.parse(await file.text())
      setDraft({ layout: validateLayout(data, file.name.replace(/\.json$/i, '')), notes: '', from: 'import' })
    } catch (failure) { setListError(`Tether cannot import ${file.name}: ${message(failure)}`) }
  }
  const remove = async (layout: Layout) => {
    setListError('')
    try { await deleteLayout(api, layout.id); setVersion(value => value + 1) } catch (failure) { setListError(message(failure)) }
  }
  const updateLayout = async (layout: Layout) => {
    setListError(''); setUpdating(layout.id)
    try {
      const result = await generateLayout(api, 'Update this layout to the current Tether UI manifest. Keep its name, panels, colours and character. Use new options only where they fit the same idea.', layout)
      setDraft({ layout: { ...result.layout, id: layout.id }, notes: result.notes, from: 'update', replaces: layout.id })
    } catch (failure) { setListError(message(failure)) }
    finally { setUpdating('') }
  }

  return <fieldset className="choice look-layouts">
    <legend>Layout</legend>
    <div className="look-options" role="radiogroup" aria-label="Layout">
      {layouts.map(layout => {
        const key = lookKey(layout)
        const own = key.startsWith(CUSTOM_PREFIX) && !!custom?.layouts.includes(layout)
        return <div key={key} className="look-option-wrap">
          <label className="look-option">
            <input type="radio" name="look-layout" value={key} checked={key === current} onChange={() => choose(layout)} />
            <span className="look-name">{layout.name}{layout.theme && <span className="look-theme" style={{ background: layout.theme.bg, borderColor: layout.theme.accent }} aria-hidden="true" />}</span>
            {layout.description && <small>{layout.description}</small>}
            {layout.phone === 'keep' && <small>Desktop and tablet only.</small>}
            {layout.stale && <small className="look-stale">The file is refused, so the last good copy shows: {layout.stale}</small>}
          </label>
          {own && <div className="look-actions">
            {(layout.manifest ?? 1) < MANIFEST_VERSION && <button type="button" className="link-button" disabled={!!updating} onClick={() => void updateLayout(layout)}>{updating === layout.id ? 'Updating…' : 'Update layout'}</button>}
            <button type="button" className="link-button" onClick={() => download(layout)}>Export</button>
            <ConfirmButton className="link-button" label="Delete" confirm="Delete it?" onConfirm={() => void remove(layout)} />
          </div>}
        </div>
      })}
    </div>
    <div className="look-row-buttons">
      {prefs.myLook && <button type="button" className="secondary" onClick={restore}>My look</button>}
      <button type="button" className="secondary" onClick={() => importer.current?.click()}>Import layout…</button>
      <input ref={importer} type="file" accept="application/json,.json" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFile(file) }} />
    </div>
    <p className="setting-note">The layout shows on wide screens. Phones keep their layout and take the colours and the style. <kbd>{keyLabel('⌥⇧L')}</kbd> goes back to Classic from anywhere.</p>
    {listError && <p className="setting-note look-error" role="alert">{listError}</p>}
    {error && <p className="setting-note look-error" role="alert">Tether cannot read the custom layouts: {error}</p>}
    {!!custom?.errors.length && <ul className="look-errors" role="alert">{custom.errors.map(item => <li key={item.file + item.reason}><code>{item.file || custom.folder}</code>: {item.reason}</li>)}</ul>}
    <LayoutMaker api={api} draft={draft} setDraft={setDraft} focus={focusMaker} current={shown} onSaved={() => setVersion(value => value + 1)} onTry={tryLayout} />
    {asking && <SaveLook next={asking} onSave={() => apply(asking, true)} onSkip={() => apply(asking, false)} onCancel={() => setAsking(null)} />}
  </fieldset>
}

// Glass. A tint colours the backdrop light and the panel edges. "Colour" makes the whole backdrop from the tint.
const TINTS: [string, string][] = [['#e8835a', 'Coral'], ['#e0a83a', 'Amber'], ['#7cb854', 'Moss'], ['#2fb5a5', 'Teal'], ['#4b9be8', 'Sky'], ['#6c6cf0', 'Indigo'], ['#a66cf0', 'Violet'], ['#e66aa0', 'Rose']]

function Photos({ api, look, prefs, set, update }: { api: Api; look: Look; prefs: Prefs; set: (patch: Partial<Look>) => void; update: Update }) {
  const [items, setItems] = useState<BackgroundItem[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'' | 'upload' | 'make'>('')
  const [wish, setWish] = useState('')
  const [voiceBusy, setVoiceBusy] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    listBackgrounds(api).then(list => { if (live.current) setItems(list) }, failure => { if (live.current) setError(message(failure)) })
    // A job from before Settings closed keeps going. Show it as busy again.
    const pending = backgroundJob()
    if (pending) void follow(pending)
    return () => { live.current = false }
    // follow is stable enough: it only reads refs and state setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api])
  const tuned = prefs.imagePromptTuned !== false

  const pick = (item: BackgroundItem) => applyPhoto(item, update)
  const upload = async (picked: File) => {
    setBusy('upload'); setError('')
    try { const item = await uploadBackground(api, picked); setItems(list => [item, ...list ?? []]); await pick(item) }
    catch (failure) { setError(message(failure)) } finally { setBusy('') }
  }
  // The finished image applies even after Settings closes. The list and messages update only while it is open.
  async function follow(handle: ReturnType<typeof startBackground>) {
    setBusy('make'); setError('')
    try {
      const item = await handle.promise
      if (live.current) { setItems(list => list?.some(entry => entry.id === item.id) ? list : [item, ...list ?? []]); setWish('') }
      if (!handle.resumed) await pick(item)
    } catch (failure) { if (live.current && !handle.controller.signal.aborted) setError(message(failure)) }
    finally { if (live.current) setBusy('') }
  }
  const make = () => { if (!voiceBusy && wish.trim()) void follow(startBackground(api, wish.trim(), tuned)) }
  const remove = async (item: BackgroundItem) => {
    setError('')
    try {
      await deleteBackground(api, item.id)
      setItems(list => list?.filter(entry => entry.id !== item.id) ?? null)
      if (look.photo?.id === item.id) set({ backdrop: 'ember', photo: undefined })
    } catch (failure) { setError(message(failure)) }
  }

  return <div className="look-photos">
    <div className="look-photo-grid" role="radiogroup" aria-label="Photo">
      {items?.map(item => <div key={item.id} className="look-photo">
        <label title={item.prompt ?? 'Uploaded photo'}>
          <input type="radio" name="look-photo" checked={look.backdrop === 'photo' && look.photo?.id === item.id} onChange={() => void pick(item)} />
          <img src={item.thumb ?? item.url} alt={item.prompt ? `AI image: ${item.prompt}` : 'Uploaded photo'} loading="lazy" decoding="async" />
        </label>
        <ConfirmButton className="look-photo-delete" label="×" title="Delete the photo" confirm="Delete?" onConfirm={() => void remove(item)} />
      </div>)}
      <button type="button" className="look-photo-add" disabled={!!busy} onClick={() => file.current?.click()}>{busy === 'upload' ? 'Uploading…' : 'Upload photo'}</button>
      <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={event => { const picked = event.target.files?.[0]; event.target.value = ''; if (picked) void upload(picked) }} />
    </div>
    <form className="look-maker-form" onSubmit={event => event.preventDefault()}>
      <VoiceField multiline label="image description" value={wish} setValue={setWish} onBusyChange={setVoiceBusy}
        field={{ className: 'look-textarea', rows: 2, maxLength: 1000, disabled: busy === 'make', 'aria-label': 'Describe an image', placeholder: 'Make an image, for example: misty pine forest at dawn' }} />
      <div className="look-maker-buttons">
        {busy === 'make' ? <>
          <span className="look-busy" role="status"><Spinner />Codex is making the image. This takes up to 2 minutes. You can close Settings.</span>
          <button type="button" className="secondary" onClick={() => backgroundJob()?.controller.abort()}>Stop waiting</button>
        </> : wish.trim() ? <ConfirmButton disabled={voiceBusy} className="primary" label="Make image" confirm="Uses Codex usage. Make it?" onConfirm={make} /> : <button type="button" className="primary" disabled>Make image</button>}
      </div>
    </form>
    <label className="look-check"><input type="checkbox" checked={tuned} onChange={event => update(() => ({ imagePromptTuned: event.target.checked }))} />Tuned prompt: ask for a calm wallpaper with soft areas behind the panels</label>
    <p className="setting-note">Codex makes each image with your Codex plan, one at a time. Tether keeps up to 30 photos.</p>
    {error && <p className="setting-note look-error" role="alert">{error}</p>}
  </div>
}

export function StylePicker({ api, prefs, update }: { api: Api; prefs: Prefs; update: Update }) {
  const look = prefs.look ?? DEFAULT_LOOK
  const set = (patch: Partial<Look>) => update(state => ({ look: { ...state.look ?? DEFAULT_LOOK, ...patch } }))
  const backdrop: Backdrop = (['ember', 'graphite', 'dusk', 'colour', 'photo'] as Backdrop[]).includes(look.backdrop) ? look.backdrop : 'ember'
  const blur = look.blur ?? 24
  const fill = look.fill ?? 0.55
  return <>
    <Choice<Surface> legend="Style" name="look-surface" value={look.surface === 'glass' ? 'glass' : 'flat'} options={[['flat', 'Flat'], ['glass', 'Glass']]} onChange={surface => set({ surface })} />
    {look.surface === 'glass' && <div className="look-glass">
      <Choice<Backdrop> legend="Backdrop" name="look-backdrop" value={backdrop} options={[['ember', 'Ember'], ['graphite', 'Graphite'], ['dusk', 'Dusk'], ['colour', 'Colour'], ['photo', 'Photo']]} onChange={value => set({ backdrop: value })} />
      {backdrop === 'photo' && <Photos api={api} look={look} prefs={prefs} set={set} update={update} />}
      <fieldset className="choice">
        <legend>Tint</legend>
        <div className="look-tints">
          <label title="No tint"><input type="radio" name="look-tint" checked={!look.tint} onChange={() => set({ tint: undefined })} /><span className="look-tint look-tint-none" aria-hidden="true" /><span className="look-sr">No tint</span></label>
          {TINTS.map(([colour, name]) => <label key={colour} title={name}><input type="radio" name="look-tint" checked={look.tint === colour} onChange={() => set({ tint: colour })} /><span className="look-tint" style={{ background: colour }} aria-hidden="true" /><span className="look-sr">{name}</span></label>)}
          <label className="look-tint-own" title="Your own colour"><input type="color" value={look.tint ?? '#6c6cf0'} onChange={event => set({ tint: event.target.value })} /><span className="look-sr">Your own colour</span></label>
        </div>
      </fieldset>
      <label className="look-range"><span>Blur <output>{blur}px</output></span><input type="range" min={0} max={40} step={2} value={blur} onChange={event => set({ blur: Number(event.target.value) })} /></label>
      <label className="look-range"><span>Panel fill <output>{Math.round(fill * 100)}%</output></span><input type="range" min={30} max={90} step={5} value={Math.round(fill * 100)} onChange={event => set({ fill: Number(event.target.value) / 100 })} /></label>
      <p className="setting-note">Keep the fill at 60% or more over a busy photo, so text stays easy to read.</p>
      {(look.tint || look.blur !== undefined || look.fill !== undefined) && <button type="button" className="secondary" onClick={() => set({ tint: undefined, blur: undefined, fill: undefined })}>Reset tint, blur and fill</button>}
    </div>}
  </>
}
