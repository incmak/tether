// B271 desktop mouse checks on the built CSS.
// 1. A pressed toggle (.icon-button or .rich-tool with aria-pressed="true") already has the hover fill at rest, so with
//    a mouse its hover must use a stronger fill. A real pointer hovers each control.
// 2. In a narrow workspace pane, a long chat title truncates inside its box and never runs under the header buttons.
// Run after a build: node test_desktop_mouse_ui.cjs (DIST picks another build folder).
const fs = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))

async function main() {
  let browser, page, failed = 0
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    // Only this build's stylesheets: a deployed dist keeps old chunks, and an old rule later in the cascade would win.
    const html = fs.readFileSync(path.join(DIST, 'index.html'), 'utf8')
    const entry = html.match(/assets\/index-[^"']+\.js/)[0]
    const names = new Set(`${html}\n${fs.readFileSync(path.join(DIST, entry), 'utf8')}`.match(/assets\/[^"'\s]+\.css/g))
    const css = [...names].map(name => fs.readFileSync(path.join(DIST, name), 'utf8')).join('\n')
    browser = await getBrowser()
    const context = browser.contexts()[0] || await browser.newContext()
    page = await context.newPage()
    // A background tab of the shared browser runs no animation frames, so the waits poll on a timer.
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.setContent(`<!doctype html><style>${css}</style><div style="padding:40px;display:flex;gap:12px">
      <button id="pressed" class="icon-button" aria-pressed="true">A</button>
      <button id="rich" class="rich-tool" aria-pressed="true">B</button>
      <button id="plain" class="icon-button" aria-pressed="false">C</button>
      <button id="off" class="icon-button" aria-pressed="true" disabled>D</button></div>`)
    // No transition, so the computed fill is final as soon as :hover matches.
    await page.addStyleTag({ content: '* { transition: none !important; }' })
    check('mouse: hover and fine pointer', await page.evaluate(() => matchMedia('(hover: hover) and (pointer: fine)').matches))
    const fill = id => page.$eval(`#${id}`, element => getComputedStyle(element).backgroundColor)
    const hover = async id => {
      await page.mouse.move(2, 2)
      await page.waitForFunction(() => !document.querySelector('button:hover'), null, { polling: 50 })
      const rest = await fill(id)
      await page.hover(`#${id}`, { force: true })
      await page.waitForFunction(target => document.getElementById(target).matches(':hover'), id, { polling: 50 })
      return { rest, hovered: await fill(id) }
    }
    for (const id of ['pressed', 'rich', 'plain']) {
      const { rest, hovered } = await hover(id)
      check(`${id}: the hover fill differs from the rest fill`, rest !== hovered, `${rest} -> ${hovered}`)
    }
    const pressed = await hover('pressed'), plain = await hover('plain')
    check('pressed rest fill is the plain hover fill (the pressed look is unchanged)', pressed.rest === plain.hovered, `${pressed.rest} vs ${plain.hovered}`)
    const off = await hover('off')
    check('disabled pressed: no hover change', off.rest === off.hovered, `${off.rest} -> ${off.hovered}`)

    // Pane headers at three pane widths: column title (narrow), and row title (wide).
    const header = width => `<div class="chat-panel" style="width:${width}px"><header class="chat-header">
      <button class="chat-title"><h1>Plan the release notes for the next update</h1><div><span>Claude</span><span>~/apps/a-long-folder-name</span></div></button>
      ${'<button class="icon-button">x</button>'.repeat(7)}</header></div>`
    await page.setContent(`<!doctype html><style>${css}</style><div class="workspace">${[331, 420, 700].map(header).join('')}</div>`)
    const rows = await page.$$eval('.chat-header', heads => heads.map(head => {
      const title = head.querySelector('.chat-title').getBoundingClientRect()
      const buttons = [...head.querySelectorAll('.icon-button')].map(b => b.getBoundingClientRect())
      return { width: Math.round(head.getBoundingClientRect().width), kids: [...head.querySelector('.chat-title').children].map(kid => {
        const r = kid.getBoundingClientRect()
        return { tag: kid.tagName, inside: r.right <= title.right + 0.5, clear: !buttons.some(b => r.left < b.right - 0.5 && r.right > b.left + 0.5 && r.top < b.bottom - 0.5 && r.bottom > b.top + 0.5) }
      }) }
    }))
    for (const row of rows) for (const kid of row.kids) {
      check(`pane ${row.width}px: title ${kid.tag} stays inside the title box`, kid.inside)
      check(`pane ${row.width}px: title ${kid.tag} does not run under a button`, kid.clear)
    }
  } catch (error) {
    failed++
    console.log(`FAIL harness ${error.stack}`)
  } finally {
    if (page) await page.close().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all ok')
  process.exit(failed ? 1 : 0)
}
main()
