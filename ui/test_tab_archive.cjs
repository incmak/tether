const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const compilerOptions = { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
const transpile = code => ts.transpileModule(code, { compilerOptions }).outputText
// Load the real modules with inert React imports. No component or terminal action runs.
function load(file) {
  const exports = {}
  runInNewContext(transpile(readFileSync(file, 'utf8')), {
    exports,
    require: name => name === './paneState' ? load('src/paneState.ts') : name === 'react' ? { lazy: () => null, forwardRef: component => component, createContext: value => ({ value }) } : {},
  })
  return exports
}
const archive = load('src/tabArchive.ts')
const { isTabArchived, withTabArchived, withoutTab, tabNeighbor } = archive
const sidebar = load('src/Sidebar.tsx')
const { groupSessions, isArchived } = sidebar
const ordering = load('src/PaneTabs.tsx')
const { orderPanes, reorderPanes } = ordering
const { selectPane } = load('src/paneSelection.ts')
const plain = value => JSON.parse(JSON.stringify(value))
const ids = panes => Array.from(panes, pane => pane.id)
const pane = (id, extra = {}) => ({ id, tmux_session: 'work', label: `Work:0.${id}`, dir: '/work', thread: id, agent: 'codex', state: 'Idle', ...extra })
const a = pane('a'), b = pane('b'), c = pane('c')
const entry = target => ({ thread: target.thread, dir: target.dir })
const map = { a: entry(a) }
assert.equal(isTabArchived(a, {}), false)
assert.equal(isTabArchived(a, map), true)
assert.equal(isTabArchived({ ...a, dir: '/new' }, map), false, 'Reused id in another directory stays visible')
assert.equal(isTabArchived({ ...a, thread: 'new' }, map), false, 'Reused id with another thread stays visible')
assert.equal(isTabArchived({ ...a, thread: null }, map), true, 'A poll without a thread still matches')
assert.equal(isTabArchived(a, { a: { ...entry(a), thread: null } }), true, 'An archive without a thread still matches')
assert.equal(isTabArchived({ ...a, dir: '/new', thread: null }, map), false)
const oldMap = { a: entry(a), dead: entry(b) }
const added = withTabArchived(oldMap, b, new Set(['a', 'b']))
assert.deepEqual(plain(added), { a: entry(a), b: entry(b) }, 'Archive prunes dead ids')
assert.deepEqual(oldMap, { a: entry(a), dead: entry(b) }, 'Archive does not mutate its input')
assert.deepEqual(plain(withoutTab(added, 'a')), { b: entry(b) })
assert.deepEqual(plain(added), { a: entry(a), b: entry(b) }, 'Restore does not mutate its input')
assert.notEqual(withoutTab(added, 'missing'), added)
const session = { panes: [a, b, c] }
assert.equal(tabNeighbor(session, 'b'), a)
assert.equal(tabNeighbor(session, 'a'), b)
assert.equal(tabNeighbor(session, 'c'), b)
assert.equal(tabNeighbor({ panes: [a] }, 'a'), undefined)
assert.equal(tabNeighbor(session, 'missing'), undefined)
console.log('Tab archive: identity reuse, null threads, immutable maps, pruning and neighbors pass')

// Distinct labels and dirs prove metadata still comes from the first sorted pane.
const first = pane('first', { label: 'Original:0.1', dir: '/original', state: 'Approval', agent: 'claude' })
const second = pane('second', { label: 'Changed:0.2', state: 'Working' })
const last = pane('last', { label: 'Changed:0.10', state: 'Idle', agent: 'shell' })
const input = [last, second, first]
const [full] = groupSessions(input)
assert.deepEqual(plain(full), { id: 'work', name: 'Original', dir: '/original', panes: [first, second, last], agent: 'claude', state: 'Approval' })
assert.deepEqual(plain(groupSessions(input, () => false)), plain([full]), 'No hidden panes preserves the original output')
const [hiddenFirst] = groupSessions(input, item => item.id === first.id)
assert.equal(hiddenFirst.name, full.name)
assert.equal(hiddenFirst.dir, full.dir)
assert.equal(hiddenFirst.id, full.id)
assert.equal(hiddenFirst.agent, 'codex')
assert.equal(hiddenFirst.state, 'Working')
assert.equal(isArchived(hiddenFirst, { work: 'Original' }), true)
const [shellOnly] = groupSessions(input, item => item.id !== last.id)
assert.equal(shellOnly.agent, 'shell')
assert.equal(shellOnly.state, 'Idle')
assert.equal(groupSessions(input, () => true).length, 0)
assert.deepEqual(ids(input), ['last', 'second', 'first'], 'Grouping does not sort the input in place')
console.log('Tab archive: grouping keeps session identity, recomputes visible state/agent and drops empty sessions')

const d = pane('d')
const all = [a, b, c, d]
const saved = ['c', 'a', 'b', 'd']
const reordered = reorderPanes(all, saved, ['d', 'c'])
assert.deepEqual(Array.from(reordered), ['d', 'a', 'b', 'c'], 'Visible reorder keeps hidden positions')
assert.deepEqual(ids(orderPanes(all, reordered)), ['d', 'a', 'b', 'c'], 'Restore uses the retained positions')
assert.deepEqual(ids(orderPanes(all, ['c', 'a'])), ['c', 'a', 'b', 'd'], 'Restored ids absent from paneOrder keep tmux order at the end')
assert.deepEqual(Array.from(reorderPanes(all, undefined, ['d', 'a'])), ['d', 'b', 'c', 'a'])
console.log('Tab archive: visible reorder preserves hidden positions; missing saved ids are safe')

// Exercise App's actual declarations with in-memory prefs, without mounting its UI.
const appSource = ts.createSourceFile('App.tsx', readFileSync('src/App.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const app = appSource.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'App')
const declarations = app.body.statements.filter(ts.isVariableStatement).flatMap(node => Array.from(node.declarationList.declarations))
const appConst = name => {
  const declaration = declarations.find(node => node.name.getText(appSource) === name)
  assert(declaration, `App declares ${name}`)
  return transpile(`exports.${name} = ${declaration.initializer.getText(appSource)}`)
}
const layoutEffect = app.body.statements.find(node => ts.isExpressionStatement(node) && ts.isCallExpression(node.expression) && node.expression.expression.getText(appSource) === 'useLayoutEffect' && node.getText(appSource).includes('previousSelection.current'))
function fixture(panes, initial) {
  let prefs = { archivedTabs: {}, archived: {}, paneOrder: {}, lastPane: {}, selected: 'b', ...initial }
  let updates = 0
  const context = {
    ...archive, ...sidebar, ...ordering, panes, exports: {}, data: {},
    useMemo: calculate => calculate(), useLayoutEffect: effect => effect(),
    previousSelection: { current: { sessions: [] } },
    update: change => { updates++; prefs = { ...prefs, ...change(prefs) } },
  }
  function render() {
    context.prefs = prefs
    runInNewContext(appConst('sessions'), context)
    context.sessions = context.exports.sessions
    context.orderedSessions = context.sessions
    context.pane = selectPane(context.sessions, prefs.selected, context.previousSelection.current, 'other', prefs.lastPane)
    for (const name of ['archivedTabs', 'live', 'archiveTab', 'restoreTab']) runInNewContext(appConst(name), context)
    runInNewContext(transpile(layoutEffect.getText(appSource)), context)
    return { ...context.exports }
  }
  return { render, get prefs() { return prefs }, get updates() { return updates }, update: context.update }
}
const other = pane('other', { tmux_session: 'elsewhere', label: 'Elsewhere' })
const active = fixture([other, a, b, c], { paneOrder: { work: ['c', 'b', 'a'] }, lastPane: { work: 'b', elsewhere: 'other' } })
const activeView = active.render()
// Changes since the preceding render must survive the archive callback.
active.update(current => ({ archivedTabs: { ...current.archivedTabs, other: entry(other) }, lastPane: { ...current.lastPane, elsewhere: 'changed' } }))
const beforeArchive = active.updates
activeView.archiveTab(b)
assert.equal(active.updates, beforeArchive + 1, 'Archive and selection use one update')
assert.equal(active.prefs.selected, 'c', 'Archive uses the visual neighbor, not the cross-session default')
assert.equal(active.prefs.lastPane.work, 'c')
assert.equal(active.prefs.lastPane.elsewhere, 'changed', 'Archive uses current lastPane')
assert(isTabArchived(other, active.prefs.archivedTabs), 'Archive uses the current archive map')
const afterArchive = active.render()
assert.equal(active.updates, beforeArchive + 1, 'Layout effect does not trigger a close fallback')
assert.equal(afterArchive.live.has('b'), false, 'Hidden tabs leave desktop slots')
assert.deepEqual(ids(afterArchive.archivedTabs), ['other', 'b'])
assert.deepEqual(ids(activeView.sessions.flatMap(session => session.panes)), ['other', 'c', 'b', 'a'])
active.update(() => ({ selected: 'b' }))
const reopened = active.render()
assert.equal(reopened.live.has('b'), true, 'An archived tab opened elsewhere shows while selected')
assert(!ids(reopened.archivedTabs).includes('b'))
active.update(() => ({ selected: 'a' }))
assert.equal(active.render().live.has('b'), false, 'The tab hides again after selection moves away')
active.render().restoreTab(b)
assert.equal(isTabArchived(b, active.prefs.archivedTabs), false)
assert.equal(active.prefs.selected, 'a', 'Restore does not move selection')
const inactive = fixture([other, a, b, c], { selected: 'other', lastPane: { work: 'b' } })
inactive.render().archiveTab(b)
assert.equal(inactive.prefs.selected, 'other')
assert.equal(inactive.prefs.lastPane.work, 'a', 'Archiving a remembered inactive tab updates its neighbor')
const remembered = fixture([other, a, b, c], { selected: 'other', lastPane: { work: 'c' } })
remembered.render().archiveTab(b)
assert.equal(remembered.prefs.lastPane.work, 'c')
const single = fixture([a, b], { archivedTabs: { a: entry(a) } })
single.render().archiveTab(b)
assert.equal(single.prefs.archived.work, 'Work', 'Last visible tab archives its session')
assert.equal(single.prefs.selected, 'b')
assert.equal(isTabArchived(b, single.prefs.archivedTabs), false)
const singleUpdates = single.updates
single.render()
assert.equal(single.updates, singleUpdates)
const hiddenSession = fixture([other, a], { selected: 'other', archivedTabs: { a: entry(a) }, archived: { work: 'Work' } })
hiddenSession.render().restoreTab(a)
assert.equal(hiddenSession.prefs.archived.work, undefined, 'Restore of the only shown tab also restores its hidden session')
const multi = fixture([other, a, b], { selected: 'other', archivedTabs: { a: entry(a) }, archived: { work: 'Work' } })
multi.render().restoreTab(a)
assert.equal(multi.prefs.archived.work, 'Work', 'Restore preserves session archive when other tabs show')
// B446: selecting a child of an archived task reveals both tabs.
const child = pane('child', { task: 'a' })
const archivedTask = fixture([a, b, child], { archivedTabs: { a: entry(a) } })
const hiddenTaskView = archivedTask.render()
assert.deepEqual(ids(hiddenTaskView.sessions.flatMap(session => session.panes)), ['b'], 'Archiving a task hides its child')
assert.equal(hiddenTaskView.live.has(child.id), false, 'The hidden child leaves desktop slots')
assert.deepEqual(ids(hiddenTaskView.archivedTabs), ['a'], 'Only the task has its own archive entry')
archivedTask.update(() => ({ selected: child.id }))
const selectedChildView = archivedTask.render()
assert.deepEqual(ids(selectedChildView.sessions.flatMap(session => session.panes)), ['a', 'b', 'child'], 'Selecting the child reveals its archived task')
assert(selectedChildView.live.has(child.id), 'The selected child can use a desktop slot')
const empty = fixture([], { selected: null })
assert.equal(empty.render().live.size, 0)
console.log('Tab archive: App atomic selection, current prefs, selected exception, slots, last-tab/session restore and empty polling pass')
