const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const esbuild = require('esbuild')
const { devices } = require('playwright-core')
const { getBrowser } = require('./browser_test_helper.cjs')

const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/ttyd-1.7.4/index.html'), 'utf8')
const origin = 'https://ttyd.test'
const textarea = '.xterm-helper-textarea'
const stock = process.argv.includes('--stock')

async function open(context) {
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin && url.pathname === '/terminal/live/') {
      return route.fulfill({ contentType: 'text/html', body: fixture })
    }
    if (url.origin === origin && url.pathname === '/terminal/live/token') {
      return route.fulfill({ json: { token: '' } })
    }
    return route.abort()
  })
  await page.addInitScript(() => {
    window.sentFrames = []
    window.WebSocket = class extends EventTarget {
      static OPEN = 1
      readyState = 0

      constructor() {
        super()
        queueMicrotask(() => {
          this.readyState = WebSocket.OPEN
          this.dispatchEvent(new Event('open'))
          const data = new TextEncoder().encode('2{}').buffer
          this.dispatchEvent(new MessageEvent('message', { data }))
        })
      }

      send(data) {
        window.sentFrames.push(Array.from(data))
      }
    }
  })
  await page.goto(origin + '/terminal/live/')
  await page.waitForFunction(selector => window.term && document.activeElement?.matches(selector), textarea)
  return page
}

async function androidIme(page) {
  const cdp = await page.context().newCDPSession(page)
  // Keep xterm's deferred 229 check after the trusted edit, as when Android delivers both together.
  await page.clock.install()
  await page.clock.pauseAt(new Date(Date.now() + 1000))
  const edit = async (method, params) => {
    const key = { key: 'Process', windowsVirtualKeyCode: 229, nativeVirtualKeyCode: 229 }
    await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...key })
    await cdp.send(method, params)
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', ...key })
    await page.clock.runFor(1)
  }
  return {
    compose: (text, replacementStart, replacementEnd) => edit('Input.imeSetComposition', {
      text, selectionStart: text.length, selectionEnd: text.length, replacementStart, replacementEnd,
    }),
    insert: text => edit('Input.insertText', { text }),
  }
}

async function readPane(page) {
  const frames = await page.evaluate(() => window.sentFrames)
  const data = frames.filter(frame => frame[0] === 48)
    .map(frame => Buffer.from(frame.slice(1)).toString('utf8')).join('')
  const submitted = []
  let line = []
  for (const char of data) {
    if (char === '\x7f') line.pop()
    else if (char === '\r') {
      submitted.push(line.join(''))
      line = []
    } else line.push(char)
  }
  return { line: line.join(''), submitted, data }
}

async function assertPane(page, line, submitted = []) {
  const pane = await readPane(page)
  assert.deepEqual({ line: pane.line, submitted: pane.submitted }, { line, submitted }, JSON.stringify(pane))
  return pane
}

const selectDraft = page => page.locator(textarea).evaluate(el => el.select())

async function paste(page, text) {
  await page.locator(textarea).evaluate((el, text) => {
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', text)
    el.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData }))
  }, text)
}

async function gboard(page) {
  const { compose, insert } = await androidIme(page)
  for (const text of ['w', 'wh', 'wha', 'what']) {
    await compose(text)
    if (!stock) await assertPane(page, text)
  }
  await insert('what')
  await insert(' ')
  // Re-open the earlier word, then let the IME replace the draft with a suggestion.
  await compose('what', 0, 4)
  await insert('what')
  await selectDraft(page)
  await insert("what's ")
  await selectDraft(page)
  await insert('whats on ')
  await compose('what ', 0, 9)
  await insert('what ')
  await compose('m')
  await insert('m')
  return assertPane(page, 'what m')
}

const cases = [
  ['Gboard re-composes an earlier word', gboard],
  ['suggestion replaces a word of the same length', async page => {
    const { compose, insert } = await androidIme(page)
    await insert('teh ')
    await compose('the ', 0, 4)
    await insert('the ')
    return assertPane(page, 'the ')
  }],
  ['backspace shrinks the composition', async page => {
    const { compose, insert } = await androidIme(page)
    await compose('book')
    await assertPane(page, 'book')
    await compose('boo')
    await insert('boo')
    return assertPane(page, 'boo')
  }],
  ['glide typing commits a word and a space', async page => {
    const { insert } = await androidIme(page)
    await insert('swipe')
    await insert(' ')
    return assertPane(page, 'swipe ')
  }],
  ['deleting a composed emoji sends one DEL', async page => {
    const { compose } = await androidIme(page)
    await compose('🙂')
    await compose('')
    const pane = await assertPane(page, '')
    assert.equal(pane.data, '🙂\x7f')
    return pane
  }],
  ['Enter submits once and the next word starts a new line', async page => {
    const { insert } = await androidIme(page)
    await insert('first')
    await page.keyboard.press('Enter')
    assert.equal(await page.locator(textarea).inputValue(), '')
    await insert('next')
    const pane = await assertPane(page, 'next', ['first'])
    assert.equal(pane.data, 'first\rnext')  // the pane model drops a DEL on an empty line; the raw bytes do not
    return pane
  }],
  ['Android hardware keyboard sends each character once', async page => {
    await page.keyboard.type('Hello')
    return assertPane(page, 'Hello')
  }],
  ['desktop typing and paste use xterm', async page => {
    assert.equal(await page.evaluate(() => window.__tetherIme), undefined)
    await page.keyboard.type('hello')
    await paste(page, ' paste')
    return assertPane(page, 'hello paste')
  }, 'desktop'],
  ['a missing send API warns and leaves xterm active', async page => {
    assert.equal(await page.evaluate(() => window.__tetherIme), undefined)
    await page.keyboard.type('hello')
    return assertPane(page, 'hello')
  }, 'missing'],
  ['paste and blur clear the Android textarea without replaying it', async page => {
    const { insert } = await androidIme(page)
    await insert('one')
    await paste(page, ' two')
    await insert(' three')
    await page.locator(textarea).blur()
    await page.locator(textarea).focus()
    await insert(' four')
    return assertPane(page, 'one two three four')
  }],
  ['the public send API takes precedence over the internal API', async page => {
    const { insert } = await androidIme(page)
    await insert('public')
    assert.equal(await page.evaluate(() => window.publicSends), 1)
    return assertPane(page, 'public')
  }, 'public'],
]

async function install(page, patch, mode) {
  const warnings = []
  page.on('console', message => {
    if (message.type() === 'warning') warnings.push(message.text())
  })
  await page.addScriptTag({ content: patch })
  const installed = await page.evaluate(mode => {
    const term = window.term
    if (mode === 'missing') {
      window.terminalIme.installTerminalIme(window, { element: term.element })
    } else {
      if (mode === 'public') {
        const core = term._core.coreService
        window.publicSends = 0
        term.input = function (data, wasUserInput) {
          if (this !== term || wasUserInput !== true) throw new Error('Wrong public input arguments')
          window.publicSends++
          core.triggerDataEvent(data, wasUserInput)
        }
      }
      window.terminalIme.installTerminalIme(window, term)
      window.terminalIme.installTerminalIme(window, term)
    }
    return window.__tetherIme
  }, mode)
  if (mode === 'missing') {
    assert.ok(warnings.some(warning => /IME fix is off.*ttyd 1\.7\.4/.test(warning)), JSON.stringify(warnings))
  }
  if (mode !== 'desktop' && mode !== 'missing') assert.equal(installed, true)
}

async function main() {
  const patch = stock ? null : esbuild.buildSync({
    entryPoints: [path.join(__dirname, 'src/terminalIme.ts')],
    bundle: true, format: 'iife', globalName: 'terminalIme', write: false,
  }).outputFiles[0].text
  const browser = await getBrowser({ local: true })
  try {
    for (const [name, run, mode] of stock ? cases.slice(0, 1) : cases) {
      const device = mode === 'desktop' ? devices['Desktop Chrome'] : devices['Pixel 7']
      const viewport = mode === 'desktop' ? device.viewport : { ...device.viewport, width: 320 }
      const context = await browser.newContext({ ...device, viewport, serviceWorkers: 'block' })
      try {
        const page = await open(context)
        if (patch) await install(page, patch, mode)
        const { line, submitted } = await run(page)
        console.log(`ok ${name} ${JSON.stringify({ line, submitted })}`)
      } finally {
        await context.close()
      }
    }
  } finally {
    await browser.close()
  }
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
