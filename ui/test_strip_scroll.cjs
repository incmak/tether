// Strip scroll: a two-finger trackpad swipe over a chat's stack row or tab strip, or BAND px above or below them,
// scrolls that strip; elsewhere in the header it does nothing; it never switches tabs (stripScroll.ts). Real TS, no DOM.
// Only the header listens (Chat.tsx): every scene here is under the pointer in the header.
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
const scene = (over = {}) => () => ({ strips: [stacks, tabs], native: false, ...over })
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

// 4. In the band below the tab strip: every event scrolls the tab strip by its own delta, 1:1. (In the app the chat
// lies there and has no listener; the logic still picks the strip for any header part that reaches below it.)
{
  const plans = feed(createStripWheel(), 1, 172, scene())
  assert.ok(plans.every(plan => plan.do === 'scroll' && plan.strip === 1), JSON.stringify(plans))
  assert.deepEqual(plans.map(plan => plan.dx), deltas)
}
// ... and 8 px above the stack row (over the title row) scrolls the stack row, to the left too.
{
  const plans = feed(createStripWheel(), -1, 76, scene())
  assert.ok(plans.every(plan => plan.do === 'scroll' && plan.strip === 0 && plan.dx <= 0), JSON.stringify(plans))
}

// 5. At the strip's end, or over a strip that does not overflow: the whole gesture is cancelled (no Back) and scrolled
// by hand, which moves nothing there.
for (const over of [{ scrollLeft: 1000 }, { scrollWidth: 1000 }]) {
  const end = strip(121, 165, over)
  const plans = feed(createStripWheel(), 1, 140, () => ({ strips: [stacks, end], native: false }))
  assert.ok(plans.every(plan => plan.do === 'scroll' && plan.strip === 1), JSON.stringify(plans))
}
// Review: at the start, the fingers go right (nothing to scroll), then turn left in the same gesture: that scrolls.
for (const deltaMode of [0, 1]) {
  const wheel = createStripWheel(), start = () => ({ strips: [stacks, strip(121, 165, { scrollLeft: 0 })], native: false })
  assert.equal(wheel(event(-20, 0, 140, { deltaMode }), start).do, 'scroll')
  const back = wheel(event(20, 16, 140, { deltaMode }), start)
  assert.equal(back.do, 'scroll'); assert.ok(back.dx > 0)
}

// 6. The title row outside the zone: no Back/Forward.
assert.ok(feed(createStripWheel(), 1, 40, scene()).every(plan => plan.do === 'block'))
// A horizontal scroller in the title row keeps its own scroll.
assert.ok(feed(createStripWheel(), 1, 40, scene({ native: true })).every(plan => plan.do === 'pass'))

// 7. A vertical scroll over a strip that overflows scrolls it sideways: down moves right, up moves left. A mouse notch
// glides; a trackpad's small deltas follow 1:1. Outside the strips (the band too) and over a strip that does not
// overflow, it passes. A pinch passes.
{
  const wheel = createStripWheel()
  assert.deepEqual({ ...wheel(event(0, 0, 140, { deltaY: 100 }), scene()) }, { do: 'scroll', strip: 1, dx: 100, glide: true })
  assert.deepEqual({ ...wheel(event(0, 400, 100, { deltaY: -100 }), scene()) }, { do: 'scroll', strip: 0, dx: -100, glide: true })
  assert.deepEqual({ ...wheel(event(0, 800, 100, { deltaY: 3, deltaMode: 1 }), scene()) }, { do: 'scroll', strip: 0, dx: 48 }, 'Firefox: 3 lines')
  assert.deepEqual({ ...wheel(event(0, 1200, 140, { deltaY: 4, deltaX: 1 }), scene()) }, { do: 'scroll', strip: 1, dx: 4 }, 'trackpad')
  assert.equal(wheel(event(0, 1600, 172, { deltaY: 100 }), scene()).do, 'pass', 'the band below the tab strip')
  assert.equal(wheel(event(0, 2000, 40, { deltaY: 100 }), scene()).do, 'pass', 'the title row')
  assert.equal(wheel(event(0, 2400, 140, { deltaY: 100 }), () => ({ strips: [stacks, strip(121, 165, { scrollWidth: 1000 })], native: false })).do, 'pass', 'no overflow')
  assert.equal(wheel(event(30, 2800, 172, { ctrlKey: true }), scene()).do, 'pass')
}
// The setting off: a vertical scroll over a strip still scrolls it; a sideways swipe passes (Back/Forward).
{
  const wheel = createStripWheel(false)
  assert.equal(wheel(event(0, 0, 140, { deltaY: 100 }), scene()).do, 'scroll')
  assert.ok(feed(wheel, 1, 140, scene(), 400).every(plan => plan.do === 'pass'))
}
// A trackpad swipe over a strip whose first event leans vertical, then goes sideways: the strip follows the sideways
// deltas.
{
  const wheel = createStripWheel()
  assert.equal(wheel(event(1, 0, 140, { deltaY: 3 }), scene()).dx, 3)
  assert.deepEqual({ ...wheel(event(-20, 16, 140, { deltaY: 2 }), scene()) }, { do: 'scroll', strip: 1, dx: -20 })
}

// 8. One decision per gesture: a later event with a vertical tilt still scrolls the strip; after QUIET_MS a new one.
{
  const wheel = createStripWheel()
  assert.equal(wheel(event(20, 0, 172), scene()).do, 'scroll')
  const tilted = wheel(event(1, 16, 172, { deltaY: 4 }), scene())
  assert.equal(tilted.do, 'scroll'); assert.equal(tilted.dx, 1)
  assert.equal(wheel(event(20, 16 + QUIET_MS + 1, 140), scene({ native: true })).do, 'pass', 'a new gesture: native over the strip')
}

// Review: a gesture that starts vertical outside the strips passes, all of it, also when it turns sideways: its later events cannot be
// cancelled, so a scroll by hand would scroll twice.
{
  const wheel = createStripWheel()
  assert.equal(wheel(event(0, 0, 172, { deltaY: 10 }), scene()).do, 'pass')
  assert.equal(wheel(event(30, 16, 172), scene()).do, 'pass')
  assert.equal(wheel(event(30, 32, 40), scene()).do, 'pass')
  assert.equal(wheel(event(30, 32 + QUIET_MS + 1, 172), scene()).do, 'scroll', 'after a pause: a new gesture')
}

// 9. Line and page deltas (a mouse): scaled to px.
assert.equal(createStripWheel()(event(3, 0, 172, { deltaMode: 1 }), scene()).dx, 48)

console.log('strip scroll: PASS')
