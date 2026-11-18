const { strict: assert } = require('node:assert')
const { test } = require('node:test')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const { shoot, useContext } = require('./tools/verify.cjs')

const PAGE = '<!doctype html><style>html,body{margin:0;overflow-x:hidden}</style>' +
  '<main class="app-shell"><h1 id="landmark">Fixture</h1></main>'
const COOKIE = 'fixture=verifier-secret'

test('verify measures recipe answers and capture, and owns popups', { timeout: 110000 }, async t => {
  let redirectHits = 0
  const fixture = http.createServer((req, res) => {
    if (req.url === '/redirect-decoy') {
      redirectHits++
      res.writeHead(200).end('Decoy')
      return
    }
    if (req.headers.cookie !== COOKIE || req.headers['accept-encoding'] !== 'identity') {
      res.writeHead(401).end('Missing fixture headers')
      return
    }
    if (req.url === '/mobile/api/fixture/echo') {
      let body = ''
      req.on('data', data => { body += data })
      req.on('end', () => res.writeHead(200, { 'Content-Type': 'application/json' })
        .end(JSON.stringify({ method: req.method, body, header: req.headers['x-test'] })))
      return
    }
    if (req.url === '/mobile/api/start') {
      res.writeHead(403, { 'Content-Type': 'application/json', 'X-Tether-Verify-Blocked': '1' }).end('{}')
      return
    }
    if (req.url.startsWith('/chat/redirect/')) {
      res.writeHead(Number(req.url.split('/').at(-1)), { Location: '/redirect-decoy' }).end()
      return
    }
    if (req.url === '/chat/popup-error') {
      res.writeHead(200, { 'Content-Type': 'text/html' }).end('<script>throw new Error("Fixture popup error")</script>')
      return
    }
    res.writeHead(200, { 'Content-Type': 'text/html', 'Set-Cookie': COOKIE }).end(PAGE)
  })
  let port
  try {
    port = await new Promise((resolve, reject) => {
      fixture.once('error', reject)
      fixture.listen(0, '127.0.0.1', () => resolve(fixture.address().port))
    })
  } catch (error) {
    if (['EACCES', 'EPERM'].includes(error.code)) return t.skip('not run (sandbox): ' + error.message)
    throw error
  }
  const info = { url: 'http://127.0.0.1:' + port + '/chat/', cookie: { name: 'fixture', value: 'verifier-secret' }, pane: '%0' }
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-browser-'))
  let browser, ownContext, run
  try {
    const local = !process.env.TETHER_CDP_URL
    browser = await getBrowser({ local })
    if (local) ownContext = await browser.newContext({ serviceWorkers: 'block' })
    const context = ownContext || browser.contexts()[0]
    run = await useContext(context, info)
    const capture = (name, surface) => shoot(run, info, name, { landmark: '#landmark', open: async () => {}, ...surface }, 320, folder)
    const endpoint = 'fixture/error'
    const fail = async page => {
      await page.route('**/mobile/api/' + endpoint, route => route.fulfill({ status: 500 }))
      await page.evaluate(route => fetch('/mobile/api/' + route).then(response => response.text()), endpoint)
    }
    const unexpected = await capture('unplanned-error', { open: fail })
    assert.equal(unexpected.ok, false)
    assert.match(unexpected.problems.join('\n'), /GET \/mobile\/api\/fixture\/error answered 500/)
    const expected = await capture('planned-error', { open: fail, expect: { [endpoint]: 500 } })
    assert.equal(expected.ok, true, expected.problems.join('\n'))

    const redirected = await capture('redirects', {
      open: async page => {
        for (const status of [301, 302, 303, 307, 308]) {
          const answer = await page.evaluate(async url => {
            const response = await fetch(url)
            return [response.status, await response.text()]
          }, '/chat/redirect/' + status)
          assert.deepEqual(answer, [502, 'The fixture relay refused a redirect.'])
        }
      },
    })
    assert.equal(redirected.ok, false)
    for (const status of [301, 302, 303, 307, 308]) {
      assert.ok(redirected.problems.includes(`GET /chat/redirect/${status} redirects to /redirect-decoy`))
    }
    assert.equal(redirectHits, 0)

    let release
    const wait = new Promise(resolve => { release = resolve })
    try {
      const pending = await capture('pending', {
        open: async page => {
          let started
          const routed = new Promise(resolve => { started = resolve })
          await page.route('**/mobile/api/fixture/pending', async route => {
            started()
            await wait
            await route.fulfill({ status: 200 }).catch(() => {})
          })
          await page.evaluate(() => { void fetch('/mobile/api/fixture/pending').catch(() => {}) })
          await routed
        },
      })
      assert.equal(pending.ok, false)
      assert.match(pending.problems.join('\n'), /API still in flight after 5 s: .*\/fixture\/pending/)
      assert.doesNotMatch(pending.problems.join('\n'), /did not finish/)
    } finally { release() }

    const duringCapture = async (name, status) => {
      let release, timer
      const wait = new Promise(resolve => { release = resolve })
      try {
        return await capture(name, {
          open: async page => {
            let started
            const routed = new Promise(resolve => { started = resolve })
            const url = '/mobile/api/fixture/' + name
            await page.route('**' + url, async route => {
              started()
              await wait
              await route.fulfill({ status }).catch(() => {})
            })
            const screenshot = page.screenshot.bind(page)
            page.screenshot = async options => {
              await page.evaluate(url => { void fetch(url).catch(() => {}) }, url)
              await routed
              const image = await screenshot(options)
              if (status) timer = setTimeout(release, 100)
              return image
            }
          },
        })
      } finally {
        clearTimeout(timer)
        release()
      }
    }
    const lateError = await duringCapture('late-error', 500)
    assert.equal(lateError.ok, false)
    assert.ok(lateError.problems.includes('GET /mobile/api/fixture/late-error answered 500'))
    const latePending = await duringCapture('late-pending')
    assert.equal(latePending.ok, false)
    assert.ok(latePending.problems.includes('GET /mobile/api/fixture/late-pending did not finish before capture ended'))

    const clipped = await capture('clipped', {
      open: page => page.locator('.app-shell').evaluate(el => {
        el.insertAdjacentHTML('beforeend', '<div class="bad-panel" style="width:400px;overflow-x:auto"><div style="width:500px">Cut</div></div>')
      }),
    })
    assert.equal(clipped.ok, false)
    assert.match(clipped.problems.join('\n'), /cut off by the screen edge: <div.bad-panel>/)
    const scroller = await capture('strip', {
      open: page => page.locator('.app-shell').evaluate(el => {
        el.insertAdjacentHTML('beforeend', '<div class="strip" style="width:100vw;overflow-x:auto"><div style="width:500px">Tabs</div></div>')
      }),
    })
    assert.equal(scroller.ok, true, scroller.problems.join('\n'))
    assert.match(scroller.notes.join('\n'), /<div.strip> scrolls sideways/)

    const hidden = await capture('hidden', {
      ready: page => page.locator('#landmark').evaluate(el => el.remove()),
    })
    assert.equal(hidden.ok, false)
    assert.match(hidden.problems.join('\n'), /landmark is outside the viewport or hidden at capture/)

    const covered = await capture('covered', {
      open: page => page.locator('.app-shell').evaluate(el => {
        el.insertAdjacentHTML('beforeend', '<dialog class="cover">Settings</dialog>')
        el.querySelector('dialog').showModal()
      }),
    })
    assert.equal(covered.ok, false)
    assert.ok(covered.problems.includes('landmark is covered by <dialog.cover>'))

    const partlyCovered = await capture('partly-covered', {
      open: page => page.locator('.app-shell').evaluate(el => {
        const { x, y, width, height } = el.querySelector('#landmark').getBoundingClientRect()
        el.insertAdjacentHTML('beforeend', `<div class="cover" style="position:fixed;left:${x}px;top:${y}px;width:${width / 3}px;height:${height}px"></div>`)
      }),
    })
    assert.equal(partlyCovered.ok, false)
    assert.ok(partlyCovered.problems.includes('landmark is covered by <div.cover>'))

    const inDialog = await capture('own-dialog', {
      open: page => page.locator('.app-shell').evaluate(el => {
        el.insertAdjacentHTML('beforeend', '<dialog><h2 style="pointer-events:none">Settings</h2></dialog>')
        el.querySelector('dialog').showModal()
      }),
      landmark: 'dialog h2',
    })
    assert.equal(inDialog.ok, true, inDialog.problems.join('\n'))

    const blockedPopup = await capture('blocked-popup', {
      open: async page => {
        const opened = page.waitForEvent('popup', { timeout: 3000 })
        await page.evaluate(() => { window.open('/mobile/api/start', '_blank') })
        await opened
      },
    })
    assert.equal(blockedPopup.ok, false)
    assert.match(blockedPopup.problems.join('\n'), /GET \/mobile\/api\/start blocked by the test Tether \(403\)/)
    assert.ok(blockedPopup.requests.includes('GET /mobile/api/start'))

    const popupError = await capture('popup-error', {
      open: async page => {
        const opened = page.waitForEvent('popup', { timeout: 3000 })
        await page.evaluate(() => { window.open('/chat/popup-error', '_blank') })
        const popup = await opened
        await popup.waitForLoadState('load', { timeout: 3000 })
      },
    })
    assert.equal(popupError.ok, false)
    assert.ok(popupError.problems.includes('page error: Fixture popup error'))

    let popup
    const popped = await capture('popup', {
      open: async page => {
        assert.equal(await page.evaluate(() => document.cookie), '')
        const echo = await page.evaluate(() => fetch('/mobile/api/fixture/echo',
          { method: 'POST', body: 'sample', headers: { 'X-Test': 'forwarded' } }).then(response => response.json()))
        assert.deepEqual(echo, { method: 'POST', body: 'sample', header: 'forwarded' })
        const opened = context.waitForEvent('page', { timeout: 3000 })
        await page.evaluate(() => { window.open('/chat/popup', '_blank') })
        popup = await opened
        await popup.locator('#landmark').waitFor({ timeout: 3000 })
      },
    })
    assert.equal(popped.ok, true, popped.problems.join('\n'))
    assert.equal(popup.isClosed(), true)
  } finally {
    try {
      try { if (run) await run.stop() }
      finally { if (ownContext) await ownContext.close() }
    } finally {
      try { if (browser) await browser.close() }
      finally {
        await new Promise(resolve => fixture.close(resolve))
        fs.rmSync(folder, { recursive: true, force: true })
      }
    }
  }
})
