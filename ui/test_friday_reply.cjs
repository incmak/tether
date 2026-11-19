const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const { test } = require('node:test')

function load(file, imports = {}) {
  const exports = {}
  runInNewContext(ts.transpileModule(readFileSync(join(__dirname, 'src', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, { exports, require: name => imports[name] })
  return exports
}

const { replyPart, replyPage, REPLY_PART, REPLY_WINDOWS, findLastReply, isReply } = load('fridayReply.ts', { './api': load('api.ts') })
const { speakable } = load('speech.ts', { react: {} })
test('reply parts preserve text, word and surrogate boundaries, and offsets', () => {
  const fixture = readFileSync(join(__dirname, 'fixtures', 'factory-floor-reply.md'), 'utf8')
  const spoken = speakable(fixture)
  // Convert the cross-realm result to a plain object for assertions.
  const part = (...args) => ({ ...replyPart(...args) })
  const realReply = part(spoken)
  assert.equal(REPLY_PART, 6000)
  assert.ok(spoken.length > 1500)
  assert.deepEqual(realReply, { text: spoken, total_chars: spoken.length, offset: 0, truncated: false })
  for (let n = 1; n <= 5; n++) assert.ok(realReply.text.includes(`Q${n} - `))

  const long = 'alpha beta gamma '.repeat(883).slice(0, 15000)
  const first = part(long)
  const second = part(long, first.next_offset)
  const third = part(long, second.next_offset)
  assert.deepEqual([first.truncated, second.truncated, third.truncated], [true, true, false])
  assert.equal(third.next_offset, undefined)
  assert.equal(first.text + second.text + third.text, long)
  assert.deepEqual([first.offset, second.offset, third.offset], [0, first.text.length, first.text.length + second.text.length])
  for (const item of [first, second, third]) {
    assert.ok(item.text.length <= REPLY_PART)
    assert.equal(item.total_chars, 15000)
  }

  assert.deepEqual(part('alpha beta', 0, 8), {
    text: 'alpha ', total_chars: 10, offset: 0, truncated: true, next_offset: 6,
  })
  assert.equal(part('alpha beta', 0, 5).text, 'alpha')
  assert.deepEqual(part('a😀b', 0, 2), { text: 'a', total_chars: 4, offset: 0, truncated: true, next_offset: 1 })
  assert.equal(part('a😀b', 1).text, '😀b')
  assert.equal(part('a😀b', 2).text, '😀b')

  for (const offset of [-1, NaN, -Infinity]) {
    assert.deepEqual(part('abc', offset), { text: 'abc', total_chars: 3, offset: 0, truncated: false })
  }
  for (const offset of [3, 99, Infinity]) {
    assert.deepEqual(part('abc', offset), { text: '', total_chars: 3, offset: 3, truncated: false })
  }
  assert.deepEqual(part(''), { text: '', total_chars: 0, offset: 0, truncated: false })
})

const reply = (id = 'reply') => ({ id, role: 'assistant', text: 'The answer.' })
const other = i => i % 2
  ? { id: `user-${i}`, role: 'user', text: 'Next question' }
  : { id: `actions-${i}`, role: 'assistant', kind: 'actions', count: '3', text: 'Read files' }
const fakeChat = messages => {
  const calls = []
  return { calls, fetch: async limit => {
    calls.push(limit)
    return { messages: messages.slice(-limit), earlier: Math.max(0, messages.length - limit) }
  } }
}
for (const [distance, limits] of [[25, [20, 200]], [300, [20, 200, 2000]]]) {
  test(`finds a reply ${distance} messages back`, async () => {
    const message = reply()
    const fake = fakeChat([message, ...Array.from({ length: distance }, (_, i) => other(i))])
    const found = await findLastReply(fake.fetch)
    assert.equal(found.message, message)
    assert.equal(found.searchedAll, true)
    assert.deepEqual(fake.calls, limits)
  })
}
test('empty history and earlier zero use the No reply yet path after one fetch', async () => {
  for (const messages of [[], Array.from({ length: 10 }, (_, i) => other(i))]) {
    const fake = fakeChat(messages)
    const result = await findLastReply(fake.fetch)
    assert.equal(result.message, null)
    assert.equal(result.searchedAll, true)
    assert.deepEqual(fake.calls, [20])
  }
})
test('missing earlier means the complete history was searched', async () => {
  const calls = []
  const result = await findLastReply(async limit => { calls.push(limit); return { messages: [other(0)] } })
  assert.equal(result.message, null)
  assert.equal(result.searchedAll, true)
  assert.deepEqual(calls, [20])
})
test('stops at 2000 and reports an incomplete search when older messages remain', async () => {
  const fake = fakeChat([reply(), ...Array.from({ length: 2000 }, (_, i) => other(i))])
  const result = await findLastReply(fake.fetch)
  assert.equal(result.message, null)
  assert.equal(result.searchedAll, false)
  assert.deepEqual(fake.calls, [20, 200, 2000])
  assert.deepEqual([...REPLY_WINDOWS], [20, 200, 2000])
})
test('a found reply stops the search even with older messages left', async () => {
  const message = reply()
  const fake = fakeChat([...Array.from({ length: 50 }, (_, i) => other(i)), message])
  const result = await findLastReply(fake.fetch)
  assert.equal(result.message, message)
  assert.equal(result.searchedAll, false)
  assert.deepEqual(fake.calls, [20])
})
test('only nonblank assistant text without a kind counts, and newest reply wins', async () => {
  for (const message of [other(0), other(1), { ...reply(), text: '  \n ' },
    ...['notice', 'advisor', 'terminal', 'peer', 'watch', 'side'].map(kind => ({ ...reply(), kind }))]) {
    assert.equal(isReply(message), false)
  }
  const newest = reply('newest')
  assert.equal(isReply(newest), true)
  const fake = fakeChat([reply('old'), newest, other(0), other(1)])
  assert.equal((await findLastReply(fake.fetch)).message, newest)
})
test('fetch errors propagate without extra requests', async () => {
  const calls = []
  await assert.rejects(findLastReply(async limit => { calls.push(limit); throw new Error('offline') }), /offline/)
  assert.deepEqual(calls, [20])
})
test('paging keeps offsets for the same id and for callers without an id', () => {
  for (const previousId of ['same', undefined]) {
    const result = replyPage('first second', 'same', 6, previousId)
    assert.equal(result.text, 'second')
    assert.equal(result.offset, 6)
    assert.equal(result.reply_id, 'same')
    assert.equal(result.new_reply, undefined)
  }
})
test('paging restarts at zero when the reply id changes, even for an empty previous id', () => {
  for (const previousId of ['old', '']) {
    const result = replyPage('new reply', 'new', 6000, previousId)
    assert.equal(result.text, 'new reply')
    assert.equal(result.offset, 0)
    assert.equal(result.reply_id, 'new')
    assert.equal(result.new_reply, true)
  }
})

// Exercise the actual Live tool handler with fixture-only API calls, without mounting React or using a terminal.
function liveRead(fetchChat, findPane) {
  const source = readFileSync(join(__dirname, 'src', 'Live.tsx'), 'utf8')
  const ast = ts.createSourceFile('Live.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  let handler
  const visit = node => {
    if (ts.isPropertyAssignment(node) && node.name.getText(ast) === 'read_last_reply') handler = node.initializer.getText(ast)
    ts.forEachChild(node, visit)
  }
  visit(ast)
  assert.ok(handler, 'Live must register the read_last_reply tool')
  const pane = { id: '%fixture', title: 'Fixture chat' }
  return runInNewContext(ts.transpileModule(`(${handler})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2020 },
  }).outputText, {
    live: { current: { agents: [pane], target: pane, api: path => {
      const params = new URLSearchParams(path.split('?')[1])
      assert.equal(params.get('session'), pane.id)
      return fetchChat(Number(params.get('limit')))
    } } },
    sessionChoice: () => null, findPane: findPane ?? (() => pane), paneTitle: item => item.title,
    findLastReply, replyPage, speakable, encodeURIComponent,
  })
}
test('Live distinguishes an empty history from a capped search', async () => {
  for (const [count, expected, limits] of [
    [0, 'No reply yet.', [20]],
    [2001, 'No reply in the last 2000 messages.', [20, 200, 2000]],
  ]) {
    const fake = fakeChat(Array.from({ length: count }, (_, i) => other(i)))
    const result = await liveRead(fake.fetch)({ chat: 'Fixture chat' })
    assert.equal(result.text, expected)
    assert.equal(result.chat, 'Fixture chat')
    assert.deepEqual(fake.calls, limits)
  }
})
test('Live reads the current chat only when no chat is named', async () => {
  let fetches = 0
  const read = liveRead(async () => { fetches++; return { messages: [reply('r1')], earlier: 0 } }, () => undefined)
  // 20 Nov review: a missed name read the current chat and Friday reported it as the named one.
  const missed = await read({ chat: 'predicta' })
  assert.equal(missed.error, 'No chat called predicta.')
  assert.equal(fetches, 0)
  const current = await read({})
  assert.equal(current.chat, 'Fixture chat')
  assert.equal(fetches, 1)
})
test('Live pages the spoken reply and restarts when a new reply arrives', async () => {
  let message = { ...reply('old'), text: 'alpha beta '.repeat(1000) }
  const read = liveRead(async () => ({ messages: [message], earlier: 0 }))
  const first = await read({ chat: 'Fixture chat' })
  assert.equal(first.reply_id, 'old')
  assert.equal(first.truncated, true)
  const second = await read({ chat: 'Fixture chat', offset: first.next_offset, reply_id: first.reply_id })
  assert.equal(first.text + second.text, speakable(message.text))
  assert.equal(second.new_reply, undefined)
  message = reply('new')
  const fresh = await read({ chat: 'Fixture chat', offset: first.next_offset, reply_id: first.reply_id })
  assert.equal(fresh.reply_id, 'new')
  assert.equal(fresh.text, speakable(message.text))
  assert.equal(fresh.offset, 0)
  assert.equal(fresh.new_reply, true)
})
