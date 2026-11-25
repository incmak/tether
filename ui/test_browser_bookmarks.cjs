// Serve the built panel on a loopback fixture. Keep every browser API and stream local.
const assert = require('node:assert/strict')
const { mkdir, readFile } = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const dist = path.join(__dirname, 'dist')
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
const prefix = '/mobile/api/'
const device = { id: 'desktop', label: 'Desktop', width: 640, height: 400, touch: false }
const current = { id: 'fixture', preset: 'desktop', title: 'Fixture page', url: 'https://example.invalid/guide', mode: 'private', width: 640, height: 400, touch: false }
const saved = { id: 'saved', title: 'Saved page', url: 'http://saved.invalid/start', added: 1 }
const empty = 'No bookmarks yet. Open a page, then bookmark it.'
const focused = locator => locator.evaluate(node => node === document.activeElement)

async function fixture({ browser, origin }, options, test) {
  const { width = 1280, which = 'test', tabs = [current], bookmarks = [], windowView = false, holdLoads } = options
  const context = await browser.newContext({ viewport: { width, height: 800 }, hasTouch: width === 320, serviceWorkers: 'block' })
  context.setDefaultTimeout(10_000)
  const requests = []
  const errors = []
  let rows = bookmarks
  let state = tabs
  let fail = options.fail ?? {}
  try {
    await context.addInitScript(which => {
      localStorage.setItem('tether-browser-which', which)
      window.WebSocket = undefined
      window.EventSource = class extends EventTarget { close() {} }
    }, which)
    await context.route('**/*', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith(prefix)) return route.continue()
      const endpoint = url.pathname.slice(prefix.length)
      const method = request.method()
      const body = request.postDataJSON()
      requests.push({ endpoint, method, body, browser: url.searchParams.get('browser') })
      if (endpoint === 'browser/bookmarks' && holdLoads) await holdLoads
      if (fail[endpoint]) return route.fulfill({ status: 400, json: { error: fail[endpoint] } })
      const reply = json => route.fulfill({ json })
      switch (endpoint) {
        case 'browser/tabs': return reply({ tabs: state, presets: [device], window_view: windowView })
        case 'browser/bookmarks': return reply({ bookmarks: rows })
        case 'browser/bookmarks/add':
          rows = [{ id: 'added', url: body.url, title: body.title, added: 2 }, ...rows]
          return reply({ bookmarks: rows })
        case 'browser/bookmarks/remove':
          rows = rows.filter(row => row.id !== body.id)
          return reply({ bookmarks: rows })
        case 'browser/navigate':
          state = state.map(tab => tab.id === body.tab ? { ...tab, url: body.url } : tab)
          return reply(state.find(tab => tab.id === body.tab))
        case 'browser/new':
          state = [{ ...current, id: 'new', url: body.url, mode: body.mode }]
          return reply(state[0])
        case 'browser/device': return reply(state[0])
        case 'host': return reply({ name: 'Fixture host' })
        default: return reply({})
      }
    })
    const page = await context.newPage()
    page.on('pageerror', error => errors.push(error.message))
    const firstLoad = holdLoads ? page.waitForRequest('**/mobile/api/browser/bookmarks*') : null
    await Promise.all([firstLoad, page.goto(`${origin}/chat/?browser=window`)])
    const panel = page.locator('.browser-panel')
    await panel.waitFor()
    const button = panel.getByRole('button', { name: 'Bookmarks', exact: true })
    const dialog = panel.getByRole('dialog', { name: 'Bookmarks', exact: true })
    await test({ page, panel, button, dialog, requests, clearFailures: () => { fail = {} } })
    assert.deepEqual(errors, [])
  } finally {
    await context.close()
  }
}

async function main() {
  const index = await readFile(path.join(dist, 'index.html'))
  const server = http.createServer(async (req, res) => {
    const { pathname } = new URL(req.url, 'http://127.0.0.1')
    if (pathname === '/chat/') return res.writeHead(200, { 'Content-Type': mime['.html'] }).end(index)
    const asset = path.resolve(dist, '.' + pathname.slice('/chat'.length))
    if (pathname.startsWith('/chat/') && asset.startsWith(dist + path.sep)) {
      const content = await readFile(asset).catch(() => null)
      if (content) return res.writeHead(200, { 'Content-Type': mime[path.extname(asset)] ?? 'application/octet-stream' }).end(content)
    }
    res.writeHead(404).end()
  })
  let browser
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = 'http://127.0.0.1:' + server.address().port
    browser = await getBrowser({ local: true })
    const run = (options, test) => fixture({ browser, origin }, options, test)

    for (const width of [320, 1280]) {
      const { promise: holdLoads, resolve: releaseLoads } = Promise.withResolvers()
      await run({ width, holdLoads }, async ({ page, panel, button, dialog, requests }) => {
        await Promise.all([page.waitForRequest('**/mobile/api/browser/bookmarks'), button.press('Enter')])
        const add = dialog.getByRole('button', { name: 'Bookmark this page', exact: true })
        assert(await focused(add))
        await dialog.getByText('Loading bookmarks…', { exact: true }).waitFor()
        releaseLoads()
        await dialog.getByText(empty, { exact: true }).waitFor()
        assert.deepEqual(requests.filter(row => row.endpoint === 'browser/bookmarks').map(row => row.method), ['GET', 'GET'])
        await add.click()
        const removeCurrent = dialog.getByRole('button', { name: 'Remove this page', exact: true })
        await removeCurrent.waitFor()
        assert.deepEqual(requests.find(row => row.endpoint === 'browser/bookmarks/add').body, { url: current.url, title: current.title })
        assert.equal(await button.locator('svg').getAttribute('fill'), 'currentColor')
        const open = dialog.getByRole('button', { name: 'Fixture page example.invalid', exact: true })
        const remove = dialog.getByRole('button', { name: 'Remove Fixture page', exact: true })
        assert.equal(await open.getAttribute('title'), `${current.title}\n${current.url}`)
        if (width === 320) {
          const box = await dialog.boundingBox()
          assert(box.x >= 0 && box.x + box.width <= width)
          assert((await open.boundingBox()).height >= 44)
          const hitArea = await remove.boundingBox()
          assert(hitArea.width >= 44 && hitArea.height >= 44)
        }
        if (process.env.SHOTS) {
          await mkdir(process.env.SHOTS, { recursive: true })
          await page.screenshot({ path: path.join(process.env.SHOTS, `bookmarks-${width}.png`) })
        }
        await page.keyboard.press('Tab')
        assert(await focused(open))
        await page.keyboard.press('Tab')
        assert(await focused(remove))
        await page.keyboard.press('Escape')
        await dialog.waitFor({ state: 'hidden' })
        assert(await focused(button))
        // Tab past the last row: focus leaves, so the popover closes (Escape there could not reach it).
        await button.press('Enter')
        await remove.focus()
        await page.keyboard.press('Tab')
        await dialog.waitFor({ state: 'hidden' })
        assert(await focused(panel.getByRole('button', { name: /^Display:/ })))
        await button.press('Enter')
        const navigated = page.waitForRequest(request => request.url().endsWith('/browser/navigate'))
        await open.click()
        assert.deepEqual((await navigated).postDataJSON(), { tab: current.id, url: current.url })
        await dialog.waitFor({ state: 'hidden' })
        await button.click()
        await remove.click()
        await dialog.getByText(empty, { exact: true }).waitFor()
        assert.deepEqual(requests.find(row => row.endpoint === 'browser/bookmarks/remove').body, { id: 'added' })
        assert.equal(await button.locator('svg').getAttribute('fill'), 'none')
        assert(await focused(add))
        await panel.getByRole('textbox', { name: 'Address' }).click()
        await dialog.waitFor({ state: 'hidden' })
      })
      console.log(`Browser bookmarks: ${width}px, loading, empty, add, open, remove, keyboard and outside click pass`)
    }

    await run({ fail: { 'browser/bookmarks': 'Could not load bookmarks.' } }, async ({ page, button, dialog, clearFailures }) => {
      await button.click()
      await dialog.getByRole('alert').filter({ hasText: 'Could not load bookmarks.' }).waitFor()
      clearFailures()
      await dialog.getByRole('button', { name: 'Try again', exact: true }).press('Enter')
      await dialog.getByText(empty, { exact: true }).waitFor()
      await page.keyboard.press('Escape')
      await dialog.waitFor({ state: 'hidden' })
      assert(await focused(button))
    })
    console.log('Browser bookmarks: load error, keyboard Retry and Escape pass')

    {
      const { promise: holdLoads, resolve: releaseLoads } = Promise.withResolvers()
      await run({ holdLoads, bookmarks: [saved], fail: {
        'browser/bookmarks/add': 'Could not add bookmark.',
        'browser/bookmarks/remove': 'Could not remove bookmark.',
        'browser/navigate': 'Could not open bookmark.',
      } }, async ({ panel, button, dialog }) => {
        const status = panel.locator('.browser-status')
        await button.click()
        releaseLoads()
        const open = dialog.getByRole('button', { name: 'Saved page saved.invalid', exact: true })
        await open.waitFor()
        await dialog.getByRole('button', { name: 'Bookmark this page', exact: true }).click()
        await status.filter({ hasText: 'Could not add bookmark.' }).waitFor()
        await dialog.getByRole('button', { name: 'Remove Saved page', exact: true }).click()
        await status.filter({ hasText: 'Could not remove bookmark.' }).waitFor()
        await open.click()
        await status.filter({ hasText: 'Could not open bookmark.' }).waitFor()
      })
    }
    console.log('Browser bookmarks: add, remove and open errors reach the status line')

    {
      // The current page is bookmarked already (say, from the phone). Until the list arrives, the first action must not add.
      const { promise: holdLoads, resolve: releaseLoads } = Promise.withResolvers()
      await run({ holdLoads, bookmarks: [{ id: 'old', url: current.url, title: 'Old title', added: 1 }] }, async ({ page, button, dialog, requests }) => {
        await button.click()
        const add = dialog.getByRole('button', { name: 'Bookmark this page', exact: true })
        assert.equal(await add.getAttribute('aria-disabled'), 'true')
        await add.click({ force: true })
        assert(!requests.some(row => row.endpoint === 'browser/bookmarks/add'))
        releaseLoads()
        const removeCurrent = dialog.getByRole('button', { name: 'Remove this page', exact: true })
        await removeCurrent.waitFor()
        assert.equal(await removeCurrent.getAttribute('aria-disabled'), 'false')
        // A second open refreshes again: the action waits for that list too.
        await page.keyboard.press('Escape')
        const { promise: holdAgain, resolve: releaseAgain } = Promise.withResolvers()
        await page.route('**/mobile/api/browser/bookmarks', async route => { await holdAgain; await route.fallback() })
        await button.click()
        assert.equal(await removeCurrent.getAttribute('aria-disabled'), 'true')
        releaseAgain()
        await page.waitForFunction(() => document.querySelector('.browser-bookmarks button')?.getAttribute('aria-disabled') === 'false')
        assert(!requests.some(row => row.endpoint === 'browser/bookmarks/add'))
      })
    }
    console.log('Browser bookmarks: the first action waits for a fresh list')

    await run({}, async ({ panel }) => {
      // The same focus rule holds for the other panel menus.
      const device = panel.getByRole('button', { name: /^Display:/ })
      await device.click()
      const menu = panel.getByRole('menu')
      await menu.waitFor()
      await panel.getByRole('textbox', { name: 'Address' }).focus()
      await menu.waitFor({ state: 'hidden' })
    })
    console.log('Browser bookmarks: the Display menu closes when focus leaves')

    for (const which of ['test', 'signed']) {
      await run({ which, tabs: [], bookmarks: [saved] }, async ({ page, button, dialog, requests }) => {
        assert.equal(await button.locator('svg').getAttribute('fill'), 'none')
        await button.press('Enter')
        const add = dialog.getByRole('button', { name: 'Bookmark this page', exact: true })
        assert(await add.isDisabled() && await focused(add))
        await dialog.getByRole('button', { name: 'Saved page saved.invalid', exact: true }).click()
        await page.getByRole('tab', { name: /Fixture page/, selected: true }).waitFor()
        assert.deepEqual(requests.find(row => row.endpoint === 'browser/new').body,
          { mode: which === 'test' ? 'private' : 'saved', url: saved.url, ...(which === 'signed' ? { browser: which } : {}) })
        assert.equal(requests.find(row => row.endpoint === 'browser/bookmarks').browser, which === 'signed' ? 'signed' : null)
      })
    }
    console.log('Browser bookmarks: open from an empty Test or Signed-in browser passes')

    for (const [options, note] of [
      [{ tabs: [{ ...current, mode: 'agent', controller: 'agent' }] }, 'Take over the tab to open a bookmark.'],
      [{ which: 'signed', windowView: true }, 'Switch back to page view to open a bookmark.'],
    ]) {
      await run({ ...options, bookmarks: [saved] }, async ({ button, dialog, requests }) => {
        await button.click()
        await dialog.getByText(note, { exact: true }).waitFor()
        const open = dialog.getByRole('button', { name: 'Saved page saved.invalid', exact: true })
        await open.waitFor()
        assert(await open.isDisabled())
        assert(!requests.some(row => ['browser/navigate', 'browser/new'].includes(row.endpoint)))
      })
    }
    console.log('Browser bookmarks: watch-only and whole-window guards pass')
  } finally {
    await browser?.close()
    await new Promise(resolve => server.close(resolve))
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
