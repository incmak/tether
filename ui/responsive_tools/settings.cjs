// Settings responsive regressions against a production build. All API reads and writes use fixtures.
// Run via ui/test_responsive_tools_ui.cjs. DIST, SHOTS, PHASE and SECOND select build and evidence.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('../browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.resolve(__dirname, '../dist'))
const SHOTS = shotsDir('responsive/tools')
const now = Math.floor(Date.now() / 1000)
const ask = (pane, extra) => ({ pane, thread: null, since: now - 600, next: '', kind: 'reply', opened: false, snoozed_until: null, urgent: false, ...extra })
const paneA = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Deploy fix', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null,
  ask: ask('%1', { since: now - 120, urgent: true, next: 'The release script needs the signing key. Which key should I use: staging or production?' }) }
const paneB = { id: '%2', tmux_session: '$1', label: 'project:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/project', state: 'Idle', writable: true, thread: 't2', approval: null,
  ask: ask('%2', { since: now - 3600, kind: 'approval', next: 'Run npm install?' }) }
const levels = [['Check-in', 'Confirms the plan first.'], ['Guided', 'Confirms the plan, then works.'], ['Standard', 'Asks when a decision is material.'],
  ['Trusted', 'Uses sensible defaults.'], ['Autopilot', 'Does not ask about normal decisions.'], ['Full auto', 'Runs the task alone. Asks only when blocked; urgent asks reach the user at once.']]
  .map(([name, summary], level) => ({ level, name, summary }))
const auto = { agent: 'auto', model: 'auto', effort: 'auto' }
const advisorDefault = () => ({ main: { agent: 'claude', model: 'latest', effort: 'high' }, second: { agent: 'codex', model: 'latest', effort: 'high' } })
let autonomy = { levels, global: 2, level: 2, source: 'global', project: null, reviewers: { critical: auto, normal: auto, light: auto }, tiers: ['critical', 'normal', 'light'],
  models: { claude: [{ slug: 'opus', name: 'Opus' }, { slug: 'claude-fable-5-2', name: 'Fable 5.2' }], codex: [{ slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol', efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'] }, { slug: 'gpt-6-luna', name: 'GPT-6 Luna', efforts: ['low', 'medium'] }] },
  efforts: ['low', 'medium', 'high', 'xhigh', 'max'], advisor: advisorDefault(), latest: { claude: 'Fable 5.2', codex: 'GPT-6.1 Sol' },
  catalog: {
    claude: { latest: { launch: 'fable', slug: 'claude-fable-5-2', name: 'Fable 5.2' }, models: [{ slug: 'claude-fable-5-2', name: 'Fable 5.2', efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }, { slug: 'claude-mythos-5-2', name: 'Mythos 5.2', family: 'mythos', available: false, access: 'no-access', efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }, { slug: 'claude-opus-5-5', name: 'Opus 5.5', efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }], new_families: [], source: 'claude-code', changed: { from: 'Fable 5.1', to: 'Fable 5.2', at: now - 3600 } },
    codex: { latest: { launch: 'gpt-6.1-sol', slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol' }, models: [{ slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol', efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'] }, { slug: 'gpt-6-luna', name: 'GPT-6 Luna', efforts: ['low', 'medium'] }], new_families: [], source: 'codex', changed: null } },
  learning: { enabled: true, global: [{ id: 'aaaa1111', text: 'When you change UI, check it at 390 px before you report done.', kind: 'avoid', source: 'user', agent: 'codex', hits: 3, created: now - 86400 * 3, last_hit: now - 3600, stuck: true, status: 'confirmed' }],
    review: { due: true, reason: 'disputed', new: 1, disputed: 1, total: 3 } } }
const fixture = {
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  sessions: { sessions: [paneA, paneB], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },  // Home and the sidebar read the work board
}

const setupFresh = () => ({
  hook: { state: 'missing', path: '/srv/u/.claude/settings.json', entry: { type: 'command', command: '/usr/bin/python3 /srv/tether/server/autonomy.py hook', timeout: 5 } },
  ping: { state: 'off', preset: null, fields: {}, base_url: '', presets: [{ id: 'briskbot', name: 'BriskBot (Microsoft Teams)', available: true }] },
})
let setup = setupFresh()
async function main() {
  let browser, server
  const errors = [], failures = []
  const phase = process.env.PHASE || 'after'
  const token = process.env.VARIANT || (process.env.SECOND ? 'i' : 'W')
  const long = token.repeat(80)
  paneA.ask = null; paneB.ask = null
  setup.hook.path = `/srv/${long}/settings.json`
  setup.ping.state = 'on'; setup.ping.fields = {space:'team',mention:long}; setup.ping.preset='briskbot'
  fixture.prefs.prefs = {sessionFolder:`/srv/${long}`}
  fixture.host = {name:long, resources:false,terminal:false}
  for(const catalog of Object.values(autonomy.catalog)){catalog.latest.name=long;catalog.models[0].name=long}
  fixture.autonomy = autonomy
  fixture['autonomy/setup'] = setup
  fixture.passkeys = {here:1,passkeys:[{id:'fake-key',name:long,created:'2026-11-16',last_used:null}]}
  fixture.layouts = {layouts:[{id:'responsive-fixture',name:long.slice(0,40),description:long,panels:['chat'],grid:{columns:[{main:'chat'}]},style:{radius:0,gap:0,surface:'flat'},theme:null}], errors:[],folder:'/srv/layouts'}
  fixture['look/layouts'] = fixture.layouts
  fixture['codex-level'] = {levels:levels.slice(0,4),global:2,level:2,source:'global',project:null,runs:{today:123,running:12,panes:[{pane:'%1',label:long,running:12}]},run:{model:'auto',effort:'auto',fast:false},models:[]}
  const usageRow={region:long,label:long,kind:'button',depth:3,clicks:123,seen:123,rate:1,steps:123}
  fixture['ui-usage']={enabled:true,days:30,device:'all',visits:123,clicks:123,controls:123,ready:true,top:[usageRow],demote:[usageRow],promote:[usageRow],unused:[usageRow],views:[]}
  fixture.activity={enabled:true,agents:true,zone:'UTC',since:now-86400,store:`/srv/${long}`,days:[{day:'2026-11-16',you:36000,agents:72000}],projects:[{name:long,you:36000,agents:72000}],publish:{folder:`/srv/${long}`,file:`${long}.json`,busy:false,pending:false,published:null,last:{at:now,ok:false,error:long}}}
  fixture['push/config']={supported:false,publicKey:''}
  fixture.backgrounds={items:[]}
  try {
    await fs.mkdir(SHOTS,{recursive:true})
    server=http.createServer(async(req,res)=>{try { const url=new URL(req.url,'http://localhost'); const file=url.pathname==='/chat/'?path.join(DIST,'index.html'):path.resolve(DIST,'.'+url.pathname.slice(5)); if(!file.startsWith(DIST+path.sep))throw Error('path'); const body=await fs.readFile(file);res.writeHead(200,{'Content-Type':({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream'});res.end(body)}catch{res.writeHead(404);res.end()}})
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
    const origin=`http://localhost:${server.address().port}`
    browser=await getBrowser({local:true})
    const context=await browser.newContext({serviceWorkers:'block'})
    const page=await context.newPage(); page.setDefaultTimeout(10000)
    page.on('pageerror',e=>errors.push(e.message))
    await page.routeWebSocket('**/*',socket=>socket.close())
    await page.route('**/*',async route=>{
      const u=new URL(route.request().url())
      if(u.pathname.includes('/api/')){
        const endpoint=u.pathname.split('/api/')[1]
        if(endpoint==='stream')return route.fulfill({status:204,body:''})
        if(endpoint.startsWith('chat'))return route.fulfill({json:fixture.chat})
        if(endpoint==='prefs'&&route.request().method()!=='GET')return route.fulfill({json:{prefs:{},version:1}})
        const result=fixture[endpoint]
        return result?route.fulfill({json:result}):route.fulfill({status:503,json:{error:`Fixture unavailable: ${long}`}})
      }
      if(u.origin===origin&&!u.pathname.endsWith('/sw.js'))return route.continue()
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(()=>{localStorage.clear();localStorage.setItem('tmux-chat.prefs.synced','1');localStorage.setItem('tmux-chat.prefs.v1',JSON.stringify({theme:'light',voiceOff:true,voiceSound:false,look:{layout:'classic',surface:'glass',backdrop:'ember'}}))})
    await page.emulateMedia({reducedMotion:'reduce',colorScheme:'light'})
    const shot=async(name)=>page.screenshot({path:path.join(SHOTS,`${phase}-settings-${name}-${page.viewportSize().width}-${token}.png`)})
    const audit=async(name)=>{
      const bad=await page.locator('dialog[open]').last().evaluate(root=>{
        const content=root.querySelector('.preferences-content')||root;const issues=[]
        if(document.documentElement.scrollWidth>innerWidth)issues.push('page scroll')
        const bounds=root.getBoundingClientRect();if(bounds.left < -1 || bounds.right > innerWidth+1 || bounds.top < -1 || bounds.bottom > innerHeight+1)issues.push('dialog outside viewport')
        if(content.scrollWidth>content.clientWidth+1)issues.push(`content ${content.scrollWidth}>${content.clientWidth}`)
        for(const el of root.querySelectorAll('button,label')){
          if(!el.getClientRects().length)continue
          const s=getComputedStyle(el)
          if(el.textContent.trim()&&el.scrollWidth>el.clientWidth+1&&s.textOverflow!=='ellipsis')issues.push(`${el.className||el.textContent.slice(0,35)} ${el.scrollWidth}>${el.clientWidth}`)
        }
        for(const el of root.querySelectorAll('.setup-step .switch-row > span:not(.setup-buttons)')){const row=el.parentElement.getBoundingClientRect(),r=el.getBoundingClientRect();if(r.width<Math.min(180,row.width)-1)issues.push(`crushed setup note ${r.width}`)}
        for(const el of root.querySelectorAll('.segmented span'))if(el.scrollWidth>el.clientWidth+1)issues.push(`crushed choice ${el.textContent}`)
        for(const el of root.querySelectorAll('button,label:has(input[type=radio]),label:has(input[type=checkbox])')){
          if(!el.getClientRects().length)continue;const r=el.getBoundingClientRect();if(r.width<24 || r.height<24)issues.push(`target below 24px: ${el.className} ${r.width}x${r.height}`)
        }
        const visible=[]
        for(const el of root.querySelectorAll('button,label:has(input[type=radio]),label:has(input[type=checkbox])')){
          const r=el.getBoundingClientRect();if(!r.width||!r.height)continue;let l=r.left,t=r.top,rr=r.right,b=r.bottom;
          for(let p=el.parentElement;p;p=p.parentElement){const style=getComputedStyle(p);if(/auto|scroll|hidden|clip/.test(style.overflow+style.overflowX+style.overflowY)){const pr=p.getBoundingClientRect();l=Math.max(l,pr.left);t=Math.max(t,pr.top);rr=Math.min(rr,pr.right);b=Math.min(b,pr.bottom)}}
          if(rr>l+2&&b>t+2&&b>0&&t<innerHeight)visible.push({el,l,t,rr,b})
        }
        for(let i=0;i<visible.length;i++)for(let j=i+1;j<visible.length;j++){const a=visible[i],b=visible[j];if(!a.el.contains(b.el)&&!b.el.contains(a.el)&&Math.min(a.rr,b.rr)-Math.max(a.l,b.l)>1&&Math.min(a.b,b.b)-Math.max(a.t,b.t)>1)issues.push(`overlap ${a.el.className}/${b.el.className}`)}
        return issues
      })
      if(bad.length)failures.push(`${name}: ${bad.join('; ')}`)
      console.log(`${bad.length?'FAIL':'ok'} ${phase} ${name}`,bad.join('; '))
      await shot(name)
    }
    for(const [width,height] of [[320,640],[390,844],[740,360],[768,1024],[1024,768],[1440,900]]){
      setup.ping.state='on'
      await page.setViewportSize({width,height});await page.goto(`${origin}/chat/?session=%251`)
      await page.waitForTimeout(400)
      if(width<800)await page.getByRole('button',{name:/menu|sessions|open the list/i}).first().click()
      await page.getByRole('button',{name:'Settings',exact:true}).first().click()
      await page.locator('dialog.preferences[open]').waitFor()
      await audit(`${width}-index`)
      for(const category of ['Appearance','Sessions & chat','Voice & alerts','Agents & browser','Keyboard shortcuts','About & system']){
        const nav=page.locator('nav.preferences-nav:visible').getByRole('button',{name:new RegExp(category.replace('&','&'))})
        if(await nav.count()===0)await page.getByRole('button',{name:'All settings',exact:true}).click()
        await page.locator('nav.preferences-nav:visible').getByRole('button',{name:new RegExp(category)}).click()
        await page.waitForTimeout(200)
        await audit(`${width}-${category.split(' ')[0]}`)
        if(category==='Agents & browser'){
          await page.getByRole('button',{name:'Review change'}).click()
          await page.locator('.setup-review').scrollIntoViewIfNeeded();await audit(`${width}-setup-review`)
          for(const role of ['advisor','critical']){
            await page.locator(`#helper-row-${role}`).click();const sheet=page.locator('dialog.helper-sheet[open]');await sheet.waitFor();
            const b=await sheet.boundingBox();if(b.x<0||b.y<0||b.x+b.width>width+1||b.y+b.height>height+1)failures.push(`${width}-${role} dialog outside viewport`);
            await audit(`${width}-helper-${role}`);await page.keyboard.press('Escape');await sheet.waitFor({state:'detached'})
          }
        }
        if(category==='Appearance'){
          await page.locator('input[name="look-surface"][value="glass"]').check({force:true})
          await page.locator('input[name="look-backdrop"][value="photo"]').check({force:true})
          await page.locator('.look-photos').scrollIntoViewIfNeeded();await audit(`${width}-photo`)
          await page.locator('input[name="look-backdrop"][value="ember"]').check({force:true})
        }
      }
      setup.ping.state='off'
      await page.reload();await page.waitForTimeout(250)
      if(width<800)await page.getByRole('button',{name:/menu|sessions|open the list/i}).first().click()
      await page.getByRole('button',{name:'Settings',exact:true}).first().click()
      const search=page.getByPlaceholder(/Search settings/)
      await search.fill('setup');await page.getByRole('button',{name:'Set up',exact:true}).click();await page.locator('form.setup-review').scrollIntoViewIfNeeded();await audit(`${width}-ping-review`)
      await search.fill(long);await audit(`${width}-search-empty`)
      await search.fill('setup');await page.locator('.setup-step').first().waitFor();await audit(`${width}-setup-search`)
    }
    if(errors.length)throw new Error(`Page errors: ${errors.join('\n')}`)
    if(failures.length)throw new Error(failures.join('\n'))
  }finally{await browser?.close();if(server)await new Promise(resolve=>server.close(resolve))}
}
main().catch(error=>{console.error(error.stack);process.exitCode=1})
