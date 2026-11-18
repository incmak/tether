// B208: the verify driver's own rules (tools/verify.cjs): its options, the surface recipes it accepts, and which
// server answers fail a shot. No browser and no server; the full run is `node tools/verify.cjs --surface chat`.
const { strict: assert } = require('node:assert')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { test } = require('node:test')
const { parseArgs, loadSurfaces, answerProblem, observeAnswers, folderName, shotsFolder, sourceTreeId, stampReason, startServer, SURFACES } = require('./tools/verify.cjs')

assert.deepEqual(parseArgs(['--surface', 'settings,backlog', '--surface', 'chat']).surface, ['settings', 'backlog', 'chat'])
assert.deepEqual(parseArgs([]).widths, [320, 1280], 'both widths by default')
assert.deepEqual(parseArgs(['--widths', '320,768,1280']).widths, [320, 768, 1280])
assert.equal(parseArgs(['--local']).local, true)
for (const bad of [['--widths', '200'], ['--widths', 'wide'], ['--widths', ''], ['--name', '../up'], ['--name', 'a b'], ['--surface'], ['--frobnicate']]) {
  assert.throws(() => parseArgs(bad), Error, `refuses ${bad.join(' ')}`)
}

assert.deepEqual(loadSurfaces({ surface: ['settings', 'settings', 'chat'], recipe: [] }).chosen, ['settings', 'chat'])
assert.throws(() => loadSurfaces({ surface: ['setings'], recipe: [] }), /Unknown surface: setings/)
for (const [name, surface] of Object.entries(SURFACES)) {
  assert.equal(typeof surface.open, 'function', name)
  assert.ok(surface.landmark && surface.about, `${name} has a landmark and a description`)
}

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tether-verify-tool-'))
try {
  const recipe = (name, body) => { const file = path.join(temp, `${name}.cjs`); fs.writeFileSync(file, body); return file }
  const good = recipe('good', "module.exports = { 'undo-history': { open: async () => {}, landmark: '.undo-history' } }")
  const loaded = loadSurfaces({ surface: ['chat'], recipe: [good] })
  assert.deepEqual(loaded.chosen, ['chat', 'undo-history'], 'a recipe adds its surfaces to the run')
  assert.equal(loaded.surfaces['undo-history'].landmark, '.undo-history')
  // A shot must prove which screen it shows: a recipe without a landmark, or without a way to open, is refused.
  assert.throws(() => loadSurfaces({ surface: [], recipe: [recipe('no-landmark', "module.exports = { thing: { open: async () => {} } }")] }), /needs open\(page, ctx\) and a landmark/)
  assert.throws(() => loadSurfaces({ surface: [], recipe: [recipe('empty-landmark', "module.exports = { thing: { open: async () => {}, landmark: '' } }")] }), /landmark/)
  assert.throws(() => loadSurfaces({ surface: [], recipe: [recipe('no-open', "module.exports = { thing: { landmark: '.x' } }")] }), /needs open/)
  assert.throws(() => loadSurfaces({ surface: [], recipe: [recipe('bad-name', "module.exports = { '../x': { open: async () => {}, landmark: '.x' } }")] }), /surface name/)
} finally { fs.rmSync(temp, { recursive: true, force: true }) }

assert.equal(answerProblem('GET', '/mobile/api/sessions', 200), '')
assert.equal(answerProblem('GET', '/mobile/api/sessions', 304), '')
assert.equal(answerProblem('GET', '/chat/assets/x.js', 404), '')
assert.match(answerProblem('GET', '/chat/', 302), /redirects/)
assert.match(answerProblem('GET', '/mobile/api/sessions', 307), /redirects/)
assert.match(answerProblem('GET', '/terminal/live/', 403, {}, true), /GET \/terminal\/live\/.*ALLOWED.*page\.route/)
assert.match(answerProblem('POST', '/mobile/api/prefs', 403), /POST \/mobile\/api\/prefs answered 403/)

const defaults = observeAnswers()
defaults.answer('GET', '/mobile/api/browser/agents', 503)
defaults.answer('GET', '/mobile/api/browser/signed/access', 503)
defaults.finish()
assert.deepEqual(defaults.problems, [], 'fixture browser endpoints may say they are off')

const unexpected = observeAnswers()
unexpected.answer('GET', '/mobile/api/fixture/error', 500)
unexpected.finish()
assert.deepEqual(unexpected.problems, ['GET /mobile/api/fixture/error answered 500'])

const intentional = observeAnswers({ 'fixture/error': 500, 'fixture/success': 200 })
intentional.answer('GET', '/mobile/api/fixture/error', 200)
intentional.answer('GET', '/mobile/api/fixture/error', 500)
intentional.answer('POST', '/mobile/api/fixture/success', 200)
intentional.finish()
assert.deepEqual(intentional.problems, [])

const missing = observeAnswers({ 'fixture/error': 500, 'fixture/blocked': 403 })
missing.answer('GET', '/mobile/api/fixture/error', 200)
missing.answer('GET', '/mobile/api/fixture/blocked', 403, true)
missing.finish()
assert.match(missing.problems[0], /blocked by the test Tether/)
assert.deepEqual(missing.problems.slice(1), [
  'expected /mobile/api/fixture/error to answer 500, but that status never arrived',
  'expected /mobile/api/fixture/blocked to answer 403, but that status never arrived',
])

assert.equal(stampReason(true, [320, 1280]), '')
assert.equal(stampReason(true, [1280, 768, 320]), '')
assert.match(stampReason(false, [320, 1280]), /--dist/)
assert.match(stampReason(true, [320, 768]), /1280 px/)
assert.match(stampReason(true, [1280]), /320 px/)
assert.equal(stampReason(true, [320, 1280], true), 'sources changed during the run')

const tracked = Buffer.from('ui/tracked.cjs')
const untracked = Buffer.from('server/new\nsource.py')
const contents = new Map([[tracked, 'tracked content'], [untracked, 'new content']])
const identity = (files, head = 'head') => sourceTreeId(head, files, file => contents.get(file) ?? null)
const first = identity([tracked, untracked])
assert.match(first, /^head\+[a-f0-9]{64}$/)
assert.equal(first, identity([untracked, tracked]), 'sorts the input paths')
assert.notEqual(first, identity([tracked]), 'includes untracked sources from the list')
assert.notEqual(first, identity([tracked, untracked], 'another-head'))
contents.set(untracked, 'changed content')
assert.notEqual(first, identity([tracked, untracked]), 'includes file contents')
contents.set(untracked, '')
const empty = identity([tracked, untracked])
contents.delete(untracked)
assert.notEqual(empty, identity([tracked, untracked]), 'distinguishes empty files from missing tracked files')

assert.equal(folderName('b208', new Date(2026, 10, 18, 3, 4, 5)), path.join('.release-work', '2026-11-18', 'verify', 'b208-030405'))
assert.equal(folderName('recipe', new Date(2026, 0, 1, 0, 0, 0)), path.join('.release-work', '2026-01-01', 'verify', 'recipe-000000'))
const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-folder-'))
try {
  const folder = path.join(parent, 'custom')
  assert.equal(shotsFolder({ shots: folder }), folder)
  assert.throws(() => shotsFolder({ shots: folder }), { code: 'EEXIST' })
} finally { fs.rmSync(parent, { recursive: true, force: true }) }
console.log('verify tool: ok')

test('driver checks the launcher exit after it closes stdin', async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-launcher-'))
  const python = path.join(folder, 'python3')
  fs.writeFileSync(python, `#!/bin/sh
echo '{}'
while read -r line; do :; done
if [ "$4" = fails ]; then
    echo 'private tmux still answers' >&2
    exit 3
fi
`, { mode: 0o700 })
  try {
    for (const dist of ['passes', 'fails']) {
      const oldPath = process.env.PATH
      process.env.PATH = folder
      let server
      try { server = startServer(dist) }
      finally { process.env.PATH = oldPath }
      await server.ready
      if (dist === 'fails') await assert.rejects(server.stop(), /exit 3: private tmux still answers/)
      else await server.stop()
    }
  } finally { fs.rmSync(folder, { recursive: true, force: true }) }
})

// Review round 4: with NODE_USE_ENV_PROXY=1, Node's global agent follows HTTP_PROXY. The relay carries the sign-in
// cookie, so it must go to the test server directly whatever the proxy settings say.
test('the relay ignores environment proxy settings', async () => {
  const http = require('node:http')
  const { execFile } = require('node:child_process')
  const listen = handler => new Promise(resolve => { const server = http.createServer(handler); server.listen(0, '127.0.0.1', () => resolve(server)) })
  const proxied = [], direct = []
  const proxy = await listen((req, res) => { proxied.push(req.url); res.writeHead(502); res.end('decoy proxy') })
  const fixture = await listen((req, res) => { direct.push(req.headers.cookie); res.writeHead(200); res.end('fixture') })
  const script = `
    const { loopbackRequest } = require(${JSON.stringify(path.join(__dirname, 'tools/verify.cjs'))})
    const request = { allHeaders: async () => ({ accept: '*/*' }), method: () => 'GET', postDataBuffer: () => null }
    loopbackRequest('http://127.0.0.1:${fixture.address().port}/mobile/api/sessions', request, 'mobile_tmux_session=secret')
      .then(answer => { console.log(answer.status + ' ' + answer.body) })`
  try {
    const proxyUrl = `http://127.0.0.1:${proxy.address().port}`
    const output = await new Promise((resolve, reject) => execFile(process.execPath, ['-e', script], { timeout: 30000,
      env: { ...process.env, NODE_USE_ENV_PROXY: '1', HTTP_PROXY: proxyUrl, http_proxy: proxyUrl, NO_PROXY: '', no_proxy: '' } },
      (error, stdout, stderr) => error ? reject(new Error(stderr || error.message)) : resolve(stdout.trim())))
    assert.equal(output, '200 fixture')
    assert.deepEqual(direct, ['mobile_tmux_session=secret'])
    assert.deepEqual(proxied, [], 'the decoy proxy must get no request')
  } finally { proxy.close(); fixture.close() }
})
