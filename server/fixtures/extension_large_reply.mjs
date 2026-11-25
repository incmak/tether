import assert from 'node:assert/strict';
import {setImmediate as nextTurn} from 'node:timers/promises';

const scenario = process.argv[2];
const CHUNK = 1_000_000;
const tabId = 7, epoch = 'e', grant = 'g', id = 'reply';
const sent = [], intervals = new Map(), timers = new Map();
let socket, onEvent, result, now = 0, timerId = 0;
const ignoredEvent = {addListener() {}};
globalThis.chrome = {
  action: {setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {}, onClicked: ignoredEvent},
  runtime: {onMessage: ignoredEvent, onStartup: ignoredEvent},
  storage: {
    local: {async setAccessLevel() {}, async get() { return {origin: 'https://tether.test', token: 'test'}; }},
    session: {async get() { return {}; }, async set() {}},
  },
  tabs: {async create() { return {id: tabId}; }, onRemoved: ignoredEvent, onUpdated: ignoredEvent},
  windows: {onRemoved: ignoredEvent},
  commands: {onCommand: ignoredEvent},
  alarms: {create() {}, onAlarm: ignoredEvent},
  debugger: {
    async attach() {}, async detach() {}, onDetach: ignoredEvent,
    onEvent: {addListener(listener) { onEvent = listener; }},
    async sendCommand(_target, method) {
      if (method === 'Page.getFrameTree') return {frameTree: {frame: {id: 'target'}}};
      return result;
    },
  },
};
globalThis.WebSocket = class {
  static OPEN = 1;
  bufferedAmount = 0;
  constructor() { socket = this; }
  send(data) {
    sent.push(JSON.parse(data));
    this.bufferedAmount += Buffer.byteLength(data);
  }
  receive(message) { this.onmessage({data: JSON.stringify({...message, epoch})}); }
  close() { this.readyState = 3; this.onclose(); }
};
Date.now = () => now;
globalThis.setInterval = (fn, ms) => intervals.set(ms, fn);
globalThis.setTimeout = (fn, ms) => {
  timers.set(++timerId, {fn, at: now + ms});
  return timerId;
};
globalThis.clearTimeout = id => timers.delete(id);

async function poll() {
  now += 100;
  for (const [id, {fn, at}] of [...timers]) {
    if (at <= now) {
      timers.delete(id);
      fn();
    }
  }
  await nextTurn();
}

await import('../../extension/worker.js');
await nextTurn();
socket.readyState = WebSocket.OPEN;
socket.onopen();
assert.equal(sent[0].type, 'hello');
socket.receive(scenario === 'legacy' ? {type: 'ready'} : {type: 'ready', chunks: true});
socket.receive({type: 'grant', id: grant, reason: 'Test reply', minutes: 1});
await nextTurn();
assert.equal(sent.find(message => message.type === 'granted')?.target, 'target');
socket.bufferedAmount = 0;

function replies() {
  return sent.filter(message => message.id === id);
}
function endReasons() {
  return sent.filter(message => message.type === 'ended').map(message => message.reason);
}
async function evaluate(value) {
  result = {result: {type: 'string', value}};
  socket.receive({type: 'command', id, grant, tab: tabId, method: 'Runtime.evaluate', params: {expression: 'value'}});
  await nextTurn();
}
function expectedReply() {
  return JSON.stringify({type: 'result', id, epoch, result});
}
async function drain() {
  for (let i = 0; i < 6; i++) {
    socket.bufferedAmount = 0;
    await poll();
  }
  const chunks = replies();
  assert.ok(chunks.length > 1);
  for (const [seq, message] of chunks.entries()) {
    assert.equal(message.type, 'chunk');
    assert.equal(message.epoch, epoch);
    assert.equal(message.seq, seq);
    assert.equal(message.total, chunks.length);
    assert.ok(message.data.length <= CHUNK);
  }
  assert.ok(chunks.map(message => message.data).join('') === expectedReply(), 'joined reply differs');
  return chunks;
}

switch (scenario) {
  case 'paced': {
    socket.bufferedAmount = CHUNK;
    await evaluate('A'.repeat(5 * CHUNK));
    assert.equal(replies().length, 0, 'a full buffer must pause the first chunk');
    await poll();
    assert.equal(replies().length, 0, 'a full buffer must stay paused');
    socket.bufferedAmount--;
    await poll();
    assert.equal(replies().length, 1, 'the first chunk can send after the buffer drains');
    await poll();
    assert.equal(replies().length, 1, 'the second chunk must wait for room');
    await drain();
    break;
  }
  case 'event':
    await evaluate('A'.repeat(5 * CHUNK));
    onEvent({tabId}, 'Runtime.consoleAPICalled', {});
    assert.deepEqual(endReasons(), []);
    assert.ok(sent.some(message => message.type === 'event' && message.method === 'Runtime.consoleAPICalled'));
    await drain();
    break;
  case 'draining':
  case 'stuck':
    socket.bufferedAmount = 2 * CHUNK;
    for (now = 200; now <= 26_000 && socket.readyState === WebSocket.OPEN; now += 200) {
      if (scenario === 'draining') socket.bufferedAmount -= 10;
      intervals.get(200)();
    }
    assert.deepEqual(endReasons(), scenario === 'draining' ? [] : ['heartbeat_lost']);
    assert.equal(socket.readyState === WebSocket.OPEN, scenario === 'draining');
    break;
  case 'too-large': {
    await evaluate('A'.repeat(64 * 1024 * 1024 + 1));
    assert.equal(replies().length, 1);
    assert.equal(replies()[0].type, 'error');
    assert.equal(replies()[0].code, 'TOO_LARGE');
    assert.match(replies()[0].message, /64 MB.*(?:clip|JPEG)/);
    assert.deepEqual(endReasons(), []);
    break;
  }
  case 'legacy':
    await evaluate('A'.repeat(5 * CHUNK));
    assert.equal(replies().length, 1);
    assert.ok(JSON.stringify(replies()[0]) === expectedReply(), 'legacy result differs');
    break;
  case 'surrogate': {
    const prefix = '{"type":"result","id":"reply","epoch":"e","result":{"result":{"type":"string","value":"';
    await evaluate('é'.repeat(CHUNK - prefix.length - 1) + '😀' + 'A'.repeat(CHUNK));
    const chunks = await drain();
    assert.equal(chunks[0].data.length, CHUNK - 1);
    assert.ok(chunks[1].data.startsWith('😀'));
    break;
  }
  case 'ended':
    socket.bufferedAmount = CHUNK;
    await evaluate('A'.repeat(5 * CHUNK));
    socket.receive({type: 'end', id: grant});
    socket.bufferedAmount = 0;
    await poll();
    assert.equal(replies().length, 0, 'ending the grant must discard the paused reply');
    assert.deepEqual(endReasons(), ['tether_end']);
    break;
  default:
    assert.fail('Unknown scenario');
}
