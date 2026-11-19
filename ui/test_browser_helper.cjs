const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const vm = require('node:vm')
const { sharedEndpoint, serveLocal } = require('./browser_test_helper.cjs')

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tether-browser-helper-'))
try {
  const env = { MOBILE_TMUX_HOME: root }
  assert.equal(sharedEndpoint(env), null)
  assert.deepEqual(sharedEndpoint({ ...env, TETHER_CDP_URL: 'ws://fixture' }), { url: 'ws://fixture', source: 'TETHER_CDP_URL' })
  const configDir = path.join(root, '.config/mobile-tmux')
  fs.mkdirSync(configDir, { recursive: true })
  const configPath = path.join(configDir, 'agent-browser.json')
  const config = { enabled: true, port: 29123, token: 'a'.repeat(43) }
  fs.writeFileSync(configPath, JSON.stringify(config))
  assert.equal(sharedEndpoint(env).url, `ws://127.0.0.1:29123/cdp/${config.token}`)
  assert.equal(sharedEndpoint({ MOBILE_TMUX_CONFIG: configDir }).source, 'Tether browser config')
  fs.writeFileSync(configPath, '{')
  assert.throws(() => sharedEndpoint(env), /Cannot read Tether browser config/)
  fs.writeFileSync(configPath, 'null')
  assert.throws(() => sharedEndpoint(env), /Invalid Tether browser config/)
  for (const invalid of [{ enabled: false }, { port: 0 }, { port: '29123' }, { token: '../private' }]) {
    fs.writeFileSync(configPath, JSON.stringify({ ...config, ...invalid }))
    assert.throws(() => sharedEndpoint(env), /disabled|Invalid Tether browser config/)
  }
  console.log('Browser helper: config discovery, overrides, missing config, and invalid config pass')
} finally { fs.rmSync(root, { recursive: true, force: true }) }

async function testConnections() {
  const logs = [], calls = []
  const fakeBrowser = { newContext: async () => calls.push('context'), close: async () => calls.push('close') }
  const chromium = {
    connectOverCDP: async (url, options) => { calls.push({ url, options }); return fakeBrowser },
    launch: async options => { calls.push(options); if (!options.channel) throw new Error('missing'); return fakeBrowser },
  }
  const env = { TETHER_CDP_URL: 'ws://secret-endpoint' }
  const sandbox = { module: { exports: {} }, process: { env, platform: process.platform }, console: { log: text => logs.push(text) },
    require: name => name === 'playwright-core' ? { chromium }
      : name === 'node:child_process' ? { execFileSync: () => { throw new Error('not installed') } }
      : name === 'node:fs' ? { ...fs, readFileSync: () => { throw Object.assign(new Error('absent'), { code: 'ENOENT' }) } }
      : require(name) }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'browser_test_helper.cjs'), 'utf8'), sandbox)
  const { getBrowser } = sandbox.module.exports
  assert.equal(await getBrowser(), fakeBrowser)
  assert.equal(calls[0].url, env.TETHER_CDP_URL)
  assert.equal(calls.length, 1, 'Shared discovery must not launch another browser')
  assert(logs[0].includes('TETHER_CDP_URL'))
  chromium.connectOverCDP = async () => { throw new Error(env.TETHER_CDP_URL) }
  await assert.rejects(getBrowser, error => /Cannot connect/.test(error.message) && !error.message.includes('secret-endpoint'))
  delete env.TETHER_CDP_URL
  calls.length = 0
  assert.equal(await getBrowser(), fakeBrowser)
  assert.equal(calls[0].headless, true)
  assert.equal(calls[1].channel, 'chrome')
  assert.equal(calls[2], 'context')
  assert(logs.at(-1).includes('local headless system Chrome'))
  chromium.launch = async () => { throw new Error('missing') }
  await assert.rejects(getBrowser, /npx playwright-core install --with-deps chromium/)
  console.log('Browser helper: shared connection, no secret leak, local fallback, and explicit failure pass (mocked browsers)')
}
async function testServingDuringTeardown() {
  const disposed = new Error('route.fetch: Request context disposed.\nCall log:\n  - GET fixture asset')
  let aborts = 0
  const route = {
    fetch: async () => { throw disposed },
    abort: async () => { aborts++ },
  }
  await serveLocal(route)
  assert.equal(aborts, 1, 'Cancel the intercepted request when its request context is disposed')

  const networkError = new Error('route.fetch: read ECONNRESET')
  await assert.rejects(serveLocal({ ...route, fetch: async () => { throw networkError } }), error => error === networkError)
  assert.equal(aborts, 1, 'A live fetch failure must still fail the test')

  // Teardown closes the page, then resets the local server: a fetch still in flight (often a prefetch) fails late.
  const closedPage = { isClosed: () => true }, livePage = { isClosed: () => false }
  const late = { fetch: async () => { throw networkError }, abort: async () => { aborts++; throw new Error('route.abort: Target closed') },
    request: () => ({ frame: () => ({ page: () => closedPage }) }) }
  await serveLocal(late)
  assert.equal(aborts, 2, 'A fetch error after the page closed is teardown, not a test failure')
  const live = { ...late, request: () => ({ frame: () => ({ page: () => livePage }) }) }
  await assert.rejects(serveLocal(live), error => error === networkError)
  const worker = { ...late, request: () => ({ frame: () => { throw new Error('Service Worker requests do not have an associated frame.') } }) }
  await assert.rejects(serveLocal(worker), error => error === networkError)
  assert.equal(aborts, 2, 'An open page or a request with no page still fails the test')
}

Promise.all([testConnections(), testServingDuringTeardown()]).catch(error => { console.error(error); process.exitCode = 1 })
