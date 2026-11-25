// Browser responsive regression checks against the production app. All APIs use fixtures.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir, prefsStore } = require('../browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, '../dist'))
const SHOTS = shotsDir('responsive/tools')
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
let sessions = calm
const fixture = {
  chat: { messages: [], busy: false, thread: 't3', agent: 'claude', writable: true, session: '%3', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%3', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null }, 'work/board': { projects: [], items: [] },
}


let LONG = 'W'.repeat(80)
const SECOND = 'account_archive_'.repeat(7)
const size = { id: 'desktop', label: 'Desktop', width: 1024, height: 768, touch: false }
const tab = { id: 'b1', url: 'https://example.com/' + LONG, title: LONG, preset: 'desktop', mode: 'private', width: 1024, height: 768, touch: false }
let state = 'tabs'
let selectedBrowser = 'test'
fixture['browser/tabs'] = { tabs: [tab, { ...tab, id: 'b2' }], presets: [size, { ...size, id: 'tablet', label: 'Tablet' }, { ...size, id: 'phone', label: 'Phone' }], nav: { back: true, forward: true }, browsers: [{id: 'test', available: true}, {id: 'signed', available: true}, {id: 'chrome', available: true}] }
fixture['browser/agents'] = { enabled: true, available: true, reason: '', agents: [], asks: [] }
fixture['browser/extension/devices'] = [{id:'device',name:LONG,connected:true,lastSeen:NOW,chrome:'Chrome/123'}]
fixture['browser/extension/pair'] = { code: 'ABCDEFGH', expires: NOW + 300 }
fixture['browser/signed/access'] = { browser: 'signed', requests: [], grants: [], ended: [] }
fixture.host = { name: LONG, office: false }

async function main() {
  let browser, page, server, failed = 0
  const before = process.env.PHASE === 'before'
  const problems = []
  const check = (name, ok, why = '') => { console.log(`${ok ? 'ok' : 'FAIL'} ${name} ${why}`); if (!ok) { failed++; problems.push({name,why}) } }
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
    const context = browser.contexts()[0]
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => check('page error', false, error.message))
    await page.routeWebSocket('**/*', socket => socket.close())
    const prefs = prefsStore()
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({status:404,body:''})
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'prefs') return route.fulfill({ json: route.request().method() === 'GET' ? prefs.get() : prefs.post(route.request().postDataJSON()) })
        if (endpoint === 'stream' || endpoint.includes('/stream')) return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'sessions') return route.fulfill({ json: sessions })
        if (endpoint === 'browser/tabs') {
          if (state === 'error') return route.fulfill({status:500,json:{error:LONG+LONG}})
          if (state === 'loading') { await new Promise(resolve=>setTimeout(resolve,1000)); return route.fulfill({status:200,json:fixture['browser/tabs']}) }
          const which = url.searchParams.get('browser') || selectedBrowser
          return route.fulfill({json:{...fixture['browser/tabs'],tabs: state === 'empty' || which === 'chrome' ? [] : fixture['browser/tabs'].tabs.map(row=>({...row, mode:which==='signed'?'saved':row.mode}))}})
        }
        if (endpoint === 'browser/device') return route.fulfill({json:tab})
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if (!location.protocol.startsWith('http')) return
      const image=document.createElement('canvas');image.width=320;image.height=240
      const jpeg=image.toDataURL('image/jpeg').split(',')[1]
      class FixtureSource extends EventTarget {
        static CLOSED=2
        readyState=1
        constructor(url) { super(); if(url.includes('/browser/stream')) setTimeout(()=>this.dispatchEvent(new MessageEvent('frame',{data:JSON.stringify({seq:1,width:320,height:240,jpeg})})),40) }
        close() { this.readyState=2 }
      }
      window.EventSource=FixtureSource
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced','1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({voiceOff:true, voiceSound:false}))
    })
    const shot = async name => page.screenshot({path:path.join(SHOTS, `browser-${before?'before':'after'}-${name}.png`)})
    const open = async (width,height,which='test',next='tabs') => {
      state=next; selectedBrowser=which
      await page.waitForTimeout(150)
      await page.goto('about:blank')
      await page.setViewportSize({width,height})
      await page.goto(`${origin}/chat/?session=%253`, {waitUntil:'load'})
      await page.locator('.chat-panel').first().waitFor()
      await page.evaluate(which=>{localStorage.setItem('tether-browser-which',which);window.dispatchEvent(new CustomEvent('tether-browser-watch',{detail:{tab:'b1',pane:'%3',browser:which}}))},which)
      await page.locator('.browser-panel').first().waitFor()
      await page.waitForTimeout(200)
    }
    const bounds = async (name, root='.browser-panel') => {
      const results = await page.locator(root).first().evaluate(el=>{
        const r=el.getBoundingClientRect()
        const bad=[...el.querySelectorAll('button,input,.browser-menu')].filter(node=>{
          const b=node.getBoundingClientRect(), style=getComputedStyle(node)
          if (!b.width || !b.height || style.visibility==='hidden' || node.closest('.browser-tabs') && !node.closest('.browser-menu')) return false
          return b.left<r.left-1 || b.right>r.right+1 || b.left<0 || b.right>innerWidth
        }).map(n=>({text:n.getAttribute('aria-label')||n.textContent,left:n.getBoundingClientRect().left,right:n.getBoundingClientRect().right}))
        const menus=[...el.querySelectorAll('.browser-menu')].filter(node=>{const b=node.getBoundingClientRect();return b.top<0||b.bottom>innerHeight+1}).map(n=>n.textContent)
        const controls=[...el.querySelectorAll('button,input')].filter(n=>{const b=n.getBoundingClientRect();return b.width&&b.height&&!n.closest('.browser-tab')&&(!el.querySelector('.browser-menu')||n.closest('.browser-menu'))})
        const small=controls.filter(n=>{const b=n.getBoundingClientRect();return b.width<24||b.height<24}).map(n=>n.getAttribute('aria-label')||n.textContent)
        const clipped=controls.filter(n=>n.tagName==='BUTTON'&&n.scrollWidth>n.clientWidth+1&&getComputedStyle(n).textOverflow!=='ellipsis'&&![...n.querySelectorAll('*')].some(c=>getComputedStyle(c).textOverflow==='ellipsis')).map(n=>n.getAttribute('aria-label')||n.textContent)
        const overlap=controls.some((n,i)=>controls.slice(i+1).some(m=>{const a=n.getBoundingClientRect(),b=m.getBoundingClientRect();return Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1}))
        return {page:document.documentElement.scrollWidth<=innerWidth,bad,menus,small,clipped,overlap}
      })
      check(name+' page fits',results.page)
      check(name+' controls fit',!results.bad.length,JSON.stringify(results.bad))
      check(name+' menus fit vertically',!results.menus.length)
      check(name+' touch targets at least 24',!results.small.length,JSON.stringify(results.small))
      check(name+' control labels fit',!results.clipped.length,JSON.stringify(results.clipped))
      check(name+' controls do not overlap',!results.overlap)
    }
    for(const [width,height] of [[320,640],[390,844],[740,360],[768,1024],[1024,768],[1440,900]]) {
      await open(width,height)
      await bounds(`${width} test`)
      await shot(`${width}-test`)
      await page.getByRole('button',{name:'Browser: Test',exact:true}).click()
      await bounds(`${width} switch menu`)
      await shot(`${width}-menu`)
      await page.getByRole('button',{name:/^Browser:/}).click()
      await page.waitForTimeout(150)
      await open(width,height,'signed')
      await bounds(`${width} signed`)
      await shot(`${width}-signed`)
    }
    await open(1440,900)
    await page.getByRole('button',{name:'Open the browser in its own window',exact:true}).click()
    await page.locator('.browser-window').waitFor()
    await bounds('floating window','.browser-window')
    await shot('floating-window')
    const grip=page.getByRole('button',{name:'Move the browser window.',exact:false})
    await grip.focus()
    await page.keyboard.press('ArrowLeft')
    await page.keyboard.press('Shift+ArrowLeft')
    await bounds('floating keyboard move and resize','.browser-window')
    await page.getByRole('button',{name:'Close the browser window',exact:true}).click()
    await open(320,640)
    await page.getByRole('button',{name:'Browser: Test',exact:true}).click()
    await page.keyboard.press('Escape')
    check('Escape leaves the browser sheet open',await page.locator('.browser-sheet[open]').count()===1)
    if(!await page.locator('.browser-sheet[open]').count()) await page.getByRole('button',{name:'Open the browser',exact:true}).click()
    await page.getByRole('button',{name:'New tab',exact:true}).click()
    await bounds('new tab menu')
    await shot('new-tab-menu')
    await page.getByRole('button',{name:'New tab',exact:true}).click()
    await page.waitForTimeout(150)
    await page.locator('.browser-sheet').evaluate(el=>{el.style.width='180px'})
    await bounds('180 test')
    await shot('180-test')
    await page.getByRole('button',{name:'Browser: Test',exact:true}).click()
    await bounds('180 switch menu')
    await shot('180-menu')
    await page.getByRole('button',{name:/^Browser:/}).click()
      await page.waitForTimeout(150)
    await page.locator('.browser-icon[aria-label^="Device:"]').click()
    await bounds('180 device menu')
    await shot('180-device')
    await page.locator('.browser-icon[aria-label^="Device:"]').click()
    await page.waitForTimeout(150)
    await page.locator('.browser-sheet').evaluate(el=>{el.style.top='360px'})
    await page.getByRole('button',{name:'Browser: Test',exact:true}).click()
    await bounds('180 low panel menu')
    await shot('180-low-menu')
    await open(320,640)
    await page.getByRole('button',{name:'Add notes to this frame',exact:true}).click()
    await bounds('notes toolbar')
    await shot('notes')
    for(const tool of ['Point','Pen','Rectangle','Text']) await page.getByRole('button',{name:tool,exact:true}).click()
    const marks=page.locator('.browser-marks')
    const markBox=await marks.boundingBox()
    await marks.dispatchEvent('pointerdown',{clientX:markBox.x+markBox.width-2,clientY:markBox.y+2,pointerId:1,pointerType:'mouse',button:0,buttons:1})
    await page.locator('.browser-label').waitFor({state:'attached'})
    await bounds('note text near frame edge')
    await shot('note-text-edge')
    if(!before) await page.getByRole('textbox',{name:'Note text',exact:true}).fill(SECOND)
    await page.keyboard.press('Enter')
    await page.locator('.browser-sheet').evaluate(el=>{el.style.width='180px'})
    await bounds('180 notes toolbar')
    await shot('180-notes')
    await page.getByRole('button',{name:'Close notes',exact:true}).click()
    await open(320,640,'test','empty')
    await bounds('empty browser')
    await shot('empty')
    await open(320,640,'test','loading')
    await bounds('starting browser')
    await shot('starting')
    await open(320,640,'test','error')
    await bounds('error')
    await shot('error')
    await open(320,640,'chrome')
    await bounds('pairing')
    await shot('pairing')
    await page.getByRole('button',{name:'Pair another browser',exact:true}).click()
    await bounds('pairing code')
    await shot('pairing-code')
    await page.locator('.browser-sheet').evaluate(el=>{el.style.width='180px'})
    await bounds('180 pairing')
    await shot('180-pairing')
    // The status cards render real asks and grants, without a browser overlay over them.
    for (const variant of ['ask','grant','ended']) {
      fixture['browser/signed/access'] = {browser:'signed', requests:[], grants:[], ended:[]}
      if (variant==='ask') fixture['browser/signed/access'].requests=[{id:'ask',pane:'%3',agent:LONG,reason:LONG,minutes:123,url:'https://'+LONG+'.example.com',created:NOW,browser:'server'}]
      if (variant==='grant') fixture['browser/signed/access'].grants=[{id:'grant',pane:'%3',agent:'Codex',tab:'b1',expires:NOW+600,reason:LONG,browser:'server'}]
      if (variant==='ended') fixture['browser/signed/access'].ended=[{id:'ended',pane:'%3',agent:'Codex',reason:'ended',message:LONG+LONG,at:NOW,browser:'server'}]
      for (const [width,height] of [[320,640],[390,844],[740,360],[768,1024],[1024,768],[1440,900]]) {
        await page.waitForTimeout(150)
        await page.goto('about:blank')
        await page.setViewportSize({width,height})
        await page.goto(`${origin}/chat/?session=%253`, {waitUntil:'load'})
        const chip=page.locator('.status-chip',{hasText:variant==='ask'?'Wants your signed-in browser':variant==='grant'?'Using your signed-in browser':'Access ended'}).first()
        await chip.waitFor()
        if(await chip.getAttribute('aria-expanded')!=='true') await chip.click()
        await page.locator('.agent-browser-card:visible').first().waitFor()
        await bounds(`${width} agent ${variant}`, '.agent-browser-card:visible')
        const card=page.locator('.agent-browser-card:visible').first()
        check(`${width} agent ${variant} content fits`,await card.evaluate(el=>el.scrollWidth<=el.clientWidth+1))
        if(variant==='ended') check(`${width} ended reason wraps`,await card.locator('small').evaluate(el=>el.scrollWidth<=el.clientWidth+1))
        if(variant==='grant') check(`${width} grant title stays readable`,await card.locator('.agent-browser-tabs span').evaluate(el=>el.clientWidth>=100))
        await shot(`${width}-agent-${variant}`)
        if(width===320) {
          await card.evaluate(el=>el.style.width='144px')
          await bounds(`180 agent ${variant}`, '.agent-browser-card:visible')
          check(`180 agent ${variant} content fits`,await card.evaluate(el=>el.scrollWidth<=el.clientWidth+1))
          await shot(`180-agent-${variant}`)
        }
      }
    }
    // A different unbroken token checks the same contracts without relying on one glyph's width.
    LONG=SECOND
    fixture['browser/tabs'].tabs=fixture['browser/tabs'].tabs.map(row=>({...row,title:SECOND,url:'https://example.com/'+SECOND}))
    await open(320,640,'test','error')
    await bounds('second error token')
    await shot('second-error')
    await open(320,640)
    await page.locator('.browser-sheet').evaluate(el=>{el.style.width='180px'})
    await page.getByRole('button',{name:'Browser: Test',exact:true}).click()
    await bounds('second token 180 menu')
    await shot('second-menu')
    fixture['browser/signed/access']={browser:'signed',requests:[],ended:[],grants:[{id:'second-grant',pane:'%3',agent:'Codex',tab:'b1',expires:NOW+600,reason:SECOND,browser:'chrome'}]}
    await page.goto('about:blank')
    await page.goto(`${origin}/chat/?session=%253`,{waitUntil:'load'})
    const secondChip=page.locator('.status-chip',{hasText:'Using your Chrome'}).first()
    await secondChip.click()
    await bounds('second grant token','.agent-browser-card:visible')
    const secondCard=page.locator('.agent-browser-card:visible').first()
    check('second grant title stays readable',await secondCard.locator('.agent-browser-tabs span').evaluate(el=>el.clientWidth>=100))
    await secondCard.evaluate(el=>el.style.width='144px')
    await bounds('second grant 180','.agent-browser-card:visible')
    await shot('second-grant')
    await fs.writeFile(path.join(SHOTS,`browser-${before?'before':'after'}-results.json`),JSON.stringify({failed,problems},null,2))
    if(failed && !before) process.exitCode=1
  } finally {
    if(page) await page.close()
    if(browser) await browser.close()
    if(server) await new Promise(resolve=>server.close(resolve))
  }
}
main().catch(error=>{console.error(error);process.exitCode=1})
