// B271/B378 desktop mouse and pane header checks on the built CSS.
// 1. A pressed toggle (.icon-button or .rich-tool with aria-pressed="true") already has the hover fill at rest, so with
//    a mouse its hover must use a stronger fill. A real pointer hovers each control.
// 2. At each workspace header tier, controls fold into the menu, which stays in the pane, and the title keeps room.
//    The pressed terminal, unavailable menu, review control, and phone layout have their own checks.
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

    const fixtures = [
      { width: 200, minTitle: 60, folded: true, foldedSlot: true, menuFits: true },
      { width: 300, minTitle: 100, folded: true, foldedSlot: true, menuFits: true },
      { width: 331, minTitle: 100, folded: true },
      { width: 420, minTitle: 100, folded: true },
      { width: 421, minTitle: 100 },
      { width: 700, minTitle: 100 },
      { width: 331, pressed: true, folded: true, label: '331px, terminal open' },
      { width: 300, disabled: true, label: '300px, menu disabled' },
      { width: 331, review: true, minTitle: 80, folded: true, label: '331px, review button' },
    ]
    const icon = (classes = '', attributes = '') => `<button class="icon-button ${classes}" ${attributes}><svg width="20" height="20" aria-hidden="true"></svg></button>`
    const header = ({ width, pressed = false, disabled = false, review = false }) => {
      const foldable = disabled ? '' : ' foldable'
      return `<div class="chat-panel" style="width:${width}px"><header class="chat-header">
        <kbd class="slot-badge">⌥1</kbd>
        <button class="chat-title"><h1>Plan the release notes for the next update</h1><div><span class="agent-word">Claude</span><bdi class="header-folder">a-long-folder-name</bdi></div></button>
        ${review ? icon('review-toggle') : ''}
        ${icon(`side-pin-toggle${foldable}`)}
        ${icon(`search-toggle${foldable}`, disabled ? 'disabled' : '')}
        ${icon(`terminal-toggle${foldable}`, `aria-pressed="${pressed}"`)}
        <div class="keys-container">${icon('', disabled ? 'disabled' : 'aria-expanded="true"')}
          ${disabled ? '' : `<div class="keys-menu">
            <button class="keys-fold">Search this chat</button>
            <button class="keys-fold">${pressed ? 'Back to chat' : 'Open terminal'}</button>
            <button class="keys-fold-slot">Show another pane here</button>
            <button class="keys-fold-slot">Zoom this slot<kbd>Alt+Z</kbd></button>
          </div>`}
        </div>
        ${icon('slot-button slot-pick')}${icon('slot-button slot-zoom')}${icon('slot-button')}
      </header></div>`
    }
    await page.setContent(`<!doctype html><style>${css}</style><div class="workspace with-rail">${fixtures.map(header).join('')}</div>`)
    const selectors = {
      pin: '.side-pin-toggle', search: '.search-toggle', terminal: '.terminal-toggle',
      menu: '.keys-container > button', badge: '.slot-badge', pick: '.slot-pick', zoom: '.slot-zoom',
      close: '.slot-button:not(.slot-pick, .slot-zoom)', review: '.review-toggle',
    }
    const rows = await page.$$eval('.chat-header', (heads, selectors) => heads.map(head => {
      const visible = element => element && element.offsetParent !== null && getComputedStyle(element).display !== 'none'
      const panel = head.parentElement.getBoundingClientRect()
      const title = head.querySelector('.chat-title').getBoundingClientRect()
      const buttons = [...head.querySelectorAll('.icon-button')].filter(visible).map(button => button.getBoundingClientRect())
      const menu = head.querySelector('.keys-menu')?.getBoundingClientRect()
      const displays = selector => [...head.querySelectorAll(selector)].map(item => getComputedStyle(item).display)
      return {
        width: panel.width,
        controls: Object.fromEntries(Object.entries(selectors).map(([name, selector]) => [name, !!visible(head.querySelector(selector))])),
        fold: displays('.keys-fold'), foldSlot: displays('.keys-fold-slot'),
        menu: menu && { inside: menu.left >= panel.left - 0.5 && menu.right <= panel.right + 0.5,
          why: `menu [${menu.left}, ${menu.right}], panel [${panel.left}, ${panel.right}]` },
        kids: [...head.querySelector('.chat-title').children].map(kid => {
          const r = kid.getBoundingClientRect()
          return { tag: kid.tagName, width: r.width, inside: r.left >= title.left - 0.5 && r.right <= title.right + 0.5,
            clear: !buttons.some(b => r.left < b.right - 0.5 && r.right > b.left + 0.5 && r.top < b.bottom - 0.5 && r.bottom > b.top + 0.5) }
        }),
      }
    }), selectors)
    for (const [index, row] of rows.entries()) {
      const { width, minTitle, folded, foldedSlot, pressed, disabled, review, menuFits, label = `${width}px` } = fixtures[index]
      check(`pane ${label}: panel width is ${width}px`, row.width === width, `measured ${row.width}px`)
      const expected = { pin: !folded, search: !folded, terminal: !folded || !!pressed, menu: true,
        badge: !foldedSlot, pick: !foldedSlot, zoom: !foldedSlot, close: true, review: !!review }
      for (const [name, shown] of Object.entries(expected)) {
        check(`pane ${label}: ${name} is ${shown ? 'visible' : 'hidden'}`, row.controls[name] === shown)
      }
      if (!disabled) {
        for (const [name, displays, shown] of [['chat', row.fold, folded], ['slot', row.foldSlot, foldedSlot]]) {
          check(`pane ${label}: both ${name} fold items are ${shown ? 'shown' : 'display:none'}`,
            displays.length === 2 && displays.every(display => shown ? display !== 'none' : display === 'none'), displays.join(', '))
        }
      }
      if (minTitle) {
        const h1 = row.kids.find(kid => kid.tag === 'H1')
        check(`pane ${label}: title H1 is at least ${minTitle}px wide`, h1.width >= minTitle, `measured ${h1.width}px`)
        for (const kid of row.kids) {
          check(`pane ${label}: title ${kid.tag} stays inside the title box`, kid.inside)
          check(`pane ${label}: title ${kid.tag} does not run under a visible button`, kid.clear)
        }
      }
      if (menuFits) check(`pane ${label}: open menu stays within the panel horizontally`, row.menu?.inside, row.menu?.why)
    }

    await page.setViewportSize({ width: 320, height: 800 })
    await page.setContent(`<!doctype html><style>${css}</style>${header({ width: 320 })}`)
    for (const [name, selector] of [['search', selectors.search], ['terminal', selectors.terminal]]) {
      check(`phone 320px: ${name} is visible`, await page.locator(selector).isVisible())
    }
    // The open … menu hangs over the tab strip. A tab's × (z-index 2) must not paint over it: the header stacks above.
    await page.setContent(`<!doctype html><style>${css}</style><div class="workspace"><div class="chat-panel" style="width:400px">
      <header class="chat-header"><button class="chat-title"><h1>Plan the release</h1></button><div class="keys-container">
        <button class="icon-button">…</button><div class="keys-menu"><button id="row">Session options</button></div></div></header>
      <div class="pane-tabs closable" style="padding-left:0"><button id="tab-close" class="tab-close current" style="margin:0">x</button></div></div></div>`)
    const paint = await page.evaluate(() => {
      const row = document.getElementById('row'), close = document.getElementById('tab-close')
      const menu = row.parentElement.getBoundingClientRect()
      close.style.marginLeft = `${menu.left + 20 - close.getBoundingClientRect().left}px`
      close.style.top = `${menu.top + 6 - close.getBoundingClientRect().top}px`
      const box = close.getBoundingClientRect(), under = box.top >= menu.top && box.bottom <= menu.bottom && box.left >= menu.left && box.right <= menu.right
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
      return { under, onMenu: !!hit?.closest('.keys-menu') }
    })
    check('the test places a tab × under the open … menu', paint.under)
    check('a tab × does not paint over the open … menu', paint.onMenu)
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
