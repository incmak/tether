// The phone composer morph (composerMorph.ts): the pill row grows into the full box and back without a jump.
// The outline grows from the old size, the parts slide from where they were, and reduced motion turns it off.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http'), fs = require('node:fs/promises'), path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const pane = (id, name) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null })
const reply = 'The numbers add up across all the statements: the balance sheet balances, totals foot, and figures that appear in more than one place match.\n\nTwo things to know when you open it:\n\n- **Table of contents:** it is a Word field, so it starts empty.\n- **Page breaks:** a few tables run onto the next page.'
const fixture = {
  sessions: { sessions: [pane('%1', 'Deploy fix'), pane('%2', 'Docs pass')], default_thread: 't1' },
  chat: { messages: [{ id: 'u1', role: 'user', text: 'Make the SEC-style doc', time: new Date().toISOString() }, { id: 'a1', role: 'assistant', text: reply, time: new Date().toISOString() }], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: true, available: true, reason: '', agents: [], asks: [] }, 'browser/tabs': { tabs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}
let failed = 0
const check = (name, ok, detail = '') => { console.log(`${ok ? 'ok' : 'FAIL'} ${name}${ok ? '' : ' ' + detail}`); if (!ok) failed++ }
;(async () => {
  const server = http.createServer((q, r) => { r.writeHead(404); r.end() }); await new Promise(r => server.listen(0, '127.0.0.1', r))
  const origin = `http://localhost:${server.address().port}`
  const browser = await getBrowser(); const context = await browser.newContext({ viewport: { width: 390, height: 844 } }); const page = await context.newPage()
  const posts = []
  let ratings = { a1: { rating: 'up', note: '' } }
  const ratingsT2 = { a1: { rating: 'up', note: '' } }
  // A held save: the test releases it with a status after it changes the conversation.
  let gate = null, failing = false
  const gets = []
  const hold = () => { let release; const promise = new Promise(resolve => { release = resolve }); gate = { promise, release }; return gate }
  try {
    await page.routeWebSocket('**/*', s => s.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const ep = url.pathname.slice(12)
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON(); posts.push({ ep, body })
          if (ep === 'feedback' && gate) { const held = gate; gate = null; const status = await held.promise; if (status !== 200) return route.fulfill({ status, json: { error: 'Disk full.' } }) }
          if (ep === 'feedback' && failing) return route.fulfill({ status: 500, json: { error: 'Disk full.' } })
          if (ep === 'feedback' && body.message === 'fail') return route.fulfill({ status: 500, json: { error: 'Disk full.' } })
          return route.fulfill({ json: ep === 'feedback' ? { saved: body.rating ? { rating: body.rating, note: body.note } : null } : {} })
        }
        if (ep === 'stream') return route.fulfill({ status: 204, body: '' })
        if (ep.startsWith('chat')) return route.fulfill({ json: url.searchParams.get('session') === '%2' ? { ...fixture.chat, thread: 't2', session: '%2' } : fixture.chat })
        if (ep === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (ep === 'feedback') gets.push(url.searchParams.get('thread'))
        if (ep === 'feedback') return route.fulfill({ json: { ratings: url.searchParams.get('thread') === 't2' ? ratingsT2 : ratings } })
        return route.fulfill({ json: fixture[ep] || {} })
      }
      if (url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.join(DIST, decodeURIComponent(url.pathname.slice(5)))
        try { return await route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort()
    })
    await page.addInitScript(() => {
      localStorage.clear(); localStorage.setItem('tmux-chat.prefs.synced', '1'); localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
      // A desktop browser with no share sheet: Share copies the text. Record what was copied.
      delete Navigator.prototype.share
      window.__copied = []
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__copied.push(text) } } })
    })
    await page.goto(`${origin}/chat/?session=%251`); await page.locator('.phone-tabbar').waitFor()
    const actions = page.locator('.chat-page .assistant-message .message-actions').last()
    await actions.waitFor()


    const box = page.locator('.chat-page form.composer')
    const field = page.locator('.chat-page .composer textarea')
    await box.waitFor()
    check('the idle phone composer is the one-row pill', await box.evaluate(el => el.classList.contains('compact')))
    // Sample every frame after the tap: the outline height, the field's offset and whether the box is morphing.
    const sample = () => page.evaluate(() => new Promise(resolve => {
      const el = document.querySelector('.chat-page form.composer'), input = el.querySelector('textarea')
      const frames = [], start = performance.now()
      const step = () => {
        const before = getComputedStyle(el, '::before')
        frames.push({ t: performance.now() - start, morphing: el.hasAttribute('data-morph'), outline: el.hasAttribute('data-morph') ? parseFloat(before.height) : el.getBoundingClientRect().height,
          fieldTop: input.getBoundingClientRect().top - el.getBoundingClientRect().bottom, fieldLeft: input.getBoundingClientRect().left, fieldScreen: input.getBoundingClientRect().top })
        if (performance.now() - start < 520) requestAnimationFrame(step); else resolve(frames)
      }
      requestAnimationFrame(step)
    }))
    const smooth = (name, frames, from, to) => {
      const heights = frames.map(frame => frame.outline)
      const steps = heights.slice(1).map((h, i) => h - heights[i])
      const growing = to > from
      check(`${name}: the box morphs over several frames`, frames.filter(frame => frame.morphing).length >= 8, JSON.stringify(frames.length))
      check(`${name}: the outline starts near the old size`, Math.abs(heights[0] - from) < (to - from) * 0.5 * Math.sign(to - from) + 1, `${heights[0]} from ${from}`)
      check(`${name}: the outline moves one way only`, steps.every(step => growing ? step >= -0.5 : step <= 0.5), JSON.stringify(heights.map(Math.round)))
      check(`${name}: no frame jumps more than 40% of the change`, steps.every(step => Math.abs(step) <= Math.abs(to - from) * 0.4), JSON.stringify(heights.map(Math.round)))
      check(`${name}: it ends at the new size and stops morphing`, Math.abs(heights.at(-1) - to) < 1 && !frames.at(-1).morphing, `${heights.at(-1)} vs ${to}`)
      const tops = frames.map(frame => frame.fieldTop), lefts = frames.map(frame => frame.fieldLeft)
      const screen = frames.map(frame => frame.fieldScreen)
      check(`${name}: the field does not jump on the screen (tab bar step)`, screen.slice(1).every((v, i) => Math.abs(v - screen[i]) <= 30), JSON.stringify(screen.map(Math.round)))
      check(`${name}: the text field slides, it does not jump`, tops.slice(1).every((v, i) => Math.abs(v - tops[i]) <= 30) && lefts.slice(1).every((v, i) => Math.abs(v - lefts[i]) <= 12), JSON.stringify({ tops: tops.map(Math.round), lefts: lefts.map(Math.round) }))
    }
    const pill = (await box.boundingBox()).height
    const sampling = sample(); await field.click(); const open = await sampling
    const full = (await box.boundingBox()).height
    check('a tap opens the full box', !(await box.evaluate(el => el.classList.contains('compact'))) && full > pill + 20, `${pill} -> ${full}`)
    smooth('opening', open, pill, full)
    await page.screenshot({ path: path.join(process.env.SHOTS || '/tmp', 'composer-open.png') }).catch(() => {})
    // Controls that fade in take no taps until they show.
    await field.evaluate(el => el.blur()); await page.waitForTimeout(400)
    const fading = await page.evaluate(() => new Promise(resolve => {
      const el = document.querySelector('.chat-page form.composer')
      el.querySelector('textarea').focus()
      requestAnimationFrame(() => {
        const entering = [...el.querySelectorAll('[data-morph-in]')]
        const blocked = entering.filter(control => { const r = control.getBoundingClientRect(); const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !control.contains(hit) })
        resolve({ entering: entering.length, blocked: blocked.length, inert: entering.filter(control => control.inert).length })
      })
    }))
    check('fading controls take no taps or focus', fading.entering > 0 && fading.blocked === fading.entering && fading.inert === fading.entering, JSON.stringify(fading))
    await page.waitForTimeout(450)
    check('the controls take taps and focus after the morph', await box.evaluate(el => !el.querySelector('[data-morph-in], .composer-controls [inert]')))
    const sampling2 = sample(); await field.evaluate(el => el.blur()); const close = await sampling2
    check('leaving the empty box closes it to the pill', await box.evaluate(el => el.classList.contains('compact')))
    smooth('closing', close, full, pill)
    check('the outline and focus ring come back after the morph', await box.evaluate(el => !el.hasAttribute('data-morph') && getComputedStyle(el).borderTopColor !== 'rgba(0, 0, 0, 0)'))
    // A tap in the middle of the close reverses it from where it is, not from the start.
    await field.click(); await page.waitForTimeout(400); await field.evaluate(el => el.blur()); await page.waitForFunction(() => document.querySelector('.chat-page form.composer').hasAttribute('data-morph'), null, { polling: 'raf', timeout: 2000 }); await page.waitForTimeout(50)
    const mid = await box.evaluate(el => ({ morphing: el.hasAttribute('data-morph'), compact: el.classList.contains('compact'), height: parseFloat(getComputedStyle(el, '::before').height) }))
    check('the close is in the middle of its morph', mid.morphing && mid.compact, JSON.stringify(mid))
    const sampling3 = sample(); await field.click(); const again = await sampling3
    check('a tap during the close ends open with no stuck morph', !(await box.evaluate(el => el.classList.contains('compact') || el.hasAttribute('data-morph'))), JSON.stringify(again.at(-1)))
    check('the reopen starts from the size on screen', Math.abs(again[0].outline - mid.height) < 25, `${again[0].outline} mid ${mid.height}`)
    // A blur in the middle of the open reverses it from the size on screen.
    await field.evaluate(el => el.blur()); await page.waitForTimeout(400)
    // Timed in the page: a browser round trip is slower than the morph.
    const { opening, back } = await page.evaluate(() => new Promise(resolve => {
      const el = document.querySelector('.chat-page form.composer'), input = el.querySelector('textarea')
      input.focus()
      setTimeout(() => {
        const opening = { compact: el.classList.contains('compact'), height: parseFloat(getComputedStyle(el, '::before').height) }
        input.blur()
        const back = [{ outline: opening.height }], start = performance.now()
        const step = () => {
          back.push({ outline: el.hasAttribute('data-morph') ? parseFloat(getComputedStyle(el, '::before').height) : el.getBoundingClientRect().height })
          if (performance.now() - start < 450) requestAnimationFrame(step); else resolve({ opening, back })
        }
        step()
      }, 90)
    }))
    check('the open is in the middle of its morph', !opening.compact && opening.height > pill + 2 && opening.height < full - 2, JSON.stringify({ opening, pill, full }))
    check('a blur during the open starts the close from the size on screen', back.slice(1).every((frame, i) => Math.abs(frame.outline - back[i].outline) < 25), JSON.stringify(back.map(frame => Math.round(frame.outline))))
    check('the reversed open ends closed with no stuck morph', await box.evaluate(el => el.classList.contains('compact') && !el.hasAttribute('data-morph')))
    // A change that does not morph (here: reduced motion turned on mid-open) must still end the running morph.
    await page.waitForTimeout(400)
    const quick = await page.evaluate(() => new Promise(resolve => {
      const el = document.querySelector('.chat-page form.composer'), input = el.querySelector('textarea'), media = window.matchMedia
      input.focus()
      setTimeout(() => {
        const morphing = el.hasAttribute('data-morph')
        window.matchMedia = query => query.includes('reduced-motion') ? { matches: true } : media.call(window, query)
        input.blur()
        setTimeout(() => {
          window.matchMedia = media
          resolve({ morphing, compact: el.classList.contains('compact'), morph: el.hasAttribute('data-morph'), border: getComputedStyle(el).borderTopColor })
        }, 400)
      }, 60)
    }))
    check('a change that skips the morph ends the running one, with the outline back', quick.morphing && quick.compact && !quick.morph && quick.border !== 'rgba(0, 0, 0, 0)', JSON.stringify(quick))
    // Reduced motion: the box changes at once.
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const sampling4 = sample(); await field.click(); const still = await sampling4
    check('reduced motion: no morph frames', still.every(frame => !frame.morphing))
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    // Desktop width: the box has one shape, so nothing morphs.
    await field.evaluate(el => el.blur()); await page.setViewportSize({ width: 1200, height: 800 }); await page.waitForTimeout(400)
    const wide = page.locator('form.composer').first()
    const sampling5 = sample(); await wide.locator('textarea').focus(); const desk = await sampling5
    check('desktop: focus does not morph the box', desk.every(frame => !frame.morphing))
  } catch (error) { console.log("FAIL", error.stack); failed++ } finally {
    await context.close(); server.close()
  }
  console.log(failed ? `composer morph: ${failed} failed` : 'composer morph: all checks pass')
  process.exit(failed ? 1 : 0)
})()
