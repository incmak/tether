// Exercise the real hook with controlled browser events, requests and time.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

async function main() {
  let clock = 1000, effect, cleanup, calls = 0
  const states = [], timers = new Map(), events = new Map(), streams = [], signals = []
  let fail = false, nextTimer = 0
  const react = {
    useState(initial) { const at = states.length; states.push(initial); return [initial, value => { states[at] = value }] },
    useRef(value) { return { current: value } },
    useCallback(fn) { return fn },
    useEffect(fn) { effect = fn },
  }
  class Stream {
    listeners = new Map()
    constructor(url) { this.url = url; streams.push(this) }
    addEventListener(name, fn) { this.listeners.set(name, fn) }
    close() { this.closed = true }
  }
  const document = {
    hidden: false,
    addEventListener(name, fn) { events.set(name, fn) },
    removeEventListener(name) { events.delete(name) },
  }
  const output = { exports: {} }, windowMock = { EventSource: Stream }
  runInNewContext(ts.transpileModule(readFileSync('src/hooks.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    exports: output.exports,
    require: name => name === 'react' ? react : { errorText: error => error.message },
    document, window: windowMock, EventSource: Stream, AbortController,
    Date: { now: () => clock },
    setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer },
    clearTimeout(id) { timers.delete(id) },
  })
  const flush = async () => { for (let n = 0; n < 8; n++) await Promise.resolve() }
  const tick = async () => {
    clock += 1500
    const [id, fn] = timers.entries().next().value
    timers.delete(id); await fn(); await flush()
  }
  const hook = output.exports.usePolling(async signal => {
    calls++; signals.push(signal)
    if (fail) throw new Error('offline')
    return { messages: [], source: 'poll' }
  }, 1500, 'stream?session=%251&preview=0')
  cleanup = effect(); await flush()
  assert.equal(calls, 1)
  assert.equal(streams[0].url, '/mobile/api/stream?session=%251&preview=0')
  streams[0].onmessage({ data: '{"messages":[],"source":"stream"}' })
  assert.equal(states[0].source, 'stream')
  assert.equal(signals[0].aborted, true)
  await tick(); assert.equal(calls, 1, 'healthy SSE stops polls')
  streams[0].listeners.get('idle')(); await tick(); assert.equal(calls, 1)
  streams[0].onerror(); fail = true
  await tick(); assert.equal(states[1], 'offline', 'failed fallback drives reconnect banner')
  fail = false
  await tick(); assert.equal(states[1], ''); assert.equal(states[0].source, 'poll')
  streams[0].onmessage({ data: '{"messages":[],"source":"reconnected"}' })
  const before = calls; await tick(); assert.equal(calls, before)
  streams[0].listeners.get('unavailable')(); streams[0].listeners.get('idle')()
  await tick(); assert.equal(calls, before + 1, 'idle must not hide an unavailable stream')
  document.hidden = true; events.get('visibilitychange')(); await flush()
  assert.equal(streams[0].closed, true); assert.equal(timers.size, 0)
  document.hidden = false; events.get('visibilitychange')(); await flush()
  assert.equal(streams.length, 2)
  streams[1].onmessage({ data: '{"messages":[]}' })
  clock += 26000; await tick()
  assert.equal(streams[1].closed, true, 'stalled streams reconnect and poll')
  assert.equal(streams.length, 3)
  await hook.refresh(); assert.equal(states[1], '')
  cleanup(); assert.equal(streams[2].closed, true); assert.equal(timers.size, 0); assert.equal(events.size, 0)
  // B63: fetch first, then open the stream with the version on screen; `same` keeps the stream live without a poll.
  let release
  output.exports.usePolling(async signal => { calls++; signals.push(signal); await new Promise(resolve => { release = resolve }); return { version: 'v1' } }, 1500,
    shown => `stream?session=%252${shown?.version ? `&have=${shown.version}` : ''}`, { version: 'v0' })
  let opened = streams.length, before63 = calls
  cleanup = effect(); await flush()
  assert.equal(calls, before63 + 1); assert.equal(streams.length, opened, 'no stream while the first fetch is on the wire')
  release(); await flush()
  assert.equal(streams.length, opened + 1); assert.equal(streams[opened].url, '/mobile/api/stream?session=%252&have=v1')
  streams[opened].listeners.get('same')({ data: '{"version":"v1"}' })
  await tick(); assert.equal(calls, before63 + 1, 'same proves the stream is live: no poll')
  streams[opened].onmessage({ data: '{"version":"v2"}' }); assert.equal(states.at(-3).version, 'v2')
  // EventSource reconnects with its first URL (have=v1) while the screen shows v2: that `same` must not count.
  streams[opened].listeners.get('same')({ data: '{"version":"v1"}' })
  assert.equal(streams[opened].closed, true, 'a stale same closes the stream')
  const resync = tick(); await flush()
  assert.equal(calls, before63 + 2, 'and the next tick fetches again'); release(); await resync
  assert.equal(streams.length, opened + 2); assert.equal(streams[opened + 1].url, '/mobile/api/stream?session=%252&have=v1')
  // A fallback poll still on the wire when a valid `same` arrives is aborted, so its failure cannot show an error.
  streams[opened + 1].onerror(); const pending = tick(); await flush()
  assert.equal(calls, before63 + 3); const fallback = signals.at(-1)
  streams[opened + 1].listeners.get('same')({ data: '{"version":"v1"}' })
  assert.equal(fallback.aborted, true, 'valid same aborts the older poll')
  assert.equal(states.at(-2), '', 'valid same clears the error')
  release(); await pending
  cleanup(); assert.equal(streams[opened + 1].closed, true)
  output.exports.usePolling(async () => { calls++; throw new Error('offline') }, 1500, shown => `stream?have=${shown?.version}`, { version: 'v0' })
  opened = streams.length
  cleanup = effect(); await flush()
  assert.equal(streams[opened].url, '/mobile/api/stream?have=v0', 'a failed fetch still opens the stream with the data on screen')
  cleanup()
  console.log('SSE hook B63: stream opens after the fetch with the shown version; same keeps it live')
  delete windowMock.EventSource
  output.exports.usePolling(async () => { calls++; return {} }, 1500, 'stream?session=unsupported')
  const streamCount = streams.length, callCount = calls
  cleanup = effect(); await flush(); await tick()
  assert.equal(streams.length, streamCount, 'unsupported browsers use polling')
  assert.equal(calls, callCount + 2)
  cleanup(); assert.equal(timers.size, 0)
  console.log('SSE hook: push, fallback, reconnect, idle, visibility, stall and cleanup passed')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
