// The extension's "Open Tether search" command (extension/search.mjs): which tab it shows and what address it sets.
const assert = require('node:assert/strict')

;(async () => {
  const { pairedOrigin, isTetherTab, searchTarget } = await import('../extension/search.mjs')
  const origin = 'https://tether.test'

  assert.equal(pairedOrigin(origin), origin)
  for (const bad of ['', undefined, 'http://tether.test', 'https://tether.test/', 'https://a:b@tether.test', 'not a url'])
    assert.equal(pairedOrigin(bad), null, String(bad))
  assert.equal(searchTarget('http://tether.test', [], null), null, 'no valid pairing: the worker opens the options page')

  // Only the exact origin and /chat/ paths count. /chatx, /mobile/chat/ and look-alike hosts do not.
  assert.equal(isTetherTab({ url: 'https://tether.test/chat/' }, origin), true)
  assert.equal(isTetherTab({ url: 'https://tether.test/chat/?session=%251' }, origin), true)
  for (const url of ['https://tether.test/chatx', 'https://tether.test/mobile/chat/', 'https://tether.test.evil.example/chat/', 'http://tether.test/chat/', 'https://tether.test:8443/chat/', undefined])
    assert.equal(isTetherTab({ url }, origin), false, String(url))

  // No Tether tab: open a new one with ?go=search.
  assert.deepEqual(searchTarget(origin, [{ id: 1, url: 'https://example.com/' }], null), { create: 'https://tether.test/chat/?go=search' })

  // The most recent Tether tab wins; only its hash changes.
  const tabs = [
    { id: 1, windowId: 10, url: 'https://tether.test/chat/', lastAccessed: 5 },
    { id: 2, windowId: 20, url: 'https://tether.test/chat/?session=%253', lastAccessed: 9 },
    { id: 3, windowId: 30, url: 'https://tether.test/chatx', lastAccessed: 99 },
  ]
  assert.deepEqual(searchTarget(origin, tabs, null), { tab: 2, windowId: 20, url: 'https://tether.test/chat/?session=%253#search' })
  // The tab under a debugger task is never touched.
  assert.deepEqual(searchTarget(origin, tabs, 2), { tab: 1, windowId: 10, url: 'https://tether.test/chat/#search' })
  assert.deepEqual(searchTarget(origin, [tabs[1]], 2), { create: 'https://tether.test/chat/?go=search' })
  // A tab that already has #search is only focused: the same address again would replace its history entry.
  assert.deepEqual(searchTarget(origin, [{ id: 4, windowId: 1, url: 'https://tether.test/chat/#search' }], null), { tab: 4, windowId: 1, url: null })
  // Another hash is replaced, not kept.
  assert.equal(searchTarget(origin, [{ id: 5, windowId: 1, url: 'https://tether.test/chat/#x' }], null).url, 'https://tether.test/chat/#search')
  console.log('ok extension search target')
})().catch(error => { console.error(error); process.exit(1) })
