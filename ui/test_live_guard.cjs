const { strict: assert } = require('node:assert')
const { mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join, relative } = require('node:path')

async function main() {
  const { build } = await import('vite')
  const folder = mkdtempSync(join(tmpdir(), 'tether-live-guard-'))
  const message = 'This folder is the live Tether UI. Build to another folder (vite build --outDir <dir>), then run scripts/deploy-ui.py <dir> ui/dist.'
  try {
    writeFileSync(join(folder, '.live-ui'), '')
    writeFileSync(join(folder, 'keep.txt'), 'keep')
    // Force clearing even outside root so this test proves the guard protects files.
    for (const outDir of [folder, relative(__dirname, folder)]) {
      await assert.rejects(build({
        root: __dirname,
        configFile: join(__dirname, 'vite.config.ts'),
        build: { outDir, emptyOutDir: true },
      }), { message })
      assert.equal(readFileSync(join(folder, 'keep.txt'), 'utf8'), 'keep')
      assert.equal(readFileSync(join(folder, '.live-ui'), 'utf8'), '')
    }
    console.log('Live guard: Vite rejects marked output folders before clearing files')
  } finally {
    rmSync(folder, { recursive: true, force: true })
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
