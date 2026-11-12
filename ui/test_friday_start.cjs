const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const never = new Promise(() => {})
const microphoneMessage = 'The microphone did not open. Close other apps that use it, then try again.'

function setup({ init = Promise.resolve(), connect = Promise.resolve(), importWork, transportImportWork, disconnect = Promise.resolve() } = {}) {
  const exports = {}
  const timeouts = new Map()
  const intervals = new Map()
  const stopped = []
  const sent = []
  const listeners = new Map()
  let now = 0
  let nextId = 0
  let disconnects = 0
  class PeerConnection {}
  const window = {
    RTCPeerConnection: PeerConnection,
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: (name, listener) => { if (listeners.get(name) === listener) listeners.delete(name) },
  }
  class PipecatClient {
    async initDevices() { return init }
    async connect() { assert.notEqual(window.RTCPeerConnection, PeerConnection); return connect }
    disconnect() { disconnects++; return disconnect }
    tracks() {
      return { local: Object.fromEntries(['audio', 'video', 'screenAudio', 'screenVideo'].map(kind => [kind, {
        stop: () => stopped.push(kind),
      }])) }
    }
    sendClientMessage(t, d) { sent.push({ t, d: JSON.parse(JSON.stringify(d)) }) }
  }
  class DeviceVoice {
    async open() {}
    close() {}
    stop() {}
  }
  runInNewContext(ts.transpileModule(readFileSync('src/fridayAgent.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, {
    exports, window, Headers, TypeError, Audio: class {},
    require: name => {
      if (name === './deviceVoice') return { DeviceVoice }
      if (name === '@pipecat-ai/client-js') return importWork ?? { PipecatClient }
      if (name === '@pipecat-ai/small-webrtc-transport') return transportImportWork ?? { SmallWebRTCTransport: class {} }
      throw Error(`Unexpected import: ${name}`)
    },
    setTimeout: (fn, delay) => { const id = ++nextId; timeouts.set(id, { fn, at: now + delay }); return id },
    clearTimeout: id => timeouts.delete(id),
    setInterval: fn => { const id = ++nextId; intervals.set(id, fn); return id },
    clearInterval: id => intervals.delete(id),
    console,
  })
  const agent = new exports.FridayAgent({ tools: {}, onClosed() {} })
  return {
    agent, window, PeerConnection, timeouts, intervals, stopped, sent, listeners,
    disconnects: () => disconnects,
    advance: async ms => {
      await settle()
      const end = now + ms
      while (true) {
        const next = [...timeouts].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
        if (!next) break
        now = next[1].at
        timeouts.delete(next[0])
        next[1].fn()
        await settle()
      }
      now = end
      await settle()
    },
  }
}

async function settle() { for (let i = 0; i < 40; i++) await Promise.resolve() }

function observe(agent) {
  const state = { settled: false }
  agent.open().then(
    () => { state.settled = true },
    error => { state.settled = true; state.error = error },
  )
  return state
}

async function main() {
  const mic = setup({ init: never, disconnect: never })
  const micOpen = observe(mic.agent)
  await mic.advance(9999)
  assert.equal(micOpen.settled, false)
  await mic.advance(1)
  assert.equal(micOpen.settled, true, 'Microphone startup must stop after ten seconds')
  assert.equal(micOpen.error.message, microphoneMessage)
  assert.equal(mic.disconnects(), 1)
  assert.deepEqual(mic.stopped, ['audio', 'video', 'screenAudio', 'screenVideo'])
  assert.equal(mic.timeouts.size, 0)

  const connection = setup({ connect: never, disconnect: Promise.reject(Error('disconnect failed')) })
  const connectionOpen = observe(connection.agent)
  await connection.advance(19999)
  assert.equal(connectionOpen.settled, false)
  await connection.advance(1)
  assert.equal(connectionOpen.settled, true)
  assert.equal(connectionOpen.error.message, 'Friday did not answer.')
  assert.equal(connection.disconnects(), 1)
  assert.equal(connection.window.RTCPeerConnection, connection.PeerConnection)
  assert.deepEqual(connection.stopped, ['audio', 'video', 'screenAudio', 'screenVideo'])
  assert.equal(connection.timeouts.size, 0)

  const normal = setup()
  await normal.agent.open()
  assert.equal(normal.timeouts.size, 0, 'Successful startup clears every timeout')
  assert.equal(normal.window.RTCPeerConnection, normal.PeerConnection)
  assert.equal(normal.disconnects(), 0)
  normal.agent.interrupt('tap', 'thinking')
  normal.agent.interrupt()
  assert.deepEqual(normal.sent, [
    { t: 'interrupt', d: { source: 'tap', phase: 'thinking' } },
    { t: 'interrupt', d: { source: 'tap' } },
  ])
  normal.agent.close('pagehide')
  normal.agent.close()
  assert.deepEqual(normal.sent.slice(2), [{ t: 'bye', d: { reason: 'pagehide' } }])
  assert.equal(normal.listeners.size, 0)
  await settle()
  assert.equal(normal.intervals.size, 0)

  const hidden = setup()
  await hidden.agent.open()
  hidden.listeners.get('pagehide')()
  hidden.agent.close()
  assert.deepEqual(hidden.sent, [{ t: 'bye', d: { reason: 'pagehide' } }])
  assert.equal(hidden.listeners.size, 0)
  await settle()
  assert.equal(hidden.disconnects(), 1)

  const unopened = setup()
  unopened.agent.close()
  assert.deepEqual(unopened.sent, [])
  await settle()

  const denied = new DOMException('Permission denied', 'NotAllowedError')
  const denial = setup({ init: Promise.reject(denied) })
  await assert.rejects(denial.agent.open(), error => error === denied)
  assert.equal(denial.timeouts.size, 0)

  const imports = setup({ importWork: never })
  const importsOpen = observe(imports.agent)
  await imports.advance(14999)
  assert.equal(importsOpen.settled, false)
  await imports.advance(1)
  assert.equal(importsOpen.settled, true)
  assert.equal(importsOpen.error.message, 'The voice files did not load.')
  assert.equal(imports.disconnects(), 0, 'No client exists before imports finish')
  assert.equal(imports.timeouts.size, 0)

  for (const key of ['importWork', 'transportImportWork']) {
    for (const message of [
      'Failed to fetch dynamically imported module: https://x/assets/a.js',
      'error loading dynamically imported module: https://x/assets/a.js',
      'Importing a module script failed.',
    ]) {
      const missing = setup({ [key]: Promise.reject(new TypeError(message)) })
      await assert.rejects(missing.agent.open(), { message: 'Tether was updated. Reload the page to use Friday.' })
      assert.equal(missing.timeouts.size, 0)
      assert.equal(missing.disconnects(), 0)
    }
    for (const failure of [new TypeError('Other import error'), new Error('Failed to fetch dynamically imported module')]) {
      const failed = setup({ [key]: Promise.reject(failure) })
      await assert.rejects(failed.agent.open(), error => error === failure)
      assert.equal(failed.timeouts.size, 0)
    }
  }
  console.log('Friday startup: timeouts, cleanup, mic denial identity and import reload errors pass')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
