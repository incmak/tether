import {command, request, refuse, object} from './policy.mjs';
import {searchTarget} from './search.mjs';

let socket = null, epoch = null, pending = null, grant = null;
let connected = false, lastPong = 0, reconnect = null, retry = 1000;
let serial = 0, approvalWindow = null, lastEnd = 'Not connected';
const seenRequests = new Set();
const send = data => {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data));
};
function error(id, e) {
  send({type: 'error', id: typeof id === 'string' ? id : null, epoch,
    code: e.code || 'DEBUGGER_ERROR', message: String(e.message).slice(0, 200)});
}
function badge() {
  void chrome.action.setBadgeText({text: grant ? 'STOP' : pending ? '?' : ''});
  void chrome.action.setBadgeBackgroundColor({color: grant ? '#a52020' : '#23564a'});
  void chrome.action.setTitle({title: grant ? 'Stop Tether control now' : 'Tether: connect or stop'});
}
function end(reason) {
  const old = grant, oldPending = pending;
  grant = null; pending = null; serial++; lastEnd = reason; badge();
  if (old) {
    send({type: 'ended', id: old.id, tab: old.tab, epoch: old.epoch, reason});
    void chrome.debugger.detach({tabId: old.tab}).catch(() => {});
  }
  if (oldPending) send({type: 'ended', id: oldPending.id, epoch: oldPending.epoch, reason});
}
async function showApproval() {
  const w = await chrome.windows.create({url: chrome.runtime.getURL('control.html'),
    type: 'popup', width: 440, height: 540, focused: true});
  approvalWindow = w.id;
}
async function allow(id) {
  const p = pending;
  if (!p || p.id !== id || Date.now() >= p.deadline || !connected)
    refuse('STALE_APPROVAL', 'Request expired or connection changed');
  pending = null;
  const generation = ++serial;
  const tab = await chrome.tabs.create({url: 'about:blank', active: true});
  if (generation !== serial || !connected) return;
  const g = {id: p.id, tab: tab.id, epoch, expires: Date.now() + p.minutes * 60000,
    ready: false, frames: new Map(), inFlight: 0, commands: 0, seen: new Set(), reason: p.reason};
  grant = g; badge();
  try {
    await chrome.storage.session.set({orphanTab: tab.id});
    if (grant !== g) return;
    await chrome.debugger.attach({tabId: tab.id}, '1.3');
    if (grant !== g) { await chrome.debugger.detach({tabId: tab.id}).catch(() => {}); return; }
    if (p.url && p.url !== 'about:blank') await chrome.debugger.sendCommand({tabId: tab.id}, 'Page.navigate', {url: p.url});
    const tree = await chrome.debugger.sendCommand({tabId: tab.id}, 'Page.getFrameTree');
    if (grant !== g) return;
    g.ready = true; lastEnd = ''; badge();
    send({type: 'granted', id: g.id, tab: g.tab, target: tree.frameTree.frame.id, expires: g.expires, epoch});
  } catch (e) { if (grant === g) end('attach_failed'); error(p.id, e); }
}
async function handle(data) {
  let m;
  try {
    if (typeof data !== 'string' || data.length > 32000000) refuse('BAD_MESSAGE', 'Expected bounded JSON');
    m = JSON.parse(data);
    if (!object(m)) refuse('BAD_MESSAGE', 'Expected an object');
    if (m.type === 'ready') {
      if (typeof m.epoch !== 'string' || connected) refuse('BAD_AUTH', 'Unexpected connection response');
      epoch = m.epoch; connected = true; lastPong = Date.now(); retry = 1000; return;
    }
    if (!connected || m.epoch !== epoch) refuse('OLD_EPOCH', 'Connection changed');
    if (m.type === 'pong') { lastPong = Date.now(); return; }
    if (m.type === 'end') {
      if (grant?.id === m.id || pending?.id === m.id) end('tether_end');
      return;
    }
    if (m.type === 'grant') {
      const r = request(m, epoch);
      if (pending || grant) refuse('BUSY', 'Only one request or grant is allowed');
      if (seenRequests.has(r.id)) refuse('DUPLICATE_REQUEST', 'Use a new request and approval');
      if (seenRequests.size >= 1024) refuse('LIMIT', 'Reconnect before more requests');
      seenRequests.add(r.id);
      pending = {...r, deadline: Date.now() + 60000}; badge();
      const p = pending;
      const {confirm} = await chrome.storage.local.get('confirm');
      if (pending !== p || !connected) return;
      if (confirm === true) await showApproval();
      else await allow(r.id);
      return;
    }
    if (m.type !== 'command') refuse('BAD_MESSAGE', 'Unknown message type');
    const params = command(m, grant, epoch);
    const g = grant;
    if (!g.ready) refuse('NOT_READY', 'Debugger attachment is not ready');
    if (g.seen.has(m.id)) refuse('DUPLICATE_COMMAND', 'Commands are never replayed');
    if (g.seen.size >= 300000) { end('command_limit'); refuse('LIMIT', 'Request a new grant'); }
    if (g.commands >= 128) { end('backpressure'); refuse('LIMIT', 'Too many outstanding commands'); }
    if (m.method === 'Page.screencastFrameAck') {
      if (!g.frames.has(params.sessionId)) refuse('UNKNOWN_FRAME', 'Frame was not issued for this grant');
      const count = g.frames.get(params.sessionId);
      if (count === 1) g.frames.delete(params.sessionId);
      else g.frames.set(params.sessionId, count - 1);
      g.inFlight--;
    }
    g.seen.add(m.id); g.commands++;
    try {
      if (grant !== g || !connected || Date.now() >= g.expires) refuse('EXPIRED', 'Grant ended before dispatch');
      const result = await chrome.debugger.sendCommand({tabId: g.tab}, m.method, params);
      if (grant === g && connected && Date.now() < g.expires) send({type: 'result', id: m.id, epoch, result});
    } finally { g.commands--; }
  } catch (e) { error(m?.id, e); }
}
async function connect() {
  clearTimeout(reconnect); end('connection_changed');
  const previous = socket; socket = null; previous?.close();
  connected = false; epoch = null; seenRequests.clear();
  const generation = serial;
  const {origin, token} = await chrome.storage.local.get(['origin', 'token']);
  if (serial !== generation || !origin || !token) return;
  const u = new URL(origin);
  if (u.protocol !== 'https:' || u.origin !== origin) return;
  u.protocol = 'wss:'; u.pathname = '/mobile/api/browser/extension/connect';
  const ws = new WebSocket(u.href); socket = ws;
  const authTimer = setTimeout(() => { if (socket === ws && !connected) ws.close(); }, 5000);
  ws.onopen = () => { if (socket === ws) send({type: 'hello', token, chrome: navigator.userAgent}); };
  ws.onmessage = e => { if (socket === ws) void handle(e.data); };
  ws.onclose = () => {
    clearTimeout(authTimer);
    if (socket !== ws) return;
    connected = false; end('connection_lost'); socket = null;
    reconnect = setTimeout(() => void connect(), retry + Math.random() * 500);
    retry = Math.min(retry * 2, 60000);
  };
  ws.onerror = () => ws.close();
}
function state() {
  return {connected, lastEnd, pending: pending && {...pending},
    grant: grant && {id: grant.id, tab: grant.tab, expires: grant.expires, reason: grant.reason}};
}
chrome.runtime.onMessage.addListener((m, sender, respond) => {
  const control = sender.url === chrome.runtime.getURL('control.html');
  const options = sender.url === chrome.runtime.getURL('options.html');
  if (sender.id !== chrome.runtime.id || (!control && !options)) return false;
  (async () => {
    await ready;
    if (m.type === 'state') return state();
    if (m.type === 'stop' && control) { end('local_stop'); return state(); }
    if (m.type === 'deny' && control) { if (pending?.id === m.id) end('local_denied'); return state(); }
    if (m.type === 'allow' && control) { await allow(m.id); return state(); }
    if (m.type === 'pair' && options) {
      const u = new URL(m.origin);
      if (u.protocol !== 'https:' || u.origin !== m.origin || u.username || u.password)
        refuse('BAD_ORIGIN', 'Enter an HTTPS Tether origin');
      if (typeof m.code !== 'string' || !/^[A-F0-9]{16}$/.test(m.code)) refuse('BAD_CODE', 'Enter a fresh pairing code');
      if (!await chrome.permissions.contains({origins: [u.origin + '/*']})) refuse('NO_PERMISSION', 'Allow this Tether origin first');
      const response = await fetch(u.origin + '/mobile/api/browser/extension/exchange', {
        method: 'POST', credentials: 'omit', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: m.code, name: 'Mac Chrome'}), signal: AbortSignal.timeout(10000)});
      if (!response.ok) refuse('PAIR_REFUSED', 'Pairing refused. Get a fresh code from Tether.');
      const pair = await response.json();
      if (typeof pair.token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(pair.token)) refuse('BAD_PAIR', 'Invalid device response');
      await chrome.storage.local.set({origin: u.origin, token: pair.token, device: pair.id, confirm: m.confirm === true});
      await connect(); return state();
    }
    refuse('BAD_MESSAGE', 'Unsupported local operation');
  })().then(respond, e => respond({error: e.code || 'ERROR', message: e.message}));
  return true;
});
chrome.action.onClicked.addListener(async () => {
  await ready;
  if (grant) end('action_stop');
  else if (pending) await showApproval();
  else await chrome.runtime.openOptionsPage();
});
// The global "Open Tether search" key: show the Tether tab and ask it to open search. Only the tab's hash changes;
// the page treats it as a request to open the menu and nothing more. No debugger, socket or page script is used.
chrome.commands.onCommand.addListener(async name => {
  if (name !== 'open-search') return;
  await ready;
  const {origin} = await chrome.storage.local.get('origin');
  const target = searchTarget(origin, await chrome.tabs.query({}), grant?.tab);
  if (!target) { await chrome.runtime.openOptionsPage(); return; }
  if (target.create) {
    const tab = await chrome.tabs.create({url: target.create, active: true});
    await chrome.windows.update(tab.windowId, {focused: true});
    return;
  }
  if (target.tab === grant?.tab) return;
  await chrome.windows.update(target.windowId, {focused: true});
  await chrome.tabs.update(target.tab, target.url ? {active: true, url: target.url} : {active: true});
});
chrome.debugger.onDetach.addListener(source => { if (source.tabId === grant?.tab) end('debugger_detached'); });
chrome.tabs.onRemoved.addListener(tab => { if (tab === grant?.tab) end('tab_closed'); });
chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  const g = grant;
  if (g?.ready && tabId === g.tab && connected && (change.url !== undefined || change.title !== undefined))
    send({type: 'tab', grant: g.id, tab: tabId, epoch, url: tab.url || 'about:blank', title: tab.title || ''});
});
chrome.windows.onRemoved.addListener(id => {
  if (id === approvalWindow) { approvalWindow = null; if (pending) end('approval_closed'); }
});
chrome.debugger.onEvent.addListener((source, method, params) => {
  const g = grant;
  if (!g?.ready || source.tabId !== g.tab || source.sessionId || method.startsWith('Target.')) return;
  if (!connected || Date.now() >= g.expires) { end('expired'); return; }
  if (socket.bufferedAmount > 4000000) { end('stream_backpressure'); return; }
  if (method === 'Page.screencastFrame') {
    if (g.inFlight >= 3) { end('stream_backpressure'); return; }
    g.frames.set(params.sessionId, (g.frames.get(params.sessionId) || 0) + 1); g.inFlight++;
  }
  send({type: 'event', grant: g.id, tab: g.tab, epoch, method, params});
});
const ready = (async () => {
  await chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
  const {orphanTab} = await chrome.storage.session.get('orphanTab');
  if (Number.isInteger(orphanTab)) await chrome.debugger.detach({tabId: orphanTab}).catch(() => {});
  badge();
})();
void ready.then(connect);
chrome.runtime.onStartup.addListener(() => { void ready.then(connect); });
// Alarms wake an idle worker after Chrome discards it. No grant survives that restart.
void chrome.alarms.create('reconnect', {periodInMinutes: 1});
chrome.alarms.onAlarm.addListener(() => { if (!socket) void ready.then(connect); });
setInterval(() => {
  if (pending && Date.now() >= pending.deadline) end('approval_expired');
  if (grant && Date.now() >= grant.expires) end('expired');
  if (connected && Date.now() - lastPong > 25000) { end('heartbeat_lost'); socket?.close(); }
}, 200);
setInterval(() => { if (connected) send({type: 'ping', epoch}); }, 10000);
