// Photo backdrop: placeholder checks, load before show, retries on a dropped link, and the old-look upgrade.
// A tiny hook runner stands in for React; timers and images are fakes. No network, no terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const compile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

// One component, hooks in call order, effects run after each render when their deps change.
function hookRunner() {
  let slots = [], at = 0, pending = [], rerender = null
  const react = {
    useState(initial) {
      const slot = at++
      if (!(slot in slots)) slots[slot] = { value: initial }
      return [slots[slot].value, value => { slots[slot].value = value; rerender?.() }]
    },
    useRef(initial) { const slot = at++; if (!(slot in slots)) slots[slot] = { current: initial }; return slots[slot] },
    useEffect(effect, deps) {
      const slot = at++, old = slots[slot]
      if (!old || deps.some((dep, i) => !Object.is(dep, old.deps[i]))) pending.push({ slot, effect, deps })
      else slots[slot] = old
    },
  }
  function mount(hook) {
    let result
    rerender = () => {
      at = 0; pending = []; result = hook()
      for (const { slot, effect, deps } of pending) { slots[slot]?.cleanup?.(); slots[slot] = { deps, cleanup: effect() } }
    }
    rerender()
    return { get: () => result, again: () => rerender(), unmount: () => { for (const slot of slots) slot?.cleanup?.(); rerender = null } }
  }
  return { react, mount }
}

function fakeWindow() {
  let now = 0, id = 0
  const timers = new Map(), listeners = new Map()
  return {
    setTimeout(fn, ms) { timers.set(++id, { at: now + ms, fn }); return id },
    clearTimeout(timer) { timers.delete(timer) },
    addEventListener(name, fn) { listeners.set(name, fn) },
    removeEventListener(name, fn) { if (listeners.get(name) === fn) listeners.delete(name) },
    fire(name) { listeners.get(name)?.() },
    has: name => listeners.has(name),
    pending: () => timers.size,
    async advance(ms) {
      now += ms
      for (const [timer, { at, fn }] of [...timers]) if (at <= now) { timers.delete(timer); fn() }
      await flush()
    },
  }
}
// Objects made inside the vm have other prototypes, so compare their JSON.
const same = (actual, expected) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected)
const flush = () => new Promise(resolve => setImmediate(resolve))

// Each new Image takes the next outcome: 'ok', 'fail' (not loaded) or 'broken' (loaded, decode rejects).
function fakeImages(outcomes) {
  const made = []
  class Image {
    constructor() { this.complete = false; this.naturalWidth = 0; made.push(this) }
    decode() {
      const outcome = outcomes.shift() ?? 'ok'
      if (outcome === 'ok') return Promise.resolve()
      if (outcome === 'broken') { this.complete = true; this.naturalWidth = 2560 }
      return Promise.reject(new Error('decode'))
    }
  }
  return { Image, made }
}

// The real patterns from layouts.ts; the rest of that module needs the theme manifest.
const pattern = name => new RegExp(readFileSync('src/layouts.ts', 'utf8').match(new RegExp(`${name} = /(.+)/\\n`))[1])
const layouts = { DEFAULT_LOOK: { layout: 'classic' }, PHOTO_URL: pattern('PHOTO_URL'), PHOTO_PLACEHOLDER: pattern('PHOTO_PLACEHOLDER') }
const API = {}   // The app passes one stable api object.
function load({ react, window, Image, list = async () => [] }) {
  const exports = {}
  runInNewContext(compile(readFileSync('src/photoLook.ts', 'utf8')), {
    exports, window, Image, Promise, Error, Math, Object,
    require(name) {
      if (name === 'react') return react
      if (name === './backgrounds') return { listBackgrounds: list }
      if (name === './layouts') return layouts
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  return exports
}

const ID = 'a'.repeat(32)
const URL = `/mobile/api/backgrounds/${ID}.png`
const SMALL = `/mobile/api/backgrounds/${ID}.webp`
const PH = 'data:image/webp;base64,UklGRjQAAABXRUJQ'

async function main() {
  // Photo URLs: Tether's own files only, including the small copy of an old WebP.
  for (const good of [URL, SMALL, `/mobile/api/backgrounds/${ID}.small.webp`]) assert.ok(layouts.PHOTO_URL.test(good), good)
  for (const bad of [`/mobile/api/backgrounds/${ID}.thumb.webp`, `https://x.test/${ID}.webp`, `/mobile/api/backgrounds/${ID}.webp") ; x`])
    assert.ok(!layouts.PHOTO_URL.test(bad), bad)
  // The placeholder pattern takes a short WebP data URI only.
  assert.ok(layouts.PHOTO_PLACEHOLDER.test(PH))
  for (const bad of ['data:image/png;base64,AAAA', 'data:image/webp;base64,AA")', 'javascript:alert(1)', `data:image/webp;base64,${'A'.repeat(4001)}`])
    assert.ok(!layouts.PHOTO_PLACEHOLDER.test(bad), bad)

  { // The full photo waits for decode; the placeholder shows at once.
    const { react, mount } = hookRunner(), window = fakeWindow(), images = fakeImages(['ok'])
    const { usePhotoStage } = load({ react, window, Image: images.Image })
    const stage = mount(() => usePhotoStage(API, { id: ID, url: SMALL, placeholder: PH }, () => {}))
    same(stage.get(), { '--g-photo-ph': `url("${PH}")` })
    await flush(); stage.again()
    same(stage.get(), { '--g-photo': `url("${SMALL}")`, '--g-photo-ph': `url("${PH}")` })
    stage.unmount(); assert.ok(!window.has('online'))
  }

  { // A dropped link keeps the placeholder and retries; the photo shows only once it has loaded.
    const { react, mount } = hookRunner(), window = fakeWindow(), images = fakeImages(['fail', 'fail', 'ok'])
    const { usePhotoStage } = load({ react, window, Image: images.Image })
    const stage = mount(() => usePhotoStage(API, { id: ID, url: SMALL, placeholder: PH }, () => {}))
    await flush(); stage.again()
    assert.equal(stage.get()['--g-photo'], undefined)
    assert.equal(window.pending(), 1)
    window.fire('online'); await flush(); stage.again()   // the link returns: try now, not at the timer
    assert.equal(stage.get()['--g-photo'], undefined)
    await window.advance(15000); stage.again()
    assert.equal(stage.get()['--g-photo'], `url("${SMALL}")`)
    assert.equal(images.made.length, 3)
    stage.unmount(); assert.equal(window.pending(), 0)
  }

  { // An 'online' try while another is still pending makes the older one stale: one timer, none after unmount.
    const { react, mount } = hookRunner(), window = fakeWindow(), images = fakeImages(['fail', 'fail'])
    const { usePhotoStage } = load({ react, window, Image: images.Image })
    const stage = mount(() => usePhotoStage(API, { id: ID, url: SMALL, placeholder: PH }, () => {}))
    window.fire('online')   // before the first decode settles
    await flush()
    assert.equal(images.made.length, 2); assert.equal(window.pending(), 1)
    stage.unmount(); assert.equal(window.pending(), 0)
    await window.advance(60000); assert.equal(images.made.length, 2)
  }

  { // Loaded but not decodable here: it is complete, so it shows. Retries stop after the last one.
    const { react, mount } = hookRunner(), window = fakeWindow(), images = fakeImages(['broken'])
    const { usePhotoStage } = load({ react, window, Image: images.Image })
    const stage = mount(() => usePhotoStage(API, { id: ID, url: SMALL, placeholder: PH }, () => {}))
    await flush(); stage.again()
    assert.equal(stage.get()['--g-photo'], `url("${SMALL}")`)
    const gone = fakeImages(['fail', 'fail', 'fail', 'fail', 'fail'])
    const runner = hookRunner(), quiet = fakeWindow()
    const lost = load({ react: runner.react, window: quiet, Image: gone.Image })
    const view = runner.mount(() => lost.usePhotoStage(API, { id: ID, url: SMALL }, () => {}))
    for (const ms of [0, 4000, 15000, 60000, 60000]) await quiet.advance(ms)
    view.again()
    assert.equal(gone.made.length, 4); assert.equal(quiet.pending(), 0)
    same(view.get(), {})
  }

  { // An old look without a placeholder asks the list again until the server has made the small copies.
    const { react, mount } = hookRunner(), window = fakeWindow(), images = fakeImages([])
    const answers = [[{ id: ID, url: URL }], [{ id: ID, url: SMALL, thumb: 'x', placeholder: PH }]]
    let asked = 0, prefs = { look: { backdrop: 'photo', photo: { id: ID, url: URL } }, myLook: { photo: { id: ID, url: URL } } }
    const list = async () => { asked++; return answers.shift() }
    const { usePhotoStage } = load({ react, window, Image: images.Image, list })
    const update = change => { prefs = { ...prefs, ...change(prefs) } }
    const stage = mount(() => usePhotoStage(API, prefs.look.photo, update))
    await flush()
    assert.equal(asked, 1); assert.equal(prefs.look.photo.placeholder, undefined)
    await window.advance(4000)
    assert.equal(asked, 2)
    const fresh = { id: ID, url: SMALL, placeholder: PH }
    same(prefs.look.photo, fresh); same(prefs.myLook.photo, fresh)
    stage.again(); await flush(); stage.again()
    assert.equal(stage.get()['--g-photo-ph'], `url("${PH}")`)
    assert.equal(asked, 2)
    stage.unmount()
  }

  { // A bad placeholder from the server is not stored; a deleted photo stops the checks.
    const { react, mount } = hookRunner(), window = fakeWindow()
    let asked = 0, changed = false
    const list = async () => { asked++; return asked === 1 ? [{ id: ID, url: SMALL, placeholder: 'data:image/webp;base64,")x' }] : [] }
    const { usePhotoStage } = load({ react, window, Image: fakeImages([]).Image, list })
    const stage = mount(() => usePhotoStage(API, { id: ID, url: URL }, () => { changed = true }))
    await flush(); await window.advance(4000); await window.advance(15000)
    assert.equal(asked, 2); assert.equal(changed, false); assert.equal(window.pending(), 0)
    stage.unmount()
  }

  { // applyPhoto stores a valid placeholder and drops a bad one.
    let look = {}
    const update = change => { look = change({ look })?.look ?? look }
    const fail = { Image: class { decode() { return Promise.reject(new Error('no canvas here')) } } }
    const { applyPhoto } = load({ react: hookRunner().react, window: fakeWindow(), Image: fail.Image })
    await applyPhoto({ id: ID, url: SMALL, placeholder: PH }, update)
    same(look.photo, { id: ID, url: SMALL, placeholder: PH })
    await applyPhoto({ id: ID, url: SMALL, placeholder: 'data:text/html,<b>' }, update)
    same(look.photo, { id: ID, url: SMALL })
  }
  console.log('photo stage: ok')
}
main().catch(error => { console.error(error); process.exit(1) })
