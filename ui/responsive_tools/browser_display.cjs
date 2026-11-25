const assert = require('node:assert/strict')

const presets = [
  { id: 'desktop', label: 'Desktop', width: 1280, height: 800, touch: false },
  { id: 'pixel', label: 'Pixel 8', width: 412, height: 915, touch: true },
  { id: 'iphone', label: 'iPhone 15', width: 393, height: 852, touch: true },
  { id: 'tablet', label: 'iPad Air', width: 820, height: 1180, touch: true },
]
const live = '/browser/live/display-fixture/'
const panels = ['.browser-sheet[open] .browser-panel', 'section.panel[aria-label="Browser"] .browser-panel:visible']
const deviceSelector = '.browser-bar button[aria-label^="Device:"]'
const menuSelector = '.browser-menu.end:has([aria-label="Picture"])'

function displaySurfaces({ before = false } = {}) {
  const surfaces = {}
  for (const [state, label] of [['whole-window', 'Whole Chrome window'], ['watch-only', 'Watch-only agent tab']]) {
    for (const theme of ['light', 'dark']) {
      const whole = state === 'whole-window'
      const which = whole ? 'signed' : 'test'
      const tab = {
        ...presets[0], id: 'display-fixture', preset: 'desktop',
        title: 'Browser preview', url: 'http://localhost/preview/',
        mode: whole ? 'saved' : 'agent', ...(whole ? {} : { controller: 'agent' }),
      }
      async function prepare(page, ctx) {
        let windowView = false
        await page.route('**/mobile/api/browser/tabs*', route => route.fulfill({ json: {
          tabs: [tab], presets, live, live_port: Number(new URL(ctx.url).port), live_run: tab.id, window_view: windowView,
          browsers: [{ id: 'test', available: true }, { id: 'signed', available: true }],
        } }))
        await page.route('**/mobile/api/browser/show', route => route.fulfill({ json: { ok: true } }))
        await page.route('**/mobile/api/browser/window-view', route => {
          windowView = route.request().postDataJSON().enabled
          return route.fulfill({ json: { window_view: windowView } })
        })
        await page.route(`**${live}`, route => route.fulfill({
          contentType: 'text/html', body: '<!doctype html><title>Browser preview</title><style>body{margin:24px;font:16px system-ui}</style><h1>Browser preview</h1>',
        }))
      }
      surfaces[`browser-display-${state}-${theme}`] = {
        about: `${label} Display menu in ${theme} mode`,
        landmark: panels.map(panel => `${panel} ${before ? deviceSelector : menuSelector}`).join(', '),
        expect: { 'browser/tabs': 200, 'browser/show': 200, ...(whole ? { 'browser/window-view': 200 } : {}) },
        prepare: before ? undefined : prepare,
        async open(page, ctx) {
          // The origin/main baseline driver opens recipes without the pre-navigation hook.
          if (before) await prepare(page, ctx)
          await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: theme })
          await page.evaluate(({ which, theme }) => {
            document.documentElement.dataset.theme = theme
            window.VideoDecoder = class { static async isConfigSupported() { return { supported: true } } }
            localStorage.setItem('tether-browser-which', which)
          }, { which, theme })
          if (ctx.phone) {
            await page.evaluate(detail => window.dispatchEvent(new CustomEvent('tether-browser-watch', { detail })),
              { tab: tab.id, browser: which, pane: ctx.pane })
          } else {
            const toggle = page.getByRole('group', { name: 'Panels', exact: true }).getByRole('button', { name: 'Browser', exact: true })
            if (await toggle.getAttribute('aria-pressed') !== 'true') await toggle.click()
          }
          const panel = page.locator(panels[ctx.phone ? 0 : 1])
          const browser = panel.getByRole('button', { name: /^Browser:/ })
          const label = whole ? 'Signed in' : 'Test'
          if (await browser.getAttribute('aria-label') !== `Browser: ${label}`) {
            await browser.click()
            await panel.getByRole('menuitemradio', { name: new RegExp(`^${label} browser`) }).click()
          }
          if (whole) {
            await panel.getByRole('button', { name: 'Show the whole Chrome window' }).click()
            await panel.getByRole('button', { name: 'Show the whole Chrome window', pressed: true }).waitFor()
          } else {
            await panel.locator('.browser-agent').getByText(/uses this tab\. You watch\./).waitFor()
          }
          await panel.frameLocator('.browser-live').getByRole('heading', { name: 'Browser preview' }).waitFor()
          if (before) {
            assert(await panel.getByRole('button', { name: 'Device: Desktop' }).isDisabled())
          } else {
            await panel.getByRole('button', { name: 'Display: Desktop · Sharp text' }).press('Enter')
          }
        },
        async ready(page, ctx) {
          if (before) return
          const menu = page.locator(`${panels[ctx.phone ? 0 : 1]} ${menuSelector}`)
          const picture = menu.getByRole('group', { name: 'Picture' })
          await picture.getByText('Less work for this device', { exact: true }).waitFor()
          assert.equal(await menu.getByRole('menuitemradio', { disabled: true }).count(), presets.length)
          assert.equal(await picture.getByRole('menuitemradio', { disabled: false }).count(), 3)
          assert.equal(await picture.getByRole('menuitemradio', { disabled: true }).count(), 0)
          const box = await menu.boundingBox()
          for (const row of await menu.getByRole('menuitemradio').all()) {
            const bounds = await row.boundingBox()
            assert(bounds && bounds.y >= box.y && bounds.y + bounds.height <= box.y + box.height)
          }
        },
      }
    }
  }
  return surfaces
}

module.exports = { displaySurfaces }
