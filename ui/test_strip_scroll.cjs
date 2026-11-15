// Strip scroll: a two-finger trackpad swipe over a chat's stack row or tab strip, or BAND px above or below them,
// scrolls that strip; elsewhere in the header it does nothing; it never switches tabs (stripScroll.ts). Real TS, no DOM.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const mod = {}
runInNewContext(ts.transpileModule(readFileSync('src/stripScroll.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports: mod, Math, Infinity })
const { createStripWheel, nearestStrip, room, BAND, QUIET_MS } = mod
assert.equal(BAND, 10)

// The header at 1280 px: title row 0-83, stack row 84-120, tab strip 121-165, chat below. Both strips overflow.
const strip = (top, bottom, extra) => ({ top, bottom, left: 0, right: 1000, scrollLeft: 100, scrollWidth: 2000, clientWidth: 1000, ...extra })
const stacks = strip(84, 120), tabs = strip(121, 165)
const scene = (over = {}) => () => ({ strips: [stacks, tabs], native: false, header: false, ...over })
const event = (dx, at, y, over = {}) => ({ deltaX: dx, deltaY: 0, deltaMode: 0, ctrlKey: false, timeStamp: at, clientX: 500, clientY: y, ...over })
// One gesture: the fingers, then momentum, 16 ms apart.
const deltas = [2, 6, 14, 26, 38, 44, 40, 30, 26, 18, 12, 8, 5, 3, 2, 1]
function feed(wheel, sign, y, look, start = 0, over = {}) {
  return deltas.map((dx, i) => wheel(event(sign * dx, start + i * 16, y, over), look))
}

// 1. Hit zone: inside a strip, and BAND px above or below; the nearer strip where the zones overlap.
assert.equal(nearestStrip([stacks, tabs], 500, 100), 0)
assert.equal(nearestStrip([stacks, tabs], 500, 140), 1)
assert.equal(nearestStrip([stacks, tabs], 500, 74), 0, '10 px above the stack row')
assert.equal(nearestStrip([stacks, tabs], 500, 73), -1, '11 px above: the title row')
assert.equal(nearestStrip([stacks, tabs], 500, 175), 1, '10 px below the tab strip')
assert.equal(nearestStrip([stacks, tabs], 500, 176), -1, '11 px below: the chat')
const apart = [strip(100, 120), strip(130, 160)]  // zones overlap from 120 to 130
assert.equal(nearestStrip(apart, 500, 123), 0, 'nearer to the upper strip')
assert.equal(nearestStrip(apart, 500, 128), 1, 'nearer to the lower strip')
assert.equal(nearestStrip([strip(100, 100)], 500, 100), -1, 'a hidden strip (no height) is not a target')

// 2. Room: a strip at its start cannot scroll back; at its end it cannot scroll on; no overflow, no room.
assert.equal(room(strip(0, 1, { scrollLeft: 0 }), -5), false)
assert.equal(room(strip(0, 1, { scrollLeft: 0 }), 5), true)
assert.equal(room(strip(0, 1, { scrollLeft: 1000 }), 5), false)
assert.equal(room(strip(0, 1, { scrollLeft: 999.6 }), 5), false, 'a fraction of a pixel is the end')
assert.equal(room(strip(0, 1, { scrollWidth: 1000 }), 5), false)

// 3. Over a strip that can scroll: the browser scrolls it natively, every event passes; no tab step exists at all.
{
  const plans = feed(createStripWheel(), 1, 140, scene({ native: true }))
  assert.ok(plans.every(plan => plan.do === 'pass'), JSON.stringify(plans))
}

// 4. In the band below the tab strip (over the chat): every event scrolls the tab strip by its own delta, 1:1.
{
  const plans = feed(createStripWheel(), 1, 172, scene())
  assert.ok(plans.every(plan => plan.do === 'scroll' && plan.strip === 1), JSON.stringify(plans))
  assert.deepEqual(plans.map(plan => plan.dx), deltas)
}
// ... and 8 px above the stack row (over the title row) scrolls the stack row, to the left too.
{
  const plans = feed(createStripWheel(), -1, 76, scene({ header: true }))
  assert.ok(plans.every(plan => plan.do === 'scroll' && plan.strip === 0 && plan.dx <= 0), JSON.stringify(plans))
}

// 5. At the strip's end, or over a strip that does not overflow: cancel the whole gesture (no Back), scroll nothing.
for (const over of [{ scrollLeft: 1000 }, { scrollWidth: 1000 }]) {
  const end = strip(121, 165, over)
  const plans = feed(createStripWheel(), 1, 140, () => ({ strips: [stacks, end], native: false, header: true }))
  assert.ok(plans.every(plan => plan.do === 'block'), JSON.stringify(plans))
}

// 6. The title row outside the zone: no Back/Forward. The chat outside the zone: B81 Back/Forward (pass).
assert.ok(feed(createStripWheel(), 1, 40, scene({ header: true })).every(plan => plan.do === 'block'))
assert.ok(feed(createStripWheel(), 1, 400, scene()).every(plan => plan.do === 'pass'))
// A horizontal scroller in the title row keeps its own scroll.
assert.ok(feed(createStripWheel(), 1, 40, scene({ header: true, native: true })).every(plan => plan.do === 'pass'))

// 7. A vertical wheel (a mouse too) and a pinch pass through, also over a strip.
{
  const wheel = createStripWheel()
  assert.equal(wheel(event(0, 0, 140, { deltaY: 120 }), scene()).do, 'pass')
  assert.equal(wheel(event(0, 400, 172, { deltaY: 3, deltaX: 1 }), scene()).do, 'pass')
  assert.equal(wheel(event(30, 800, 172, { ctrlKey: true }), scene()).do, 'pass')
}

// 8. One decision per gesture: a later event with a vertical tilt still scrolls the strip; after QUIET_MS a new one.
{
  const wheel = createStripWheel()
  assert.equal(wheel(event(20, 0, 172), scene()).do, 'scroll')
  const tilted = wheel(event(1, 16, 172, { deltaY: 4 }), scene())
  assert.equal(tilted.do, 'scroll'); assert.equal(tilted.dx, 1)
  assert.equal(wheel(event(20, 16 + QUIET_MS + 1, 400), scene()).do, 'pass', 'a new gesture over the chat')
}

// 9. Line and page deltas (a mouse): scaled to px.
assert.equal(createStripWheel()(event(3, 0, 172, { deltaMode: 1 }), scene()).dx, 48)

console.log('strip scroll: PASS')
