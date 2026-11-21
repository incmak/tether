const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(file, imports = {}, globals = {}) {
  const source = readFileSync(join(__dirname, 'src', file), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    fileName: file,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  })
  const exports = {}
  runInNewContext(outputText, { exports, require: name => imports[name] ?? {}, URLSearchParams, AbortController, ...globals }, { filename: file })
  return exports
}

const imports = { './api': { errorText: error => error.message } }
const { chipLabel, closeDraft, dropNotes, editPrompt, makePrompt, sendPrompt } = load('fridayNotes.ts', imports)
const docs = { id: 'T1', title: 'Docs', chat: '', state: 'open', notes: [{ text: 'Keep this.', at: 100 }] }
const api = {
  id: 'T2', title: 'API', chat: '', state: 'open',
  notes: [{ text: 'Use port 8080.', at: 101 }, { text: 'Keep the route.', at: 102 }],
}

test('one topic shows its title and note count', () => {
  const { lead, count } = chipLabel([api])
  assert.equal(lead, 'Held for API')
  assert.equal(count, '2')
})

test('two topics show the topic count and the sum of their notes', () => {
  const { lead, count } = chipLabel([docs, api])
  assert.equal(lead, 'Held: 2 topics')
  assert.equal(count, '3')
})

test('Friday needs a confirmed topic ID before it can call the drop API', async () => {
  const request = () => assert.fail('The drop API must not be called without an ID')
  for (const args of [{ topic: 'API' }, { topic: 'API', id: 2, note: 1 }]) {
    const result = await dropNotes(request, args)
    assert.equal(result.error, 'Not deleted. Friday must read it back and hear yes first.')
  }
})

test('Friday sends the confirmed ID and uses the exact text for a note drop', async () => {
  const calls = []
  const request = (path, body) => {
    calls.push([path, { ...body }])
    return { dropped: body.note === undefined ? 'topic' : 'note', topic: 'API', left: 0 }
  }
  assert.equal((await dropNotes(request, { topic: 'unused', id: 'T2', note: 1, text: 'Keep the route.' })).ok, true)
  assert.equal((await dropNotes(request, { id: 'T2', count: 2, texts: ['A', 'B'] })).ok, true)
  assert.deepEqual(calls, [
    ['friday-notes/drop', { id: 'T2', note: 1, text: 'Keep the route.' }],
    ['friday-notes/drop', { id: 'T2', count: 2, texts: ['A', 'B'] }],
  ])
})

const grant = { id: 'T2', rev: 2, pane: '%42', thread: 'api-thread' }
const revision = { rev: 2, text: 'Run the checks.', summary: 'Adds a test step.', skill: null, at: 103 }
const draft = { state: 'ready', error: null, asked_close: false, revisions: [revision],
  chat: { pane: '%12', thread: 'draft-thread', name: 'Draft API' } }

test('sendPrompt refuses every incomplete grant without an API call', async () => {
  let calls = 0
  const request = () => { calls++ }
  for (const key of Object.keys(grant)) {
    const args = { ...grant, topic: 'API' }
    delete args[key]
    assert.equal((await sendPrompt(request, args)).error, 'Not sent. Friday must read it back and hear yes first.')
  }
  assert.equal(calls, 0)
})

test('sendPrompt sends the grant only and asks about close when the store claims the question', async () => {
  for (const [eligible, claimed] of [[true, true], [true, false], [false, false]]) {
    const calls = []
    const request = async (path, body) => {
      calls.push([path, { ...body }])
      if (path.endsWith('/draft-asked')) return { asked_close: true, ask_close: claimed }
      return { chat: 'API Server', rev: 2, length: 15, ask_close: eligible }
    }
    const result = await sendPrompt(request, { ...grant, topic: 'API', text: 'Forged' })
    assert.deepEqual({ ...result }, { ok: true, chat: 'API Server', rev: 2, length: 15, ask_close: claimed })
    assert.deepEqual(calls, [
      ['friday-notes/draft-send', grant],
      ...(eligible ? [['friday-notes/draft-asked', { id: 'T2' }]] : []),
    ])
  }
  let calls = 0
  const refused = await sendPrompt(async () => { calls++; throw new Error('The draft changed.') }, grant)
  assert.equal(refused.error, 'The draft changed.')
  assert.equal(calls, 1)
})

test('editPrompt refuses a missing, unfinished, or empty draft without an edit call', async () => {
  for (const value of [null, { ...draft, state: 'drafting' }, { ...draft, revisions: [] }]) {
    const calls = []
    const request = async (path, body) => { calls.push([path, body]); return { topics: [{ ...api, draft: value }] } }
    const result = await editPrompt(request, { topic: 'API', change: 'Add a step.' }, () => assert.fail('Watch started'))
    assert.equal(result.error, 'There is no finished prompt for API yet.')
    assert.deepEqual(calls, [['friday-notes?topic=API&scope=open', undefined]])
  }
})

test('makePrompt and editPrompt find open topics and watch only after success', async () => {
  for (const [tool, route, body] of [
    [makePrompt, 'draft', { id: 'T2' }],
    [editPrompt, 'draft-edit', { id: 'T2', rev: 2, edit: '  Add a step. ' }],
  ]) {
    const calls = []
    const request = async (path, payload) => {
      calls.push([path, payload && { ...payload }])
      return payload ? { state: 'drafting', chat: 'Draft API' } : { topics: [{ ...api, draft }] }
    }
    const result = await tool(request, { topic: 'API', change: '  Add a step. ' }, id => calls.push(['watch', id]))
    assert.deepEqual({ ...result }, { ok: true, topic: 'API', state: 'drafting', chat: 'Draft API' })
    assert.deepEqual(calls, [
      ['friday-notes?topic=API&scope=open', undefined], [`friday-notes/${route}`, body], ['watch', 'T2'],
    ])
    const error = await tool(async () => { throw new Error('Several topics match: API Docs, API Ops. Which one?') },
      { topic: 'API', change: 'Add a step.' }, () => assert.fail('Watch started'))
    assert.equal(error.error, 'Several topics match: API Docs, API Ops. Which one?')
  }
})

test('closeDraft uses the sent topic and its stored draft conversation, including on a repeated close', async () => {
  for (const state of ['ready', 'closed']) {
    const calls = []
    const request = async (path, body) => {
      calls.push([path, body && { ...body }])
      return { topics: [{ ...api, state: 'sent', draft: { ...draft, state } }] }
    }
    assert.equal((await closeDraft(request, { topic: 'API', pane: '%99', thread: 'wrong-thread' })).ok, true)
    assert.deepEqual(calls, [
      ['friday-notes?topic=API&scope=draft', undefined],
      ['friday-notes/draft-close', { id: 'T2', pane: '%12', thread: 'draft-thread' }],
    ])
  }
})

function fakeClock() {
  const timers = new Map()
  let now = 0
  let id = 0
  return {
    setTimeout(fn, delay) { timers.set(++id, { fn, at: now + delay }); return id },
    clearTimeout(id) { timers.delete(id) },
    async tick(ms) {
      const target = now + ms
      for (;;) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0]
        if (!next || next[1].at > target) break
        const [id, { fn, at }] = next
        now = at
        timers.delete(id)
        await fn()
      }
      now = target
    },
  }
}

test('draft watch polls once per topic and speaks ready or failed exactly once', async () => {
  const clock = fakeClock()
  const { createDraftWatch } = load('fridayNotes.ts', imports, clock)
  let calls = 0
  let current = { ...draft, state: 'drafting' }
  const lines = []
  const watch = createDraftWatch(async path => {
    assert.equal(path, 'friday-notes')
    calls++
    return { topics: [{ ...api, draft: current }] }
  }, (text, show) => lines.push([text, show && { ...show }]))
  watch.start(api.id)
  watch.start(api.id)
  await clock.tick(2999)
  assert.equal(calls, 0)
  await clock.tick(1)
  assert.equal(calls, 1)
  assert.deepEqual(lines, [])
  current = draft
  await clock.tick(3000)
  await clock.tick(18000)
  assert.equal(calls, 2)
  assert.deepEqual(lines, [[
    'The prompt for API is ready, version 2. Adds a test step. Say send it to a chat, read all of it, or a change.',
    { id: 'T2', rev: 2 },
  ]])
  current = { ...draft, state: 'failed', error: 'The draft chat closed.' }
  watch.start(api.id)
  await clock.tick(6000)
  assert.equal(calls, 3)
  assert.deepEqual(lines.at(-1), ['The prompt for API failed. The draft chat closed.', undefined])
})

test('draft watch stops at seven minutes, on cleanup, and on a fetch error', async () => {
  const clock = fakeClock()
  const { createDraftWatch } = load('fridayNotes.ts', imports, clock)
  let calls = 0
  let fails = false
  const watch = createDraftWatch(async () => {
    calls++
    if (fails) throw new Error('Unavailable')
    return { topics: [{ ...api, draft: { ...draft, state: 'drafting' } }] }
  }, () => assert.fail('An unfinished draft must stay silent'))
  watch.start(api.id)
  await clock.tick(8 * 60 * 1000)
  assert.equal(calls, 139)
  watch.start(api.id)
  watch.start(docs.id)
  watch.clear()
  await clock.tick(3000)
  assert.equal(calls, 139)
  fails = true
  watch.start(api.id)
  await clock.tick(15000)
  assert.equal(calls, 140)
})

test('ending the call aborts a draft poll that is already in flight', async () => {
  const clock = fakeClock()
  const { createDraftWatch } = load('fridayNotes.ts', imports, clock)
  let signal
  let finishRead
  let announcements = 0
  const watch = createDraftWatch((path, body, abort) => {
    signal = abort
    return new Promise(resolve => { finishRead = resolve })
  }, () => { announcements++ })
  watch.start(api.id)
  const poll = clock.tick(3000)
  watch.clear()
  assert.equal(signal.aborted, true)
  finishRead({ topics: [{ ...api, draft }] })
  await poll
  assert.equal(announcements, 0)
})
