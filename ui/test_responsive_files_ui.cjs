// Files and Share responsive sweep: local, fixture-only browser tests at phone and desktop widths.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = path.resolve(__dirname, '../.release-work/2026-11-16/responsive/files/shots', process.env.PHASE || 'after')
const FIXTURES = path.resolve(__dirname, 'responsive_files')
const NOW = Math.floor(Date.now() / 1000)
const pane = (id, session, name, label, extra) => ({ id, tmux_session: session, label, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const sessions = { sessions: [pane('%3', '$1', 'W'.repeat(80), 'fixture', {})], default_thread: 't3' }
const LONG = 'W'.repeat(80), OTHER = 'folder_' + 'm'.repeat(93)
const XLSX = require('xlsx')
const workbook = XLSX.utils.book_new()
for (const name of ['Sheet_' + 'W'.repeat(25), 'Second sheet']) XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([['Long cell', LONG, OTHER], ['Second row', 12345, 'Value']]), name)
const officeBytes = XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' })
// A one-page PDF with a blue rectangle so the test can check that the canvas drew the page.
const drawing = '0 0 1 rg\n0 0 300 400 re f\n'
const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R >>', `<< /Length ${Buffer.byteLength(drawing)} >>\nstream\n${drawing}endstream`]
let pdf = '%PDF-1.4\n', offsets = [0]
objects.forEach((object, i) => { offsets.push(Buffer.byteLength(pdf)); pdf += `${i + 1} 0 obj\n${object}\nendobj\n` })
const xref = Buffer.byteLength(pdf)
pdf += `xref\n0 5\n0000000000 65535 f \n${offsets.slice(1).map(n => String(n).padStart(10, '0') + ' 00000 n \n').join('')}trailer\n<< /Root 1 0 R /Size 5 >>\nstartxref\n${xref}\n%%EOF`
const entries = [
  { name: LONG, dir: true, kind: 'folder', size: null, modified: NOW },
  ...['.md', '.txt', '.xlsx', '.pdf'].map((ext, i) => ({ name: (i % 2 ? OTHER : LONG) + ext, dir: false, kind: ext === '.pdf' ? 'pdf' : 'text', size: 123456, modified: NOW })),
  { name: OTHER, dir: true, kind: 'folder', size: null, modified: NOW },
  ...['.docx', '.pptx'].map(ext => ({ name: LONG + ext, dir: false, kind: 'text', size: 8000, modified: NOW })),
  ...Array.from({ length: 122 }, (_, i) => ({ name: `note_${i}.txt`, dir: false, kind: 'text', size: 1234, modified: NOW })),
]
let mode = 'normal', pending = [], uploadIndex = 0, finishUploads = false
const errorText = 'The_destination_is_not_writable_' + LONG.repeat(2)
const usage = { used: 123456789012, quota: 9876543210123, count: 130 }
const sharedFiles = entries.filter(e => !e.dir).slice(0, 4).map((e, i) => ({ id: 'share' + i, name: e.name, size: e.size, kind: i === 3 ? 'pdf' : 'text', mime: 'text/plain', created: new Date().toISOString(), modified: '2026-11-16', source: 'you', has_thumb: false }))
const fixture = {
  chat: { messages: [], busy: false, thread: 't3', agent: 'claude', writable: true, session: '%3', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%3', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null }, 'work/board': { projects: [], items: [] },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser({ local: true })
    const context = await browser.newContext({ serviceWorkers: 'block', hasTouch: Number(process.env.WIDTH) < 900 })
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    page.on('pageerror', error => pageErrors.push(error.stack))
    if (process.env.DEBUG) page.on('console', message => { if (message.type() === 'error') console.log('console:', message.text().slice(0, 600)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        const key = endpoint.split('?')[0]
        const respond = json => route.fulfill({ json })
        if (key === 'files/places') return respond({ pinned: ['~/' + LONG], sessions: [{ session: '%3', name: LONG, cwd: '~' }], disk: { used: 1234, total: 12345678 }, home: '~' })
        if (key === 'files/list' || key === 'files/search') {
          if (mode === 'loading') { pending.push(route); return }
          if (mode === 'error') return route.fulfill({ status: 403, json: { error: errorText } })
          const rows = mode === 'empty' ? [] : entries
          return respond({ path: url.searchParams.get('path'), entries: rows, hits: rows, truncated: false, repo: null })
        }
        if (key === 'files/stat') return respond({ path: url.searchParams.get('path'), size: 123456, modified: NOW, git: null, owner: 'fixture', mode: '644', mime: 'text/plain' })
        if (key === 'files/preview') return respond({ kind: 'text', lines: ['# A heading', LONG, 'A second line.'], truncated: false })
        if (key === 'files/raw' && /\.(docx|pptx)$/.test(url.searchParams.get('path') || '')) return route.fulfill({ contentType: 'application/octet-stream', body: await fs.readFile(path.join(FIXTURES, 'responsive.' + url.searchParams.get('path').split('.').pop())) })
        if (key === 'files/raw' && url.searchParams.get('path')?.endsWith('.xlsx')) return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: officeBytes })
        if (key === 'share/raw/share3') return route.fulfill({ contentType: 'application/pdf', body: Buffer.from(pdf) })
        if (key === 'files/raw') return route.fulfill({ contentType: 'text/plain', body: '# A heading\n\n' + LONG + '\n\nA link: [example](https://example.com)' })
        if (key === 'files/upload' || key === 'share/upload') {
          if (uploadIndex++ === 0 || finishUploads) return route.fulfill({ status: 403, json: { error: errorText } })
          pending.push(route); return
        }
        if (key === 'share/list') {
          if (mode === 'loading') { pending.push(route); return }
          if (mode === 'error') return route.fulfill({ status: 403, json: { error: errorText } })
          return respond({ files: mode === 'empty' || url.searchParams.get('source') === 'agent' ? [] : sharedFiles, next: null, usage })
        }
        if (key === 'share/uploads') return respond({ uploads: [] })
        if (key.startsWith('share/raw/')) return route.fulfill({ contentType: 'text/plain', body: '# Preview\n' + LONG })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'sessions') return route.fulfill({ json: sessions })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // Serve every document request from the fixture build; other network requests stay blocked.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        try { return route.fulfill({ status: 200, contentType: mime[path.extname(file)] || 'application/octet-stream', body: await fs.readFile(file) }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      // A reload keeps what the page stored; a fresh open starts from these prefs.
      const asked = new URL(location.href).searchParams.get('prefs')
      if (asked === null || sessionStorage.getItem('kept') === asked) return
      sessionStorage.setItem('kept', asked)
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false, ...JSON.parse(asked) }))
    })

    const failures = []
    const audit = async (name, scope) => {
      await page.waitForTimeout(120)
      const issues = await page.locator(scope).first().evaluate(root => {
        const bad = [], bounds = root.getBoundingClientRect()
        if (document.documentElement.scrollWidth > innerWidth) bad.push('page horizontal scroll')
        if (bounds.left < -1 || bounds.right > innerWidth + 1 || bounds.top < -1 || bounds.bottom > innerHeight + 1) bad.push('surface outside viewport')
        if (root.scrollWidth > root.clientWidth + 1) bad.push('surface content overflows')
        for (const el of root.querySelectorAll('.xp-list, .fx-content, .sh-scroll, .fx-pick-list, .sh-pane-body')) {
          if (el.clientWidth && el.scrollWidth > el.clientWidth + 1) bad.push(el.className + ' has horizontal scroll')
        }
        for (const el of root.querySelectorAll('.sheet-sub')) {
          if (getComputedStyle(el).display === 'flex' && el.scrollWidth > el.clientWidth + 1) bad.push('sheet path clips without a rendered ellipsis')
        }
        const controls = [...root.querySelectorAll('button, a, input:not([hidden])')].filter(el => {
          const r = el.getBoundingClientRect(), css = getComputedStyle(el)
          const list = el.closest('.xp-list, .fx-content'), viewport = list?.getBoundingClientRect()
          if (viewport && (r.top < viewport.top || r.bottom > viewport.bottom)) return false
          return r.width && r.height && css.visibility !== 'hidden' && !el.closest('[hidden]') && r.bottom > bounds.top && r.top < bounds.bottom
        })
        for (const el of controls) {
          const r = (el.tagName === 'INPUT' ? el.closest('label') || el : el).getBoundingClientRect(), css = getComputedStyle(el)
          const scroller = el.closest('.xp-crumbs, .rich-toolbar, .fx-tabs, .office-tabs, .sh-views, .text-viewer-body')
          if (!scroller && (r.left < bounds.left - 1 || r.right > bounds.right + 1)) bad.push((el.getAttribute('aria-label') || el.textContent).slice(0, 45) + ' leaves surface')
          if (el.tagName !== 'INPUT' && el.textContent.trim() && el.scrollWidth > el.clientWidth + 1 && css.textOverflow !== 'ellipsis' && !scroller) bad.push((el.getAttribute('aria-label') || el.textContent).slice(0, 45) + ' clips')
          // WCAG 2.5.8 exempts inline links in prose; rich-editor links are text, not separate controls.
          const inlineLink = el.tagName === 'A' && css.display === 'inline' && el.closest('.rich-doc, .text-viewer-doc')
          // Native checkboxes have a larger cell/label target. Hidden file inputs have no hit area.
          if (!inlineLink && !['checkbox', 'file'].includes(el.type) && !(el.tagName === 'INPUT' && el.getBoundingClientRect().width === 0) && (r.width < 24 || r.height < 24)) bad.push((el.getAttribute('aria-label') || el.textContent || el.outerHTML).slice(0, 90) + ' target below 24px')
        }
        for (let i = 0; i < controls.length; i++) for (let j = i + 1; j < controls.length; j++) {
          const a = controls[i], b = controls[j]
          if ((a.closest('.sh-grid') && b.closest('.sh-actionbar')) || (b.closest('.sh-grid') && a.closest('.sh-actionbar'))) continue
          if (a.closest('.xp-fab') || b.closest('.xp-fab') || a.contains(b) || b.contains(a)) continue
          // A tile's selection toggle intentionally sits on that same tile.
          if (a.closest('.sh-grid') && a.closest('li') === b.closest('li')) continue
          const x = a.getBoundingClientRect(), y = b.getBoundingClientRect()
          if (Math.min(x.right, y.right) - Math.max(x.left, y.left) > 1 && Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top) > 1) bad.push('controls overlap: ' + a.textContent.slice(0, 25) + ' / ' + b.textContent.slice(0, 25))
        }
        return [...new Set(bad)]
      })
      await page.screenshot({ path: path.join(SHOTS, name + '.png') })
      check(name, !issues.length, issues.join('; '))
      if (issues.length) failures.push({ name, issues })
    }
    const open = async (screen, width, height, state = 'normal') => {
      for (const route of pending.splice(0)) await route.abort().catch(() => {})
      mode = state; uploadIndex = 0; finishUploads = false
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%253&prefs=${encodeURIComponent(JSON.stringify({ voiceOff: true }))}`, { waitUntil: 'load' })
      await page.locator('.chat-panel').first().waitFor()
      const wide = width >= 900
      if (wide) await page.locator('.app-rail').getByRole('button', { name: screen, exact: true }).click()
      else if (screen === 'Files') await page.locator('.phone-tabbar').getByRole('button', { name: 'Files', exact: true }).click()
      else { await page.locator('.phone-tabbar').getByRole('button', { name: 'More', exact: true }).click(); await page.locator('.phone-more-menu').getByRole('menuitem', { name: 'Share', exact: true }).click() }
      await page.locator(screen === 'Share' ? '.share' : wide ? '.explorer' : '.xp').waitFor()
      await page.waitForTimeout(300)
      return wide
    }
    const sizes = [[320, 640], [390, 844], [740, 360], [768, 1024], [900, 640], [1024, 768], [1440, 900]].filter(([w]) => !process.env.WIDTH || w === Number(process.env.WIDTH))
    const fileUploads = Array.from({ length: 30 }, (_, i) => ({ name: `${i}_${i % 2 ? OTHER : LONG}.txt`, mimeType: 'text/plain', buffer: Buffer.from('fixture') }))
    for (const [w, h] of sizes) {
      const wide = await open('Files', w, h)
      const root = wide ? '.explorer' : '.xp'
      await audit(`${w}-files-list`, root)
      if (wide) {
        await page.getByRole('button', { name: 'New tab', exact: true }).click()
        check(`${w}: tab close targets`, await page.locator('.fx-tab-close').evaluateAll(buttons => buttons.every(button => { const r = button.getBoundingClientRect(); return r.width >= 24 && r.height >= 24 })))
        await audit(`${w}-folder-tabs`, root)
        await page.locator('.fx-tab-close').last().click()
        await page.getByRole('button', { name: 'Grid view', exact: true }).click()
        await audit(`${w}-files-grid`, root)
        await page.getByRole('button', { name: 'List view', exact: true }).click()
        await page.locator('.fx-c-more button').nth(1).click()
        await audit(`${w}-row-menu`, '.panel-menu[open]')
        await page.keyboard.press('Escape')
      } else {
        await page.getByRole('button', { name: 'View options', exact: true }).click()
        await audit(`${w}-view-options`, '.xp-sheet')
        await page.keyboard.press('Escape')
        await page.locator('.xp-fab').click()
        await audit(`${w}-add-sheet`, '.xp-sheet')
        await page.locator('.xp-sheet').getByRole('button', { name: 'New folder', exact: true }).click()
        await audit(`${w}-new-folder`, '.xp-sheet')
        await page.keyboard.press('Escape')
      }
      if (wide) {
        const beforeRow = await page.locator('.fx-row[data-path]').nth(1).boundingBox()
        await page.locator('.fx-row[data-path]').nth(1).click()
        const afterRow = await page.locator('.fx-row[data-path]').nth(1).boundingBox()
        check(`${w}: selection keeps file rows still`, Math.abs(beforeRow.y - afterRow.y) < 1, `${beforeRow.y} -> ${afterRow.y}`)
        check(`${w}: short selection labels stay readable`, await page.locator('.fx-tb-actions .fx-tb-text').evaluateAll(labels => labels.filter(el => ['Download', 'Copy to…', 'Move to…', 'Delete'].includes(el.textContent)).every(el => el.scrollWidth <= el.clientWidth + 1)))
        await audit(`${w}-files-selection`, root)
        await page.locator('.fx-row[data-path]').nth(1).focus()
        await page.keyboard.press('Control+a')
        check(`${w}: 130 selected`, (await page.locator(wide ? '.fx-selcount' : '.xp-count').textContent()).includes('130 selected'))
        await audit(`${w}-files-130-selected`, root)
        await page.keyboard.press('Escape')
        await page.locator('.fx-row[data-path]').nth(1).click()
        await page.locator('.fx-toolbar').getByRole('button', { name: 'Copy to…', exact: true }).click()
        await audit(`${w}-copy-picker`, '.fx-picker')
        await page.locator('.fx-pick-list button').first().click()
        await audit(`${w}-copy-long-folder`, '.fx-picker')
        await page.locator('.fx-pick-list button').nth(1).click()
        await audit(`${w}-copy-second-folder`, '.fx-picker')
        await page.keyboard.press('Escape')
        await page.locator('.fx-toolbar').getByRole('button', { name: 'Move to…', exact: true }).click()
        await audit(`${w}-move-picker`, '.fx-picker')
        await page.keyboard.press('Escape')
        await page.locator('.fx-row[data-path]').nth(1).click()
        await page.keyboard.press('F2')
        await audit(`${w}-rename`, root)
        await page.keyboard.press('Escape')
        await page.locator('.fx-row[data-path]').nth(1).dblclick()
        await page.locator('.text-viewer[open]').waitFor()
        await audit(`${w}-text-viewer`, '.text-viewer[open]')
        await page.getByRole('button', { name: 'Edit', exact: true }).last().click()
      } else {
        await page.locator('.xp-text-button').getByText('Select', { exact: true }).click()
        await page.locator('.xp-row-main').nth(1).click()
        await audit(`${w}-files-selection`, root)
        await page.locator('.xp-sort').click()
        await page.locator('.xp-sort').click()
        check(`${w}: 130 selected`, (await page.locator(wide ? '.fx-selcount' : '.xp-count').textContent()).includes('130 selected'))
        await audit(`${w}-files-130-selected`, root)
        await page.locator('.xp-sort').click()
        await page.locator('.xp-row-main').nth(1).click()
        await page.locator('.xp-actionbar').getByRole('button', { name: 'Copy to', exact: true }).click()
        await audit(`${w}-copy-picker`, '.xp-picker')
        await page.locator('.xp-picker .xp-row-main').first().click()
        await audit(`${w}-copy-long-folder`, '.xp-picker')
        await page.locator('.xp-picker .xp-row-main').nth(1).click()
        await audit(`${w}-copy-second-folder`, '.xp-picker')
        await page.locator('.xp-picker').getByRole('button', { name: 'Close', exact: true }).click()
        await page.locator('.xp-actionbar').getByRole('button', { name: 'Move to', exact: true }).click()
        await audit(`${w}-move-picker`, '.xp-picker')
        await page.locator('.xp-picker').getByRole('button', { name: 'Close', exact: true }).click()
        await page.getByRole('button', { name: 'Done', exact: true }).click()
        await page.locator('.xp-more').nth(1).click()
        await audit(`${w}-row-menu`, '.xp-sheet')
        await page.locator('.xp-sheet').getByRole('button', { name: 'Rename', exact: true }).click()
        await audit(`${w}-rename`, '.xp-sheet')
        await page.keyboard.press('Escape')
        await page.locator('.xp-row-main').nth(1).click()
        await audit(`${w}-text-preview`, '.xp-preview')
        await page.locator('.xp-preview-head button[aria-label^=Edit]').click()
      }
      await page.locator('.rich-editor, .source-editor').waitFor()
      await audit(`${w}-text-editor`, '.text-viewer[open]')
      const rich = page.getByRole('button', { name: 'Rich', exact: true })
      if (await rich.count()) { await rich.click(); await page.locator('.rich-toolbar').waitFor(); await audit(`${w}-rich-editor`, '.text-viewer[open]')
        await page.locator('.rich-doc p').last().click({ clickCount: 3 })
        await page.locator('.rich-toolbar').getByRole('button', { name: 'Link', exact: true }).click()
        await audit(`${w}-rich-link`, '.text-viewer[open]')
        await audit(`${w}-rich-link-form`, '.rich-link')
        await page.locator('.rich-link').getByRole('button', { name: 'Cancel', exact: true }).click()
        await page.locator('.text-edit-bar').getByRole('button', { name: 'Source', exact: true }).click()
        await page.locator('.source-editor').waitFor()
        await audit(`${w}-source-editor`, '.text-viewer[open]')
      }
      await open('Files', w, h)
      if (wide) await page.locator('.fx-row[data-path]').nth(3).dblclick()
      else {
        await page.locator('.xp-row-main').nth(3).click()
        await page.getByRole('button', { name: 'Open in viewer', exact: true }).click()
      }
      await page.locator('.office-sheet table').waitFor()
      await audit(`${w}-office-viewer`, '.office-viewer[open]')
      await page.locator('.office-tabs button').last().click()
      await audit(`${w}-office-second-sheet`, '.office-viewer[open]')
      for (const extension of ['docx', 'pptx']) {
        await open('Files', w, h)
        if (wide) await page.locator(`.fx-row[data-path$=".${extension}"]`).dblclick()
        else {
          await page.locator('.xp-row-main').filter({ hasText: LONG + '.' + extension }).click()
          await page.getByRole('button', { name: 'Open in viewer', exact: true }).click()
        }
        if (extension === 'docx') await page.locator('.office-doc section.docx').waitFor()
        else await page.locator('.office-slides[aria-label^="2 slides"]').waitFor()
        if (extension === 'docx' && !wide) {
          const word = await page.locator('.office-doc').evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth, page: el.querySelector('section.docx').getBoundingClientRect().width }))
          check(`${w}: Word reflows inside the phone`, word.scroll <= word.width + 1, JSON.stringify(word))
        }
        await audit(`${w}-${extension}-viewer`, '.office-viewer[open]')
      }
      await open('Files', w, h)
      if (wide) await page.locator('.fx-row[data-path]').first().dblclick()
      else await page.locator('.xp-row-main').first().click()
      await page.locator(`${root} input[type=file]`).first().setInputFiles(fileUploads)
      await page.locator(wide ? '.fx-uploads' : '.xp-upload').waitFor()
      await page.locator(wide ? '.fx-uploads li[data-state=failed]' : '.xp-upload strong').filter(wide ? {} : { hasText: '2 of 30' }).waitFor()
      await audit(`${w}-files-uploads`, wide ? '.fx-uploads' : '.xp-upload')
      if (wide) {
        finishUploads = true
        for (const route of pending.splice(0)) await route.fulfill({ status: 403, json: { error: errorText } }).catch(() => {})
        await page.waitForFunction(() => document.querySelector('.fx-uploads-head')?.textContent.includes('30 uploads failed'))
        await audit(`${w}-files-upload-failures`, '.fx-uploads')
        await page.locator('.fx-uploads ul').evaluate(el => { el.scrollTop = el.scrollHeight })
        const hide = await page.getByRole('button', { name: 'Hide uploads', exact: true }).boundingBox()
        check(`${w}: upload close stays visible after list scroll`, hide && hide.y >= 0 && hide.y + hide.height <= h)
        await audit(`${w}-files-upload-scroll`, '.fx-uploads')
        await page.getByRole('button', { name: 'Hide uploads', exact: true }).click()
      } else {
        await page.locator('.xp-upload').getByRole('button', { name: 'Stop', exact: true }).click()
        await page.locator('.xp-toast').waitFor()
        await audit(`${w}-files-upload-failure`, '.xp-toast')
      }
      for (const state of ['loading', 'empty', 'error']) { await open('Files', w, h, state); await audit(`${w}-files-${state}`, root) }
      await open('Share', w, h)
      await audit(`${w}-share-list`, '.share')
      await page.locator('.sh-tile').first().click()
      await audit(`${w}-share-preview`, '.sh-pane')
      await page.locator('.sh-pane').getByRole('button', { name: 'Rename', exact: true }).click()
      await audit(`${w}-share-rename`, '.sh-pane')
      await open('Share', w, h)
      await page.locator('.sh-tile').nth(3).click()
      await page.waitForFunction(() => {
        const canvas = document.querySelector('.sh-pdf-page')
        if (!canvas) return false
        const [red, green, blue] = canvas.getContext('2d').getImageData(canvas.width >> 1, canvas.height >> 1, 1, 1).data
        return blue > red && blue > green
      })
      await audit(`${w}-pdf-viewer`, '.sh-pane')
      if (!wide) await open('Share', w, h)
      await page.locator('.sh-head').getByRole('button', { name: 'Select', exact: true }).click()
      await page.locator('.sh-tile').first().click()
      await audit(`${w}-share-selection`, '.share')
      await open('Share', w, h)
      await page.locator('.sh-head input[type=file]').setInputFiles(fileUploads)
      await page.locator('.sh-uploads').waitFor()
      await page.locator('.sh-up-meta.error').waitFor()
      check(`${w}: Share queues 30 uploads with a failure`, (await page.locator('.sh-uploads header b').textContent()).includes('30'))
      await audit(`${w}-share-uploads`, '.sh-uploads')
      for (const state of ['loading', 'empty', 'error']) { await open('Share', w, h, state); await audit(`${w}-share-${state}`, '.share') }
    }
    check('no page errors', pageErrors.length === 0, pageErrors.join('\n').slice(0, 800))
    await fs.writeFile(path.join(SHOTS, `results-${process.env.WIDTH || 'all'}.json`), JSON.stringify({ failures, pageErrors }, null, 2))
  } catch (error) {
    failed++
    console.log('FAIL', error.stack)
    if (page && !page.isClosed()) {
      await page.screenshot({ path: path.join(SHOTS, `error-${process.env.WIDTH}.png`) }).catch(() => {})
      console.log('Visible dialogs:', await page.locator('dialog[open]').allTextContents())
    }
  } finally {
    for (const route of pending.splice(0)) await route.abort().catch(() => {})
    await page?.close().catch(() => {})
    await browser?.close().catch(() => {})
    server?.close()
  }
  if (failed) { console.log(`${failed} failed`); process.exitCode = 1 }
  else console.log('test_responsive_files_ui: ok')
}
if (process.env.WIDTH) main()
else {
  const { spawnSync } = require('node:child_process')
  let failed = false
  for (const width of [320, 390, 740, 768, 900, 1024, 1440]) {
    const run = spawnSync(process.execPath, [__filename], { env: { ...process.env, WIDTH: String(width) }, stdio: 'inherit', timeout: 120000 })
    if (run.error) console.error(run.error)
    if (run.status !== 0) failed = true
  }
  process.exitCode = failed ? 1 : 0
}
