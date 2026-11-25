// Runs the built UI and the unpacked extension in a fresh, headless Chrome for Testing.
// Build with npm run build. Set MOBILE_TMUX_CHROME to use an existing Chrome for Testing.
const assert = require('node:assert/strict')
const { execFileSync, spawn } = require('node:child_process')
const { on, once } = require('node:events')
const fs = require('node:fs/promises')
const https = require('node:https')
const os = require('node:os')
const path = require('node:path')
const { createInterface } = require('node:readline')
const { prefsStore } = require('./browser_test_helper.cjs')

const VERSION = '1.13.0'
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' }

// Puppeteer evaluations set userGesture. Record the cold-load result in the document itself,
// so the test cannot activate the app and make the command menu push an overlay entry.
const reportColdLoad = `<script>
  if (location.hash === '#search') {
    const observer = new MutationObserver(() => {
      if (!document.querySelector('dialog[open][aria-label="Search"] [role="combobox"]')) return;
      observer.disconnect();
      requestAnimationFrame(() => fetch('/fixture/search-loaded', { method: 'POST', body: JSON.stringify({
        url: location.href, focused: document.activeElement?.getAttribute('role') === 'combobox',
        activated: navigator.userActivation.isActive, historyIndex: navigation.currentEntry.index,
        entries: navigation.entries().map(entry => entry.url),
      }) }));
    });
    observer.observe(document, { subtree: true, childList: true, attributes: true });
  }
</script>`

async function serveFixture(work) {
  await fs.access(path.join(DIST, 'index.html'))
  const key = path.join(work, 'localhost.key')
  const cert = path.join(work, 'localhost.crt')
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-subj', '/CN=127.0.0.1', '-keyout', key, '-out', cert], { stdio: 'ignore' })
  const prefs = prefsStore()
  const server = https.createServer({ key: await fs.readFile(key), cert: await fs.readFile(cert) }, async (req, res) => {
    const { pathname } = new URL(req.url, 'https://127.0.0.1')
    if (pathname === '/before') {
      res.writeHead(200, { 'Content-Type': 'text/html' }).end('<!doctype html><title>Before Tether</title>')
      return
    }
    if (req.method === 'POST' && pathname === '/fixture/search-loaded') {
      const chunks = []
      for await (const chunk of req) chunks.push(chunk)
      server.emit('search-loaded', JSON.parse(Buffer.concat(chunks)))
      res.writeHead(204).end()
      return
    }
    if (pathname.startsWith('/mobile/api/')) {
      const endpoint = pathname.slice('/mobile/api/'.length)
      if (endpoint === 'stream') res.writeHead(204).end()
      else if (endpoint === 'prefs') {
        let stored = prefs.get()
        if (req.method === 'POST') {
          const chunks = []
          for await (const chunk of req) chunks.push(chunk)
          stored = prefs.post(JSON.parse(Buffer.concat(chunks)))
        }
        res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(stored))
      }
      else {
        const fixtures = { sessions: { sessions: [], default_thread: null },
          'projects/backlog': { text: '' }, 'work/board': { revision: 1, projects: [], items: [], stages: [] },
          reminders: { reminders: [], deleted: [], now: 0 } }
        res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(fixtures[endpoint] ?? {}))
      }
      return
    }
    const file = path.resolve(DIST, pathname === '/chat/' ? 'index.html' : pathname.slice('/chat/'.length))
    if (pathname.startsWith('/chat/') && file.startsWith(DIST + path.sep) && pathname !== '/chat/sw.js') {
      const body = await fs.readFile(file).catch(() => null)
      if (body) {
        res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' })
          .end(pathname === '/chat/' ? body.toString().replace('<head>', '<head>' + reportColdLoad) : body)
        return
      }
    }
    res.writeHead(404).end()
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  return server
}

function startMcp(work, chrome) {
  const child = spawn(process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['--yes', 'chrome-devtools-mcp@' + VERSION, '--headless', '--categoryExtensions', '--acceptInsecureCerts',
      '--no-usage-statistics', '--no-performance-crux', '--executablePath=' + chrome,
      '--userDataDir=' + path.join(work, 'profile'), '--chromeArg=--no-sandbox',
      '--chromeArg=--enable-unsafe-extension-debugging', '--chromeArg=--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'],
    { cwd: work, env: { ...process.env, npm_config_cache: process.env.TETHER_TEST_NPM_CACHE || path.join(work, 'npm'),
      CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS: '1' }, stdio: ['pipe', 'pipe', 'inherit'] })
  const closed = once(child, 'close')
  // Handle a launch error even if it occurs before the first response wait.
  closed.catch(() => {})
  child.stdin.on('error', () => {})
  const lines = createInterface({ input: child.stdout })
  const send = message => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n')
  let id = 0
  async function request(method, params, label = method) {
    const requestId = ++id
    const signal = AbortSignal.timeout(45000)
    const abort = new AbortController()
    const response = (async () => {
      for await (const [line] of on(lines, 'line', { signal: AbortSignal.any([signal, abort.signal]) })) {
        const message = JSON.parse(line)
        if (message.id !== requestId) continue
        const result = message.result
        if (message.error || result?.isError) throw new Error(label + ': ' + (message.error?.message || result.content[0].text))
        return result
      }
    })()
    send({ id: requestId, method, params })
    try {
      return await Promise.race([response, closed.then(([code]) => { throw new Error('MCP exited (' + code + ') during ' + label) })])
    } catch (error) {
      if (signal.aborted) throw new Error('MCP timed out during ' + label, { cause: error })
      throw error
    } finally {
      abort.abort()
    }
  }
  async function call(name, args = {}) {
    const result = await request('tools/call', { name, arguments: args }, name)
    return result.content[0].text
  }
  async function evaluate(target, fn, ...args) {
    const source = '() => (' + fn.toString() + ')(...' + JSON.stringify(args) + ')'
    const output = await call('evaluate_script', { ...target, function: source, waitForStableDom: false })
    const json = /^\x60\x60\x60json\n([\s\S]*?)\n\x60\x60\x60/m.exec(output)
    assert.ok(json, output)
    return JSON.parse(json[1])
  }
  async function stop() {
    child.stdin.end()
    const deadline = setTimeout(() => child.kill('SIGTERM'), 2000)
    try { await closed } finally { clearTimeout(deadline) }
  }
  return { call, evaluate, stop, async initialize() {
    const { serverInfo } = await request('initialize', { protocolVersion: '2025-06-18', capabilities: {},
      clientInfo: { name: 'tether-extension-test', version: '1' } })
    assert.equal(serverInfo.version, VERSION)
    send({ method: 'notifications/initialized' })
    return (await request('tools/list', {})).tools
  } }
}

function pageId(pages, url) {
  const line = pages.split('\n').find(line => /^\d+: /.test(line) && line.includes(url))
  assert.ok(line, 'Expected page ' + url + '\n' + pages)
  return Number(line.split(':', 1)[0])
}

async function searchState(evaluate, pageId) {
  return evaluate({ pageId }, async () => {
    const deadline = performance.now() + 8000
    while (!document.querySelector('dialog[open][aria-label="Search"] [role="combobox"]')) {
      if (performance.now() > deadline) {
        throw new Error('Tether search did not open at ' + location.href + '\n' + document.body.innerText.slice(0, 600))
      }
      await new Promise(requestAnimationFrame)
    }
    return location.href
  })
}

// This binding method is exposed by Chrome in the worker. It invokes the registered listener in place.
// It has no browser or OS shortcut input. Observe the real tabs event before the app consumes the URL.
async function dispatchSearch(url, existingId) {
  const updated = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated)
      reject(new Error('open-search did not navigate to ' + url))
    }, 8000)
    function onUpdated(tabId, change) {
      if (change.url !== url || (existingId !== null && tabId !== existingId)) return
      clearTimeout(timer)
      chrome.tabs.onUpdated.removeListener(onUpdated)
      resolve(tabId)
    }
    chrome.tabs.onUpdated.addListener(onUpdated)
    chrome.commands.onCommand.dispatch('open-search')
  })
  const tab = await chrome.tabs.get(updated)
  return { id: tab.id, active: tab.active, windowId: tab.windowId,
    tabs: (await chrome.tabs.query({})).map(({ id }) => id).sort((a, b) => a - b) }
}

async function check(mcp, extension, server) {
  const origin = 'https://127.0.0.1:' + server.address().port
  const { call, evaluate } = mcp
  const tools = await mcp.initialize()
  const action = tools.find(tool => tool.name === 'trigger_extension_action')
  assert.match(action.description, /default action/)
  assert.ok(!tools.some(tool => /command|shortcut/.test(tool.name)))
  const install = await call('install_extension', { path: extension })
  const id = install.match(/[a-p]{32}/)?.[0]
  assert.ok(id, install)
  const options = 'chrome-extension://' + id + '/options.html'
  const opts = pageId(await call('new_page', { url: options }), options)
  const startup = await evaluate({ pageId: opts }, async () => ({
    state: await chrome.runtime.sendMessage({ type: 'state' }),
    commands: (await chrome.commands.getAll()).map(command => command.name),
  }))
  assert.equal(startup.state?.connected, false, 'worker responds after initialization')
  assert.ok(startup.commands.includes('open-search'), 'Chrome registered the manifest command')
  const worker = (await call('list_pages')).split('\n').find(line => line.endsWith('/' + id + '/worker.js'))
  assert.ok(worker, 'MCP lists the extension service worker')
  const target = { serviceWorkerId: worker.split(':', 1)[0] }
  assert.deepEqual(await evaluate(target, () => ({
    registered: chrome.commands.onCommand.hasListeners(), dispatch: typeof chrome.commands.onCommand.dispatch,
  })), { registered: true, dispatch: 'function' })
  const errors = pageId => call('list_console_messages', { pageId, ...target, types: ['error'] })
  assert.match(await errors(opts), /<no console messages found>/)
  console.log('ok unpacked extension installed and worker responded without errors')
  await call('close_page', { pageId: opts })

  const before = origin + '/before'
  const chat = origin + '/chat/'
  const page = pageId(await call('new_page', { url: before }), before)
  const [[loaded]] = await Promise.all([
    once(server, 'search-loaded', { signal: AbortSignal.timeout(10000) }).catch(error => {
      throw new Error('Search did not open on a #search document load', { cause: error })
    }),
    evaluate(target, async (before, url) => {
      const [tab] = await chrome.tabs.query({ url: before })
      await chrome.tabs.update(tab.id, { url })
      return true
    }, before, chat + '#search'),
  ])
  assert.deepEqual(loaded, { url: chat, focused: true, activated: false, historyIndex: 1, entries: [before, chat] })
  await call('navigate_page', { pageId: page, type: 'back' })
  assert.equal(await evaluate({ pageId: page }, () => location.href), before)
  console.log('ok document load with #search opens focused search and one Back returns to the previous page')

  const existing = chat + '?fixture=existing'
  const existingPage = pageId(await call('new_page', { url: existing }), existing)
  await evaluate({ pageId: existingPage }, async () => {
    const deadline = performance.now() + 8000
    while (history.state?.tether !== 1) {
      if (performance.now() > deadline) throw new Error('Tether did not initialize history')
      await new Promise(requestAnimationFrame)
    }
    return true
  })
  const prepared = await evaluate(target, async (origin, url) => {
    await chrome.storage.local.set({ origin })
    const [tab] = await chrome.tabs.query({ url })
    await chrome.tabs.create({ windowId: tab.windowId, active: true })
    return { id: tab.id, windowId: tab.windowId, active: (await chrome.tabs.get(tab.id)).active,
      tabs: (await chrome.tabs.query({})).map(({ id }) => id).sort((a, b) => a - b) }
  }, origin, existing)
  assert.equal(prepared.active, false, 'the Tether tab starts in the background')
  const reused = await evaluate(target, dispatchSearch, existing + '#search', prepared.id)
  assert.deepEqual(reused, { id: prepared.id, active: true, windowId: prepared.windowId,
    tabs: prepared.tabs })
  assert.equal(await searchState(evaluate, existingPage), existing)
  console.log('ok open-search activates the existing background tab and sends #search')
  await call('close_page', { pageId: existingPage })

  // The original B191 implementation uses a query on a new tab. Test its exact URL and the resulting menu.
  const created = await evaluate(target, dispatchSearch, chat + '?go=search', null)
  assert.ok(!prepared.tabs.includes(created.id), 'a new tab opens when no chat tab exists')
  assert.equal(created.active, true)
  const newPage = pageId(await call('list_pages'), chat)
  assert.equal(await searchState(evaluate, newPage), chat)
  console.log('ok open-search creates an active /chat/?go=search tab and the real UI opens search')
  await call('close_page', { pageId: newPage })

  assert.equal(await evaluate(target, async () => {
    await chrome.storage.local.remove('origin')
    return (await chrome.storage.local.get('origin')).origin ?? null
  }), null)
  const unpaired = await evaluate(target, dispatchSearch, options, null)
  assert.equal(unpaired.active, true)
  const optionsPage = pageId(await call('list_pages'), options)
  assert.equal(await evaluate({ pageId: optionsPage }, () => document.title), 'Connect Chrome to Tether')
  assert.match(await errors(optionsPage), /<no console messages found>/)
  console.log('ok open-search opens the extension options page when there is no saved origin')
}

async function main() {
  const chrome = process.env.TETHER_EXTENSION_CHROME || process.env.MOBILE_TMUX_CHROME
  assert.ok(chrome, 'Set MOBILE_TMUX_CHROME to the Chrome for Testing executable')
  await fs.access(chrome)
  console.log(execFileSync(chrome, ['--version'], { encoding: 'utf8' }).trim(), '| Chrome DevTools MCP', VERSION)
  const work = await fs.mkdtemp(path.join(os.tmpdir(), 'tether-extension-search-'))
  let server, mcp
  try {
    const extension = path.join(work, 'extension')
    await fs.cp(path.resolve(__dirname, '../extension'), extension, { recursive: true })
    server = await serveFixture(work)
    mcp = startMcp(work, chrome)
    await check(mcp, extension, server)
  } finally {
    try { await mcp?.stop() } finally {
      server?.close()
      server?.closeAllConnections()
      await fs.rm(work, { recursive: true, force: true })
    }
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
