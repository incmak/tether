import assert from 'node:assert/strict';
import {setImmediate as nextTurn} from 'node:timers/promises';

const tabId = 7, epoch = 'e', grant = 'g';
const sent = [], acks = [];
let socket, onEvent, outstanding = 0;
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
    async sendCommand(target, method, params) {
      assert.deepEqual(target, {tabId});
      if (method === 'Page.getFrameTree') return {frameTree: {frame: {id: 'target'}}};
      if (method === 'Page.screencastFrameAck') acks.push(params.sessionId);
      if (method === 'Runtime.evaluate') {
        if (params.expression === 'hold') {
          outstanding++;
          return new Promise(() => {});
        }
        return {result: {type: 'number', value: 1}};
      }
      return {};
    },
  },
};
globalThis.WebSocket = class {
  static OPEN = 1;
  bufferedAmount = 0;
  constructor() { socket = this; }
  send(data) { sent.push(JSON.parse(data)); }
  receive(message) { this.onmessage({data: JSON.stringify({...message, epoch})}); }
};
globalThis.setTimeout = () => {};
globalThis.setInterval = () => {};

await import('../../extension/worker.js');
await nextTurn();
socket.readyState = WebSocket.OPEN;
socket.onopen();
assert.equal(sent[0].type, 'hello');
socket.receive({type: 'ready'});
socket.receive({type: 'grant', id: grant, reason: 'Test frames', minutes: 1});
await nextTurn();
assert.equal(sent.find(message => message.type === 'granted')?.target, 'target');

function command(id, method, params = {}) {
  socket.receive({type: 'command', id, grant, tab: tabId, method, params});
}
function frame(sessionId, number) {
  onEvent({tabId}, 'Page.screencastFrame', {sessionId, data: String(number)});
}
function forwardedFrames() {
  return sent.filter(message => message.type === 'event' && message.method === 'Page.screencastFrame')
    .map(message => Number(message.params.data));
}
function endReasons() {
  return sent.filter(message => message.type === 'ended').map(message => message.reason);
}
function reply(id) {
  return sent.find(message => message.id === id);
}

for (let number = 1; number <= 10; number++) frame(1, number);
assert.deepEqual(endReasons(), [], 'a frame burst must leave the grant active');
assert.deepEqual(forwardedFrames(), [1, 2, 3]);
assert.deepEqual(acks, [1, 1, 1, 1, 1, 1, 1]);

command('ack', 'Page.screencastFrameAck', {sessionId: 1});
await nextTurn();
assert.equal(reply('ack')?.type, 'result');
assert.equal(acks.length, 8);
frame(1, 11);
assert.deepEqual(forwardedFrames(), [1, 2, 3, 11]);
command('eval', 'Runtime.evaluate', {expression: '1'});
await nextTurn();
assert.deepEqual(reply('eval'), {type: 'result', id: 'eval', epoch, result: {result: {type: 'number', value: 1}}});

command('start', 'Page.startScreencast');
command('stale-after-start', 'Page.screencastFrameAck', {sessionId: 1});
await nextTurn();
assert.equal(reply('start')?.type, 'result');
assert.deepEqual(reply('stale-after-start'), {type: 'result', id: 'stale-after-start', epoch, result: {}});
assert.equal(acks.length, 8);

socket.bufferedAmount = 2000000;
frame(2, 12);
assert.deepEqual(forwardedFrames(), [1, 2, 3, 11]);
assert.deepEqual(acks.slice(8), [2]);
socket.bufferedAmount = 5000000;
frame(2, 13);
assert.deepEqual(forwardedFrames(), [1, 2, 3, 11]);
assert.deepEqual(acks.slice(8), [2, 2]);
assert.deepEqual(endReasons(), []);
socket.bufferedAmount = 0;
for (let number = 14; number <= 17; number++) frame(2, number);
assert.deepEqual(forwardedFrames(), [1, 2, 3, 11, 14, 15, 16]);
assert.deepEqual(acks.slice(8), [2, 2, 2]);

command('stop', 'Page.stopScreencast');
command('stale-after-stop', 'Page.screencastFrameAck', {sessionId: 2});
await nextTurn();
assert.equal(reply('stop')?.type, 'result');
assert.deepEqual(reply('stale-after-stop'), {type: 'result', id: 'stale-after-stop', epoch, result: {}});
assert.equal(acks.length, 11);

for (let i = 0; i < 129; i++) command(`hold-${i}`, 'Runtime.evaluate', {expression: 'hold'});
assert.equal(outstanding, 128);
assert.deepEqual(reply('hold-128'), {type: 'error', id: 'hold-128', epoch, code: 'LIMIT',
  message: 'Too many outstanding commands'});
assert.deepEqual(endReasons(), []);

socket.bufferedAmount = 5000000;
onEvent({tabId}, 'Runtime.consoleAPICalled', {});
assert.deepEqual(endReasons(), ['stream_overflow']);
