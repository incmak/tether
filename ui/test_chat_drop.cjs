// chatDrop.ts: which drags are file drops, folders left out, refusal reasons, and the page guard. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const code = ts.transpileModule(readFileSync('src/chatDrop.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } }).outputText
const mod = {}
const listeners = {}
const fakeWindow = { addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn) } }
runInNewContext(code, { exports: mod, require: name => { if (name === 'react') return { useState: v => [v, () => {}] }; throw new Error(name) }, Array, window: fakeWindow })
const { isFileDrag, droppedFiles, dropRefusal, folderNote, guardFileDrops } = mod

// Only drags that carry files count.
assert.equal(isFileDrag(['Files']), true)
assert.equal(isFileDrag(['text/plain', 'Files']), true)
assert.equal(isFileDrag(['text/plain']), false)
assert.equal(isFileDrag([]), false)

// Folders are left out by name; files keep their order. A second folder name check: a different index.
const file = name => ({ name })
const item = dir => ({ kind: 'file', webkitGetAsEntry: () => ({ isDirectory: dir, name: 'entry' }) })
let out = droppedFiles({ files: [file('a.png'), file('src'), file('b.txt')], items: [item(false), item(true), item(false)] })
assert.deepEqual(out.files.map(f => f.name), ['a.png', 'b.txt'])
assert.equal(JSON.stringify(out.folders), JSON.stringify(['src']))
out = droppedFiles({ files: [file('docs'), file('c.pdf')], items: [item(true), item(false)] })
assert.deepEqual(out.files.map(f => f.name), ['c.pdf'])
assert.equal(JSON.stringify(out.folders), JSON.stringify(['docs']))
// No entry API (or string items mixed in that do not line up): keep every file.
out = droppedFiles({ files: [file('x'), file('y')], items: [{ kind: 'file' }, { kind: 'file' }] })
assert.deepEqual(out.files.map(f => f.name), ['x', 'y'])
out = droppedFiles({ files: [file('x')], items: [{ kind: 'string' }, { kind: 'string' }] })
assert.deepEqual(out.files.map(f => f.name), ['x'])
assert.equal(JSON.stringify(out.folders), JSON.stringify([]))

// Refusals match the composer's canAttach rules.
assert.equal(dropRefusal({ agent: 'claude', writable: true }, '', true), '')
assert.equal(dropRefusal({ agent: 'codex', writable: true }, 'hello', true), '')
assert.match(dropRefusal({ agent: 'shell', writable: true }, '', true), /terminal tab/)
assert.match(dropRefusal({ agent: 'claude', writable: false }, '', true), /read-only/)
assert.match(dropRefusal({ agent: 'claude', writable: true }, '', false), /load/)
assert.match(dropRefusal({ agent: 'claude', writable: true }, '!ls', true), /shell mode/)

assert.equal(folderNote([]), '')
assert.match(folderNote(['src']), /“src” is a folder, so it was not attached/)
assert.match(folderNote(['a', 'b']), /2 items are folders, so they were not attached/)

// The guard: a file drop that nothing took does not open the file; a handled one and a text drag are left alone.
guardFileDrops()
const fire = (type, types, prevented = false) => {
  const event = { defaultPrevented: prevented, dataTransfer: { types, dropEffect: 'copy' }, preventDefault() { this.defaultPrevented = true } }
  for (const fn of listeners[type] || []) fn(event)
  return event
}
let event = fire('dragover', ['Files'])
assert.equal(event.defaultPrevented, true)
assert.equal(event.dataTransfer.dropEffect, 'none')
event = fire('dragover', ['Files'], true)
assert.equal(event.dataTransfer.dropEffect, 'copy', 'a handled dragover keeps its effect')
assert.equal(fire('drop', ['Files']).defaultPrevented, true)
assert.equal(fire('dragover', ['text/plain']).defaultPrevented, false)
assert.equal(fire('drop', ['text/plain']).defaultPrevented, false)

// A drag that starts on the page carries a mark, so it is never a file drop. Nothing stays behind for the next drag.
const started = { dataTransfer: { types: [], setData(type) { this.types.push(type) } } }
for (const fn of listeners.dragstart || []) fn(started)
assert.equal(isFileDrag([...started.dataTransfer.types, 'Files']), false)
assert.equal(isFileDrag(['Files']), true, 'a later desktop drag still counts')
assert.equal(fire('dragover', [...started.dataTransfer.types, 'Files']).defaultPrevented, false, 'the guard leaves page drags alone')
console.log('chat drop: file drags, folders, refusals and page guard pass')
