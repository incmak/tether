// Tools lane responsive regression sweep. Build ui/dist first. Every API stays in local fixtures.
// Each suite closes its server and local headless Chromium. Never connect to the live server/browser.
const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const { shotsDir } = require('./browser_test_helper.cjs')
const root = shotsDir('responsive/tools')
async function run(suite, second) {
  const shots = path.join(root, second ? 'second' : '.')
  fs.mkdirSync(shots, { recursive: true })
  console.log(`Tools responsive: ${suite}, ${second ? 'second input' : 'first input'}`)
  return new Promise(resolve => {
    const child = spawn(process.execPath, [path.join(__dirname, 'responsive_tools', suite + '.cjs')], {
      cwd: path.resolve(__dirname, '..'), stdio: 'inherit',
      env: { ...process.env, SECOND: second ? '1' : '', SHOTS: shots, PHASE: process.env.PHASE || 'after' },
    })
    const timer = setTimeout(() => child.kill('SIGTERM'), 600000)
    child.on('error', error => { console.error(error); clearTimeout(timer); resolve(false) })
    child.on('exit', code => { clearTimeout(timer); console.log(`Tools finished: ${suite}, ${second ? 'second' : 'first'}, exit ${code}`); resolve(code === 0) })
  })
}
async function main() {
  let failed = false
  // At most two local browsers. Separate output folders keep both input sets independent.
  for (const suite of ['picker', 'browser', 'settings', 'live']) {
    const results = await Promise.all([run(suite, false), run(suite, true)])
    if (results.some(ok => !ok)) failed = true
  }
  if (failed) process.exitCode = 1
}
main().catch(error => { console.error(error); process.exitCode = 1 })
