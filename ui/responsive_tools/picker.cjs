// Sidebar views: Catch up (what waits on you, by goal) and the Sessions tree with need-you counts and Active only.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('../browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, '../dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../../.release-work/2026-11-16/responsive/tools/shots')
const NOW = Math.floor(Date.now() / 1000)
const ask = (pane, ago, extra = {}) => ({ pane, thread: null, since: NOW - ago, next: `Next for ${pane}`, kind: 'reply', opened: false, snoozed_until: null, ...extra })
const pane = (id, session, name, label, extra) => ({ id, tmux_session: session, label, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const busy = {
  sessions: [
    pane('%1', '$1', 'Release notes', 'tether:0.1', { group: 'g1', ask: ask('%1', 3 * 3600) }),
    pane('%2', '$1', 'Deploy check', 'tether:0.2', { group: 'g1', ask: ask('%2', 1800, { urgent: true, opened: true }) }),
    pane('%3', '$1', 'Sidebar build', 'tether:0.3', { state: 'Working', since: NOW - 300 }),
    pane('%4', '$1', 'Old idea', 'tether:0.4', { group: 'g2' }),
    pane('%5', '$1', 'Older idea', 'tether:0.5', { group: 'g2' }),
    pane('%6', '$1', 'Voice fix', 'tether:0.6', { finished: NOW - 600 }),
    pane('%10', '$2', 'BriskBot', 'briskbot', { state: 'Approval', since: NOW - 2 * 3600 }),
    pane('%20', '$3', 'Notes one', 'notes:0.0', {}), pane('%21', '$3', 'Notes two', 'notes:0.1', {}),
    pane('%30', '$4', 'Scratch', 'scratch', {}),
  ],
  default_thread: 't3', groups: { $1: [{ id: 'g1', name: 'Release', project: '' }, { id: 'g2', name: 'Quiet stack', project: '' }] },
}
const calm = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%30', '$4', 'Scratch', 'scratch', {})], default_thread: 't3' }
const LONG = process.env.SECOND ? '第二个很长的名称'.repeat(14) : 'Unbroken'.repeat(12)
const fullSessions = {sessions:[pane('%3', '$1', LONG, LONG, {dir:'/workspace/'+LONG}),pane('%4', '$2', LONG, LONG, {dir:'/workspace/'+LONG})],default_thread:'t3'}
const noSessions = {sessions:[],default_thread:null}
let sessions = fullSessions
let history = { mode: 'full' }
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
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser({ local: true })
    const context = await browser.newContext({hasTouch:!!process.env.SECOND,serviceWorkers:'block'})
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    if (process.env.DEBUG) page.on('console', message => { if (message.type() === 'error') console.log('console:', message.text().slice(0, 600)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('history')) {
          const { mode, pending } = history
          if (pending) await pending
          if (mode === 'error') return route.fulfill({status:503,json:{error:LONG}})
          return route.fulfill({json:{chats:mode==='empty'?[]:[{id:'old1',agent:'claude',title:LONG,dir:'/workspace/'+LONG,time:new Date().toISOString()}],total:123,next:null,counts:{all:123,claude:123,codex:0},dirs:Array.from({length:12},(_,i)=>({dir:'/workspace/'+LONG+i,count:123-i}))}})
        }
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'sessions') return route.fulfill({ json: sessions })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // The shared browser may run on another host: serve dist from here, not through the local server.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
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

    for (const [width,height] of [[320,640],[390,844],[740,360],[768,1024],[1024,768],[1440,900]]) {
      await page.setViewportSize({width,height})
      await page.goto(`${origin}/chat/?go=new&session=%253&prefs=${encodeURIComponent(JSON.stringify({theme:process.env.SECOND?'dark':'light',look:{layout:'classic',surface:'glass',backdrop:'dusk'}}))}`)
      await page.locator('.chat-panel').first().waitFor()
      check(`glass ${width}`,await page.locator('.app-shell').getAttribute('data-surface')==='glass')
      await page.getByRole('tab',{name:'Open existing',exact:true}).click()
      await page.locator('.cp-title').first().waitFor()
      const cp=page.locator('.cp')
      const sessionClip=await cp.locator('.cp-session').first().evaluate(el=>{const range=document.createRange();range.selectNodeContents(el);if(range.getBoundingClientRect().right<=el.getBoundingClientRect().right+1)return true;return [el,...el.querySelectorAll('*')].some(n=>{const s=getComputedStyle(n);return n.scrollWidth>n.clientWidth&&s.textOverflow==='ellipsis'&&!s.display.includes('flex')})})
      check(`session ellipsis ${width}`,sessionClip)
      await cp.locator('.cp-main').first().hover()
      const faults=await cp.evaluate(root=>{
        const faults=[]
        if(root.closest('dialog').scrollWidth>root.closest('dialog').clientWidth+1) faults.push('dialog overflow')
        for(const el of root.querySelectorAll('button,label')) {
          const r=el.getBoundingClientRect(),s=getComputedStyle(el)
          if(r.width && (r.width<24||r.height<24)) faults.push('target '+el.className+' '+r.width+'x'+r.height)
          if(r.width && el.scrollWidth>el.clientWidth+1 && s.textOverflow!=='ellipsis') faults.push('clipped '+el.className)
        }
        const move=root.querySelector('.cp-move'),end=root.querySelector('.cp-end')
        if(move&&end){const a=move.getBoundingClientRect(),b=end.getBoundingClientRect();if(a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top)faults.push('move overlaps status')}
        return faults
      })
      check(`picker ${width}`,!faults.length,JSON.stringify(faults))
      check(`page ${width}`,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      await page.screenshot({path:path.join(SHOTS,`${process.env.PHASE||'after'}-picker-${width}.png`)})
      await cp.getByRole('button',{name:/^Folder:/}).click()
      const folder=page.locator('.sm-panel:visible')
      await folder.waitFor()
      check(`folder menu ${width}`,await folder.evaluate(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1&&el.scrollWidth<=el.clientWidth+1}))
      await page.screenshot({path:path.join(SHOTS,`${process.env.PHASE||'after'}-picker-folders-${width}.png`)})
      await page.getByRole('combobox',{name:'Search folders',exact:true}).fill('no-such-folder')
      await page.getByText('No folders match.',{exact:true}).waitFor()
      await page.keyboard.press('Escape')
      await folder.waitFor({state:'hidden'})
      // Long server failures, the initial skeleton, and an empty result use the real picker.
      // Switch tabs to remount it without navigating away from an open dialog.
      const newSession = page.getByRole('dialog',{name:'New session',exact:true})
      const openExisting = newSession.getByRole('tab',{name:'Open existing',exact:true})
      const retry = cp.getByRole('button',{name:'Retry',exact:true})
      for(const mode of ['error','loading','empty']) {
        await newSession.getByRole('tab',{name:'New',exact:true}).click()
        await cp.waitFor({state:'detached'})
        const loading = mode === 'loading' ? Promise.withResolvers() : null
        history = { mode, pending: loading?.promise }
        sessions = mode === 'error' ? fullSessions : noSessions
        try {
          await openExisting.click()
          if(mode==='error') {
            await retry.waitFor()
            const errors=await cp.locator('.cp-list').evaluate(root=>({w:root.scrollWidth,c:root.clientWidth}))
            check(`error ${width}`,errors.w<=errors.c+1,JSON.stringify(errors))
            check(`retry target ${width}`,await retry.evaluate(el=>{const r=el.getBoundingClientRect();return r.width>=24&&r.height>=24}))
            const attempt = Promise.withResolvers()
            history = { mode, pending: attempt.promise }
            try {
              await retry.click()
              await cp.getByRole('button',{name:'Trying again…',exact:true}).waitFor()
            } finally {
              attempt.resolve()
            }
            await retry.waitFor()
          }
          if(mode==='loading') {
            await cp.locator('.skeleton .sk-label').waitFor()
            check(`skeleton ${width}`,await cp.locator('.skeleton').count()>0 && await cp.locator('.sk-label').count()>0)
          }
          if(mode==='empty') {
            await cp.locator('.cp-empty').waitFor()
            check(`empty ${width}`,await cp.locator('.cp-empty').count()>0)
          }
          await page.screenshot({
            path:path.join(SHOTS,`${process.env.PHASE||'after'}-picker-${mode}-${width}.png`),
            animations:'disabled',
          })
        } finally {
          loading?.resolve()
        }
        if(loading) await cp.locator('.cp-title').first().waitFor()
      }
      history = { mode: 'full' }
      sessions = fullSessions
      await page.keyboard.press('Escape')
      // A tab picker has a Move action. Hover/focus must reserve its space beside the status.
      await page.goto(`${origin}/chat/?session=%253&prefs=${encodeURIComponent(JSON.stringify({theme:process.env.SECOND?'dark':'light',look:{layout:'classic',surface:'glass',backdrop:'dusk'}}))}`)
      await page.locator('.tab-add').first().click()
      await page.getByRole('tab',{name:'Open existing',exact:true}).click()
      await page.locator('.cp-move').first().waitFor()
      await page.locator('.cp-move').first().focus()
      await page.locator('.cp-list li').first().hover()
      check(`move status separation ${width}`,await page.locator('.cp-move').first().evaluate(move=>{const end=move.parentElement.querySelector('.cp-end'),a=move.getBoundingClientRect(),b=end.getBoundingClientRect();return a.left>=b.right||a.top>=b.bottom||b.top>=a.bottom}))
      await page.screenshot({path:path.join(SHOTS,`${process.env.PHASE||'after'}-picker-move-${width}.png`)})
      await page.keyboard.press('Escape')
      const cdp=await context.newCDPSession(page)
      const viewportMeta=page.locator('meta[name="viewport"]')
      const originalViewport=await viewportMeta.getAttribute('content')
      for(const scale of [2,4]) {
        await cdp.send('Emulation.setPageScaleFactor',{pageScaleFactor:scale})
        // The viewport event schedules a frame before the badge updates.
        const badge=page.getByRole('button',{name:`Zoomed to ${scale*100}%. Reset to 100%.`,exact:true})
        await badge.waitFor()
        check(`zoom ${scale} ${width}`,await badge.evaluate(el=>{const r=el.getBoundingClientRect(),v=visualViewport;return el.matches(':popover-open')&&r.left>=v.offsetLeft&&r.right<=v.offsetLeft+v.width+1&&r.top>=v.offsetTop&&r.bottom<=v.offsetTop+v.height+1}))
        if(width===320)await page.screenshot({path:path.join(SHOTS,`${process.env.PHASE||'after'}-zoom-${scale}-${width}.png`)})
        await badge.click()
        // Reset hides the badge or changes its label to the pinch-out instruction.
        await badge.waitFor({state:'hidden'})
        await page.waitForFunction(content=>document.querySelector('meta[name="viewport"]').content===content,originalViewport)
      }
      await cdp.send('Emulation.setPageScaleFactor',{pageScaleFactor:1})
      await cdp.detach()

    }
    check('no page errors',!pageErrors.length,JSON.stringify(pageErrors))
  } finally { if(page)await page.close();if(browser)await browser.close();if(server)await new Promise(done=>server.close(done)) }
  if(failed)process.exitCode=1
}
main().catch(error=>{console.error(error);process.exitCode=1})
