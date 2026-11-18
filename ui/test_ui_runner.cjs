const assert = require('node:assert/strict')
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tether-ui-runner-'))
try {
  const runner = path.join(dir, 'run_ui_tests.cjs')
  fs.copyFileSync(path.join(__dirname, 'run_ui_tests.cjs'), runner)
  fs.writeFileSync(path.join(dir, 'browser_test_helper.cjs'), '')
  fs.writeFileSync(path.join(dir, 'test_unit.cjs'), 'throw new Error("unit file was selected")')

  const browserFiles = ['test_cold_load.cjs', 'test_responsive_files_ui.cjs', 'test_new_browser.cjs']
  for (const name of browserFiles) {
    fs.writeFileSync(path.join(dir, name), `
      const { ${'getBrowser'} } = require('./browser_test_helper.cjs')
      const assert = require('node:assert/strict')
      assert.equal(process.env.TETHER_CDP_URL, '')
      assert.equal(process.env.MOBILE_TMUX_CONFIG, process.env.SHOTS)
      console.log('out ${name}')
      console.error('err ${name}')
      process.exitCode = ${name === 'test_responsive_files_ui.cjs' ? 7 : 0}
    `)
  }

  const shots = path.join(dir, 'shots')
  const result = spawnSync(process.execPath, [runner, '--browser', '--parallel'], {
    cwd: dir, env: { ...process.env, SHOTS: shots, TETHER_CDP_URL: 'unused' }, encoding: 'utf8', timeout: 10_000,
  })
  assert.ifError(result.error)
  assert.equal(result.status, 1, result.stderr)
  assert.deepEqual([...result.stdout.matchAll(/^=== (.+) ===$/gm)].map(match => match[1]).sort(), browserFiles.slice().sort())
  assert.equal(result.stdout.match(/^=== (.+) ===$/m)[1], 'test_cold_load.cjs')
  const run = fs.readdirSync(shots)[0]
  for (const name of browserFiles) {
    const output = `out ${name}\nerr ${name}\n`
    assert.ok(result.stdout.includes(`=== ${name} ===\n${output}`), result.stdout)
    assert.equal(fs.readFileSync(path.join(shots, run, path.basename(name, '.cjs'), 'test.log'), 'utf8'), output)
  }
  assert.ok(result.stdout.endsWith('FAIL test_responsive_files_ui.cjs: exit 7\n\nUI tests: 2 passed, 1 failed, 3 total\n'), result.stdout)
  console.log('UI runner preserves selection, isolation, grouped output and failure status')
} finally {
  fs.rmSync(dir, { recursive: true, force: true })
}
