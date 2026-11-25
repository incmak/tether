// Responsive nav sweep. Every API request stays in fixtures; no terminal is touched.
// Run: node test_responsive_nav_ui.cjs. DIST selects a build; PHASE names the screenshots.
// App.css findings outside this lane stay explicit in the report. Existing 12px resize
// dividers also remain a reported limitation; the test does not call them compliant.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('responsive/nav')
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
let sessions = busy
const failures = {}, delays = {}
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
    const context = process.env.COARSE ? await browser.newContext({hasTouch:true,isMobile:true}) : browser.contexts()[0]
    if (!context) throw new Error('Local browser has no context')
    page = await context.newPage()
    page.setDefaultTimeout(3000)
    await page.emulateMedia({reducedMotion:'reduce', colorScheme:'light'})
    page.on('pageerror', error => pageErrors.push(error.stack))
    if (process.env.DEBUG) page.on('console', message => { if (message.type() === 'error') console.log('console:', message.text().slice(0, 600)) })
    await context.routeWebSocket('**/*', socket => socket.close())
    await context.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (delays[endpoint]) await new Promise(resolve=>setTimeout(resolve,delays[endpoint]))
        if (failures[endpoint]) return route.fulfill({status:500,json:{error:failures[endpoint]}})
        if (endpoint === 'sessions') return route.fulfill({ json: sessions })
        return route.fulfill({ json: fixture[endpoint.split('?')[0]] || {} })
      }
      // Serve only this build; all other non-API requests are blocked.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        try { return route.fulfill({ status: 200, contentType: mime[path.extname(file)] || 'application/octet-stream', body: await fs.readFile(file) }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await context.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      // A reload keeps what the page stored; a fresh open starts from these prefs.
      const asked = new URL(location.href).searchParams.get('prefs')
      if (asked === null || sessionStorage.getItem('kept') === asked) return
      sessionStorage.setItem('kept', asked)
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: false, voiceSound: false, ...JSON.parse(asked) }))
    })
    const phase = process.env.PHASE || 'after'
    const LONG = 'Unbroken'.repeat(10), findings = []
    const viewports = [[320,640],[390,844],[740,360],[768,1024],[1024,768],[1440,900]].filter(([w])=>!process.env.WIDTH||w===Number(process.env.WIDTH))
    fixture.host={name:LONG,office:false}
    fixture.history={chats:[],total:0,next:null,counts:{all:0,claude:0,codex:0},dirs:[]}
    fixture['task-models'] = { claude: [{ id:'opus', label: LONG }], codex: [{ id:'gpt', label: LONG }] }
    fixture['worktree-info'] = { repo: true, base: '/srv/worktrees/' + LONG }
    fixture['suggest-groups']={groups:[{name:LONG,sessions:['$1','$2']}],source:'folder'}
    fixture['tab-brief'] = { brief: { description: LONG, state: 'done' }, enabled: true, backlog: [{ id:'B1',title:LONG,state:'doing' }] }
    fixture['files/list'] = { path:'/srv', parent:'/', entries:[{ name:LONG, dir:true,secret:false,hidden:false }], truncated:false }
    fixture['files/places'] = { home:'~',sessions:[],pinned:[] }
    fixture['projects/backlog'] = { text: `## ${LONG}\n- [ ] ${LONG} #B1\n  - ${LONG}\n- [>] Working #B2\n- [x] Done #B3\n`, version:'v1', limit:200000, next_id:4 }
    const project = { name: 'Project'.repeat(4), prefix:'P', folder:'/srv/'+LONG, triage:'', work:'', feeds:[],jira:'',qaStatus:'',jiraUser:'' }
    fixture['work/board'] = { revision:1,stages:[],jira:{connected:false},feeds:{},projects:[project,{...project,name:'Second'}],items:[{id:'P-001',project:project.name,title:LONG,stage:'ready',qa:false,ask:null,askFrom:null,refs:[],tabs:[],history:[],source:'',created:NOW,updated:NOW,doneAt:null,jira:null,version:1}],backlog:{items:[],error:''} }
    sessions = { sessions:[pane('%3','$1',LONG,'Fixture:0.3',{reviewers:['%4']}), ...Array.from({length:30},(_,i)=>pane(`%${100+i}`,'$1',LONG+i,`Fixture:1.${i}`,{state:'Approval',group:'g1',ask:ask(`%${100+i}`,60+i)})),pane('%5','$1',LONG+'-second','Fixture:0.5',{}),pane('%4','$2','Reviewer'+LONG,'Second:0.0',{})],groups:{$1:[{id:'g1',name:LONG,project:''}]},default_thread:'t3' }
    let serial = 0
    const open = async (width, height, prefs = {}, go = '') => {
      // Each navigation starts a fresh page; the long sweep does not retain a history of app documents.
      if(page.url()!=='about:blank') {
        await page.close()
        page=await context.newPage()
        page.setDefaultTimeout(3000)
        page.on('pageerror',error=>pageErrors.push(error.stack))
        await page.emulateMedia({reducedMotion:'reduce',colorScheme:'light'})
      }
      await page.setViewportSize({width,height})
      await page.goto(`${origin}/chat/?session=%253&go=${go}&prefs=${encodeURIComponent(JSON.stringify({sideMode:'sessions', attention:false, testSerial:++serial,...prefs}))}`)
      await page.locator('.sidebar-inner').first().waitFor({state:'attached'})
      await page.waitForTimeout(160)
      if (await page.locator('dialog.action-deck[open]').count()) await page.keyboard.press('Escape')
    }
    const inspect = async (name, selector, extra, outsideLane = '') => {
      const root = page.locator(selector).filter({visible:true}).first()
      await root.waitFor({state:'visible'})
      await page.waitForTimeout(80)
      const bad = await root.evaluate(el => {
        const out = [], viewport = {width:innerWidth,height:innerHeight}
        const visible = n => {const b=n.getBoundingClientRect();return b.width&&b.height&&getComputedStyle(n).visibility!=='hidden'}
        const box = el.getBoundingClientRect()
        if (box.left < -.5 || box.right > viewport.width+.5 || box.top < -.5 || box.bottom > viewport.height+.5) out.push('root leaves viewport')
        if (document.documentElement.scrollWidth > innerWidth) out.push('page horizontal scroll')
        if (!el.matches('.app-shell') && el.scrollWidth > el.clientWidth+1) out.push(`root overflow ${el.scrollWidth}/${el.clientWidth}`)
        const controls = [...el.querySelectorAll(el.matches('.app-shell') ? '.panel-close, .panel-reopen, .panel-grip, .app-rail button' : 'button, [role=option], input, select, textarea, a[href]')].filter(visible)
        for (const n of controls) {
          const r=n.getBoundingClientRect(), s=getComputedStyle(n)
          // Off-screen items in scroll lists are intentionally reachable by scrolling.
          if (r.top < box.top || r.bottom > box.bottom) continue
          let scrollParent=n.parentElement, scrolls=false
          while(scrollParent && scrollParent!==el) {if(['auto','scroll'].includes(getComputedStyle(scrollParent).overflowX)){scrolls=true;break}scrollParent=scrollParent.parentElement}
          if (!scrolls && (r.left < box.left-.5 || r.right > box.right+.5)) out.push(`control outside: ${n.className}`)
          if (!/INPUT|TEXTAREA|SELECT/.test(n.tagName) && n.textContent.trim() && !n.querySelector('.visually-hidden') && n.scrollWidth > n.clientWidth+1 && s.textOverflow!=='ellipsis') out.push(`control clips: ${n.className} ${n.scrollWidth}/${n.clientWidth}`)
          if (n.matches('input[role=switch]')) continue
          if (n.matches('.panel-grip')) continue // 12px existing divider needs dedicated layout space; see report.
          const pseudo=getComputedStyle(n,'::before'), hitWidth=parseFloat(pseudo.width)||r.width, hitHeight=parseFloat(pseudo.height)||r.height
          if(n.tagName==='INPUT' && n.closest('label')?.getBoundingClientRect().height>=24) continue
          if (Math.max(r.width,hitWidth) < 23.5 || Math.max(r.height,hitHeight) < 23.5) out.push(`small target: ${n.className} ${r.width}x${r.height}`)
        }
        for (const n of el.querySelectorAll(el.matches('.app-shell') ? '.panel-close, .panel-reopen' : 'button')) {
          const sib=n.nextElementSibling
          if (!visible(n)||!sib?.matches('button')||!visible(sib)) continue
          const a=n.getBoundingClientRect(),b=sib.getBoundingClientRect()
          if (Math.min(a.right,b.right)-Math.max(a.left,b.left)>.5 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>.5) out.push('sibling controls overlap')
        }
        return [...new Set(out)]
      })
      if (extra) bad.push(...await extra(root))
      const shot = `${phase}-${name}.png`
      await page.screenshot({path:path.join(SHOTS,shot)})
      findings.push({name,selector,bad,shot,...outsideLane?{outsideLane}:{}})
      if(outsideLane && bad.length) console.log(`OUTSIDE LANE ${name}: ${outsideLane}: ${bad.join('; ')}`)
      else check(name,bad.length===0,bad.join('; '))
    }
    const run = async (name, fn) => { try { await fn() } catch(e) {check(name,false,e.message.split('\n')[0]); findings.push({name,error:e.message}); await page.screenshot({path:path.join(SHOTS,`${phase}-${name}-error.png`)}).catch(()=>{})} }
    const close = async () => { await page.keyboard.press('Escape'); await page.waitForTimeout(100) }
    const longSelectGroups=async()=> {
      const original=sessions
      for(const [width,name] of [[320,LONG],[390,'M'.repeat(80)]]) {
        sessions={...original,sessions:original.sessions.map(pane=>pane.tmux_session==='$1'?{...pane,label:name+':'+pane.label.split(':').at(-1)}:pane)}
        await open(width,844)
        await page.keyboard.press('Control+k')
        await page.locator('.command-menu input').fill('q'.repeat(80))
        await page.locator('.command-row').last().click()
        await inspect(`select-long-group-${width}`,'.sm-panel',async root=>root.locator('.sm-list').evaluate(el=>el.scrollWidth>el.clientWidth+1?['select group heading scrolls horizontally']:[]))
      }
      sessions=original
    }
    const shortBacklogBody=async()=> {
      const original=fixture['projects/backlog']
      try { for(const [width,height] of [[740,360],[800,320]]) {
        fixture['projects/backlog']={...original,text:''}
        await open(width,height,{},'backlog')
        await page.locator('.backlog-empty').scrollIntoViewIfNeeded()
        await inspect(`backlog-short-empty-${width}`,'.backlog-page',async()=>page.locator('.backlog').evaluate(el=>getComputedStyle(el).overflowY!=='auto'||el.scrollTop===0?['body does not scroll the pinned tools away']:[]))
        fixture['projects/backlog']={...original,text:Array.from({length:18},(_,i)=>`- [ ] Task ${i+1} #B${i+1}`).join('\n')}
        await open(width,height,{},'backlog')
        const last=page.locator('.backlog-row').last()
        await last.hover()
        await last.getByRole('button',{name:/Reword/}).click()
        await inspect(`backlog-short-reword-${width}`,'.backlog-page')
        await page.locator('.backlog-reword').press('Escape')
        await page.locator('.backlog-search input').fill('q'.repeat(80))
        await page.locator('.backlog-empty').waitFor()
        await page.locator('.backlog-empty').getByRole('button',{name:'Clear filters',exact:true}).scrollIntoViewIfNeeded()
        await inspect(`backlog-short-no-match-${width}`,'.backlog-page',async()=>page.locator('.backlog-empty').evaluate(el=>el.scrollWidth>el.clientWidth+1?['no-match query overflows']:[]))
        await page.locator('.backlog-empty').getByRole('button',{name:'Clear filters',exact:true}).click()
        check(`backlog-short-clear-${width}`,await page.locator('.backlog-row').count()===18)
        await page.getByRole('button',{name:'Edit as text',exact:true}).click()
        await page.locator('.backlog-text').scrollIntoViewIfNeeded()
        await inspect(`backlog-short-text-${width}`,'.backlog-page')
      }
      } finally { fixture['projects/backlog']=original }
    }
    if(process.env.BACKLOG_ONLY) {
      await run('short-backlog-body',shortBacklogBody)
      await fs.writeFile(path.join(SHOTS,`${phase}-short-backlog.json`),JSON.stringify(findings,null,2))
      if(failed) process.exitCode=1
      return
    }
    if(process.env.SELECT_ONLY) {
      await run('select-long-groups',longSelectGroups)
      await fs.writeFile(path.join(SHOTS,`${phase}-select.json`),JSON.stringify(findings,null,2))
      if(failed) process.exitCode=1
      return
    }
    if(process.env.ERROR_ONLY) {
      for(const [endpoint,go,selector,label,name] of [
        ['sessions','work','.home','Try again','home-long-error-second-input'],
        ['projects/backlog','backlog','.backlog-page','Dismiss','backlog-long-error-second-input'],
      ]) await run(name,async()=> {
        failures[endpoint]='ServerCouldNotReadThisData'.repeat(30)
        await open(740,360,{},go)
        await page.getByRole('button',{name:label,exact:true}).scrollIntoViewIfNeeded()
        await inspect(name,selector,async root=>{
          const outer=await root.boundingBox(),button=await page.getByRole('button',{name:label,exact:true}).boundingBox()
          return button.y+button.height>outer.y+outer.height+1?[`${label} cannot scroll into view`]:[]
        })
        delete failures[endpoint]
      })
      await fs.writeFile(path.join(SHOTS,`${phase}-errors.json`),JSON.stringify(findings,null,2))
      if(failed) process.exitCode=1
      return
    }
    if(!process.env.EXTENDED) for (const [w,h] of viewports) await run(`views-${w}`, async()=> {
      await open(w,h)
      if(w<900) await page.locator('.mobile-menu:visible').first().click()
      const side = w<900 ? '.drawer' : '.sidebar'
      await inspect(`sessions-${w}`,side)
      await page.locator(`${side} .session-main`).first().click({button:'right'})
      await inspect(`session-menu-${w}`,'dialog.sheet[open]:not(.drawer)')
      await close()
      if(w<900 && !await page.locator('dialog.drawer[open]').count()) await page.locator('.mobile-menu:visible').first().click()
      const stackPane=page.locator(`${side} [data-select-pane="%100"]`)
      if(!await stackPane.count()) await page.locator(`${side} .pane-stack-head`).filter({hasText:LONG}).click()
      await stackPane.click({button:'right'})
      await inspect(`row-menu-${w}`,'dialog.sheet[open]:not(.drawer)',undefined,'App.css: shrink and wrap the pane title; ellipsize the stack name in the action row')
      await page.getByRole('button',{name:'Link a review…',exact:true}).click()
      await inspect(`review-picker-${w}`,'.review-picker')
      await page.getByRole('radio',{name:'Reviewed by',exact:true}).click()
      await inspect(`review-picker-other-${w}`,'.review-picker')
      await close()
      if(w<900 && !await page.locator('dialog.drawer[open]').count()) await page.locator('.mobile-menu:visible').first().click()
      await page.locator(`${side} [aria-label="Search sessions"]`).fill(LONG)
      await inspect(`search-${w}`,side)
      await page.locator(`${side} [aria-label="Search sessions"]`).fill('')
      await page.locator(`${side} .side-mode-switch button`,{hasText:'Catch up'}).click()
      await inspect(`catchup-${w}`,side)
      await page.locator(`${side} [aria-label="Organize sessions"]`).click()
      await inspect(`organize-${w}`,'dialog.sheet[open]:not(.drawer)')
      await page.getByRole('button',{name:'Suggest groups',exact:true}).click()
      await page.locator('.suggest-list').waitFor()
      await inspect(`organize-suggest-${w}`,'dialog.sheet[open]:not(.drawer)',async root=>root.locator('.suggest-list').evaluate(el=>el.scrollWidth>el.clientWidth+1?['suggested group name scrolls horizontally']:[]),'App.css: wrap .suggest-list strong')
      await close()
      await page.locator(`${side} .host-switch`).click()
      await inspect(`host-${w}`,'.host-menu',async root=>root.locator('.host-add').evaluate(n=>{const b=n.getBoundingClientRect(); return document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)?.closest('button')===n?[]:['host menu is outside modal input subtree']}))
      if(phase!=='before'||w>=900) await page.getByRole('button',{name:'Add a host',exact:true}).click()
      if(phase!=='before'||w>=900) await inspect(`host-form-${w}`,'.host-menu')
      if(phase==='before') await page.locator(`${side} .host-switch`).click()
      else {
        await page.keyboard.press('Escape')
        check(`host-escape-${w}`,await page.locator('.host-menu').count()===0 && (w>=900 || await page.locator('dialog.drawer[open]').count()===1))
      }
      await page.locator(`${side} [aria-label="New session"]`).click()
      await page.locator('[aria-label="First task"]').fill(LONG)
      await inspect(`new-session-${w}`,'dialog.task-pop')
      await page.getByRole('tab',{name:'Open existing',exact:true}).click()
      await inspect(`open-existing-${w}`,'dialog.task-pop')
      await page.getByRole('tab',{name:'New',exact:true}).click()
      await page.locator('.create-folder-row').click()
      await inspect(`folders-${w}`,'dialog.task-pop',async root=>root.locator('.create-folders').evaluate(el=>el.scrollWidth>el.clientWidth+1?['folder list scrolls horizontally']:[]))
      await page.locator('.create-folder-row').click()
      await page.getByRole('switch',{name:/New worktree/}).check()
      await page.getByRole('textbox',{name:'Branch name'}).fill(LONG)
      await inspect(`worktree-${w}`,'dialog.task-pop')
      await close()
      if(w<900 && await page.locator('dialog.drawer[open]').count()) await close()
      await page.keyboard.press('Control+k')
      await page.locator('.command-menu input').fill('q'.repeat(80))
      await inspect(`commands-${w}`,'dialog.command-menu',async root=>root.locator('.command-list').evaluate(el=>el.scrollWidth>el.clientWidth+1?['command list scrolls horizontally']:[]))
      await page.keyboard.press('Control+End')
      await page.keyboard.press('Tab')
      if(await page.locator('.sm-panel:visible').count()) {await inspect(`select-${w}`,'.sm-panel');await close()}
      await close()
      await page.locator('button.chat-title:visible').first().click()
      await inspect(`details-${w}`,'dialog.session-details')
      await page.getByRole('button',{name:'Rename this tab'}).click()
      await inspect(`rename-${w}`,'dialog.session-details')
      await close()
      await close()
      await page.locator('.review-toggle:visible').first().click()
      await inspect(`review-links-${w}`,'dialog.review-pop')
      await close()
      await page.locator('.pane-tabs .tab-add:visible').first().click()
      await page.locator('[aria-label="Task"]').fill(LONG)
      await page.locator('.task-model select').selectOption('opus')
      await inspect(`new-tab-${w}`,'dialog.task-pop')
      delays.delegate=1200;failures.delegate=LONG
      await page.locator('.task-create').click()
      await inspect(`task-pending-${w}`,'dialog.task-pop')
      await page.locator('.task-error').waitFor()
      await inspect(`task-error-${w}`,'dialog.task-pop')
      delete delays.delegate;delete failures.delegate
      await close()
      if(w<900) {
        await page.locator('textarea:visible').first().blur()
        await inspect(`bottom-bar-${w}`,'.phone-tabbar')
        await page.locator('.phone-tab-more').click()
        await inspect(`bottom-more-${w}`,'.phone-more-menu')
        await page.locator('.phone-tab-more').click()
      }
      if(await page.locator('.group-add:visible').count()) await page.locator('.group-add:visible').first().click()
      else { await page.locator('.group-switch:visible').first().click(); await page.getByRole('button',{name:'New stack',exact:true}).click() }
      await page.getByRole('textbox',{name:'Stack name',exact:true}).fill(LONG.slice(0,40))
      await inspect(`new-stack-${w}`,'dialog.task-pop')
      await close()
      await open(w,h,{},'board')
      await page.locator('.work-sheet').waitFor()
      await inspect(`work-${w}`,'.work-sheet')
      await page.locator('.work-bcard').first().click()
      await inspect(`work-detail-${w}`,'.work-drawer')
      await close()
      await page.locator('.work-projects button',{hasText:project.name}).click()
      await page.getByRole('button',{name:/^Settings of /}).click()
      await inspect(`project-settings-${w}`,'.work-form-sheet')
      await close()
      await page.getByRole('button',{name:'New item',exact:true}).click()
      await inspect(`new-work-item-${w}`,'.work-form-sheet')
      await close()
      await close()
      await open(w,h,{},'backlog')
      await inspect(`backlog-${w}`,'.backlog-page')
      await page.locator('.backlog-open').first().click()
      await inspect(`backlog-detail-${w}`,'.backlog-page')
      await open(w,h,{},'work')
      await inspect(`home-${w}`,'.home')
      if(w>=900) await inspect(`rail-${w}`,'.app-rail')
    })
    if(!process.env.EXTENDED) for (const width of [180,210,248]) await run(`narrow-${width}`,async()=> {
      await open(1440,900,{sideMode:'sessions'})
      await page.evaluate(w=>document.querySelector('.app-shell').style.setProperty('--lw-sessions',`${w}px`),width)
      await inspect(`sidebar-${width}`,'.sidebar',async root=>root.locator('.group-toggle').first().evaluate(el=>el.getBoundingClientRect().height>Math.max(36,parseFloat(getComputedStyle(el).minHeight)+1)?['group header wraps into crushed lines']:[]))
    })
    await run('short-backlog-body',shortBacklogBody)
    await run('select-long-groups',longSelectGroups)
    // A second name and a three-digit rollup exercise the same shrinking-text contract.
    await run('custom-group-129',async()=> {
      const original=sessions
      sessions={...sessions,sessions:[sessions.sessions[0],...Array.from({length:129},(_,i)=>pane(`%${300+i}`,'$1',LONG+i,`Fixture:2.${i}`,{state:'Approval'}))]}
      await open(1440,900,{groupBy:'custom',groups:[{id:'custom',name:LONG}],groupOf:{$1:'custom'}})
      await page.evaluate(()=>document.querySelector('.app-shell').style.setProperty('--lw-sessions','180px'))
      await inspect('custom-group-129','.sidebar',async root=>root.locator('.group-toggle').first().evaluate(el=>el.getBoundingClientRect().height>Math.max(36,parseFloat(getComputedStyle(el).minHeight)+1)?['custom group header wraps']:[]))
      sessions=original
    })
    await run('project-name-second-input',async()=> {
      const original=fixture['work/board'], name='P'.repeat(32)
      fixture['work/board']={...original,projects:[{...project,name},{...project,name:'Second'}],items:original.items.map(item=>({...item,project:name}))}
      try {
        await open(390,844,{},'board')
        await page.locator('.work-projects button',{hasText:name}).click()
        await page.getByRole('button',{name:/^Settings of /}).click()
        await inspect('project-name-second-input','.work-form-sheet')
      } finally {fixture['work/board']=original}
    })
    // Info uses the actual rendered component in a constrained host. This is not a saved layout.
    for(const width of [180,220]) await run(`info-${width}`,async()=> {
      fixture.chat.attention={id:'d',reason:'dialog',text:'Status',dialog:{title:'Status',blocks:[{type:'entries',items:[{title:LONG,lines:[LONG]}]}],hints:[],focus:'none',mode:'',text:'Status'}}
      await open(1024,768)
      await page.locator('.dlg-card').waitFor()
      await page.locator('.status-content:visible').first().evaluate((el,w)=>{el.style.width=`${w}px`;el.style.flex='none'},width)
      await inspect(`info-${width}`,'.dlg-card',async root=>root.locator('.dlg-entries').evaluate(el=>el.scrollWidth>el.clientWidth+1?['entries grid exceeds host']:[]))
      delete fixture.chat.attention
    })
    const original=sessions
    for(const [w,h] of viewports) await run(`lessons-${w}`,async()=> {
      sessions=calm
      fixture['autonomy/review']={due:true,reason:'new',new:129,disputed:0,total:129,last_review:0,snoozed_until:0,groups:[{key:'new',lessons:[{id:'l1',project:'fixture',project_name:LONG,text:LONG,kind:'keep',source:'user',agent:'claude',hits:1,created:NOW,last_hit:NOW,status:'trial'}]}],check:{state:'failed',error:LONG}}
      await open(w,h,{attention:true},'attention')
      await inspect(`lessons-${w}`,'.lessons-review',async root=>root.locator('.review-body').evaluate(el=>el.scrollWidth>el.clientWidth+1?['lesson review body scrolls horizontally']:[]))
      await page.locator('.review-actions [aria-label^="Edit the lesson"]').click()
      await inspect(`lesson-edit-${w}`,'.lessons-review')
      delays['autonomy/review']=1500
      await open(w,h,{attention:true},'attention')
      await inspect(`lessons-loading-${w}`,'.lessons-review')
      delete delays['autonomy/review']
    })
    sessions=original
    for(const [w,h] of viewports) await run(`states-${w}`,async()=> {
      sessions={sessions:[],default_thread:''}
      await open(w,h,{},'work')
      await inspect(`home-empty-${w}`,'.home')
      delays.sessions=1500
      await open(w,h,{},'work')
      await inspect(`home-loading-${w}`,'.home')
      delete delays.sessions
      failures.sessions=LONG
      await open(w,h,{},'work')
      await inspect(`home-error-${w}`,'.home')
      delete failures.sessions
      const backlog=fixture['projects/backlog']
      fixture['projects/backlog']={...backlog,text:''}
      await open(w,h,{},'backlog')
      await inspect(`backlog-empty-${w}`,'.backlog-page')
      delays['projects/backlog']=1500
      await open(w,h,{},'backlog')
      await inspect(`backlog-loading-${w}`,'.backlog-page')
      delete delays['projects/backlog']
      failures['projects/backlog']=LONG
      await open(w,h,{},'backlog')
      await inspect(`backlog-error-${w}`,'.backlog-page')
      delete failures['projects/backlog'];fixture['projects/backlog']=backlog
      failures['work/board']=LONG
      await open(w,h,{},'board')
      await inspect(`work-error-${w}`,'.work-sheet',async root=>root.locator('.work-error').evaluate(el=>el.scrollWidth>el.clientWidth+1?['work error leaves scroll body']:[]))
      delete failures['work/board']
      delays['work/board']=1500
      await open(w,h,{},'board')
      await inspect(`work-loading-${w}`,'.work-sheet')
      delete delays['work/board']
    })
    for(const [endpoint,go,selector,label,name] of [
      ['sessions','work','.home','Try again','home-long-error-second-input'],
      ['projects/backlog','backlog','.backlog-page','Dismiss','backlog-long-error-second-input'],
    ]) await run(name,async()=> {
      failures[endpoint]='ServerCouldNotReadThisData'.repeat(30)
      try {
        await open(740,360,{},go)
        await page.getByRole('button',{name:label,exact:true}).scrollIntoViewIfNeeded()
        await inspect(name,selector,async root=>{
          const outer=await root.boundingBox(),button=await page.getByRole('button',{name:label,exact:true}).boundingBox()
          return button.y+button.height>outer.y+outer.height+1?[`${label} cannot scroll into view`]:[]
        })
      }finally{delete failures[endpoint]}
    })
    sessions=original
    const custom={id:'nav-proof',name:'Nav proof',panels:['sessions','chat','agents','status','waiting'],grid:{columns:[{main:'sessions',width:180},{main:'chat',bottom:'waiting'},{main:'status',width:180}]},style:{gap:8,radius:8,surface:'flat'},theme:null}
    await run('custom-layout',async()=> {
      await open(process.env.COARSE?320:1440,process.env.COARSE?640:900,{look:{layout:'custom:nav-proof',custom,surface:'flat',backdrop:'ember'}})
      check('custom layout selected',await page.locator('.app-shell').getAttribute('data-layout')===(process.env.COARSE?null:'custom'))
      await inspect('custom-layout','.app-shell')
      if(process.env.COARSE) return
      await page.getByRole('button',{name:'Close Waiting on you',exact:true}).click()
      await inspect('custom-layout-closed','.app-shell')
      await page.getByTitle('Open Waiting on you',{exact:true}).click()
    })
    if(!process.env.COARSE) for(const layout of ['status','islands','vscode','zed']) await run(`layout-${layout}`,async()=> {
      await open(1440,900,{look:{layout,surface:'flat',backdrop:'ember'},panelSizes:{[layout]:{sessions:180,status:180}}})
      await inspect(`layout-${layout}`,'.app-shell')
      const grip=page.getByRole('separator',{name:'Resize Sessions'})
      if(await grip.count()) {
        const before=await grip.getAttribute('aria-valuenow')
        await grip.focus();await page.keyboard.press('ArrowRight')
        check(`resize-${layout}`,Number(await grip.getAttribute('aria-valuenow'))>=Number(before))
        await page.keyboard.press('Enter')
      }
      const close=page.getByRole('button',{name:/^Close (Status|Waiting on you)$/}).first()
      if(await close.count()) {
        await close.click()
        await inspect(`layout-closed-${layout}`,'.app-shell')
        await page.locator('.panel-reopen').first().click()
      }
    })
    await fs.writeFile(path.join(SHOTS,`${phase}-findings.json`),JSON.stringify(findings,null,2))
    check('no page errors',!pageErrors.length,pageErrors.join('\n').slice(0,1000))
  } catch(error) { failed++; console.error(error.stack) }
  finally {
    await page?.close().catch(()=>{})
    await browser?.close().catch(()=>{})
    if(server) { server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)) }
  }
  if(failed) process.exitCode=1
}
main()
