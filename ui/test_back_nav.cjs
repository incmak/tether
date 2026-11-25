// B81: desktop Back and Forward. backNav.ts history steps with a fake history, and the Back/Forward keys (navKeys.ts).
// Real TS with stubbed imports. No DOM or browser.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function compile(file) {
  return ts.transpileModule(readFileSync(`src/${file}`, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
}

function makeEvents() {
  const listeners = new Map()
  const calls = []
  return {
    calls,
    addEventListener(type, fn) {
      calls.push(['add', type])
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(fn)
    },
    removeEventListener(type, fn) {
      calls.push(['remove', type])
      listeners.get(type)?.delete(fn)
    },
    dispatch(type, event = {}) { listeners.get(type)?.forEach(fn => fn(event)) },
    count(type) { return listeners.get(type)?.size ?? 0 },
  }
}

function dispatchNavigate(navigation, navigationType, { sameDocument = false, hashChange = false, downloadRequest = null } = {}) {
  navigation?.dispatchEvent(Object.assign(new Event('navigate'), {
    destination: { sameDocument }, navigationType, hashChange, downloadRequest,
  }))
}

// A browser history: entries, a pointer, and popstate after each traversal (async, like a browser).
function makeHistory(window) {
  const tasks = []
  const base = 'https://tether.test/chat/'
  const entries = [{ state: null, url: base }]
  let at = 0, backs = 0, reloads = 0
  const location = {
    get href() { return entries[at].url },
    get hash() { return new URL(entries[at].url).hash },
    reload() { reloads++ },
  }
  const navigate = type => dispatchNavigate(window.navigation, type, { sameDocument: true })
  // Like a browser, the pointer and history.state change only when the traversal task runs.
  const go = step => tasks.push(() => {
    if (at + step < 0 || at + step >= entries.length) return
    navigate('traverse')
    at += step
    window.dispatch('popstate', { state: entries[at].state })
  })
  const history = {
    get state() { return entries[at].state },
    get length() { return entries.length },
    pushState(state, _, url) {
      navigate('push')
      const now = entries[at].url
      entries.splice(at + 1)
      entries.push({ state: structuredClone(state), url: url ? String(url) : now })
      at++
    },
    replaceState(state, _, url) {
      navigate('replace')
      entries[at] = { state: structuredClone(state), url: url ? String(url) : entries[at].url }
    },
    back() { backs++; go(-1) },
    forward() { go(1) },
  }
  // The extension's tabs.update to a new hash: a same-page navigation. A new entry with no state, Forward entries
  // dropped, then popstate (null state) and hashchange.
  const hashNav = hash => {
    const url = new URL(entries[at].url); url.hash = hash
    entries.splice(at + 1); entries.push({ state: null, url: String(url) }); at++
    window.dispatch('popstate', { state: null })
    window.dispatch('hashchange')
  }
  const settle = () => { for (let n = 0; tasks.length && n < 50; n++) tasks.shift()() }
  return { history, location, hashNav, settle, entries, go, userBack: () => go(-1),
    get at() { return at }, get backs() { return backs }, get reloads() { return reloads } }
}

function loadNav({ active = true, navigation } = {}) {
  const window = Object.assign(makeEvents(), { navigation })
  const h = makeHistory(window)
  const store = new Map()
  const exports = {}
  let timer
  runInNewContext(compile('backNav.ts'), {
    exports, structuredClone, Number, String, Math, Set, Error, URL,
    setTimeout: fn => { timer = fn; return 1 }, clearTimeout: () => { timer = undefined },
    history: h.history, location: h.location,
    navigator: { userActivation: { get isActive() { return active } } },
    sessionStorage: { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) },
    window,
    require(name) {
      if (name === 'react') return { useEffect() {}, useLayoutEffect() {}, useRef: value => ({ current: value }), useSyncExternalStore: (_, get) => get() }
      throw Error(`Unexpected import: ${name}`)
    },
  })
  return { nav: exports, h, window, expireTraversal: () => timer() }
}

const places = []
const can = nav => nav.useNavAvailability()

// 1. Places: the first is replaced, the next ones are pushed. Back and Forward walk them; Back stops at Tether's first entry.
{
  const { nav, h } = loadNav()
  nav.setPlaceHandler(place => places.push(place.screen))
  nav.setPlace({ screen: 'home' }, { push: true })
  assert.equal(h.entries.length, 1)
  assert.deepEqual({ ...can(nav) }, { back: false, forward: false })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  nav.setPlace({ screen: 'files', pane: '%1' }, { push: true })
  assert.equal(h.entries.length, 3)
  assert.deepEqual({ ...can(nav) }, { back: true, forward: false })
  nav.goBack(); h.settle()
  assert.equal(places.at(-1), 'chats')
  assert.deepEqual({ ...can(nav) }, { back: true, forward: true })
  nav.goBack(); h.settle()
  assert.equal(places.at(-1), 'home')
  assert.deepEqual({ ...can(nav) }, { back: false, forward: true })
  nav.goBack(); h.settle()
  assert.equal(h.at, 0, 'Back at the first Tether entry stays in the app')
  nav.goForward(); h.settle(); nav.goForward(); h.settle()
  assert.equal(places.at(-1), 'files')
  assert.deepEqual({ ...can(nav) }, { back: true, forward: false })
  // A new place after Back drops the Forward steps, like a browser.
  nav.goBack(); h.settle()
  nav.setPlace({ screen: 'terminals', pane: '%1' }, { push: true })
  assert.deepEqual({ ...can(nav) }, { back: true, forward: false })
  console.log('ok places: back, forward, stop at first entry')
}

// 2. A named view: Back closes it, Forward opens it again on its own entry (no new entry).
{
  const { nav, h } = loadNav()
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  let closed = 0, opened = []
  let id = nav.openOverlay(() => closed++, 'backlog:page')
  assert.equal(h.entries.length, 2)
  assert.equal(h.history.state.view, 'backlog:page')
  nav.goBack(); h.settle()
  assert.equal(closed, 1)
  nav.closeOverlay(id)  // The component unmounts: its entry is gone already, so no second Back.
  h.settle()
  assert.equal(h.at, 0)
  assert.deepEqual({ ...can(nav) }, { back: false, forward: true })
  nav.setViewOpener(view => { opened.push(view); id = nav.openOverlay(() => closed++, view); return true })
  nav.goForward(); h.settle()
  assert.deepEqual(opened, ['backlog:page'])
  assert.equal(h.entries.length, 2, 'Forward reuses the view entry')
  assert.equal(h.at, 1)
  nav.goBack(); h.settle()
  assert.equal(closed, 2)
  console.log('ok named view: back closes, forward opens again')
}

// 3. An overlay without a name (a menu): Forward steps over its entry. With nothing after it, Forward turns off.
{
  const { nav, h } = loadNav()
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  const id = nav.openOverlay(() => {})
  nav.goBack(); h.settle(); nav.closeOverlay(id); h.settle()
  assert.deepEqual({ ...can(nav) }, { back: false, forward: true })
  nav.goForward(); h.settle()
  assert.equal(h.at, 0, 'A closed menu is not reopened: Forward comes back')
  assert.deepEqual({ ...can(nav) }, { back: false, forward: false })
  console.log('ok dead overlay entry: forward steps over and turns off')
}

// 4. Forward steps over a closed menu entry to the place after it.
{
  const { nav, h } = loadNav()
  const places = []
  nav.setPlaceHandler(place => places.push(place.screen))
  nav.setPlace({ screen: 'home' }, { push: true })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  // Entry layout after Back from chats: home(0) chats(1). Add a dead overlay entry between by hand-made history.
  h.history.back(); h.settle()
  const id = nav.openOverlay(() => {})       // home + menu (1), chats dropped
  nav.closeOverlay(id); h.settle()           // menu closed with its own Back: home(0), menu(1) ahead
  nav.goForward(); h.settle()
  assert.equal(h.at, 0)
  assert.deepEqual(places, ['home'])
  console.log('ok forward never lands on a closed menu')
}

// 5. Back from the command menu: the menu's own close goes first, then one real step.
{
  const { nav, h } = loadNav()
  const places = []
  nav.setPlaceHandler(place => places.push(place.screen))
  nav.setPlace({ screen: 'home' }, { push: true })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  const id = nav.openOverlay(() => {})
  nav.closeOverlay(id)  // The menu closes: Back over its entry is pending.
  nav.goBack()          // Waits for it.
  h.settle()
  assert.equal(h.at, 0)
  assert.deepEqual(places, ['home'])
  console.log('ok back from a menu waits for the menu close')
}

// 6. Without user activation nothing is pushed (Chrome would skip such entries).
{
  const { nav, h } = loadNav({ active: false })
  nav.setPlace({ screen: 'home' }, { push: true })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  assert.equal(h.entries.length, 1)
  assert.deepEqual({ ...can(nav) }, { back: false, forward: false })
  console.log('ok no entry without activation')
}

// 7. Quick repeated Back and Forward: each waits for the last popstate and checks the bounds again (Codex review, P1).
{
  const { nav, h } = loadNav()
  nav.setPlace({ screen: 'home' }, { push: true })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  nav.setPlace({ screen: 'files', pane: '%1' }, { push: true })
  nav.goBack(); nav.goBack(); nav.goBack(); nav.goBack()  // Four Backs before any popstate: only two steps exist.
  h.settle()
  assert.equal(h.at, 0, 'quick Backs stop at the first Tether entry')
  nav.goForward(); nav.goForward(); h.settle()
  assert.equal(h.at, 2, 'two quick Forwards take two steps')
  console.log('ok quick repeats stay inside Tether')
}

// 8. The command menu keeps the Forward steps (Codex review, P2); other overlays add their entry.
{
  const { nav, h } = loadNav()
  nav.setPlace({ screen: 'home' }, { push: true })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  nav.goBack(); h.settle()
  const id = nav.openOverlay(() => {}, undefined, true)
  assert.equal(h.entries.length, 2, 'no entry: Forward survives')
  assert.deepEqual({ ...can(nav) }, { back: false, forward: true })
  nav.closeOverlay(id); h.settle()
  assert.equal(h.at, 0, 'closing it goes nowhere')
  nav.goForward(); h.settle()
  assert.equal(h.at, 1)
  console.log('ok command menu keeps Forward')
}

// 9. An overlay that closes while an in-app Back is in flight adds no second Back (Codex review, P1).
{
  const { nav, h } = loadNav()
  h.history.pushState(null, '')  // The entry before is another page: a second Back would leave Tether.
  nav.setPlace({ screen: 'home' }, { push: true })
  const id = nav.openOverlay(() => {}, 'backlog:page')
  assert.equal(h.at, 2)
  nav.goBack(); nav.closeOverlay(id); h.settle()
  assert.equal(h.at, 1, 'one step only: the app stays open')
  assert.deepEqual({ ...can(nav) }, { back: false, forward: true })
  console.log('ok overlay close waits for the in-app Back')
}

// 10. The extension's "Open Tether search" key (#search): no overlay closes, no Back step is added, search opens once.
{
  const { nav, h } = loadNav()
  let opened = 0
  nav.setSearchOpener(() => opened++)
  nav.setPlace({ screen: 'home' }, { push: true })
  nav.setPlace({ screen: 'chats', pane: '%1' }, { push: true })
  nav.setPlace({ screen: 'files', pane: '%1' }, { push: true })
  nav.goBack(); h.settle()
  let closed = 0
  nav.openOverlay(() => closed++, 'backlog:page')
  const at = h.at
  const before = h.entries[at].state
  h.hashNav('#search'); h.settle()
  assert.equal(opened, 1, 'search opens once (popstate, then hashchange finds no hash)')
  assert.equal(closed, 0, 'the open overlay stays open')
  assert.equal(h.at, at, 'back on the real entry')
  assert.deepEqual(h.entries[at].state, before)
  assert.equal(h.location.hash, '')
  assert.equal(h.entries.length, at + 2, 'one spare entry after it')
  assert.equal(h.entries[at + 1].url, 'https://tether.test/chat/', 'the spare entry has no hash')
  assert.deepEqual({ ...can(nav) }, { back: true, forward: false }, 'Forward entries are gone, so in-app Forward is off')
  // A second press replaces the spare entry: still no extra Back step.
  h.hashNav('#search'); h.settle()
  assert.equal(opened, 2)
  assert.equal(h.entries.length, at + 2)
  assert.equal(h.at, at)
  // Browser Forward onto the spare entry comes straight back and opens nothing.
  h.history.forward(); h.settle()
  assert.equal(h.at, at)
  assert.equal(opened, 2)
  assert.equal(closed, 0)
  console.log('ok #search opens search without a history step')
}

// 11. A page that loads with #search (a reload) keeps its entry and opens search.
{
  const { nav, h } = loadNav()
  nav.setPlace({ screen: 'home' }, { push: true })
  h.history.replaceState(h.history.state, '', 'https://tether.test/chat/#search')
  let opened = 0
  nav.setSearchOpener(() => opened++)
  h.settle()
  assert.equal(opened, 1)
  assert.equal(h.at, 0, 'no Back: the app stays open')
  assert.equal(h.location.hash, '')
  assert.equal(h.entries[0].state.tether, 1)
  console.log('ok #search on load')
}

// 12. The keys: ⌘[ ⌘] on a Mac, Alt+← Alt+→ elsewhere; never in a text field or the terminal.
{
  const exports = {}
  runInNewContext(compile('navKeys.ts'), { exports, require: name => { if (name === './keys') return { IS_MAC: false }; throw Error(name) } })
  const { isNavKey } = exports
  const el = selector => ({ closest: query => query.split(',').some(part => selector === part.trim()) ? {} : null })
  const body = el('body'), field = el('textarea'), term = el('.xterm'), editable = el('[contenteditable="true"]')
  const key = (props, target = body) => ({ code: '', key: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target, ...props })
  assert.equal(isNavKey(key({ metaKey: true, code: 'BracketLeft', key: '[' }), true), 'back')
  assert.equal(isNavKey(key({ metaKey: true, code: 'BracketRight', key: ']' }), true), 'forward')
  assert.equal(isNavKey(key({ altKey: true, key: 'ArrowLeft' }), true), null, 'Alt+← moves by word on a Mac')
  assert.equal(isNavKey(key({ altKey: true, key: 'ArrowLeft' }), false), 'back')
  assert.equal(isNavKey(key({ altKey: true, key: 'ArrowRight' }), false), 'forward')
  assert.equal(isNavKey(key({ metaKey: true, code: 'BracketLeft' }), false), null)
  assert.equal(isNavKey(key({ altKey: true, shiftKey: true, key: 'ArrowLeft' }), false), null, 'Alt+Shift+← selects text')
  assert.equal(isNavKey(key({ altKey: true, code: 'BracketLeft', key: '[' }), false), null, 'Alt+[ stays the pinned-chat key')
  for (const target of [field, term, editable]) {
    assert.equal(isNavKey(key({ altKey: true, key: 'ArrowLeft' }, target), false), null)
    assert.equal(isNavKey(key({ metaKey: true, code: 'BracketLeft' }, target), true), null)
  }
  console.log('ok keys')
}

// B33: A close during a leave keeps its entry. A later same-document Back clears the gate.
{
  const { nav, h, window } = loadNav()
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {})
  window.dispatch('beforeunload')
  nav.closeOverlay(id)
  assert.equal(h.backs, 0, 'the overlay close must not start a Back')
  assert.equal(h.history.state.layer, 1)
  h.history.back(); h.settle()
  nav.closeOverlay(nav.openOverlay(() => {})); h.settle()
  assert.equal(h.backs, 2, 'the close after popstate starts a Back')
  assert.equal(h.history.state.layer, 0)
  console.log('ok beforeunload skips close Back and popstate restores it')
}

{
  const navigation = Object.assign(new EventTarget(), { currentEntry: { key: 'home' } })
  const traversals = []
  navigation.traverseTo = key => { traversals.push(key); return { finished: Promise.resolve() } }
  const { nav, h, window } = loadNav({ navigation })
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {}, undefined, false, true)
  window.dispatch('beforeunload')
  nav.closeOverlay(id)
  assert.deepEqual(traversals, [])
  assert.equal(h.backs, 0)
  assert.equal(h.history.state.layer, 1)
  console.log('ok beforeunload skips the frame traversal')
}

{
  const { nav, h, window } = loadNav()
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {})
  window.dispatch('pagehide')
  window.dispatch('pageshow', { persisted: false })
  nav.closeOverlay(id)
  assert.equal(h.backs, 0, 'a non-persisted pageshow does not clear the gate')
  window.dispatch('pageshow', { persisted: true })
  nav.closeOverlay(nav.openOverlay(() => {})); h.settle()
  assert.equal(h.backs, 1, 'a bfcache restore clears the gate')
  assert.equal(h.history.state.layer, 0)
  console.log('ok only a persisted pageshow restores close Back')
}

{
  const { nav, h, window } = loadNav()
  nav.setPlace({ screen: 'chats' }, { push: true })
  const event = 'beforeunload'
  assert.equal(window.count(event), 0)
  const first = nav.openOverlay(() => {})
  const second = nav.openOverlay(() => {})
  assert.equal(window.count(event), 1)
  nav.closeOverlay(second); h.settle()
  assert.equal(window.count(event), 1)
  nav.closeOverlay(first); h.settle()
  assert.equal(h.backs, 2, 'ordinary closes go Back')
  assert.equal(window.count(event), 0)
  assert.deepEqual(window.calls.filter(([, type]) => type === event), [['add', event], ['remove', event]])
  nav.openOverlay(() => {})
  h.history.back(); h.settle()
  assert.equal(window.count(event), 0, 'Back also removes the listener after the last overlay closes')
  console.log('ok beforeunload is registered only while overlays are open')
}

{
  const navigation = new EventTarget()
  const { nav, h, window } = loadNav({ navigation })
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {})
  window.dispatch('beforeunload')
  h.history.replaceState(h.history.state, '')
  // A same-document history write can report an error for a pending navigation without unloading the page.
  navigation.dispatchEvent(new Event('navigateerror'))
  nav.closeOverlay(id); h.settle()
  assert.equal(h.backs, 0)
  assert.equal(h.history.state.layer, 1)
  console.log('ok same-document replaceState and navigateerror keep the leave gate set')
}

{
  const { nav, h, window, expireTraversal } = loadNav()
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {})
  nav.goBack() // The traversal has no popstate before its timeout.
  nav.closeOverlay(id)
  window.dispatch('pagehide')
  expireTraversal()
  assert.equal(h.backs, 1, 'the close checks the gate when its queued step runs')
  console.log('ok queued close checks whether the page is leaving')
}

{
  const navigation = new EventTarget()
  const { nav, h } = loadNav({ navigation })
  nav.setPlace({ screen: 'chats' }, { push: true })
  nav.closeOverlay(nav.openOverlay(() => {})); h.settle()
  assert.equal(h.backs, 1)
  assert.equal(h.history.state.layer, 0)
  console.log('ok same-document history events allow an ordinary overlay close')
}

for (const navigationType of ['push', 'reload']) {
  for (const frame of [false, true]) {
    const navigation = Object.assign(new EventTarget(), {
      currentEntry: { key: 'home' },
      traverseTo() { assert.fail('a frame close must not traverse during navigation') },
    })
    const { nav, h } = loadNav({ navigation })
    nav.setPlace({ screen: 'chats' }, { push: true })
    const id = nav.openOverlay(() => {}, undefined, false, frame)
    dispatchNavigate(navigation, navigationType)
    nav.closeOverlay(id); h.settle()
    assert.equal(h.backs, 0)
    assert.equal(h.history.state.layer, 1)
    console.log(`ok cross-document ${navigationType} skips ${frame ? 'frame' : 'overlay'} close Back`)
  }
}

for (const [name, navigationType, options] of [
  ['download', 'push', { downloadRequest: 'x.zip' }],
  ['hash change', 'push', { hashChange: true }],
  ['traverse', 'traverse', {}],
]) {
  const navigation = new EventTarget()
  const { nav, h } = loadNav({ navigation })
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {})
  dispatchNavigate(navigation, navigationType, options)
  nav.closeOverlay(id); h.settle()
  assert.equal(h.backs, 1)
  assert.equal(h.history.state.layer, 0)
  console.log(`ok ${name} allows close Back`)
}

{
  const { nav, h } = loadNav()
  nav.setPlace({ screen: 'chats' }, { push: true })
  const id = nav.openOverlay(() => {})
  nav.reloadPage()
  assert.equal(h.reloads, 1)
  nav.closeOverlay(id); h.settle()
  assert.equal(h.backs, 0)
  assert.equal(h.history.state.layer, 1)
  console.log('ok reloadPage reloads and skips close Back without browser events')
}

for (const [name, keepForward, frame, escape] of [['plain', false, false], ['frame', false, true], ['keepForward', true, false], ['plain, Escape after Back', false, false, true]]) {
  const navigation = Object.assign(new EventTarget(), {
    currentEntry: { key: 'home' },
    traverseTo() { assert.fail('a frame close must not traverse during a leave') },
  })
  const { nav, h, window } = loadNav({ navigation })
  nav.setPlace({ screen: 'chats' }, { push: true })
  const closed = []
  const openFirst = () => nav.openOverlay(() => closed.push(1), 'first')
  const openSecond = () => nav.openOverlay(() => closed.push(2), 'second', keepForward, frame)
  let first = openFirst()
  let second = openSecond()
  const length = h.history.length
  assert.deepEqual(h.entries.map(entry => entry.state.layer), [0, 1, 2])
  window.dispatch('beforeunload')
  const closeBoth = () => { nav.closeOverlay(second); nav.closeOverlay(first); h.settle() }
  closeBoth()
  for (let cycle = 0; cycle < 5; cycle++) {
    const leftover = h.history.state
    first = openFirst()
    assert.equal(h.history.state, leftover, 'opening layer 1 leaves the layer-2 entry alone')
    second = openSecond()
    assert.notEqual(h.history.state, leftover, 'opening layer 2 replaces the leftover entry')
    closeBoth()
    assert.equal(h.history.length, length, 'reopening both layers keeps the same number of entries')
  }
  assert.equal(h.backs, 0)
  assert.equal(h.history.state.layer, 2)
  first = openFirst(); openSecond()
  h.userBack(); h.settle()
  assert.deepEqual(closed, [2], 'first Back closes only the upper overlay')
  assert.equal(h.history.state.layer, 1)
  if (escape) {
    // The lower overlay opened with no entry of its own. After Back it owns the layer-1 entry, so its close steps back.
    nav.closeOverlay(first); h.settle()
    assert.equal(h.backs, 1, 'closing the lower overlay after Back steps back once')
  } else {
    h.userBack(); h.settle()
    assert.deepEqual(closed, [2, 1], 'second Back closes the lower overlay')
  }
  assert.equal(h.history.state.layer, 0)
  const backs = escape ? 1 : 0
  assert.equal(h.backs, backs, 'user Back does not trigger a second Back')
  nav.closeOverlay(openFirst()); h.settle()
  assert.equal(h.backs, backs + 1, 'normal close after popstate starts exactly one Back')
  assert.equal(h.history.state.layer, 0)
  console.log(`ok ${name} nested leave keeps history stable, then Back closes each layer and restores normal close`)
}

// A frame overlay on the LOWER layer takes its entry after a cancelled leave and Back. Its close must skip the frame's
// own entries with traverseTo (to the entry under it), and the next overlay must still get its entry.
{
  const traversals = []
  let h
  const navigation = new EventTarget()
  Object.defineProperty(navigation, 'currentEntry', { get: () => ({ key: 'k' + h.at, index: h.at }) })
  navigation.entries = () => h.entries.map((_, i) => ({ key: 'k' + i }))
  navigation.traverseTo = key => { traversals.push(key); h.go(Number(key.slice(1)) - h.at); return { finished: Promise.resolve() } }
  const loaded = loadNav({ navigation })
  const { nav, window } = loaded
  h = loaded.h
  nav.setPlace({ screen: 'chats' }, { push: true })
  const below = 'k' + h.at
  const closed = []
  const openFrame = () => nav.openOverlay(() => closed.push('frame'), 'host', false, true)
  const openSheet = () => nav.openOverlay(() => closed.push('sheet'), 'sheet')
  let frame = openFrame()
  let sheet = openSheet()
  const length = h.history.length
  window.dispatch('beforeunload')
  const closeBoth = () => { nav.closeOverlay(sheet); nav.closeOverlay(frame); h.settle() }
  closeBoth()
  for (let cycle = 0; cycle < 3; cycle++) { frame = openFrame(); sheet = openSheet(); closeBoth() }
  assert.equal(h.history.length, length, 'reopening both layers keeps the same number of entries')
  frame = openFrame(); sheet = openSheet()
  h.userBack(); h.settle()
  assert.deepEqual(closed, ['sheet'])
  assert.equal(h.history.state.layer, 1)
  // The frame's page adds its own entry after the overlay's entry. The parent's state stays the overlay's.
  h.history.pushState(h.history.state, '')
  nav.closeOverlay(frame); h.settle()
  assert.deepEqual(traversals, [below], 'the frame close skips the frame entries to the entry under the overlay')
  assert.equal(h.backs, 0, 'the frame close does not use history.back')
  assert.equal(h.history.state.layer, 0)
  const next = openSheet()
  assert.equal(h.history.state.layer, 1, 'the next overlay gets its entry')
  nav.closeOverlay(next); h.settle()
  assert.equal(h.backs, 1)
  assert.equal(h.history.state.layer, 0)
  console.log('ok lower frame overlay after a cancelled leave and Back closes with traverseTo; the next overlay works')
}

// The same recovery without the Navigation API: the frame overlay has no key for the entry under it, so it never
// calls history.back() (that would step into the frame's entries). Its entry stays, and the next overlay still works.
{
  const { nav, h, window } = loadNav()
  nav.setPlace({ screen: 'chats' }, { push: true })
  const closed = []
  const openFrame = () => nav.openOverlay(() => closed.push('frame'), 'host', false, true)
  const openSheet = () => nav.openOverlay(() => closed.push('sheet'), 'sheet')
  // A fresh frame overlay, no leave: its close leaves the entry instead of history.back.
  nav.closeOverlay(openFrame()); h.settle()
  assert.equal(h.backs, 0, 'a frame close without the Navigation API does not use history.back')
  assert.equal(h.history.state.layer, 1)
  h.userBack(); h.settle()
  assert.equal(h.history.state.layer, 0)
  let frame = openFrame()
  let sheet = openSheet()
  window.dispatch('beforeunload')
  nav.closeOverlay(sheet); nav.closeOverlay(frame); h.settle()
  frame = openFrame(); sheet = openSheet()
  h.userBack(); h.settle()
  assert.deepEqual(closed, ['sheet'])
  assert.equal(h.history.state.layer, 1)
  h.history.pushState(h.history.state, '')
  nav.closeOverlay(frame); h.settle()
  assert.equal(h.backs, 0, 'the recovered frame close does not use history.back')
  // A plain sheet reuses the entry the frame left. The frame's entries follow it, so the sheet's close must not Back.
  const reuse = openSheet()
  assert.equal(h.history.state.frame, true, 'the reused entry keeps the frame flag')
  nav.closeOverlay(reuse); h.settle()
  assert.equal(h.backs, 0, 'a sheet on a frame entry does not use history.back')
  // The user's Back leaves the frame entry; a sheet on a clean entry closes with one Back again.
  h.userBack(); h.settle()
  assert.equal(h.history.state.layer, 0)
  // That Back landed on the closed frame entry and stepped over it (one history.back of its own).
  const backs = h.backs
  const next = openSheet()
  assert.equal(h.history.state.layer, 1, 'the next overlay gets its entry')
  assert.equal(h.history.state.frame, undefined, 'a new entry drops the frame flag')
  nav.closeOverlay(next); h.settle()
  assert.equal(h.backs, backs + 1, 'the next overlay closes with one Back')
  assert.equal(h.history.state.layer, 0)
  console.log('ok frame overlay without the Navigation API never uses history.back; a sheet that reuses its entry neither')
}

// With the Navigation API: a frame entry left during a leave, then reused by a plain sheet after the mark clears. The
// sheet's close skips the frame's entries with traverseTo, not history.back.
{
  const traversals = []
  let h
  const navigation = new EventTarget()
  Object.defineProperty(navigation, 'currentEntry', { get: () => ({ key: 'k' + h.at, index: h.at }) })
  navigation.entries = () => h.entries.map((_, i) => ({ key: 'k' + i }))
  navigation.traverseTo = key => { traversals.push(key); h.go(Number(key.slice(1)) - h.at); return { finished: Promise.resolve() } }
  const loaded = loadNav({ navigation })
  const { nav, window } = loaded
  h = loaded.h
  nav.setPlace({ screen: 'chats' }, { push: true })
  const below = 'k' + h.at
  const frame = nav.openOverlay(() => {}, 'host', false, true)
  // The frame's own entries are not in the parent's navigation.entries(), so this fake has none to model.
  window.dispatch('beforeunload')
  nav.closeOverlay(frame); h.settle()
  assert.equal(h.history.state.layer, 1, 'during the leave the frame entry stays')
  window.dispatch('pageshow', { persisted: true })
  const sheet = nav.openOverlay(() => {}, 'sheet')
  assert.equal(h.history.state.frame, true)
  nav.closeOverlay(sheet); h.settle()
  assert.deepEqual(traversals, [below], 'the sheet on a frame entry closes with traverseTo to the entry under it')
  assert.equal(h.backs, 0)
  assert.equal(h.history.state.layer, 0)
  console.log('ok a sheet that reuses a frame entry closes with traverseTo')
}
