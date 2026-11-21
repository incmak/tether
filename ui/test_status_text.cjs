const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const status = {}
runInNewContext(ts.transpileModule(readFileSync('src/statusText.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports: status })
const { parseStatus } = status
const plain = value => value && JSON.parse(JSON.stringify(value))
const fixtures = '../server/fixtures/injected_prompts'
const { prompts } = JSON.parse(readFileSync(`${fixtures}/tether_status.json`, 'utf8'))
const { cases } = JSON.parse(readFileSync(`${fixtures}/tether_status_written.json`, 'utf8'))

assert.equal(prompts.length, 56)
for (const [index, prompt] of prompts.entries()) {
  const card = parseStatus(prompt)
  assert(card, `recorded prompt ${index + 1}`)
  assert.equal(card.tabs.length, Number(prompt.match(/^\[Tether status\] ([0-9]+)/)[1]))
}

for (const { events, mode, text } of cases) {
  assert.deepEqual(plain(parseStatus(text)), {
    mode,
    tabs: events.map(event => ({
      name: event.name.replaceAll('`', "'").replace(/\r\n|[\r\n\u2028\u2029]/g, ' '),
      pane: event.pane,
      state: event.kind,
      text: event.text.trim().replace(/\s+/g, ' '),
    })),
  })
}

const single = cases[0].text
const multiple = cases[1].text
const noDetail = cases[2].text
assert.deepEqual(plain(parseStatus(`${single}\t \n\n `)), plain(parseStatus(single)))
assert.deepEqual(plain(parseStatus(`${noDetail}: \n`)), plain(parseStatus(noDetail)))
assert.deepEqual(plain(parseStatus(`${noDetail}: \u2028`)), plain(parseStatus(noDetail)))
assert.deepEqual(plain(parseStatus(`${noDetail} \t`)), plain(parseStatus(noDetail)))
assert.deepEqual(plain(parseStatus(single.replace('python3 /opt/tether/server/agent_coordinator_cli.py', '/another path/cli'))), plain(parseStatus(single)))
assert.equal(parseStatus(multiple.replace('finished: Build passes.', 'finished: ')).tabs[0].text, '')

for (const [reason, invalid] of Object.entries({
  'too few rows': single.replace('1 tab update.', '2 tab updates.'),
  'too many rows': `${single}\n- \`Docs\` (%2) failed`,
  'wrong plural': single.replace('tab update.', 'tab updates.'),
  'zero count': single.replace('1 tab update.', '0 tab updates.'),
  'unknown mode': single.replace('"all"', '"sometimes"'),
  'unknown state': single.replace('finished:', 'finished with errors:'),
  'missing header sentence': single.replace('Reports are set to "all". ', ''),
  'blank line inside': multiple.replace('\n- `Review`', '\n\n- `Review`'),
  'whitespace on an inner row': multiple.replace('Build passes.\n', 'Build passes. \n'),
  'CRLF': single.replaceAll('\n', '\r\n'),
  'backtick in name': single.replace('Compiler', 'Com`piler'),
  'empty name': single.replace('Compiler', ''),
  'missing detail delimiter space': `${noDetail}:`,
  'no status prefix': single.replace('[Tether status] ', ''),
  'quoted header': `Here is an example:\n${single}\n`,
})) {
  assert.equal(parseStatus(invalid), null, reason)
}

console.log(`test_status_text: ok (${prompts.length} recorded prompts, ${cases.length} writer cases)`)
