// Friday's reminder tools: spoken order, partial failure, and the upcoming list.
const fs = require('fs'), path = require('path'), Module = require('module'), ts = require('typescript'), assert = require('assert')
Module._extensions['.ts'] = (mod, file) => mod._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX } }).outputText, file)
const { addReminders, listReminders } = require(path.resolve(__dirname, 'src/fridayReminders.ts'))
const { fullLabel } = require(path.resolve(__dirname, 'src/reminders.ts'))

;(async () => {
  const calls = []
  let next = 1
  const api = async (url, body) => {
    calls.push({ url, body })
    if (body.when === 'bad') throw new Error('Cannot read the time "bad".')
    return { reminder: { id: `R${next++}`, text: body.text, due: 1795500000 } }
  }
  const result = await addReminders(api, [
    { text: 'Delete the Azure VM', when: '2026-11-24 10:00' },
    { text: 'Set up AWS MFA', when: 'bad' },
    { text: '  ', when: '2026-11-25' },
    { text: 'Decide on the Savings Plan', when: '2026-12-16' },
  ])
  assert.deepStrictEqual(calls.map(c => c.url), ['reminders/add', 'reminders/add', 'reminders/add'])
  assert.deepStrictEqual(calls[0].body, { text: 'Delete the Azure VM', when: '2026-11-24 10:00' })
  assert.strictEqual(result.ok, true)
  assert.deepStrictEqual(result.set.map(r => r.id), ['R1', 'R2'])
  assert.strictEqual(result.set[0].when, fullLabel(1795500000))
  assert.deepStrictEqual(result.failed.map(f => f.text), ['Set up AWS MFA', 'A reminder'])

  const none = await addReminders(api, [{ text: 'x', when: 'bad' }])
  assert.ok(!none.ok && /Cannot read the time/.test(none.error))
  assert.ok((await addReminders(api, 'nope')).error)
  calls.length = 0
  await addReminders(api, Array.from({ length: 7 }, (_, i) => ({ text: `T${i}`, when: 'in 2h' })))
  assert.strictEqual(calls.length, 5)

  const list = await listReminders(async () => ({ now: 0, reminders: [
    { id: 'R9', text: 'Later', due: 300, state: 'scheduled' },
    { id: 'R8', text: 'Done one', due: 100, state: 'done' },
    { id: 'R7', text: 'Now', due: 200, state: 'due' },
  ] }))
  assert.strictEqual(list.count, 2)
  assert.deepStrictEqual(list.items, [`R7 Now, ${fullLabel(200)} (due now)`, `R9 Later, ${fullLabel(300)}`])
  console.log('fridayReminders ok')
})().catch(error => { console.error(error); process.exit(1) })
