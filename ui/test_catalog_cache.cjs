// Run from ui/: node test_catalog_cache.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

let now = 0
const code = ts.transpileModule(readFileSync('src/Suggest.tsx', 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText
const exportsMap = {}
runInNewContext(code, {
  exports: exportsMap, Date: { now: () => now },
  require: name => name.startsWith('./') ? {} : require(name),
})
const { loadCatalog } = exportsMap
const catalog = name => ({ commands: [{ name }], plugins: [], mcp: [], note: '' })

async function main() {
  const old = catalog('old')
  const fresh = catalog('fresh')
  const other = catalog('other')
  const requests = []
  const api = path => new Promise((resolve, reject) => requests.push({ path, resolve, reject }))

  const first = loadCatalog(api, '%1')
  assert.equal(loadCatalog(api, '%1'), first, 'share the initial request')
  requests[0].resolve(old)
  assert.equal(await first, old)
  now = 9_999
  assert.equal(await loadCatalog(api, '%1'), old)
  assert.equal(requests.length, 1)
  console.log('Catalog reuses the last result within 10 seconds')

  now = 10_001
  const refresh = loadCatalog(api, '%1')
  now += 11_000
  assert.equal(loadCatalog(api, '%1'), refresh, 'share a refresh even after another 10 seconds')
  assert.equal(requests.length, 2)
  const otherRequest = loadCatalog(api, '%2')
  assert.equal(requests[2].path, 'catalog?session=%252')
  requests[2].resolve(other)
  requests[1].resolve(fresh)
  assert.equal(await refresh, fresh)
  assert.equal(await otherRequest, other)
  assert.equal(await loadCatalog(api, '%1'), fresh)
  assert.equal(requests.length, 3)
  console.log('Stale data refetches once per pane and resolves to the new catalog')

  now += 10_001
  const failure = loadCatalog(api, '%1')
  requests[3].reject(new Error('offline'))
  assert.equal(await failure, fresh)
  const retry = loadCatalog(api, '%1')
  requests[4].resolve(old)
  assert.equal(await retry, old, 'a failed refresh does not reset the freshness timer')
  const coldFailure = loadCatalog(api, '%3')
  requests[5].reject(new Error('offline'))
  await assert.rejects(coldFailure, /offline/)
  console.log('A failed refresh keeps the last result and allows a retry. A cold failure rejects.')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
