// B26: lazy screen chunks and build-time brotli. No browser, no server, no terminal writes.
const { strict: assert } = require('node:assert')
const { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const { brotliDecompressSync } = require('node:zlib')
const ts = require('typescript')

const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
function load(file, modules, globals = {}) {
  const exports = {}
  const source = compile(readFileSync(file, 'utf8').replaceAll('import.meta.url', '__filename'))
  new Function('exports', 'require', '__filename', ...Object.keys(globals), source)(exports, name => {
    if (!(name in modules)) throw new Error(`unexpected import ${name}`)
    return modules[name]()
  }, join(__dirname, file), ...Object.values(globals))
  return exports
}
const flush = async () => { for (let at = 0; at < 10; at++) await Promise.resolve() }

function fakeDocument(files, prefetch = true) {
  const head = [], json = files === undefined ? null : { textContent: typeof files === 'string' ? files : JSON.stringify(files) }
  Object.defineProperty(head, 'append', { value: hint => head.push(hint) })
  return {
    head, getElementById: id => (id === 'panel-files' ? json : null),
    createElement: () => ({ relList: { supports: rel => prefetch && rel === 'prefetch' } }),
  }
}
// Real lazyPanels.ts with stub chunks. `head` gets {rel, href} hints; `fetched` gets fallback fetches.
function panelsFor(loads = [], fetched = []) {
  const chunk = name => () => { loads.push(name); return {} }
  return load('src/lazyPanels.ts', {
    react: () => ({ lazy: factory => ({ factory }) }),
    './Explorer': chunk('explorer'), './ExplorerPhone': chunk('explorerPhone'), './Share': chunk('share'), './Live': chunk('live'),
  }, { navigator: {}, fetch: (url, init) => { fetched.push([url, init.priority]); return Promise.reject(new Error('offline')) } })
}
const FILES = { explorer: ['/chat/assets/Explorer-a.js', '/chat/assets/Explorer-a.css'], explorerPhone: ['/chat/assets/ExplorerPhone-b.js'], share: ['/chat/assets/Share-c.js'], live: ['/chat/assets/Live-d.js'] }
const hrefs = doc => doc.head.map(hint => { assert.equal(hint.crossOrigin, ''); return `${hint.rel} ${hint.href}` })

async function prefetch() {
  const loads = [], waiting = [], idle = run => waiting.push(run)
  const panels = panelsFor(loads)

  const saving = fakeDocument(FILES)
  panels.prefetchPanels(true, idle, true, saving)
  assert.equal(waiting.length, 0); assert.deepEqual(saving.head, [])
  console.log('UI B26: Save-Data turns prefetch off')

  const wide = fakeDocument(FILES)
  panels.prefetchPanels(true, idle, false, wide)
  assert.deepEqual(wide.head, []); assert.equal(waiting.length, 1)  // nothing before the browser is idle
  waiting.shift()()
  assert.deepEqual(hrefs(wide), ['prefetch /chat/assets/Explorer-a.js', 'prefetch /chat/assets/Explorer-a.css']); assert.equal(waiting.length, 1)
  waiting.shift()()
  assert.deepEqual(hrefs(wide).slice(2), ['prefetch /chat/assets/Share-c.js']); assert.equal(waiting.length, 0)
  assert.deepEqual(loads, [])  // a prefetch never runs the module or Vite's import helper
  console.log('UI B26: desktop prefetches Files, then Share, one screen per idle slot, as link hints; never Live; no module runs')

  const phone = fakeDocument(FILES)
  panels.prefetchPanels(false, run => run(), false, phone)
  assert.deepEqual(hrefs(phone), ['prefetch /chat/assets/ExplorerPhone-b.js', 'prefetch /chat/assets/Share-c.js'])
  console.log('UI B26: phone prefetches phone Files, then Share')

  for (const doc of [fakeDocument(undefined), fakeDocument('{broken'), fakeDocument({})]) {
    panels.prefetchPanels(true, run => run(), false, doc)
    assert.deepEqual(doc.head, [])
  }
  console.log('UI B26: no file list (dev server) or a broken one: nothing is prefetched, nothing throws')

  const fetched = [], safari = fakeDocument(FILES, false)
  panelsFor([], fetched).prefetchPanels(true, run => run(), false, safari)
  await flush()
  assert.deepEqual(safari.head, [])
  assert.deepEqual(fetched.map(([url]) => url), [...FILES.explorer, ...FILES.share])
  assert.ok(fetched.every(([, priority]) => priority === 'low'))
  console.log('UI B26: without rel=prefetch (Safari), a low-priority fetch fills the cache; a failed fetch is silent')

  await panels.Explorer.factory(); await panels.Live.factory()
  assert.deepEqual(loads, ['explorer', 'live'])
  console.log('UI B26: each lazy screen loads its own chunk')
}

function brotli() {
  const stub = () => ({ default: () => ({}) })
  const config = load('vite.config.ts', { vite: () => ({ defineConfig: value => value }), '@vitejs/plugin-react': stub, '@tailwindcss/vite': stub,
    'node:fs': () => require('node:fs'), 'node:module': () => require('node:module'), 'node:path': () => require('node:path'), 'node:zlib': () => require('node:zlib') })
  const folder = mkdtempSync(join(tmpdir(), 'tether-brotli-'))
  try {
    const script = Buffer.from('export const message = "Hello, phone!";\n'.repeat(80))
    writeFileSync(join(folder, 'app-abc.js'), script)
    writeFileSync(join(folder, 'small.js'), 'x'.repeat(1023))
    writeFileSync(join(folder, 'icon.png'), Buffer.alloc(4096))
    writeFileSync(join(folder, 'noise.json'), require('node:crypto').randomBytes(2048))
    const written = config.brotliFiles(folder)
    assert.deepEqual(written, ['app-abc.js'])
    assert.deepEqual(brotliDecompressSync(readFileSync(join(folder, 'app-abc.js.br'))), script)
    for (const name of ['small.js.br', 'icon.png.br', 'noise.json.br']) assert.equal(existsSync(join(folder, name)), false, name)
    assert.deepEqual(config.brotliFiles(join(folder, 'missing')), [])
    assert.ok(config.default.plugins.some(plugin => plugin.name === 'brotli-assets'))
    console.log('UI B26: the build writes .br for text assets of 1 KB or more when smaller, never for binaries')

    const nm = name => `/app/node_modules/${name}/index.js`
    const graph = {
      '/app/src/main.tsx': { isEntry: true, importers: [] },
      '/app/src/App.tsx': { isEntry: false, importers: ['/app/src/main.tsx'] },
      '/app/src/Share.tsx': { isEntry: false, importers: [] },  // reached only through import()
      [nm('react-dom')]: { isEntry: false, importers: ['/app/src/App.tsx'] },
      [nm('scheduler')]: { isEntry: false, importers: [nm('react-dom')] },
      [nm('pdfjs-dist')]: { isEntry: false, importers: ['/app/src/Share.tsx'] },
      [nm('cycle-a')]: { isEntry: false, importers: [nm('cycle-b')] },
      [nm('cycle-b')]: { isEntry: false, importers: [nm('cycle-a')] },
      [nm('lucide-react')]: { isEntry: false, importers: ['/app/src/App.tsx'] },
    }
    const info = id => graph[id] ?? null
    const seen = new Map()
    assert.equal(config.vendorChunk(nm('react-dom'), info, seen), 'vendor')
    assert.equal(config.vendorChunk(nm('scheduler'), info, seen), 'vendor')
    assert.equal(config.vendorChunk(nm('pdfjs-dist'), info, seen), undefined)
    assert.equal(config.vendorChunk(nm('cycle-a'), info, seen), undefined)
    assert.equal(config.vendorChunk(nm('lucide-react'), info, seen), undefined)
    assert.equal(config.vendorChunk('/app/src/App.tsx', info, seen), undefined)
    assert.equal(config.vendorChunk(nm('missing'), info, seen), undefined)
    console.log('UI B26: vendor holds only packages the entry imports statically; lazy-only packages, icons and app code stay out')

    const chunk = (fileName, more = {}) => [fileName, { type: 'chunk', fileName, isEntry: false, facadeModuleId: null, imports: [], ...more }]
    const bundle = Object.fromEntries([
      chunk('assets/index-1.js', { isEntry: true, facadeModuleId: '/app/index.html', imports: ['assets/vendor-2.js'] }),
      chunk('assets/vendor-2.js'),
      chunk('assets/Explorer-3.js', { facadeModuleId: '/app/src/Explorer.tsx', imports: ['assets/vendor-2.js', 'assets/files-4.js'], viteMetadata: { importedCss: new Set(['assets/Explorer-5.css']) } }),
      chunk('assets/files-4.js', { imports: ['assets/files-4.js'] }),
      chunk('assets/Share-6.js', { facadeModuleId: '/app/src/Share.tsx', imports: ['assets/files-4.js'] }),
      chunk('assets/Live-7.js', { facadeModuleId: '/app/src/Live.tsx' }),
      ['assets/logo.svg', { type: 'asset' }],
    ])
    assert.deepEqual(config.panelFiles(bundle, '/chat/'), {
      explorer: ['/chat/assets/Explorer-3.js', '/chat/assets/Explorer-5.css', '/chat/assets/files-4.js'],
      share: ['/chat/assets/Share-6.js', '/chat/assets/files-4.js'],
    })
    const html = config.default.plugins.find(plugin => plugin.name === 'panel-manifest').transformIndexHtml.handler
    const page = html('<head></head>', { bundle: Object.fromEntries([chunk('assets/Explorer-<x.js', { facadeModuleId: '/s/src/Explorer.tsx' })]) })
    assert.match(page, /<script type="application\/json" id="panel-files">\{"explorer":\["\/chat\/assets\/Explorer-\\u003cx\.js"\]\}<\/script>/)
    assert.equal(html('<head></head>', {}), '<head></head>')
    console.log('UI B26: index.html lists each panel\'s own chunks and CSS, without boot chunks or Live; dev serve adds nothing')
  } finally {
    rmSync(folder, { recursive: true, force: true })
  }
}


prefetch().then(brotli).catch(error => { console.error(error); process.exitCode = 1 })
