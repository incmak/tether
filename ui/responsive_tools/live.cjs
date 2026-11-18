// Agent map check against an existing build. Every API request stays in this page's fixtures; no terminal is touched.
// Run: cd ui && npm run test:ui (or node test_agent_map.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal } = require('../browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.resolve(__dirname, '../dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../../.release-work/2026-11-16/responsive/tools/shots')
const sizes = [[320, 640], [390, 844], [740, 360], [768, 1024], [1024, 768], [1440, 900]]
const now = Date.now()
const iso = seconds => new Date(now - seconds * 1000).toISOString()
const agent = (id, description, state, extra = {}) => ({ id, description, type: 'general-purpose', model: 'claude-sonnet-5-5', effort: 'medium',
  background: true, state, started: iso(400), ended: state === 'running' ? null : iso(60), seconds: 340, tokens: 87012, tools: 2, depth: 1,
  parent: null, summary: null, ...extra })
const agents = [
  agent('a1', 'Research remote browser products and compare their pricing pages', 'finished'),
  agent('a2', 'Audit the chat composer', 'running', { model: 'claude-haiku-5-5', seconds: 95, tokens: 1200 }),
  agent('a3', 'Check the deploy script', 'stopped', { summary: 'Agent "Check the deploy script" was stopped by user' }),
  agent('a4', 'Read the hooks folder', 'finished', { parent: 'a2', depth: 2, tools: 0 }),
]
const task = (id, kind, description, command, state, extra = {}) => ({ id, kind, description, command, state, started: iso(128), ended: state === 'running' ? null : iso(10),
  seconds: 128, parent: null, summary: null, ...extra })
const tasks = [
  task('b1', 'shell', 'Run the server tests', 'cd server && python3 -m unittest discover -q', 'running'),
  task('b2', 'monitor', null, 'tail -f build.log | grep --line-buffered ERROR', 'stopped', { summary: '[killed]', parent: 'a2', seconds: 21 }),
]
const taskOutput = id => ({ ...tasks.find(item => item.id === id), output: Array.from({ length: 80 }, (_, i) => `line ${i + 1}`).join('\n'), output_cut: true })
const tools = [{ kind: 'tool', id: 't1', name: 'Bash', title: 'ls server', is_error: false, status: 'complete' },
  { kind: 'tool', id: 't2', name: 'Read', title: 'server/subagents.py', is_error: true, status: 'complete' }]
const detail = id => ({ ...agents.find(item => item.id === id), prompt: 'Find the three best remote browser products.\n\nReport prices.',
  steps: [{ kind: 'text', text: 'Starting with the server folder.' }, ...tools], report: id === 'a1' ? 'Three products fit. Browserbase is the cheapest at the volume you need.' : null })
const messages = Array.from({ length: 12 }, (_, i) => ({ id: `m${i}`, role: i % 2 ? 'assistant' : 'user', text: `Message ${i + 1}.`, time: '2026-11-11T08:00:00Z' }))
const pane = { id: '%1', tmux_session: '$1', label: 'Agent map test', agent: 'claude', dir: '/srv/project', state: 'Working', writable: true, thread: 'map-thread', approval: null }
const fixture = {
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  sessions: { sessions: [pane], default_thread: pane.thread },
  chat: { messages, busy: true, thread: pane.thread, agent: 'claude', writable: true, session: pane.id, earlier: 0, limit: 100, state: 'Working', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: 'claude-opus-5-5', context: 91452, title: 'Build the agent map' }, agents, tasks },
  'subagent-tool': { id: 't1', name: 'Bash', title: 'ls server', is_error: false, status: 'complete', detail: '$ ls server\nserver.py\nsubagents.py', format: 'text', truncated: false, images: 0 },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] }, 'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
}


const long = (process.env.VARIANT === '2' || process.env.SECOND === '1') ? 'RepositoryFolder'.repeat(7) : 'W'.repeat(80)
const phase = process.env.PHASE || 'after'
agents[0].description = long
agents[1].description = 'Agent ' + long
agents[2].summary = long
agents[3].description = long + ' child'
// A three-digit map count keeps the same card layout and scrolls the list.
agents.push(...Array.from({length: 96}, (_, i) => agent(`extra${i}`, `Finished ${i} ` + long, 'finished')))
fixture.subagents.main.title = long
fixture.subagents.main.model = long
fixture.chat.messages = []
tasks[0].description = long
tasks[1].description = long
fixture['codex-runs'] = { pane: '%1', running: 1, runs: [{ id: 'run1', pid: 100, started: now - 60000, cwd: '/workspace/' + long + '/' + long,
 prompt: long, steps: [{at: now - 10000,kind:'tool',text:long}], state:'stalled', last_activity:now - 50000, final:long }] }
async function main() {
 let browser, page, server, failed = 0
 const errors = [], results=[]
 const check = (name,ok,why='') => {results.push({name,ok,why}); console.log(`${ok?'ok':'FAIL'} ${name} ${why}`); if(!ok)failed++}
 try {
  await fs.mkdir(SHOTS,{recursive:true})
  const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.webmanifest':'application/manifest+json'}
  server=http.createServer(async(req,res)=>{const url=new URL(req.url,'http://localhost'); const file=url.pathname==='/chat/'?path.join(DIST,'index.html'):path.resolve(DIST,'.'+decodeURIComponent(url.pathname.slice('/chat'.length))); if(!file.startsWith(DIST+path.sep)){res.writeHead(404);res.end();return}try{const bytes=await fs.readFile(file);res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream'});res.end(bytes)}catch{res.writeHead(404);res.end()}})
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  const origin=`http://127.0.0.1:${server.address().port}`
  browser=await getBrowser({local:true}); const context=await browser.newContext();page=await context.newPage();page.setDefaultTimeout(10000)
  page.on('pageerror',error=>errors.push(error.message))
  await page.emulateMedia({reducedMotion:'reduce',colorScheme:'light'})
  await page.routeWebSocket('**/*',socket=>socket.close())
  let detailMode='ok', trustMode='untrusted', terminalMode='ok'
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url())
   if(url.pathname.includes('/api/')){
    const endpoint=url.pathname.split('/api/')[1]
    if(endpoint==='stream')return route.fulfill({status:204,body:''})
    if(endpoint==='codex-trust'&&req.postDataJSON()?.op==='trust')return route.fulfill({status:500,json:{error:long}})
    if(endpoint==='codex-trust')return route.fulfill({json:{state:trustMode,folder:'/workspace/a',target:'/workspace/'+long,target_id:'1:1'}})
    if(endpoint==='subagent'){
     if(detailMode==='error')return route.fulfill({status:500,json:{error:long}})
     if(detailMode==='loading'){await new Promise(r=>setTimeout(r,900));}
     return route.fulfill({json:{...detail(url.searchParams.get('id')),prompt:long,report:long}})
    }
    if(endpoint==='subagent-task')return route.fulfill({json:{...taskOutput(url.searchParams.get('id')),output:long}})
    if(endpoint==='live/agent')return route.fulfill({json:{ok:false}})
    if(req.method()!=='GET')return route.fulfill({json:{ok:true,state:'stopped'}})
    return route.fulfill({json:fixture[endpoint]||{}})
   }
   if(url.pathname.startsWith('/terminal/live/')&&!url.searchParams.has('arg')){if(terminalMode==='loading')await new Promise(resolve=>setTimeout(resolve,900));if(terminalMode==='down')return route.fulfill({status:503,body:'fixture terminal unavailable'})}
   if(url.pathname.startsWith('/terminal/live/'))return route.fulfill({contentType:'text/html',body:'<!doctype html><html><body><pre>fixture terminal</pre></body></html>'})
   if(url.pathname.endsWith('/sw.js'))return route.fulfill({status:404,body:''})
   if(url.origin===origin&&url.pathname.startsWith('/chat/'))return route.continue()
   return route.abort('blockedbyclient')
  })
  await page.addInitScript(()=>{
   if('serviceWorker'in navigator)navigator.serviceWorker.register=()=>Promise.reject(new Error('off'))
   localStorage.clear();localStorage.setItem('tmux-chat.prefs.synced','1');localStorage.setItem('tmux-chat.prefs.v1',JSON.stringify({theme:'light',voiceOff:true,voiceSound:false,selected:'%1'}))
   Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>{throw new DOMException('Blocked','NotAllowedError')}}})
  })
  const audit=async(selector,label)=>{const bad=await page.locator(selector).first().evaluate((root,selector)=>{const box=root.getBoundingClientRect();return [...root.querySelectorAll('button')].filter(el=>el.getClientRects().length).flatMap(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width<24||r.height<24||r.left<box.left-1||r.right>box.right+1||(selector==='.live'&&(r.top<box.top-1||r.bottom>box.bottom+1))||el.textContent.trim()&&el.scrollWidth>el.clientWidth+1&&s.textOverflow!=='ellipsis'?[{text:el.textContent,label:el.ariaLabel,w:r.width,h:r.height,left:r.left,right:r.right,top:r.top,bottom:r.bottom,scroll:el.scrollWidth,client:el.clientWidth}]:[]})},selector);check(label,!bad.length,JSON.stringify(bad))}
  const voiceFits=async label=>{const bad=await page.locator('.live').evaluate(root=>{const top=root.querySelector('.live-top').getBoundingClientRect().bottom,bottom=root.querySelector('.live-approval').getBoundingClientRect().top;return [...root.querySelectorAll('.live-center > *')].filter(el=>el.getClientRects().length).map(el=>({name:el.className,top:el.getBoundingClientRect().top,bottom:el.getBoundingClientRect().bottom})).filter(r=>r.top<top-1||r.bottom>bottom+1)});check(label,!bad.length,JSON.stringify(bad));const error=await page.locator('.live-problem').evaluate(el=>{const box=el.parentElement,r=el.getBoundingClientRect(),b=box.getBoundingClientRect();return{top:r.top,bottom:r.bottom,boxTop:b.top,boxBottom:b.bottom,scroll:box.scrollHeight,client:box.clientHeight}});check(label+' live status visible',await page.locator('.live-status').isVisible());check(label+' microphone error fully visible',error.top>=error.boxTop+7&&error.bottom<=error.boxBottom+1&&error.scroll<=error.client+1,JSON.stringify(error))}
  const shot=async name=>page.screenshot({path:path.join(SHOTS,`${phase}-${name}.png`)})
  const fit=async(selector,label)=>{
   const metrics=await page.locator(selector).first().evaluate(el=>{
    const r=el.getBoundingClientRect();return{width:r.width,left:r.left,right:r.right,top:r.top,bottom:r.bottom,scroll:el.scrollWidth,client:el.clientWidth,vw:innerWidth,vh:innerHeight}
   })
   check(label+' page has no horizontal scroll',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));check(label,metrics.left>=-1&&metrics.right<=metrics.vw+1&&(!selector.startsWith('dialog')&&selector!=='.live'&&selector!=='.live-popover'||metrics.top>=-1&&metrics.bottom<=metrics.vh+1)&&(selector==='.live'||metrics.scroll<=metrics.client+1),JSON.stringify(metrics))
  }
  for(const[width,height]of sizes){
   await page.setViewportSize({width,height})
   fixture.sessions.sessions[0]={...pane, pane_name:long}
   fixture.chat.agent='claude';fixture.sessions.sessions[0].agent='claude'
   await page.goto(`${origin}/chat/?session=%251`)
   // The compact phone composer hides the agent pill until the text box has focus.
   if(width<=600)await page.locator('.composer textarea').click()
   await page.getByRole('button',{name:/1 agent running\. Open the agent map/}).click()
   await shot(`agent-map-${width}`);await fit('dialog.agent-map',`${width} agent map fits`);await audit('dialog.agent-map',`${width} map controls fit`)
   detailMode='loading';await page.locator('.agent-tree > .agent-branch > .agent-children > li .agent-node').first().click();await page.locator('.skeleton.agent-body').waitFor();await shot(`agent-loading-${width}`);detailMode='ok'
   await page.locator('.agent-report').waitFor()
   await shot(`agent-detail-${width}`);await fit('dialog.agent-map',`${width} agent detail fits`)
   await page.getByRole('button',{name:'Open transcript'}).click();await shot(`agent-transcript-${width}`);await fit('dialog.agent-map',`${width} transcript fits`)
   await page.getByRole('button',{name:'Back to the agent',exact:true}).click()
   await page.getByRole('button',{name:'Back to the agent map'}).click();detailMode='error';await page.locator('.agent-tree > .agent-branch > .agent-children > li .agent-node').first().click();await page.locator('dialog.agent-map [role=alert]').waitFor();await shot(`agent-error-${width}`);await fit('dialog.agent-map',`${width} agent error fits`);detailMode='ok';await page.getByRole('button',{name:'Retry',exact:true}).click();await page.locator('.agent-report').waitFor();await page.getByRole('button',{name:'Back to the agent map'}).click()
   await page.locator('.agent-tasks .agent-node').first().click();await page.getByLabel('Output',{exact:true}).waitFor()
   await shot(`agent-task-${width}`);await fit('dialog.agent-map',`${width} task fits`)
   await page.keyboard.press('Escape')
   const codex=page.getByRole('button',{name:/Codex · 1 running/});await codex.click();await page.locator('.codex-runs').waitFor()
   await shot(`codex-runs-${width}`);await fit('.codex-runs',`${width} codex runs fit`)
   await page.locator('.codex-runs').evaluate(el=>el.style.width='180px');await shot(`codex-runs-panel-${width}`);await fit('.codex-runs',`${width} codex runs 180px fits`)
   fixture.chat.agent='codex';fixture.sessions.sessions[0].agent='codex'
   await page.goto(`${origin}/chat/?session=%251`)
   await page.getByRole('button',{name:'Codex read-only'}).click();await page.locator('.codex-trust').waitFor()
   await shot(`codex-trust-${width}`);await fit('.codex-trust',`${width} codex trust fits`);await page.getByRole('button',{name:'Trust folder',exact:true}).click();await page.locator('.codex-trust-error').waitFor();await shot(`codex-trust-error-${width}`);await fit('.codex-trust',`${width} codex trust error fits`)
   fixture.sessions.sessions[0].approval={id:'approval1',text:long,options:[{text:'Yes',value:'yes'},{text:'No',value:'no'}]};await page.goto(`${origin}/chat/?session=%251`);await page.locator('.chat-panel').waitFor();if(await page.locator('dialog.action-deck[open]').count())await page.keyboard.press('Escape')
   if(width>=900)await page.getByRole('button',{name:'Friday Live mode',exact:true}).click()
   else {await page.getByRole('button',{name:'More',exact:true}).click();await page.getByRole('menuitem',{name:/Friday/}).click()}
   await page.locator('.live').waitFor();await page.locator('.live-approval').waitFor();await page.locator('.live[data-phase=idle]').waitFor();await page.waitForTimeout(250);await shot(`live-window-${width}`);await fit('.live',`${width} Live window fits`);await audit('.live',`${width} Live window controls fit`);await voiceFits(`${width} voice stays between header and approval`)
   if(width===740){await page.setViewportSize({width:740,height:640});await page.waitForTimeout(100);await page.setViewportSize({width:740,height:360});await page.waitForTimeout(100);await page.getByRole('button',{name:'Resize Friday window. Use the arrow keys.'}).press('ArrowDown');await fit('.live','Live tall to landscape then keyboard resize fits');check('Live stored geometry fits landscape',await page.locator('.live').evaluate(el=>parseFloat(el.style.height)<=344))}
   await page.getByRole('button',{name:/Choose chat:/}).click();await shot(`live-menu-${width}`);await fit('.live-popover',`${width} Live menu fits`)
   await page.getByRole('button',{name:/Choose chat:/}).click();await page.getByRole('button',{name:'Show Friday full screen'}).click();await page.waitForTimeout(250)
   await shot(`live-full-${width}`);await fit('.live',`${width} Live full fits`);await audit('.live',`${width} Live full controls fit`);await voiceFits(`${width} full voice stays between header and approval`)
   if(width===1440){await page.locator('.live-problem').evaluate(el=>{el.textContent='The voice connection closed. Using the basic voice instead.'});await voiceFits('1440 recoverable voice fallback');await shot('live-recoverable-1440')}
   await page.getByRole('button',{name:'End Live mode'}).click();await page.getByRole('button',{name:'Terminals',exact:true}).click();await page.locator('.live-terminal-wrap').waitFor();await shot(`terminals-${width}`);await fit('.live-terminal-wrap',`${width} terminal fits`)
   await page.getByRole('button',{name:'Chats',exact:true}).click();terminalMode='loading';await page.getByRole('button',{name:'Terminals',exact:true}).click();await page.getByText('Connecting to the terminal…',{exact:true}).waitFor();await shot(`terminals-connecting-${width}`);await fit('.live-terminal-empty',`${width} terminal connecting fits`);await page.locator('.live-terminal-wrap').waitFor()
   await page.getByRole('button',{name:'Chats',exact:true}).click();terminalMode='down';await page.getByRole('button',{name:'Terminals',exact:true}).click();await page.getByText('The live terminal is not running.',{exact:true}).waitFor();await shot(`terminals-down-${width}`);await fit('.live-terminal-empty',`${width} terminal down fits`);await audit('.live-terminal-empty',`${width} terminal error controls fit`);terminalMode='ok';await page.locator('.live-terminal-empty').getByRole('button',{name:'Try again',exact:true}).click();await page.locator('.live-terminal-wrap').waitFor();await shot(`terminals-retry-${width}`);check(`${width} terminal retry reconnects`,true)
   if(width===320){const saved=fixture.sessions;fixture.sessions={sessions:[],default_thread:null};await page.goto(`${origin}/chat/`);await page.getByRole('button',{name:'Terminals',exact:true}).click();await page.getByRole('heading',{name:'No chat is open',exact:true}).waitFor();await shot('terminals-empty-320');await fit('.home-blank','320 empty terminal fits');await page.getByRole('button',{name:'Choose a chat',exact:true}).click();check('320 empty terminal opens chat chooser',await page.locator('dialog.drawer[open]').count()===1);await page.keyboard.press('Escape');fixture.sessions=saved}
  }
  check('no page errors',errors.length===0,errors.join('; '))
 }catch(error){check('runner',false,error.stack);if(page)await shotFallback(page)}
 finally{if(page)await page.close().catch(()=>{});if(browser)await browser.close().catch(()=>{});if(server){server.closeAllConnections();await new Promise(r=>server.close(r))}await fs.writeFile(path.join(SHOTS,`live-${phase}-results.json`),JSON.stringify(results,null,2));if(failed)process.exitCode=1}
}
async function shotFallback(page){await page.screenshot({path:path.join(SHOTS,`${phase}-live-error.png`)}).catch(()=>{})}
if(require.main===module)main()
module.exports={main}
