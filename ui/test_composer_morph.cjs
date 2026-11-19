// The phone composer morph (composerMorph.ts): the pill row grows into the full box and back without a jump.
// The outline grows from the old size, the parts slide from where they were, and reduced motion turns it off.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http'), fs = require('node:fs/promises'), path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const phone = { width: 390, height: 844 }
const keyboard = { ...phone, height: 544 }
const sampleCount = 12
const sampleSteps = Array.from({ length: sampleCount }, (_, i) => i)
const hangGuard = { timeout: 1500 }
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
  const browser = await getBrowser(); const context = await browser.newContext({ viewport: phone, hasTouch: true }); const page = await context.newPage()
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
    await page.addInitScript(() => {
      const animate = Element.prototype.animate
      const captured = new WeakMap()
      window.composerAnimations = el => captured.get(el) ?? []
      Element.prototype.animate = function (frames, options) {
        const animation = animate.call(this, frames, options)
        const composer = this.closest('form.composer[data-morph]')
        if (composer) {
          animation.pause()
          animation.currentTime = 0
          if (!captured.has(composer)) captured.set(composer, [])
          captured.get(composer).push(animation)
        }
        return animation
      }
      window.measureComposer = el => {
        const outer = el.getBoundingClientRect()
        const input = el.querySelector('textarea').getBoundingClientRect()
        const morphing = el.hasAttribute('data-morph')
        return {
          morphing,
          outline: morphing ? parseFloat(getComputedStyle(el, '::before').height) : outer.height,
          fieldTop: input.top - outer.bottom,
          fieldLeft: input.left - outer.left,
        }
      }
    })
    await page.goto(`${origin}/chat/?session=%251`); await page.locator('.phone-tabbar').waitFor()
    const actions = page.locator('.chat-page .assistant-message .message-actions').last()
    await actions.waitFor()


    const box = page.locator('.chat-page form.composer')
    const field = page.locator('.chat-page .composer textarea')
    // Keep the original form handle so a remount cannot pass a completion check.
    const composer = await box.elementHandle()
    const measure = () => composer.evaluate(el => window.measureComposer(el))
    const waitForShape = open => page.waitForFunction(({ el, open }) =>
      el.isConnected && el.classList.contains('compact') === !open &&
      (document.activeElement === el.querySelector('textarea')) === open, { el: composer, open }, hangGuard)
    const changeShape = async (open, tap = false) => {
      if (tap) await field.tap()
      else await composer.evaluate((el, open) => el.querySelector('textarea')[open ? 'focus' : 'blur'](), open)
      await waitForShape(open)
    }
    const sampleAt = steps => composer.evaluate((el, { steps, sampleCount }) => {
      const active = window.composerAnimations(el).filter(animation => animation.playState === 'paused')
      const outline = active.find(animation => animation.effect.pseudoElement === '::before')
      if (!outline) throw new Error('Expected a paused composer outline animation')
      const { duration } = outline.effect.getTiming()
      return steps.map(step => {
        // Every part uses the same elapsed time, including the controls with a fade delay.
        for (const animation of active) animation.currentTime = step * duration / sampleCount
        return window.measureComposer(el)
      })
    }, { steps, sampleCount })
    const finishMorph = async () => {
      await composer.evaluate(el => {
        for (const animation of window.composerAnimations(el)) {
          if (animation.playState === 'paused') animation.finish()
        }
      })
      await page.waitForFunction(el => el.isConnected && !el.hasAttribute('data-morph'), composer, hangGuard)
      return measure()
    }
    // Use the real settled geometry for the endpoint, after the pseudo-element hands back the outline.
    const sampleMorph = async () => [...await sampleAt(sampleSteps), await finishMorph()]
    const smooth = (name, before, frames, to) => {
      const from = before.outline
      const samples = [before, ...frames]
      const heights = samples.map(frame => frame.outline)
      const steps = heights.slice(1).map((h, i) => h - heights[i])
      const growing = to > from
      check(`${name}: the box is morphing at each sample before completion`, frames.slice(0, -1).every(frame => frame.morphing))
      check(`${name}: the outline starts near the old size`, Math.abs(heights[1] - from) < Math.abs(to - from) * 0.5 + 1, `${heights[1]} from ${from}`)
      check(`${name}: the outline moves one way only`, steps.every(step => growing ? step >= -0.5 : step <= 0.5), JSON.stringify(heights.map(Math.round)))
      check(`${name}: no sample jumps more than 40% of the change`, steps.every(step => Math.abs(step) <= Math.abs(to - from) * 0.4), JSON.stringify(heights.map(Math.round)))
      check(`${name}: it ends at the new size and stops morphing`, Math.abs(heights.at(-1) - to) < 1 && !frames.at(-1).morphing, `${heights.at(-1)} vs ${to}`)
      const tops = samples.map(frame => frame.fieldTop), lefts = samples.map(frame => frame.fieldLeft)
      check(`${name}: the text field slides, it does not jump`, tops.slice(1).every((v, i) => Math.abs(v - tops[i]) <= 30) && lefts.slice(1).every((v, i) => Math.abs(v - lefts[i]) <= 12), JSON.stringify({ tops: tops.map(Math.round), lefts: lefts.map(Math.round) }))
    }
    check('the idle phone composer is the one-row pill', await composer.evaluate(el => el.classList.contains('compact')))
    const beforeOpen = await measure()
    const pill = beforeOpen.outline
    await changeShape(true, true)
    await page.setViewportSize(keyboard)
    const full = await composer.evaluate(el => el.getBoundingClientRect().height)
    const open = await sampleMorph()
    check('a tap opens the full box', full > pill + 20, `${pill} -> ${full}`)
    smooth('opening', beforeOpen, open, full)
    await page.screenshot({ path: path.join(process.env.SHOTS || '/tmp', 'composer-open.png') }).catch(() => {})
    // Controls that fade in take no taps until they show.
    await page.setViewportSize(phone)
    await changeShape(false)
    await finishMorph()
    await changeShape(true)
    const fading = await composer.evaluate(el => {
      const entering = [...el.querySelectorAll('[data-morph-in]')]
      const blocked = entering.filter(control => {
        const r = control.getBoundingClientRect()
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return !control.contains(hit)
      })
      return { entering: entering.length, blocked: blocked.length, inert: entering.filter(control => control.inert).length }
    })
    check('fading controls take no taps or focus', fading.entering > 0 && fading.blocked === fading.entering && fading.inert === fading.entering, JSON.stringify(fading))
    const beforeClose = await finishMorph()
    check('the controls take taps and focus after the morph', await composer.evaluate(el => !el.querySelector('[data-morph-in], .composer-controls [inert]')))
    const focusRing = await composer.evaluate(el => getComputedStyle(el).boxShadow !== 'none')
    await changeShape(false)
    const close = await sampleMorph()
    check('leaving the empty box closes it to the pill', await composer.evaluate(el => el.classList.contains('compact')))
    smooth('closing', beforeClose, close, pill)
    check('the outline and focus ring come back after the morph', focusRing && await composer.evaluate(el => getComputedStyle(el).borderTopColor !== 'rgba(0, 0, 0, 0)'))

    // Reverse at 1/12 progress, with enough distance from the previous target to detect a jump to it.
    const reverse = async opening => {
      await changeShape(opening)
      const [before] = await sampleAt([1])
      await changeShape(!opening)
      return [before, ...await sampleMorph()]
    }
    await changeShape(true, true)
    await finishMorph()
    const again = await reverse(false)
    check('the close is in the middle of its morph', again[0].morphing && again[0].outline > pill + 2 && again[0].outline < full - 2, JSON.stringify(again[0]))
    check('focus during the close ends open with no stuck morph', !(await composer.evaluate(el => el.classList.contains('compact'))) && Math.abs(again.at(-1).outline - full) < 1 && !again.at(-1).morphing, JSON.stringify(again.at(-1)))
    check('the reopen starts from the size on screen', again.slice(1, 3).every((frame, i) => Math.abs(frame.outline - again[i].outline) < 25), JSON.stringify(again.slice(0, 3)))
    await changeShape(false)
    await finishMorph()
    const back = await reverse(true)
    check('the open is in the middle of its morph', back[0].morphing && back[0].outline > pill + 2 && back[0].outline < full - 2, JSON.stringify(back[0]))
    check('a blur during the open starts the close from the size on screen', back.slice(1).every((frame, i) => Math.abs(frame.outline - back[i].outline) < 25), JSON.stringify(back.map(frame => Math.round(frame.outline))))
    check('the reversed open ends closed with no stuck morph', await composer.evaluate(el => el.classList.contains('compact')) && Math.abs(back.at(-1).outline - pill) < 1 && !back.at(-1).morphing, JSON.stringify(back.at(-1)))
    // A change that does not morph (here: reduced motion turned on mid-open) must still end the running morph.
    await changeShape(true)
    const [beforeInterrupt] = await sampleAt([1])
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await changeShape(false)
    const quick = await composer.evaluate(el => ({
      morphing: el.hasAttribute('data-morph'),
      border: getComputedStyle(el).borderTopColor,
      controlsRestored: !el.querySelector('[data-morph-in], [inert]'),
      paused: window.composerAnimations(el).some(animation => animation.playState === 'paused'),
    }))
    check('a change that skips the morph ends the running one, with the outline back', beforeInterrupt.morphing && !quick.morphing && !quick.paused && quick.controlsRestored && quick.border !== 'rgba(0, 0, 0, 0)', JSON.stringify(quick))
    // Reduced motion: the box changes at once.
    const morphState = () => composer.evaluate(el => ({
      morphing: el.hasAttribute('data-morph'),
      count: window.composerAnimations(el).length,
    }))
    const noMorphOnFocus = async (name, tap = false) => {
      const before = await morphState()
      await changeShape(true, tap)
      const after = await morphState()
      check(name, !before.morphing && !after.morphing && after.count === before.count, JSON.stringify({ before, after }))
    }
    await noMorphOnFocus('reduced motion: no morph frames', true)
    // Desktop width: the box has one shape, so nothing morphs.
    await changeShape(false)
    const desktop = { width: 1200, height: 800 }
    // Match the composer's ResizeObserver so focus starts from a measured desktop shape.
    const desktopLayout = await composer.evaluateHandle((el, width) => {
      const layout = { observed: false }
      const observer = new ResizeObserver(() => {
        if (innerWidth !== width) return
        layout.observed = true
        observer.disconnect()
      })
      observer.observe(el)
      return layout
    }, desktop.width)
    await page.setViewportSize(desktop)
    await page.waitForFunction(layout => layout.observed, desktopLayout, hangGuard)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await noMorphOnFocus('desktop: focus does not morph the box')
  } catch (error) { console.log("FAIL", error.stack); failed++ } finally {
    await context.close(); await browser.close(); server.close()
  }
  console.log(failed ? `composer morph: ${failed} failed` : 'composer morph: all checks pass')
  process.exit(failed ? 1 : 0)
})()
