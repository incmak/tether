const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function setup({ failConnect = false } = {}) {
  const exports = {}
  const timers = new Map()
  const sent = []
  const peers = []
  const order = []
  let client
  let reports = []
  let statsError = false
  class PeerConnection {
    constructor() { peers.push(this) }
    async getStats() {
      order.push('stats')
      if (statsError) throw Error('unavailable')
      return new Map(reports.map(report => [report.id, report]))
    }
  }
  const window = { RTCPeerConnection: PeerConnection, addEventListener() {}, removeEventListener() {} }
  class PipecatClient {
    constructor(options) { this.callbacks = options.callbacks; client = this }
    async initDevices() { assert.equal(window.RTCPeerConnection, PeerConnection) }
    async connect() {
      assert.notEqual(window.RTCPeerConnection, PeerConnection)
      new window.RTCPeerConnection()
      if (failConnect) throw Error('connect failed')
    }
    async disconnect() { order.push('disconnect'); this.callbacks.onDisconnected() }
    sendClientMessage(t, d) { sent.push({ t, d: JSON.parse(JSON.stringify(d)) }) }
  }
  class DeviceVoice {
    async open() {}
    close() {}
  }
  runInNewContext(ts.transpileModule(readFileSync('src/fridayAgent.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, {
    exports, window, Headers, Audio: class {}, setTimeout, clearTimeout,
    require: name => {
      if (name === './deviceVoice') return { DeviceVoice }
      if (name === '@pipecat-ai/client-js') return { PipecatClient }
      if (name === '@pipecat-ai/small-webrtc-transport') return { SmallWebRTCTransport: class {} }
      throw Error(`Unexpected import: ${name}`)
    },
    setInterval: (fn, delay) => { assert.equal(delay, 5000); timers.set(1, fn); return 1 },
    clearInterval: id => timers.delete(id),
    console: { warn: () => assert.fail('Stats must not log errors') },
  })
  const agent = new exports.FridayAgent({ tools: {}, onClosed() {} })
  return {
    agent, timers, sent, peers, order, window, PeerConnection,
    reports: value => { reports = value },
    error: value => { statsError = value },
    disconnect: () => client.callbacks.onDisconnected(),
    tick: async () => { timers.get(1)(); await settle() },
  }
}

async function settle() { for (let i = 0; i < 20; i++) await Promise.resolve() }

async function main() {
  const h = setup()
  await h.agent.open()
  assert.equal(h.window.RTCPeerConnection, h.PeerConnection)
  assert.equal(h.timers.size, 1)
  assert.equal(h.peers.length, 1)
  h.reports([
    { id: 'video', type: 'inbound-rtp', kind: 'video', packetsLost: 900 },
    { id: 'out', type: 'outbound-rtp', kind: 'audio', packetsLost: 800 },
    { id: 'audio', type: 'inbound-rtp', kind: 'audio', packetsLost: 2, packetsReceived: 100,
      jitter: .012345, concealedSamples: 20, totalSamplesReceived: 1000, concealmentEvents: 3,
      jitterBufferDelay: 1, jitterBufferEmittedCount: 100 },
  ])
  await h.tick()
  assert.deepEqual(h.sent, [{ t: 'stats', d: {
    packetsLost: 2, packetsReceived: 100, jitter: .012, concealedSamples: 20,
    totalSamplesReceived: 1000, concealmentEvents: 3, jitterBufferAvgMs: 10,
  } }])
  h.reports([{ id: 'audio', type: 'inbound-rtp', kind: 'audio', packetsLost: 3, packetsReceived: 140,
    jitter: .023456, concealedSamples: 25, totalSamplesReceived: 1400, concealmentEvents: 4,
    jitterBufferDelay: 1.6, jitterBufferEmittedCount: 120 }])
  await h.tick()
  assert.deepEqual(h.sent[1].d, {
    packetsLost: 1, packetsReceived: 40, jitter: .023, concealedSamples: 5,
    totalSamplesReceived: 400, concealmentEvents: 1, jitterBufferAvgMs: 30,
  })
  h.error(true)
  await h.tick()
  assert.equal(h.sent.length, 2)
  h.error(false)
  h.agent.close()
  assert.equal(h.timers.size, 0)
  await settle()
  assert.equal(h.sent.length, 4, 'Send bye and final stats before disconnect')
  assert.deepEqual(h.sent[2], { t: 'bye', d: { reason: 'ended' } })
  assert.equal(h.sent[3].d.packetsReceived, 0)
  assert.equal(h.sent[3].d.jitterBufferAvgMs, 0)
  assert.deepEqual(h.order.slice(-2), ['stats', 'disconnect'])
  console.log('Friday stats: audio selection, five-second deltas, jitter, buffer average, silent errors, final send and timer cleanup pass')

  const remote = setup()
  await remote.agent.open()
  remote.disconnect()
  await settle()
  assert.equal(remote.timers.size, 0)
  assert.deepEqual(remote.order, ['stats'])
  remote.agent.close()
  await settle()
  assert.deepEqual(remote.order, ['stats', 'disconnect'], 'Final stats run once')

  const failed = setup({ failConnect: true })
  await assert.rejects(failed.agent.open(), /connect failed/)
  await settle()
  assert.equal(failed.window.RTCPeerConnection, failed.PeerConnection)
  assert.equal(failed.timers.size, 0)
  failed.agent.close()
  await settle()
  console.log('Friday stats: remote disconnect and failed connect restore the constructor and clear timers')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
