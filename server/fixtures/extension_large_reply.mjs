import assert from 'node:assert/strict';
import {setImmediate as nextTurn} from 'node:timers/promises';

const scenario = process.argv[2];
const CHUNK = 1_000_000;
const encoder = new TextEncoder();
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
  peakBufferedAmount = 0;
  constructor() { socket = this; }
  send(data) {
    sent.push(JSON.parse(data));
    this.bufferedAmount += encoder.encode(data).length;
    this.peakBufferedAmount = Math.max(this.peakBufferedAmount, this.bufferedAmount);
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
async function evaluate(value, commandId = id) {
  result = {result: {type: 'string', value}};
  socket.receive({type: 'command', id: commandId, grant, tab: tabId, method: 'Runtime.evaluate',
    params: {expression: 'value'}});
  await nextTurn();
}
function expectedReply(commandId = id) {
  return JSON.stringify({type: 'result', id: commandId, epoch, result});
}
async function drain(bufferedAmount = 0) {
  for (let i = 0; i < 6; i++) {
    socket.bufferedAmount = bufferedAmount;
    await poll();
  }
  const chunks = replies();
  assert.ok(chunks.length > 1);
  for (const [seq, message] of chunks.entries()) {
    assert.equal(message.type, 'chunk');
    assert.equal(message.epoch, epoch);
    assert.equal(message.seq, seq);
    assert.equal(message.total, chunks.length);
    assert.ok(encoder.encode(message.data).length <= CHUNK, 'chunk exceeds the UTF-8 byte limit');
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
  case 'unicode-paced': {
    socket.bufferedAmount = 999_000;
    await evaluate('अ'.repeat(CHUNK));
    onEvent({tabId}, 'Runtime.consoleAPICalled', {value: 'A'.repeat(4000)});
    onEvent({tabId}, 'Runtime.consoleAPICalled', {});
    assert.deepEqual(endReasons(), []);
    assert.equal(sent.filter(message => message.type === 'event').length, 2);
    const chunks = await drain(999_000);
    assert.ok(socket.peakBufferedAmount < 2_100_000, 'slow upload exceeded the buffer budget');
    console.log(JSON.stringify(chunks));
    break;
  }
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
  case 'too-large':
  case 'too-large-devanagari':
  case 'too-large-emoji': {
    const value = scenario === 'too-large-devanagari' ? 'अ'.repeat(23_000_000) :
      scenario === 'too-large-emoji' ? '😀'.repeat(17_000_000) : 'A'.repeat(64 * 1024 * 1024 + 1);
    await evaluate(value);
    assert.equal(replies().length, 1);
    assert.equal(replies()[0].type, 'error');
    assert.equal(replies()[0].code, 'TOO_LARGE');
    const size = scenario === 'too-large-devanagari' ? 66 : 65;
    assert.match(replies()[0].message, new RegExp(`Reply is ${size} MB; the limit is 64 MB.*(?:clip|JPEG)`));
    assert.ok(replies()[0].message.length < 200);
    assert.deepEqual(endReasons(), []);
    assert.equal(socket.readyState, WebSocket.OPEN);
    await evaluate('still connected', 'next');
    assert.equal(JSON.stringify(sent.at(-1)), expectedReply('next'), 'grant must still accept commands');
    break;
  }
  case 'legacy':
    await evaluate('A'.repeat(5 * CHUNK));
    assert.equal(replies().length, 1);
    assert.ok(JSON.stringify(replies()[0]) === expectedReply(), 'legacy result differs');
    break;
  case 'surrogate': {
    const prefix = '{"type":"result","id":"reply","epoch":"e","result":{"result":{"type":"string","value":"';
    const bytes = CHUNK - prefix.length - 3;
    await evaluate('é'.repeat(Math.floor(bytes / 2)) + 'A'.repeat(bytes % 2) + '😀' + 'A'.repeat(CHUNK));
    const chunks = await drain();
    assert.equal(encoder.encode(chunks[0].data).length, CHUNK - 3);
    assert.ok(chunks[1].data.startsWith('😀'));
    break;
  }
  case 'single-unicode': {
    const prefix = '{"type":"result","id":"reply","epoch":"e","result":{"result":{"type":"string","value":"';
    const remaining = CHUNK - prefix.length - '"}}}'.length;
    const exact = 'é'.repeat(Math.floor(remaining / 2)) + 'A'.repeat(remaining % 2);
    await evaluate(exact, 'test1');
    assert.equal(sent.filter(message => message.id === 'test1').length, 1);
    assert.equal(sent.at(-1).type, 'result');
    assert.equal(encoder.encode(expectedReply('test1')).length, CHUNK);
    socket.bufferedAmount = 0;
    await evaluate(exact + 'A');
    await drain();
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
