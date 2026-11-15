const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const compilerOptions = { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS }
const transpile = code => ts.transpileModule(code, { compilerOptions }).outputText
// Load the real group rules (src/groups.ts) with an in-memory localStorage. No component or terminal action runs.
const store = new Map()
const localStorage = { getItem: key => store.has(key) ? store.get(key) : null, setItem: (key, value) => store.set(key, String(value)) }
function load(file) {
  const exports = {}
  runInNewContext(transpile(readFileSync(file, 'utf8')), { exports, localStorage, require: name => name === './paneState' ? load('src/paneState.ts') : {} })
  return exports
}
const g = load('src/groups.ts')
const plain = value => JSON.parse(JSON.stringify(value))
const ids = list => Array.from(list, item => item.id)
const pane = (id, extra = {}) => ({ id, tmux_session: '$1', label: id, agent: 'claude', state: 'Idle', ...extra })
const groups = [{ id: 'g00000001', name: 'Home dashboard', project: '' }, { id: 'g00000002', name: 'Empty', project: '' }]
const a = pane('%1'), b = pane('%2', { group: 'g00000001' }), c = pane('%3', { group: 'g00000001', state: 'Working' }), stray = pane('%4', { group: 'g0dead000' })

// A session with no groups behaves as before: no places, and the strip keeps every tab.
assert.deepEqual(plain(g.placesOf([a, b], undefined)), [])
assert.deepEqual(ids(g.groupPanes([a, b, c], [], b)), ['%1', '%2', '%3'])

// A tab whose group the session does not list is in Main.
assert.equal(g.groupOf(stray, groups), '')
assert.equal(g.groupOf(b, groups), 'g00000001')

// Places: Main only while it holds a tab, then each group in order, empty groups kept.
const places = g.placesOf([a, b, c, stray], groups)
assert.deepEqual(plain(places.map(place => [place.id, place.name, ids(place.panes)])), [['', 'Unstacked', ['%1', '%4']], ['g00000001', 'Home dashboard', ['%2', '%3']], ['g00000002', 'Empty', []]])
assert.deepEqual(plain(g.placesOf([b, c], groups).map(place => place.name)), ['Home dashboard', 'Empty'], 'Unstacked hides while empty')

// The strip and the swipe use the open tab's group only, so a swipe stops at its last tab.
assert.deepEqual(ids(g.groupPanes([a, b, c, stray], groups, c)), ['%2', '%3'])
assert.deepEqual(ids(g.groupPanes([a, b, c, stray], groups, a)), ['%1', '%4'])

// Attention: needs you > done > working.
assert.equal(g.topState([a, c], {}), 'working')
assert.equal(g.topState([a, c, pane('%5', { state: 'Approval' })], {}), 'needs')
assert.equal(g.topState([a], {}), 'idle')
const waiting = g.placesOf([a, pane('%6', { group: 'g00000001', state: 'Input needed' }), pane('%7', { group: 'g00000001', state: 'Approval' })], groups)
assert.equal(g.waitingElsewhere(waiting, ''), 2, 'The phone switcher counts waiting tabs in other groups')
assert.equal(g.waitingElsewhere(waiting, 'g00000001'), 0)

// Alt+Shift+[ and ]: step with wrap; nothing with fewer than two places.
assert.equal(g.stepPlace(places, '', 1).id, 'g00000001')
assert.equal(g.stepPlace(places, '', -1).id, 'g00000002')
assert.equal(g.stepPlace(places, 'g00000002', 1).id, '')
assert.equal(g.stepPlace(places.slice(0, 1), '', 1), undefined)

// Names: the same rules as server/groups.py.
assert.equal(g.groupNameError('  ', groups), 'Type a name.')
assert.equal(g.groupNameError('x'.repeat(41), groups), 'Use 40 characters or fewer.')
assert.equal(g.groupNameError('x'.repeat(40), groups), '')
assert.equal(g.groupNameError('bad\u0007name', groups), 'Use letters, numbers and spaces.')
assert.equal(g.groupNameError('main', groups), 'Unstacked and Main are kept for the tabs in no stack.')
assert.equal(g.groupNameError(' Unstacked', groups), 'Unstacked and Main are kept for the tabs in no stack.')
assert.equal(g.groupNameError(' home DASHBOARD ', groups), 'A stack with that name exists.')
assert.equal(g.groupNameError('Home dashboard', groups, 'g00000001'), '', 'A rename may keep its own name')

// Each group remembers its last tab on this device; a closed tab falls back to the first.
const home = places[1]
assert.equal(g.tabFor('$1', home).id, '%2')
g.rememberTab('$1', 'g00000001', '%3')
assert.equal(g.tabFor('$1', home).id, '%3')
assert.equal(g.tabFor('$1', { ...home, panes: [b] }).id, '%2')
assert.equal(g.tabFor('$1', places[2]), undefined, 'An empty group has no tab to open')
for (let i = 0; i < 250; i++) g.rememberTab('$9', `g${i}`, '%1')
assert.ok(Object.keys(JSON.parse(store.get('tether.groupLastTab'))).length <= 201, 'The memory stays small')
store.clear()
localStorage.setItem('tether.groupLastTab', '{bad json')
assert.equal(g.tabFor('$1', home).id, '%2', 'Bad saved data falls back to the first tab')

// A sideways strip shows the open item fully and moves only as far as it must (stack row and tab strip).
assert.equal(g.revealLeft(0, 300, 1200, 50, 80), 0, 'An item in view keeps the strip still')
assert.equal(g.revealLeft(0, 300, 1200, 900, 100), 712, 'An item past the right edge comes in at the right, with the gap')
assert.equal(g.revealLeft(600, 300, 1200, 100, 80), 88, 'An item past the left edge comes in at the left, with the gap')
assert.equal(g.revealLeft(0, 300, 1200, 1150, 50), 900, 'The strip never scrolls past its end')
assert.equal(g.revealLeft(500, 300, 1200, 5, 40), 0, 'Or before its start')
assert.equal(g.revealLeft(0, 300, 1200, 400, 500), 388, 'An item wider than the strip shows from its start')
assert.equal(g.revealLeft(0, 0, 0, 0, 0), 0, 'A hidden strip stays at 0')
assert.notEqual(g.stripKey('$1', ''), g.stripKey('$1', 'g00000001'), 'Each stack of a session keeps its own strip place')

console.log('test_groups: ok')
