// The held-notes sheet (src/HeldNotes.tsx with src/Live.css) in a real browser, on a fixture page with no app or
// server: an armed button disarms when the list changes (after a 409 and after a refresh), "Clear topic" posts
// {id, count, texts}, focus never hides under the sheet's top row (WCAG 2.4.11), and an emptied list closes the sheet so a
// later note shows the chip, not an open sheet. No terminal and no real note is touched.
// Run: cd ui && node test_held_notes_ui.cjs
const fs = require('node:fs')
const path = require('node:path')
const esbuild = require('esbuild')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const SRC = path.join(__dirname, 'src')
const SHOTS = shotsDir('held-notes')
const ENTRY = `
import { createRoot } from 'react-dom/client'
import { useEffect } from 'react'
import { HeldNotes } from './HeldNotes'
import { NOTES_PATH, useHeldNotes } from './fridayNotes'
const w = window as any
w.listing = []; w.calls = []; w.onDrop = () => ({ ok: true })
const api: any = async (path: string, body?: unknown) => {
  if (path === NOTES_PATH) return { topics: w.listing }
  w.calls.push([path, body])
  return w.onDrop(body)
}
const focusControls = () => document.getElementById('talk')?.focus()
function Harness() {
  const notes = useHeldNotes(api)
  useEffect(() => { w.refresh = notes.refresh }, [notes.refresh])
  return <HeldNotes api={api} topics={notes.topics} refresh={notes.refresh} onEmpty={focusControls} />
}
createRoot(document.getElementById('root')!).render(<Harness />)
`
const PAGE = `<!doctype html><html><head><meta name="viewport" content="width=device-width"></head>
<body style="margin:0"><div class="live" style="position:relative;height:100vh;display:flex;flex-direction:column;justify-content:flex-end;align-items:center">
<div class="live-center" style="margin-bottom:80px"><div id="root"></div></div>
<div class="live-controls" style="position:absolute;bottom:8px"><button id="talk">Talk</button></div></div></body></html>`

const now = 1_800_000_000
const note = (text, n) => ({ text, at: now + n })
const topic = (id, title, notes) => ({ id, title, chat: '', notes, state: 'open' })
const two = [
  topic('T1', 'Urgent Call Spec', [note('Ring for 30 seconds.', 1), note('Use the existing Twilio account.', 2)]),
  topic('T2', 'Blog ideas', [note('Write about voice-first control.', 3)]),
]
const long = [topic('T1', 'Urgent Call Spec', Array.from({ length: 14 }, (_, n) => note(`Note ${n + 1}: ring for 30 seconds, then fall back to a push.`, n)))]

async function main() {
  let browser, page, failed = 0
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    const code = esbuild.buildSync({ stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'held-harness.tsx' },
      bundle: true, write: false, format: 'iife', jsx: 'automatic', logLevel: 'silent', define: { 'process.env.NODE_ENV': '"production"' },
      loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty' } }).outputFiles[0].text
    const css = ['index.css', 'Live.css'].map(file => fs.readFileSync(path.join(SRC, file), 'utf8')).join('\n')
    fs.mkdirSync(SHOTS, { recursive: true })
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(5000)
    await page.route('**/*', route => route.fulfill({ contentType: 'text/html', body: PAGE }))
    const open = async (listing, [width, height] = [1280, 900]) => {
      await page.setViewportSize({ width, height })
      await page.goto('http://fixture.invalid/')
      await page.addStyleTag({ content: css })
      await page.evaluate(listing => { window.preset = listing }, listing)
      await page.addScriptTag({ content: code.replace('w.listing = [];', 'w.listing = w.preset;') })
      await page.locator('.live-held-chip').click()
      await page.locator('.live-held-sheet').waitFor()
    }
    const setListing = listing => page.evaluate(async listing => { window.listing = listing; await window.refresh() }, listing)
    const calls = () => page.evaluate(() => window.calls)
    const armedKeys = () => page.locator('[data-armed="true"]').count()

    // 1. A refresh that changes the list disarms the armed button; one tap then only arms, and posts nothing.
    await open(two)
    await page.getByRole('button', { name: 'Remove note 1' }).first().click()
    check('a first tap arms the note', await armedKeys() === 1)
    await setListing([two[0], topic('T2', 'Blog ideas', [...two[1].notes, note('Second blog note.', 4)])])
    await page.locator('.live-held-sheet li').nth(3).waitFor()  // React has drawn the new list.
    check('a list change after a refresh disarms', await armedKeys() === 0)
    await page.getByRole('button', { name: 'Remove note 1' }).first().click()
    check('one tap after a refresh posts nothing', (await calls()).length === 0, JSON.stringify(await calls()))

    // 2. A 409 disarms too: the store refused, the list moved, and a single tap must not delete.
    await open(two)
    await page.evaluate(changed => { window.onDrop = () => { window.listing = changed; throw new Error('Those notes changed. Look at the list again.') } },
      [topic('T1', 'Urgent Call Spec', [note('Ring for 45 seconds.', 5), two[0].notes[1]]), two[1]])
    await page.getByRole('button', { name: 'Remove note 1' }).first().click()
    await page.getByRole('button', { name: 'Confirm: remove note 1' }).click()
    await page.locator('.live-held-problem').waitFor()
    check('a 409 shows the store message', (await page.locator('.live-held-problem').textContent()).includes('Those notes changed'))
    check('the drop body names the note and its text', JSON.stringify((await calls())[0]?.[1]) === JSON.stringify({ id: 'T1', note: 1, text: 'Ring for 30 seconds.' }), JSON.stringify(await calls()))
    check('a 409 leaves nothing armed', await armedKeys() === 0)
    await page.getByRole('button', { name: 'Remove note 1' }).first().click()
    check('one tap after a 409 posts nothing', (await calls()).length === 1, JSON.stringify(await calls()))

    // 2b. A failure that leaves the list as it was (a 500, a timeout) disarms as well.
    await open(two)
    await page.evaluate(() => { window.onDrop = () => { throw new Error('Tether took too long to answer.') } })
    await page.getByRole('button', { name: 'Clear topic' }).first().click()
    await page.getByRole('button', { name: 'Tap again to clear 2 notes' }).click()
    await page.locator('.live-held-problem').waitFor()
    check('a failure with the same list leaves nothing armed', await armedKeys() === 0)
    check('focus returns to the tapped button', await page.evaluate(() => document.activeElement?.textContent) === 'Clear topic')

    // 3. Clear topic posts the id and the note count the user saw.
    await open(two)
    await page.getByRole('button', { name: 'Clear topic' }).first().click()
    await page.getByRole('button', { name: 'Tap again to clear 2 notes' }).click()
    await page.waitForFunction(() => window.calls.length === 1)
    check('clear topic posts {id, count, texts}', JSON.stringify((await calls())[0]) === JSON.stringify(['friday-notes/drop',
      { id: 'T1', count: 2, texts: ['Ring for 30 seconds.', 'Use the existing Twilio account.'] }]), JSON.stringify(await calls()))

    // 4. WCAG 2.4.11: walking back with Shift+Tab never puts focus under the top row, with or without the alert.
    for (const size of [[320, 640], [1280, 900]]) {
      for (const alert of [false, true]) {
        await open(long, size)
        if (alert) {
          await page.evaluate(() => { window.onDrop = () => { throw new Error('Those notes changed. Look at the list again.') } })
          await page.getByRole('button', { name: 'Remove note 14' }).click()
          await page.getByRole('button', { name: 'Confirm: remove note 14' }).click()
          await page.locator('.live-held-problem').waitFor()
          await page.locator('.live-held-remove:not(:disabled)').first().waitFor()
        }
        await page.locator('.live-held-clear').focus()
        const hidden = []
        for (let n = 0; n < 16; n++) {
          await page.keyboard.press('Shift+Tab')
          const box = await page.evaluate(() => {
            const at = document.activeElement, top = document.querySelector('.live-held-top'), body = document.querySelector('.live-held-body') || document.querySelector('.live-held-sheet')
            if (!at?.classList.contains('live-held-remove')) return null
            const r = at.getBoundingClientRect()
            return { label: at.getAttribute('aria-label'), top: r.top, bottom: r.bottom, cover: top.getBoundingClientRect().bottom, end: body.getBoundingClientRect().bottom }
          })
          // The 2px outline sits 2px outside the button: the ring must clear the top row and the scroll edge too.
          if (box && (box.top - 4 < box.cover - 0.5 || box.bottom + 4 > box.end + 0.5)) hidden.push(box)
        }
        const name = `${size[0]}${alert ? '-alert' : ''}`
        await page.screenshot({ path: path.join(SHOTS, `focus-${name}.png`) })
        check(`focus stays in view at ${name}`, hidden.length === 0, JSON.stringify(hidden))
      }
    }

    // 5. Removing the last note closes the sheet and focus goes to the call controls; a later note shows the chip only.
    const empty = async size => {
      await open([two[1]], size)
      await page.evaluate(() => { window.onDrop = () => { window.listing = []; return { ok: true } } })
      await page.getByRole('button', { name: 'Remove note 1' }).click()
      await page.getByRole('button', { name: 'Confirm: remove note 1' }).click()
      await page.locator('.live-held-chip').waitFor({ state: 'detached' })
    }
    for (const size of [[320, 640], [1280, 900]]) {
      // Escape with no notes reaches the rest of Live (which shrinks Live to its bubble).
      await empty(size)
      await page.evaluate(() => { window.escapes = 0; window.addEventListener('keydown', e => { if (e.key === 'Escape') window.escapes++ }) })
      await page.keyboard.press('Escape')
      check(`Escape is not swallowed with no notes at ${size[0]}`, await page.evaluate(() => window.escapes) === 1)
      // A note held later in the same call: no Escape in between.
      await empty(size)
      check(`an emptied list moves focus to Talk at ${size[0]}`, await page.evaluate(() => document.activeElement?.id) === 'talk')
      await setListing([topic('T3', 'New topic', [note('A new note.', 9)])])
      await page.locator('.live-held-chip').waitFor()
      check(`a later note shows the chip, not the sheet, at ${size[0]}`, await page.locator('.live-held-sheet').count() === 0)
      check(`the chip is closed at ${size[0]}`, await page.locator('.live-held-chip').getAttribute('aria-expanded') === 'false')
      check(`focus stays on Talk at ${size[0]}`, await page.evaluate(() => document.activeElement?.id) === 'talk')
      await page.screenshot({ path: path.join(SHOTS, `after-empty-${size[0]}.png`) })
    }
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
