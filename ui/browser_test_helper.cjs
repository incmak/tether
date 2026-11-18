// Shared browser discovery for fixture-only UI tests.
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const { chromium } = require('playwright-core')

function sharedEndpoint(env = process.env) {
  if (env.TETHER_CDP_URL) return { url: env.TETHER_CDP_URL, source: 'TETHER_CDP_URL' }
  // server/paths.py CONFIG and server/browser_cdp.py Proxy.url are authoritative.
  const home = env.MOBILE_TMUX_HOME || os.homedir()
  const configPath = path.join(env.MOBILE_TMUX_CONFIG || path.join(home, '.config/mobile-tmux'), 'agent-browser.json')
  let config
  try { config = JSON.parse(fs.readFileSync(configPath, 'utf8')) } catch (error) {
    if (error.code === 'ENOENT') return null
    throw new Error('Cannot read Tether browser config. Repair agent-browser.json or set TETHER_CDP_URL.')
  }
  if (!config || typeof config.enabled !== 'boolean' || !Number.isInteger(config.port) || config.port < 1 || config.port > 65535 || !/^[A-Za-z0-9_-]{43}$/.test(config.token)) {
    throw new Error('Invalid Tether browser config. Repair agent-browser.json or set TETHER_CDP_URL.')
  }
  if (!config.enabled) throw new Error('Tether agent browser is disabled. Enable it in Tether before running UI tests.')
  return { url: `ws://127.0.0.1:${config.port}/cdp/${config.token}`, source: 'Tether browser config' }
}

// local: true is for tests that measure this host's real network (timing, bytes, compression). The shared browser
// runs in a container without access to this host's loopback, so those tests use local headless Chromium.
async function getBrowser({ local = false } = {}) {
  const shared = local ? null : sharedEndpoint()
  if (shared) {
    console.log(`Browser: shared Chromium via ${shared.source}`)
    try { return await chromium.connectOverCDP(shared.url, { timeout: 15000 }) } catch {
      // Do not log the endpoint: its path contains a bearer token.
      throw new Error('Cannot connect to the shared Tether browser. Start Tether with its agent browser enabled, or fix TETHER_CDP_URL.')
    }
  }
  const choices = [{ label: 'Playwright Chromium', options: {} }, { label: 'system Chrome', options: { channel: 'chrome' } }]
  if (process.env.MOBILE_TMUX_CHROME) choices.unshift({ label: 'MOBILE_TMUX_CHROME', options: { executablePath: process.env.MOBILE_TMUX_CHROME } })
  for (const name of ['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome']) {
    try {
      const executablePath = execFileSync(process.platform === 'win32' ? 'where' : 'which', [name], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 2000 }).trim().split(/\r?\n/)[0]
      choices.push({ label: name, options: { executablePath } })
    } catch { /* Try the next installed browser. */ }
  }
  for (const choice of choices) {
    let browser
    try {
      browser = await chromium.launch({ headless: true, timeout: 15000, ...choice.options })
      await browser.newContext({ serviceWorkers: 'block' })
      console.log(`Browser: local headless ${choice.label}`)
      return browser
    } catch {
      if (browser) await browser.close().catch(() => {})
    }
  }
  throw new Error('No usable Chromium. Run npm ci, then npx playwright-core install --with-deps chromium in ui/, or start Tether with its agent browser enabled.')
}

// The shared browser runs in a container and cannot reach this host's loopback (by design, B89).
// Fetch a request to the test's local server from this process instead, so tests work in every browser.
// Redirects cannot work: the browser does not route the request that follows a fulfilled 3xx, and a followed one
// would serve the final body at the first URL, so relative links break. Fail clearly instead.
async function serveLocal(route) {
  let response
  try {
    response = await route.fetch()
  } catch (error) {
    // Closing the browser context can cancel an in-flight asset fetch after the page closes.
    if (!/^route\.fetch: Request context disposed\.(?:\n|$)/.test(error?.message)) throw error
    return route.abort()
  }
  if (response.url() !== route.request().url()) throw new Error(`serveLocal: ${route.request().url()} redirects to ${response.url()}; serve the final URL directly`)
  return route.fulfill({ response })
}

// Screenshot folder. SHOTS wins exactly as given. Otherwise a scratch folder, never a dated .release-work folder.
function shotsDir(name, ...sub) {
  if (process.env.SHOTS) return path.resolve(process.env.SHOTS)
  return path.join(os.tmpdir(), 'tether-ui-shots', name, ...sub)
}

module.exports = { getBrowser, sharedEndpoint, serveLocal, shotsDir }
