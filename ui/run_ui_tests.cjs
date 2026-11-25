// Setup, browser discovery, and CI coverage: ../notes/ui-tests-self-contained.md
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawn, spawnSync } = require('node:child_process')

const [mode, option, ...extra] = process.argv.slice(2)
const parallel = mode === '--browser' && option === '--parallel'
if (![undefined, '--unit', '--browser'].includes(mode) || (option && !parallel) || extra.length) {
  console.error('Usage: node run_ui_tests.cjs [--unit|--browser [--parallel]]')
  process.exit(1)
}

const tests = fs.readdirSync(__dirname).filter(name => /^test_.*\.cjs$/.test(name)).sort().filter(name => {
  // The extension test uses its own MCP-managed Chrome with extension debugging enabled.
  const browser = name === 'test_extension_search.cjs' || /const\s*\{\s*getBrowser\b[^}]*\}\s*=\s*require\(['"]\.\/browser_test_helper\.cjs['"]\)/.test(fs.readFileSync(path.join(__dirname, name), 'utf8'))
  return !mode || (mode === '--browser' ? browser : !browser)
})

const childOptions = { cwd: __dirname, timeout: 10 * 60 * 1000 }
// In parallel mode a file must end well inside the CI job limit, so a hung file is killed, its log printed and
// a FAIL line recorded. The slowest file took 203 s on the hosted runner.
const parallelFileTimeout = 5 * 60 * 1000
const failures = []
function record(test, { status, signal, error }) {
  if (status !== 0 || error) failures.push(`FAIL ${test}: ${error?.message || signal || `exit ${status}`}`)
}

function runBuffered(test, artifacts) {
  const shots = path.join(artifacts, path.basename(test, '.cjs'))
  fs.mkdirSync(shots)
  const log = path.join(shots, 'test.log')
  const fd = fs.openSync(log, 'w')
  const start = performance.now()
  let child
  try {
    // A fresh local Chromium profile per process. No worker uses the desktop CDP browser.
    const env = { ...process.env, TETHER_CDP_URL: '', MOBILE_TMUX_CONFIG: shots, SHOTS: shots }
    child = spawn(process.execPath, [path.join(__dirname, test)], { ...childOptions, timeout: parallelFileTimeout, env, stdio: ['ignore', fd, fd] })
  } finally {
    fs.closeSync(fd)
  }
  return new Promise(resolve => {
    let error
    child.once('error', cause => { error = cause })
    child.once('close', (status, signal) => {
      const seconds = ((performance.now() - start) / 1000).toFixed(2)
      console.log(`\n=== ${test} ===`)
      process.stdout.write(fs.readFileSync(log))
      console.log(`\n${test}: ${seconds}s`)
      record(test, { status, signal, error })
      resolve()
    })
  })
}

async function main() {
  if (parallel && tests.length) {
    const root = path.resolve(process.env.SHOTS || path.join(__dirname, '../.work-notes/ui-browser'))
    fs.mkdirSync(root, { recursive: true })
    const artifacts = fs.mkdtempSync(path.join(root, 'run-'))
    const workers = os.availableParallelism()
    console.log(`Browser UI tests: ${workers} workers. Logs and screenshots: ${artifacts}`)

    // Cold load measures elapsed time against a budget. Give it the same idle runner as before.
    const coldLoad = 'test_cold_load.cjs'
    if (tests.includes(coldLoad)) await runBuffered(coldLoad, artifacts)

    // Start long files first so they do not hold up the end of the queue (B287 timings).
    const slow = ['test_responsive_files_ui.cjs', 'test_responsive_chat_ui.cjs', 'test_responsive_nav_ui.cjs',
      'test_preview_drawer.cjs', 'test_activity_ui.cjs', 'test_host_view_ui.cjs',
      'test_overlay_regression.cjs', 'test_autonomy_ui.cjs', 'test_factory_ui.cjs', 'test_claude_dialog.cjs']
    const queue = [...slow.filter(test => tests.includes(test)), ...tests.filter(test => test !== coldLoad && !slow.includes(test))]
    const runWorker = async () => {
      while (queue.length) await runBuffered(queue.shift(), artifacts)
    }
    await Promise.all(Array.from({ length: Math.min(workers, queue.length) }, runWorker))
  } else {
    for (const test of tests) {
      console.log(`\n=== ${test} ===`)
      record(test, spawnSync(process.execPath, [path.join(__dirname, test)], { ...childOptions, stdio: 'inherit' }))
    }
  }
  for (const failure of failures.sort()) console.log(failure)
  console.log(`\nUI tests: ${tests.length - failures.length} passed, ${failures.length} failed, ${tests.length} total`)
  if (failures.length || !tests.length) process.exitCode = 1
}
main().catch(error => { console.error(error); process.exitCode = 1 })
