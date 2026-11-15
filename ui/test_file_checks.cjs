const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const output = {}
runInNewContext(ts.transpileModule(readFileSync('src/fileChecks.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, {
  exports: output,
  require: name => { assert.equal(name, './api'); return { request: () => { throw Error('Inject post') } } },
  setTimeout, clearTimeout,
})

function setup() {
  let now = 0
  let id = 0
  let visible = true
  let visibility
  const timers = new Map()
  const calls = []
  const store = output.createFileChecks({
    post: (pane, paths) => new Promise((resolve, reject) => calls.push({ pane, paths, resolve, reject })),
    later: (callback, delay) => { timers.set(++id, { at: now + delay, callback }); return id },
    cancel: timer => timers.delete(timer),
    visible: () => visible,
    onVisibility: listener => { visibility = listener; return () => { visibility = undefined } },
  })
  function tick(ms) {
    const end = now + ms
    while (true) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
      if (!next) break
      now = next[1].at
      timers.delete(next[0])
      next[1].callback()
    }
    now = end
  }
  const subscribe = (path, pane = 'pane', listener = () => {}) => store.subscribe(pane, path, listener)
  const answer = (call, open) => call.resolve({ results: Object.fromEntries(call.paths.map(path => [path, open])) })
  return { store, calls, timers, tick, subscribe, answer,
    visibility: value => { visible = value; visibility?.() } }
}
const settle = async () => { await Promise.resolve(); await Promise.resolve() }

async function main() {
  {
    const h = setup()
    assert.equal(h.timers.size, 0)
    assert.equal(h.store.stateOf('pane', 'a.md'), 'pending')
    assert.equal(h.timers.size, 0, 'Snapshot reads do not start timers')
    let notices = 0
    const offs = [h.subscribe('a.md', 'pane', () => notices++)]
    h.tick(100)
    offs.push(h.subscribe('b.md'), h.subscribe('a.md'))
    h.tick(49)
    assert.equal(h.calls.length, 0)
    h.tick(1)
    assert.equal(h.calls.length, 1)
    assert.deepEqual(Array.from(h.calls[0].paths), ['a.md', 'b.md'])
    h.answer(h.calls[0], true)
    await settle()
    assert.equal(h.store.stateOf('pane', 'a.md'), 'open')
    assert.equal(notices, 1)
    offs.forEach(off => off())
    assert.equal(h.timers.size, 0)
  }
  console.log('File checks: 150 ms batching, deduplication, stable snapshots, listener updates, idle timers pass')
  {
    const h = setup()
    const offs = Array.from({ length: 601 }, (_, i) => h.subscribe(`file${i}.md`))
    h.tick(150)
    assert.equal(h.calls.length, 2, 'Only two requests in flight')
    assert.equal(h.calls[0].paths.length, 200)
    assert.equal(h.calls[1].paths.length, 200)
    h.answer(h.calls[0], true)
    await settle()
    assert.equal(h.calls.length, 3)
    assert.equal(h.calls[2].paths.length, 200)
    h.answer(h.calls[1], false)
    await settle()
    assert.equal(h.calls.length, 4)
    assert.equal(h.calls[3].paths.length, 1)
    offs.forEach(off => off())
    h.answer(h.calls[2], true)
    h.answer(h.calls[3], true)
    await settle()
    assert.equal(h.timers.size, 0)
  }
  console.log('File checks: split at 200 and global two-request limit pass')
  {
    const h = setup()
    const offs = [h.subscribe('same.md', 'left'), h.subscribe('same.md', 'right'), h.subscribe('third.md', 'third')]
    h.tick(150)
    assert.deepEqual(h.calls.map(call => call.pane), ['left', 'right'])
    h.calls[0].reject(Error('401'))
    h.answer(h.calls[1], false)
    await settle()
    assert.equal(h.calls.length, 3)
    assert.equal(h.store.stateOf('left', 'same.md'), 'unknown')
    assert.equal(h.store.stateOf('right', 'same.md'), 'closed')
    h.tick(1000)
    assert.equal(h.calls.length, 3, 'Failure does not retry')
    h.answer(h.calls[2], true)
    await settle()
    offs.forEach(off => off())
  }
  console.log('File checks: pane isolation, global queue, failure gives unknown without retry pass')
  {
    const h = setup()
    const off = h.subscribe('a.md')
    h.tick(150)
    h.answer(h.calls[0], true)
    await settle()
    h.tick(29850)
    assert.equal(h.store.stateOf('pane', 'a.md'), 'open')
    h.tick(150)
    assert.equal(h.calls.length, 2)
    assert.equal(h.store.stateOf('pane', 'a.md'), 'open', 'Recheck keeps old state')
    h.answer(h.calls[1], false)
    await settle()
    assert.equal(h.store.stateOf('pane', 'a.md'), 'closed')
    h.visibility(false)
    assert.equal(h.timers.size, 0)
    h.tick(60000)
    assert.equal(h.calls.length, 2)
    h.visibility(true)
    h.tick(150)
    assert.equal(h.calls.length, 3)
    assert.equal(h.store.stateOf('pane', 'a.md'), 'closed')
    h.answer(h.calls[2], true)
    await settle()
    assert.equal(h.store.stateOf('pane', 'a.md'), 'open')
    off()
    assert.equal(h.timers.size, 0)
    h.visibility(true)
    h.tick(60000)
    assert.equal(h.calls.length, 3)
  }
  console.log('File checks: periodic and visibility rechecks retain then change state, hidden and idle timers pass')
  {
    const h = setup()
    const offs = Array.from({ length: 2001 }, (_, i) => h.subscribe(`file${i}.md`))
    h.tick(150)
    h.answer(h.calls[0], true)
    await settle()
    assert.equal(h.store.stateOf('pane', 'file0.md'), 'open')
    offs[0]()
    assert.equal(h.store.stateOf('pane', 'file0.md'), 'pending', 'Unsubscribed key above limit is forgotten')
    assert.equal(h.store.stateOf('pane', 'file1.md'), 'open', 'Subscribed key stays')
    offs.slice(1).forEach(off => off())
    assert.equal(h.store.stateOf('pane', 'file1.md'), 'open', 'At 2000 keys cached state stays')
    assert.equal(h.timers.size, 0)
    for (const call of h.calls.slice(1)) h.answer(call, false)
    await settle()
  }
  console.log('File checks: forget rule and subscribed-key retention pass')
  {
    const h = setup()
    const off = h.subscribe('cancel.md')
    off()
    h.tick(150)
    assert.equal(h.calls.length, 0)
    const offAgain = h.subscribe('cancel.md')
    h.tick(150)
    assert.equal(h.calls.length, 1)
    offAgain()
    h.answer(h.calls[0], true)
    await settle()
    assert.equal(h.timers.size, 0)
  }
  console.log('File checks: canceled batch and resubscription pass')
}
main().catch(error => { console.error(error); process.exitCode = 1 })

// Render the file-chip states with the real React contexts and server snapshot.
function checkRendering() {
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const components = {}
  let state = 'pending'
  let block = false
  const checks = []
  runInNewContext(ts.transpileModule(readFileSync('src/MediaLink.tsx', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, {
    exports: components,
    require: name => {
      if (name === '@assistant-ui/react-markdown') return { useIsMarkdownCodeBlock: () => block }
      if (name === './tabLinks') return { tabTarget: () => null }
      if (name === './preview') return { PreviewOpener: React.createContext(null), artifactUrl: () => null }
      if (name === './fileChecks') return { fileChecks: {
        stateOf: (pane, path) => { checks.push([pane, path]); return state },
        subscribe: () => { throw Error('Server rendering must not subscribe') },
      } }
      return require(name)
    },
  })
  const { InlineCode, Link, FilePane, MediaOpener } = components
  function render(component, props, pane = 'pane', opener = () => {}) {
    return renderToStaticMarkup(React.createElement(FilePane.Provider, { value: pane },
      React.createElement(MediaOpener.Provider, { value: opener }, React.createElement(component, props))))
  }
  for (const value of ['pending', 'closed', 'open', 'unknown']) {
    state = value
    const open = value === 'open' || value === 'unknown'
    const code = render(InlineCode, { children: 'Live.tsx:623', className: 'existing' })
    assert.ok(code.includes(open ? 'class="file-link"' : 'class="existing file-chip"'))
    for (const props of [{ 'data-path': 'Live.tsx:623', href: '#' }, { href: 'Live.tsx:623' }]) {
      const link = render(Link, { ...props, children: 'Live.tsx:623' })
      if (open) assert.ok(link.includes('class="file-link"'))
      else assert.equal(link, '<span class="file-chip">Live.tsx:623</span>')
    }
    assert.ok(checks.every(([pane, path]) => pane === 'pane' && path === 'Live.tsx'))
  }
  assert.equal(render(InlineCode, { children: 'a.md' }, null), '<code class="file-chip">a.md</code>')
  assert.equal(render(Link, { children: 'a.md', href: 'a.md' }, 'pane', null), '<span class="file-chip">a.md</span>')
  assert.equal(render(InlineCode, { children: 'a.md' }, 'pane', null), '<code class="file-chip">a.md</code>')
  block = true
  assert.equal(render(InlineCode, { children: 'a.md', className: 'language-md' }), '<code class="language-md">a.md</code>')
  block = false
  assert.equal(render(InlineCode, { children: 'ordinary text' }), '<code>ordinary text</code>')
  for (const href of ['https://example.com/a.md', 'http://example.com/a.md', 'mailto:person@example.com', '#section']) {
    assert.equal(render(Link, { href, children: 'label' }), `<a href="${href}" target="_blank" rel="noreferrer">label</a>`)
  }
  console.log('File rendering: all states, line removal, no pane/opener, code blocks and external links pass')
}
checkRendering()
