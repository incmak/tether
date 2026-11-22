// The Handled for you sheet across a Tether zone change. The fixture makes no network or terminal writes.
// Run: cd ui && node test_handled_zone_ui.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const esbuild = require('esbuild')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const SRC = path.join(__dirname, 'src')
const SHOTS = shotsDir('handled-zone')
const NY = 'America/New_York'
const LA = 'America/Los_Angeles'
const seconds = time => Date.parse(time) / 1000
const entry = { id: 'overnight', at: seconds('2026-11-21T06:00Z'), kind: 'auto.move', summary: 'Overnight move' }
const nyLog = { today_start: seconds('2026-11-21T05:00Z'), handled_today: 1, entries: [entry] }
const laLog = { today_start: seconds('2026-11-21T08:00Z'), handled_today: 0, entries: [entry] }
const todayEntry = {
  ...entry, id: 'morning', at: seconds('2026-11-21T09:00Z'), summary: 'Morning move',
  undo: { session: '%1', thread: 'fixture', ask: 'ask-1', entry: 'morning' },
}

const ENTRY = `
import { useState } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { HandledButton } from './Attention'
import { ApiError } from './api'
import { setTetherZone, tetherZone } from './zone'

const root = document.getElementById('root')!
const w = window as any
w.requests = []
let markSettled: (id: number) => void
const api: any = (path: string, body?: unknown, signal?: AbortSignal) => {
  if (path === 'attention') return Promise.resolve({ handled_today: 1 })
  if (path === 'attention/back') return Promise.resolve({})
  if (path !== 'attention/log') throw new Error('Unexpected request: ' + path)
  if (w.immediate) {
    const log = w.immediate
    w.immediate = null
    return Promise.resolve(log)
  }
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  w.requests.push({ promise, resolve, reject, signal, zone: tetherZone() })
  return promise
}
w.settle = async (index: number, log: unknown) => {
  const request = w.requests[index]
  if (log) request.resolve(log)
  else request.reject(new ApiError(503, 'Service unavailable.'))
  // Mark the React commit after the request's existing handlers have queued their state updates.
  await request.promise.catch(() => {})
  markSettled(index + 1)
}
w.changeZone = (zone: string) => flushSync(() => setTetherZone(zone))
w.logState = () => ({
  rows: Array.from(document.querySelectorAll('.handled-line'), row => row.textContent),
  count: document.querySelector('#handled-log .handled-count')?.textContent,
  older: !!document.querySelector('#handled-older'),
  loading: [...document.querySelectorAll('.handled-sheet [role="status"]')].some(node => node.textContent === 'Loading the log…'),
  error: document.querySelector('.handled-error p')?.textContent ?? '',
})
function Harness() {
  const [settled, setSettled] = useState(0)
  markSettled = setSettled
  return <div data-settled={settled}><HandledButton api={api} later={[]} sessions={[]} refreshKey="" onOpen={() => {}} onUpdate={() => {}} /></div>
}
setTetherZone('${NY}')
createRoot(root).render(<Harness />)
`
const PAGE = '<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width"></head><body><div id="root"></div></body></html>'
const hiddenLog = { rows: [], count: '', older: false, loading: true, error: '' }

async function main() {
  const sourceFiles = new Set(fs.readdirSync(SRC))
  const bundle = await esbuild.build({ stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'handled-zone-harness.tsx' },
    bundle: true, write: false, format: 'iife', jsx: 'automatic', logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' }, loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty' },
    // Resolve the exact source names so attention.ts and Attention.tsx stay distinct.
    plugins: [{ name: 'exact-case', setup(build) {
      build.onResolve({ filter: /^\.\// }, args => {
        if (args.resolveDir !== SRC) return
        for (const ext of ['.ts', '.tsx']) {
          const file = args.path.slice(2) + ext
          if (sourceFiles.has(file)) return { path: path.join(SRC, file) }
        }
      })
    } }],
  })
  const css = ['index.css', 'controls.css', 'App.css', 'attention.css']
    .map(file => fs.readFileSync(path.join(SRC, file), 'utf8').replace(/^@import .*;$/gm, '')).join('\n')
  const browser = await getBrowser()
  let context, passed = 0, failed = 0
  try {
    context = await browser.newContext({ locale: 'en-US', reducedMotion: 'reduce', serviceWorkers: 'block' })
    const page = await context.newPage()
    page.setDefaultTimeout(5000)
    await page.clock.setFixedTime(new Date('2026-11-21T12:00Z'))
    await page.route('**/*', route => route.fulfill({ contentType: 'text/html', body: PAGE }))
    const sheet = page.getByRole('dialog', { name: 'Handled for you' })
    const today = sheet.getByRole('region', { name: /^Today/ })
    const older = sheet.getByRole('region', { name: 'Last 7 days' })
    const retry = sheet.getByRole('button', { name: 'Retry' })
    const readLog = () => page.evaluate(() => window.logState())
    const waitForRequest = (index, zone) => page.waitForFunction(([i, name]) => window.requests[i]?.zone === name, [index, zone])
    const switchToLA = () => page.evaluate(zone => window.changeZone(zone), LA)
    const settle = async (index, log) => {
      await page.evaluate(([i, data]) => window.settle(i, data), [index, log])
      await page.locator(`[data-settled="${index + 1}"]`).waitFor()
    }
    const open = async () => {
      await page.goto('http://fixture.invalid/')
      await page.addStyleTag({ content: css })
      await page.addScriptTag({ content: bundle.outputFiles[0].text })
      await page.getByRole('button', { name: /^Handled for you today:/ }).click()
      await sheet.waitFor()
      await waitForRequest(0, NY)
    }
    const expectOvernightInLA = async () => {
      await older.waitFor()
      assert.equal(await older.getByRole('listitem').count(), 1)
      assert.equal(await older.locator('time').textContent(), 'Nov 20 10:00 PM')
      assert.equal(await older.getByText(entry.summary, { exact: true }).count(), 1)
    }
    const expectLA = async () => {
      await today.getByText('Nothing was handled for you today.').waitFor()
      assert.equal(await today.getByRole('heading').textContent(), 'Today 0')
      await expectOvernightInLA()
    }
    const test = async (name, run) => {
      try { await run(); passed++; console.log(`ok ${name}`) }
      catch (error) { failed++; console.error(`FAIL ${name}\n${error.stack}`) }
    }

    await test('the old log stays hidden during a zone reload, a 503, and Retry', async () => {
      await page.setViewportSize({ width: 320, height: 640 })
      await open()
      await settle(0, nyLog)
      assert.equal(await today.getByRole('heading').textContent(), 'Today 1')
      await today.getByText(entry.summary, { exact: true }).waitFor()
      await switchToLA()
      await waitForRequest(1, LA)
      const pending = await readLog()
      if (pending.loading) {
        fs.mkdirSync(SHOTS, { recursive: true })
        for (const [width, height] of [[320, 640], [1280, 900]]) {
          await page.setViewportSize({ width, height })
          await page.screenshot({ path: path.join(SHOTS, `loading-${width}.png`) })
        }
      }
      await settle(1, null)
      const rejected = await readLog()
      await retry.press('Enter')
      await waitForRequest(2, LA)
      const retrying = await readLog()
      await settle(2, laLog)
      await expectLA()
      await sheet.getByRole('button', { name: 'Close', exact: true }).last().click()
      await sheet.waitFor({ state: 'detached' })
      assert.equal(await page.evaluate(() => window.requests[1].signal.aborted), true)
      assert.deepEqual({ pending, rejected, retrying }, {
        pending: hiddenLog,
        rejected: { ...hiddenLog, loading: false, error: 'Could not load the log. Service unavailable.' },
        retrying: hiddenLog,
      })
    })

    await test('an old-zone Retry response cannot appear during the new-zone request', async () => {
      await open()
      await settle(0, null)
      await retry.click()
      await waitForRequest(1, NY)
      assert.equal(await page.evaluate(() => window.requests[1].signal), undefined)
      await switchToLA()
      await waitForRequest(2, LA)
      await settle(1, { ...nyLog, entries: [{ ...entry, summary: 'Late New York response' }] })
      const late = await readLog()
      await settle(2, laLog)
      await expectLA()
      assert.deepEqual(late, hiddenLog)
    })

    await test('an immediate reload switches Today and Last 7 days to the new zone', async () => {
      await open()
      await settle(0, nyLog)
      await today.getByText(entry.summary, { exact: true }).waitFor()
      const initial = await page.evaluate(([zone, data]) => {
        window.immediate = data
        window.changeZone(zone)
        return window.logState()
      }, [LA, { ...laLog, handled_today: 1, entries: [todayEntry, entry] }])
      const undo = today.getByRole('button', { name: `Undo: ${todayEntry.summary}` })
      await undo.waitFor()
      assert.equal(await today.getByRole('heading').textContent(), 'Today 1')
      assert.equal(await today.locator('time').textContent(), '01:00 AM')
      await expectOvernightInLA()
      assert.equal(await page.evaluate(() => window.requests[0].signal.aborted), true)
      assert.deepEqual(initial, hiddenLog)

      await undo.press('Enter')
      await waitForRequest(1, LA)
      const undone = today.getByText('Undone', { exact: true })
      await undone.waitFor()
      assert.equal(await today.getByRole('heading').textContent(), 'Today 0')
      assert.equal(await undone.evaluate(node => document.activeElement === node), true)
      await expectOvernightInLA()
    })
  } finally {
    try { await context?.close() } finally { await browser.close() }
  }
  console.log(`Handled zone browser tests: ${passed} passed, ${failed} failed, ${passed + failed} total`)
  process.exitCode = failed ? 1 : 0
}
main().catch(error => { console.error(error); process.exitCode = 1 })
