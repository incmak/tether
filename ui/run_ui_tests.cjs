// Setup, browser discovery, and CI coverage: ../notes/ui-tests-self-contained.md
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const mode = process.argv[2]
if (mode && !['--unit', '--browser'].includes(mode)) {
  console.error('Usage: node run_ui_tests.cjs [--unit|--browser]')
  process.exit(1)
}
const tests = fs.readdirSync(__dirname).filter(name => /^test_.*\.cjs$/.test(name)).sort().filter(name => {
  const browser = /const\s*\{\s*getBrowser\b[^}]*\}\s*=\s*require\(['"]\.\/browser_test_helper\.cjs['"]\)/.test(fs.readFileSync(path.join(__dirname, name), 'utf8'))
  return !mode || (mode === '--browser' ? browser : !browser)
})
let passed = 0, failed = 0
for (const test of tests) {
  console.log(`\n=== ${test} ===`)
  const result = spawnSync(process.execPath, [path.join(__dirname, test)], { cwd: __dirname, stdio: 'inherit', timeout: 10 * 60 * 1000 })
  if (result.status === 0 && !result.error) passed++
  else { failed++; console.error(`FAIL ${test}: ${result.error?.message || result.signal || `exit ${result.status}`}`) }
}
console.log(`\nUI tests: ${passed} passed, ${failed} failed, ${tests.length} total`)
if (failed || !tests.length) process.exitCode = 1
