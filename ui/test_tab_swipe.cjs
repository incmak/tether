// Header swipe: a two-finger trackpad swipe over a chat's header steps one tab (tabSwipe.ts). Real TS, no DOM.
// Wheel sequences are shaped like macOS trackpad input: the finger part rises, then momentum falls off.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const mod = {}
runInNewContext(ts.transpileModule(readFileSync('src/tabSwipe.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports: mod, Math, Infinity })
const { createTabSwipe, THRESHOLD, QUIET_MS } = mod

// Feed deltas 16 ms apart from `start`. Returns the steps and how many events were handled (preventDefault).
function feed(wheel, deltas, { start = 0, gap = 16, dy = 0, deltaMode = 0, ctrlKey = false } = {}) {
  const steps = []
  let handled = 0, at = start
  for (const dx of deltas) {
    const result = wheel({ deltaX: dx, deltaY: dy, deltaMode, ctrlKey, timeStamp: at })
    if (result.handled) handled++
    if (result.step) steps.push(result.step)
    at += gap
  }
  return { steps, handled, end: at }
}
const swipe = [2, 6, 14, 26, 38, 44, 40, 30]  // the fingers
const momentum = [26, 22, 18, 15, 12, 10, 8, 6, 5, 4, 3, 2, 2, 1, 1, 1]  // after the fingers lift
const negate = list => list.map(dx => -dx)

// 1. One swipe with its momentum is one step. Fingers to the left (positive deltaX) go to the next tab.
{
  const run = feed(createTabSwipe(), [...swipe, ...momentum])
  assert.deepEqual(run.steps, [1])
  assert.equal(run.handled, swipe.length + momentum.length, 'every event of the swipe is handled: no Back or Forward')
  assert.deepEqual(feed(createTabSwipe(), negate([...swipe, ...momentum])).steps, [-1], 'fingers to the right: previous tab')
  console.log('ok one swipe is one step')
}

// 2. Threshold: a short drift does not switch, but it is still handled so the browser does not go Back.
{
  const wheel = createTabSwipe()
  const run = feed(wheel, [8, 8, 8, 8])
  assert.equal(THRESHOLD >= 40, true, "a trackpad brush is not a swipe")
  assert.deepEqual(run.steps, [])
  assert.equal(run.handled, 4)
  assert.deepEqual(feed(wheel, [THRESHOLD], { start: run.end }).steps, [1], 'the same gesture adds up')
  // A turn the other way starts the count again.
  assert.deepEqual(feed(createTabSwipe(), [30, -8, -8, 30]).steps, [])
  console.log('ok threshold')
}

// 3. Cooldown: a fast swipe with large deltas, and coalesced momentum (two deltas in one event), stays one step.
{
  assert.deepEqual(feed(createTabSwipe(), [4, 16, 60, 120, 180, 160, 120, 90, 60, 45, 34, 26, 20, 15, 11, 8, 6, 4, 3, 2, 1]).steps, [1])
  assert.deepEqual(feed(createTabSwipe(), [...swipe, 26, 40, 15, 22, 10, 14, 6, 9, 4, 2, 3, 1, 2, 1]).steps, [1], 'coalesced momentum events')
  assert.deepEqual(feed(createTabSwipe(), [2, 6, 14, 26, 38, 44, 18, 40, 30, ...momentum]).steps, [1], 'an uneven finger')
  console.log('ok cooldown')
}

// 4. A second swipe is a second step: after a quiet gap, or at once when it interrupts the momentum.
{
  const wheel = createTabSwipe()
  const first = feed(wheel, [...swipe, ...momentum])
  const second = feed(wheel, [...swipe, ...momentum], { start: first.end + QUIET_MS + 10 })
  assert.deepEqual([...first.steps, ...second.steps], [1, 1])
  // The fingers touch again mid-momentum: macOS stops the momentum and the new swipe starts small.
  const quick = feed(createTabSwipe(), [...swipe, 26, 22, 18, 15, 12, 10, 8, ...swipe, ...momentum])
  assert.deepEqual(quick.steps, [1, 1])
  const back = feed(createTabSwipe(), [...swipe, 26, 22, 18, ...negate(swipe)])
  assert.deepEqual(back.steps, [1, -1], 'a swipe the other way during momentum')
  console.log('ok second swipe')
}

// 5. Not a tab swipe: vertical scroll, pinch zoom. They pass through untouched.
{
  const vertical = feed(createTabSwipe(), [3, 5, 4, 6, 2], { dy: 30 })
  assert.deepEqual(vertical, { steps: [], handled: 0, end: vertical.end })
  const pinch = feed(createTabSwipe(), [...swipe], { ctrlKey: true })
  assert.equal(pinch.handled, 0)
  assert.deepEqual(pinch.steps, [])
  // A sideways gesture with some vertical noise stays one gesture: its later events are handled too.
  const wheel = createTabSwipe()
  feed(wheel, [20, 20], { dy: 2 })
  assert.equal(wheel({ deltaX: 1, deltaY: 6, deltaMode: 0, ctrlKey: false, timeStamp: 40 }).handled, true)
  console.log('ok vertical and pinch pass through')
}

// 6. A mouse that sends lines (Shift+wheel in Firefox): one notch is one step.
{
  const wheel = createTabSwipe()
  const one = feed(wheel, [3], { deltaMode: 1 })
  const two = feed(wheel, [3], { deltaMode: 1, start: one.end + QUIET_MS + 50 })
  assert.deepEqual([...one.steps, ...two.steps], [1, 1])
  console.log('ok line deltas')
}

// 7. A slow remount after a step: the next events come late, but the quiet time counts from when the tab was drawn.
{
  const wheel = createTabSwipe()
  const run = feed(wheel, swipe)
  wheel.hold(run.end + 400)
  assert.deepEqual(feed(wheel, momentum, { start: run.end + 300 }).steps, [], 'momentum after the hold is still eaten')
  const idle = createTabSwipe()
  idle.hold(1000)  // no step yet: hold does nothing
  assert.deepEqual(feed(idle, swipe, { start: 1001 }).steps, [1])
  console.log('ok hold after a slow remount')
}
