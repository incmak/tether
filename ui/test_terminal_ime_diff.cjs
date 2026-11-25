const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const source = readFileSync(path.join(__dirname, 'src/terminalIme.ts'), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
})
const exportsFromModule = {}
runInNewContext(outputText, { exports: exportsFromModule })
const { imeDiff } = exportsFromModule

assert.equal(imeDiff('the ', 'the '), '')
assert.equal(imeDiff('teh ', 'the '), '\x7f\x7f\x7fhe ')
assert.equal(imeDiff('a🙂b', 'a🙃c'), '\x7f\x7f🙃c')
assert.equal(imeDiff('a', 'a\r\nb\nc'), '\rb\rc')
console.log('terminal IME diff: unchanged text, rewritten suffix, Unicode and newlines pass')
