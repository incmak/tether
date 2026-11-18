// Long-paste badges: when a paste collapses, and the text that goes to the agent. Run: node test_paste.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const terminal = {}
runInNewContext(ts.transpileModule(readFileSync('src/terminalText.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: terminal })

const code = ts.transpileModule(readFileSync('src/pasteText.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const moduleExports = {}
runInNewContext(code, { exports: moduleExports, Date, Math, require: name => { assert.equal(name, './terminalText'); return terminal } })
const { longPaste, pasteOf, joinPastes, inlinePaste } = moduleExports

// Claude Code 2.1.318, checked in a tmux pane: 3 lines and 800 characters stay inline; 4 lines or 801 characters collapse.
assert.equal(longPaste('a\nb\nc'), false)
assert.equal(longPaste('a\nb\nc\nd'), true)
assert.equal(longPaste('a\r\nb\r\nc'), false)
assert.equal(longPaste('y'.repeat(800)), false)
assert.equal(longPaste('y'.repeat(801)), true)
assert.equal(longPaste(''), false)
console.log('paste: collapses at the same size as Claude Code')

const log = pasteOf('one\r\ntwo\r\nthree\r\nfour\n\n', false)
assert.equal(log.text, 'one\ntwo\nthree\nfour')
assert.notEqual(pasteOf('x', true).id, pasteOf('x', true).id)
console.log('paste: line endings normalised, trailing blank lines dropped, ids unique')

const before = pasteOf('LOG', true), after = pasteOf('TAIL', false)
assert.equal(joinPastes('why does this fail?', [after]), 'why does this fail?\n\nTAIL')
assert.equal(joinPastes('why does this fail?', [before]), 'LOG\n\nwhy does this fail?')
assert.equal(joinPastes('q', [after, before]), 'LOG\n\nq\n\nTAIL')
assert.equal(joinPastes('', [after]), 'TAIL')
assert.equal(joinPastes('  ', [before, after]), 'LOG\n\nTAIL')
assert.equal(joinPastes('only text', []), 'only text')
console.log('paste: message order follows the caret at paste time')

assert.equal(inlinePaste('', after), 'TAIL')
assert.equal(inlinePaste('question  \n', after), 'question\n\nTAIL')
assert.equal(inlinePaste('question', before), 'LOG\n\nquestion')
console.log('paste: "Edit as text" puts the text where it would have been sent')

const { expandPlaceholders } = moduleExports
const four = 'line one\nline two\nline three\nline four'
assert.equal(expandPlaceholders('short message', [four]), 'short message')
assert.equal(expandPlaceholders('[Pasted text #12 +3 lines]', ['old', four]), four)
assert.equal(expandPlaceholders('[Pasted text #9]', ['y'.repeat(900)]), 'y'.repeat(900))
assert.equal(expandPlaceholders('[Pasted text #3 +3 lines]\n[Pasted text #4 +3 lines]', [four + '!', 'short', four, four + '!']), `${four}\n${four}!`)
assert.equal(expandPlaceholders('[Pasted text #1 +3 lines] and [Pasted text #2 +3 lines]', [four]), `[Pasted text #1 +3 lines] and ${four}`)
console.log('paste: Stop swaps Claude placeholders for the long messages Tether sent')

// Colored terminal logs stay plain across short paste, long-paste badges, and restored drafts.
const colored = '\x1b[90m8:15AM\x1b[0m \x1b[32mINF\x1b[0m no leaks found'
assert.equal(pasteOf(colored, false).text, '8:15AM INF no leaks found')
assert.equal(joinPastes(colored, []), '8:15AM INF no leaks found')
assert.equal(terminal.terminalText('\x1b]8;;https://example.com\x1b\\label\x1b]8;;\x1b\\'), 'label')
assert.equal(terminal.terminalText('\x1b]0;window title\x07visible\x1b[?25l\x1b[2K'), 'visible')
assert.equal(terminal.terminalText('\x9b32mgreen\x9b0m\x1bPprivate data\x1b\\'), 'green')
assert.equal(terminal.terminalText('[32m literal \\x1b[0m <script>'), '[32m literal \\x1b[0m <script>')
assert.equal(joinPastes('unknown\x01control\x1b[', []), 'unknown\x01control\x1b[', 'Unknown controls must reach the existing server rejection')
assert.equal(longPaste('\x1b[32m' + 'x'.repeat(800) + '\x1b[0m'), false)

const source = ts.createSourceFile('Composer.tsx', readFileSync('src/Composer.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let onPaste
function visit(node) {
  if (ts.isJsxAttribute(node) && node.name.getText(source) === 'onPaste') onPaste = node.initializer.expression.getText(source)
  ts.forEachChild(node, visit)
}
visit(source)
assert(onPaste)
const ctx = { terminalText: terminal.terminalText, longPaste, pasteOf, canAttach: true, draft: 'before SELECT after',
  setDraft: value => { ctx.nextDraft = value }, setPastes: update => { ctx.pastes = update([]) }, requestAnimationFrame: callback => callback(),
}
runInNewContext(ts.transpileModule('globalThis.onPaste = ' + onPaste, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, ctx)
let prevented = false, selection
const event = value => ({ clipboardData: { files: [], getData: () => value }, preventDefault: () => { prevented = true },
  currentTarget: { readOnly: false, selectionStart: 7, selectionEnd: 13, setSelectionRange: (...args) => { selection = args } } })
ctx.onPaste(event(colored))
assert(prevented)
assert.equal(ctx.nextDraft, 'before 8:15AM INF no leaks found after')
assert.deepEqual(selection, [32, 32])
ctx.onPaste(event(colored + '\nline2\nline3\nline4'))
assert.equal(ctx.pastes[0].text, '8:15AM INF no leaks found\nline2\nline3\nline4')
prevented = false
ctx.onPaste(event('plain paste'))
assert.equal(prevented, false, 'Plain short paste keeps native browser behavior')
const readOnly = event(colored); readOnly.currentTarget.readOnly = true
ctx.onPaste(readOnly)
assert.equal(prevented, false, 'Read-only input cannot change its draft')
console.log('ANSI logs: plain display/paste, hyperlink labels, caret replacement, badges, and unknown-control rejection preserved')

// Retry the screenshot's existing failed bubble through the real send boundary.
const chat = ts.createSourceFile('Chat.tsx', readFileSync('src/Chat.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let send
function findSend(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'send') send = node.getText(chat)
  ts.forEachChild(node, findSend)
}
findSend(chat)
const retryContext = { terminalText: terminal.terminalText, activePane: { agent: 'codex' }, pane: { id: '%fixture' }, data: { thread: 'fixture' },
  ownAnswer: false, squash: text => text, codexSendEvidence: () => undefined, matches: () => 0, messages: [], lastFailure: { current: null },
  write: async (path, body) => { retryContext.submission = { path, body }; return false },
}
runInNewContext(ts.transpileModule(send + '\nglobalThis.send = send', { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, retryContext)
retryContext.send(colored, { id: 1, seen: 0 }).then(() => {
  assert.equal(retryContext.submission.body.message, '8:15AM INF no leaks found')
  assert.equal(retryContext.submission.body.thread, 'fixture')
  console.log('Queued ANSI paste: Retry sends plain text with the same pane/thread identity')
}).catch(error => { console.error(error); process.exitCode = 1 })
