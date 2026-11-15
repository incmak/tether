// UI usage log (src/uiUsage.ts) on a fixture page, no app: a popover that wraps a menu counts once, a submenu that
// opens again has the same clicks to reach, controls out of view are not seen until scrolled to, a stopped log sends
// nothing more, the clear event drops the queue, and the build writes ui-words.txt for server/ui_usage.py.
// Run: cd ui && node test_ui_usage_log.cjs (ui-words.txt check needs an existing build).
const fs = require('node:fs')
const path = require('node:path')
const esbuild = require('esbuild')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const FIXTURE = `
  <main style="font: 16px sans-serif">
    <nav aria-label="Toolbar"><button id="more">More</button><button id="near">Near</button></nav>
    <div id="pop" popover="manual"><div role="menu" aria-label="Add to message">
      <button role="menuitem" id="file">File</button><button role="menuitem" id="sub-open">Shell</button></div></div>
    <div id="sub" popover="manual"><div role="menu" aria-label="Shell menu"><button role="menuitem" id="bash">Bash</button></div></div>
    <ul aria-label="Archived"><li data-ui-item><span>Fix login</span> <button aria-label="Restore Fix login" id="restore">↺</button>
      <button id="open-row">Fix login</button></li>
      <li data-ui-item><span>A</span> <button aria-label="Restore A" id="restore-a">↺</button> <button aria-label="Add A to a stack" id="add-a">+</button></li></ul>
    <div style="height: 3000px"></div>
    <button id="far">Far</button>
  </main>`

async function main() {
  let browser, page, failed = 0
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    const words = path.join(DIST, 'ui-words.txt')
    const list = fs.existsSync(words) ? new Set(fs.readFileSync(words, 'utf8').split('\n')) : new Set()
    check('build writes ui-words.txt', ['restore', 'settings', 'pane', 'title'].every(word => list.has(word)) && !list.has('zqxjkv'), words)
    const code = esbuild.buildSync({ entryPoints: [path.join(__dirname, 'src/uiUsage.ts')], bundle: true, write: false, format: 'iife', globalName: 'usage', logLevel: 'silent', resolveExtensions: ['.ts', '.tsx', '.js'] }).outputFiles[0].text
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    await page.setViewportSize({ width: 800, height: 600 })
    await page.route('**/*', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><body>${FIXTURE}</body>` }))
    await page.goto('http://fixture.invalid/')
    await page.addScriptTag({ content: code })
    await page.evaluate(() => {
      window.sent = []
      document.getElementById('more').addEventListener('click', () => document.getElementById('pop').showPopover())
      document.getElementById('sub-open').addEventListener('click', () => document.getElementById('sub').showPopover())
      document.getElementById('bash').addEventListener('click', () => document.getElementById('sub').hidePopover())
      window.gen = ''
      window.asks = 0
      window.stop = window.usage.startUsageLog(events => { if (events.length) window.sent.push(...events); else window.asks++ }, () => true, () => window.gen)
    })
    const flush = () => page.evaluate(() => window.dispatchEvent(new Event('pagehide')))
    const events = () => page.evaluate(() => window.sent)
    await page.click('#near')
    await page.waitForTimeout(2600)
    await flush()
    check('nothing is logged before the generation is known', (await events()).length === 0, JSON.stringify(await events()))
    // The first answers never came (offline): the log keeps asking, with backoff.
    const asks = await page.evaluate(() => window.asks)
    check('the log asks again for the generation', asks >= 2 && asks <= 3, String(asks))
    await page.evaluate(() => { window.gen = '1' })
    await page.waitForTimeout(1600)
    await flush()
    let seen = (await events()).filter(event => event.k === 'seen').map(event => event.l)
    check('in-view controls are seen', seen.includes('More') && seen.includes('Near'), JSON.stringify(seen))
    check('a control below the fold is not seen', !seen.includes('Far'), JSON.stringify(seen))
    check('a closed popover is not seen', !seen.includes('File'), JSON.stringify(seen))
    await page.click('#more')
    await page.waitForTimeout(800)
    await page.click('#sub-open')
    await page.waitForTimeout(800)
    await page.click('#bash')
    await page.waitForTimeout(800)
    await page.click('#sub-open')
    await page.waitForTimeout(800)
    await page.click('#bash')
    await page.waitForTimeout(800)
    await flush()
    const all = await events()
    const opens = all.filter(event => event.k === 'open')
    check('a popover that wraps a menu opens once', opens.filter(event => event.l === 'Add to message').length === 1 && !opens.some(event => event.l === 'Dialog'), JSON.stringify(opens))
    check('the menu is depth 1, the submenu depth 2', opens.find(event => event.l === 'Add to message')?.d === 1 && opens.filter(event => event.l === 'Shell menu').every(event => event.d === 2), JSON.stringify(opens))
    const bash = all.filter(event => event.k === 'click' && event.l === 'Bash').map(event => event.n)
    check('the submenu takes the same clicks each time', bash.length === 2 && bash[0] === 3 && bash[1] === 3, JSON.stringify(bash))
    check('events carry their time and generation', all.every(event => typeof event.t === 'number' && event.g === '1'))
    await page.click('#restore')
    await page.click('#open-row')
    await page.click('#restore-a')
    await page.click('#add-a')
    await flush()
    const rows = (await events()).filter(event => event.k === 'click').slice(-4).map(event => event.l)
    check('row text in a label becomes …', JSON.stringify(rows) === JSON.stringify(['Restore …', '(item)', 'Restore …', 'Add … to a stack']) && !JSON.stringify(await events()).includes('Fix login'), JSON.stringify(rows))
    await page.evaluate(() => { document.getElementById('pop').hidePopover(); window.scrollTo(0, 4000) })
    await page.waitForTimeout(2200)
    await flush()
    seen = (await events()).filter(event => event.k === 'seen').map(event => event.l)
    check('a control is seen after a scroll brings it into view', seen.includes('Far'), JSON.stringify(seen))
    // Clear: a click before the clear never reaches the server.
    await page.evaluate(() => { window.sent.length = 0 })
    await page.click('#far')
    await page.evaluate(() => window.dispatchEvent(new CustomEvent(window.usage.USAGE_CLEARED, { detail: '2' })))
    await flush()
    check('clear drops the queue', !(await events()).some(event => event.l === 'Far' && event.k === 'click'), JSON.stringify(await events()))
    // Stop: a scan that was due does not run.
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.click('#near')
    await page.evaluate(() => { window.stop(); window.sent.length = 0 })
    await page.waitForTimeout(2200)
    await flush()
    check('a stopped log sends nothing', (await events()).length === 0, JSON.stringify(await events()))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
  } finally {
    await page?.close().catch(() => {})
    await browser?.close?.().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exitCode = failed ? 1 : 0
}
main()
