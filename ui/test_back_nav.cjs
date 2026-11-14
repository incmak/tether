// B81: desktop Back and Forward. backNav.ts history steps with a fake history, and the Back/Forward keys (navKeys.ts).
// Real TS with stubbed imports. No DOM or browser.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function compile(file) {
  return ts.transpileModule(readFileSync(`src/${file}`, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
}

// A browser history: entries, a pointer, and popstate after each traversal (async, like a browser).
function makeHistory() {
  const listeners = []
  const tasks = []
  const entries = [{ state: null }]
  let at = 0
  const fire = () => tasks.push(() => listeners.forEach(fn => fn({ state: entries[at].state })))
  const history = {
    get state() { return entries[at].state },
    get length() { return entries.length },
    pushState(state) { entries.splice(at + 1); entries.push({ state: structuredClone(state) }); at++ },
    replaceState(state) { entries[at] = { state: structuredClone(state) } },
    back() { if (at > 0) { at--; fire() } },
    forward() { if (at < entries.length - 1) { at++; fire() } },
  }
  const settle = () => { for (let n = 0; tasks.length && n < 50; n++) tasks.shift()() }
  return { history, listeners, settle, entries, get at() { return at } }
}

function loadNav({ active = true } = {}) {
  const h = makeHistory()
  const store = new Map()
  const exports = {}
  runInNewContext(compile('backNav.ts'), {
    exports, structuredClone, Number, String, Math, Set, Error,
    history: h.history,
    navigator: { userActivation: { get isActive() { return active } } },
    sessionStorage: { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) },
    window: { addEventListener: (type, fn) => { if (type === 'popstate') h.listeners.push(fn) } },
    require(name) {
      if (name === 'react') return { useEffect() {}, useLayoutEffect() {}, useRef: value => ({ current: value }), useSyncExternalStore: (_, get) => get() }
      throw Error(`Unexpected import: ${name}`)
    },
  })
  return { nav: exports, h }
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

// 7. The keys: ⌘[ ⌘] on a Mac, Alt+← Alt+→ elsewhere; never in a text field or the terminal.
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
