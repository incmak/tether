// B92: the build stamp that scripts/deploy-ui.py checks. Real git in a temp repo; no browser, no server.
const { strict: assert } = require('node:assert')
const { execFileSync } = require('node:child_process')
const { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const ts = require('typescript')

function loadConfig() {
  const exports = {}
  const source = ts.transpileModule(readFileSync(join(__dirname, 'vite.config.ts'), 'utf8').replaceAll('import.meta.url', '__filename'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  const stub = () => ({ default: () => ({}) })
  const modules = { vite: () => ({ defineConfig: value => value }), '@vitejs/plugin-react': stub, '@tailwindcss/vite': stub }
  new Function('exports', 'require', '__filename', source)(exports, name => modules[name] ? modules[name]() : require(name), join(__dirname, 'vite.config.ts'))
  return exports
}

const { buildStamp } = loadConfig()
const repo = mkdtempSync(join(tmpdir(), 'tether-stamp-'))
const git = (...args) => execFileSync('git', ['-C', repo, '-c', 'user.email=t@example.invalid', '-c', 'user.name=t', ...args], { encoding: 'utf8' }).trim()
try {
  const ui = join(repo, 'ui')
  mkdirSync(ui)
  writeFileSync(join(ui, 'app.ts'), 'one')
  writeFileSync(join(repo, 'manifest.json'), '{}')
  git('init', '-q')
  git('add', '.')
  git('commit', '-qm', 'base')
  const head = git('rev-parse', 'HEAD')
  assert.deepEqual(buildStamp(ui), { commit: head, dirty: false, changes: [] })
  writeFileSync(join(repo, 'notes.txt'), 'untracked outside ui/ does not reach the build')
  assert.equal(buildStamp(ui).dirty, false)
  writeFileSync(join(repo, 'manifest.json'), '{"changed": true}')
  writeFileSync(join(ui, 'new.ts'), 'new file in ui/')
  assert.deepEqual(buildStamp(ui), { commit: head, dirty: true, changes: ['manifest.json', 'ui/new.ts'] })
  assert.equal(buildStamp(tmpdir()), null)
  console.log('B92 stamp: commit and dirty state of the build checkout')
} finally {
  rmSync(repo, { recursive: true, force: true })
}
