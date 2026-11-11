// The action deck's rules (src/deck.ts) and the shared answer buttons, with invented panes. No API calls, no terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const compile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText
function load(file, imports = {}) {
  const exports = {}
  runInNewContext(compile(readFileSync(path.join(__dirname, 'src', file), 'utf8')), {
    exports, Error,
    require(name) {
      if (name in imports) return imports[name]
      if (['react', 'react/jsx-runtime'].includes(name)) return require(name)
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  return exports
}
const agentNames = { claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', shell: 'Shell' }
const needs = load('needs.ts', { './Sidebar': { agentNames }, './paneState': load('paneState.ts') })
const deck = load('deck.ts', { './needs': needs })
const { AnswerActions } = load('answers.tsx', { './AgentBrowser': { changeAgentBrowser: async () => {}, watchTab: () => {} } })
const { sendAnswer } = load('useAnswers.ts', { './api': { ApiError: class extends Error {}, errorText: String } })
const texts = options => Array.from(options ?? [], option => option.text)
const ids = options => Array.from(options ?? [], option => option.id)

// Order: urgent first, then oldest first. The input stays as it was.
const waiting = (id, since, urgent) => ({ pane: { id }, ask: { since, urgent } })
const input = [waiting('new', 300), waiting('old', 100), waiting('urgent-new', 400, true), waiting('urgent-old', 200, true), waiting('mid', 200, false)]
assert.deepEqual(Array.from(deck.deckOrder(input), item => item.pane.id), ['urgent-old', 'urgent-new', 'old', 'mid', 'new'])
assert.equal(input[0].pane.id, 'new')
console.log('deck order: urgent first, then oldest first')

const pane = (extra = {}) => ({ id: '%1', tmux_session: '$1', agent: 'claude', state: 'Idle', thread: 't1', approval: null, ...extra })
const permission = options => pane({ state: 'Approval', needs: { kind: 'approval', text: 'Bash command · npm install' },
  approval: { id: 'permit', actionable: true, text: 'Bash command\n\n  npm install\n  Install the packages', options } })
const three = [{ id: 1, text: 'Yes' }, { id: 2, text: "Yes, and don't ask again for npm commands in /srv/project" }, { id: 3, text: 'No, and tell Claude what to do differently (esc)' }]

// A permission request: Yes, the one other "Yes" as Always, and No.
let item = deck.deckItem(permission(three))
assert.equal(item.kind, 'approval')
assert.deepEqual([item.yes.id, item.always.id, item.no.id, item.menu], [1, 2, 3, undefined])
assert.equal(deck.hasAnswers(item), true)
item = deck.deckItem(permission([{ id: 1, text: 'Yes' }, { id: 2, text: 'Yes, allow all edits during this session (shift+tab)' }, { id: 3, text: 'No' }]))
assert.equal(item.always.id, 2)
// Two more "Yes" options: A cannot mean one of them, so there is no Always.
item = deck.deckItem(permission([...three, { id: 4, text: 'Yes, and allow the folder' }]))
assert.deepEqual([item.yes.id, item.always, item.no.id], [1, undefined, 3])
item = deck.deckItem(permission([{ id: 1, text: 'Yes' }, { id: 2, text: 'No' }]))
assert.deepEqual([item.yes.id, item.always, item.no.id], [1, undefined, 2])
// A request the server cannot check (no id, or more than one cursor) gets no buttons: the card says to open the tab.
for (const approval of [{ id: null, actionable: false, text: 'Allow?' }, { id: 'permit', actionable: false, text: 'Allow?', options: three }]) {
  item = deck.deckItem(pane({ state: 'Approval', approval }))
  assert.equal(item.kind, 'approval')
  assert.equal(deck.hasAnswers(item), false)
}
// The pane moved on: no item, so no buttons and no keys.
assert.equal(deck.deckItem(pane({ state: 'Working' })), undefined)
assert.equal(deck.deckItem(pane({ state: 'Idle', finished: 5 })), undefined)
assert.equal(deck.hasAnswers(undefined), false)
console.log('deck approval card: Yes, Always, No; none for an unchecked request or a pane that moved on')

// A plan needs reading first: no item answers, so no keys. Its Yes options are buttons to click, when the server can check them.
const planOptions = [{ id: 1, text: 'Yes, and auto mode' }, { id: 2, text: 'Yes, manually approve edits' }, { id: 3, text: 'No, keep planning' }]
const planPane = approval => pane({ state: 'Approval', needs: { kind: 'plan', text: 'Ship the fix' }, approval: { id: 'plan-1', actionable: true, text: '# Ship the fix\n\n- Step one\n- Step two', options: planOptions, ...approval } })
const plan = planPane()
item = deck.deckItem(plan)
assert.equal(item.kind, 'plan')
assert.deepEqual([item.yes, item.no, item.always, item.menu], [undefined, undefined, undefined, undefined])
assert.equal(deck.hasAnswers(item), false)
assert.deepEqual(texts(deck.planOptions(plan, item)), ['Yes, and auto mode', 'Yes, manually approve edits'])
for (const unchecked of [planPane({ actionable: false }), planPane({ id: null })]) assert.deepEqual(texts(deck.planOptions(unchecked, deck.deckItem(unchecked))), [])
// A plan chooser that came as a plain menu has no checked route: Open tab only.
const planMenu = pane({ state: 'Input needed', needs: { kind: 'plan', text: 'Ship the fix' }, attention: { id: 'm', reason: 'menu', text: 'Plan', options: planOptions } })
item = deck.deckItem(planMenu)
assert.equal(item.kind, 'plan')
assert.equal(deck.hasAnswers(item), false)
assert.deepEqual(texts(deck.planOptions(planMenu, item)), [])
// Only a plan has plan buttons.
assert.deepEqual(texts(deck.planOptions(permission(three), deck.deckItem(permission(three)))), [])
assert.deepEqual(texts(deck.planOptions(plan, undefined)), [])
for (const code of ['KeyY', 'KeyA', 'KeyN', 'Digit1', 'Digit2', 'Digit3', 'Numpad1', 'Enter']) assert.equal(deck.deckKey(code, deck.deckItem(plan)), undefined)
console.log('deck plan card: no keys; checked Yes options as buttons, no Keep planning, none when unchecked')

// A question: menu options 1 to 9, never the own-answer row. A list with tick boxes or a held question stays in the tab.
const many = Array.from({ length: 11 }, (_, index) => ({ id: index + 1, text: `Option ${index + 1}` }))
const menu = (attention = {}) => pane({ state: 'Input needed', needs: { kind: 'question', text: 'Which one?' },
  attention: { id: 'menu', reason: 'menu', text: 'Which one?\n❯ 1. Option 1\n  2. Option 2', question: 'Which one?', options: [...many.slice(0, 4), { id: 5, text: 'Type something.', free: true }], ...attention } })
item = deck.deckItem(menu())
assert.deepEqual(ids(item.menu), [1, 2, 3, 4])
assert.deepEqual(ids(deck.deckItem(menu({ options: many })).menu), [1, 2, 3, 4, 5, 6, 7, 8, 9])
assert.equal(deck.hasAnswers(deck.deckItem(menu({ choice: { selection: 'multi' } }))), false)
assert.equal(deck.hasAnswers(deck.deckItem(menu({ ask_current: { multi: true } }))), false)
assert.equal(deck.hasAnswers(deck.deckItem(menu({ askBridge: { id: 'held' } }))), false)
assert.equal(deck.hasAnswers(deck.deckItem(menu({ reason: 'prompt' }))), false)
// The Home card's item is unchanged: still options 1 to 3, and never Always.
const home = needs.inboxItems([menu({ options: many }), permission(three)], {})
assert.deepEqual(ids(home.find(found => found.kind === 'question').menu), [1, 2, 3])
assert.equal(home.find(found => found.kind === 'approval').always, undefined)
console.log('deck question card: options 1-9, none for tick boxes, held questions or the own-answer row; Home items unchanged')

// Keys. J is next on every card. The others answer only what the card offers.
item = deck.deckItem(permission(three))
assert.deepEqual([deck.deckKey('KeyY', item).id, deck.deckKey('KeyA', item).id, deck.deckKey('KeyN', item).id], [1, 2, 3])
assert.equal(deck.deckKey('Digit1', item), undefined)
assert.equal(deck.deckKey('KeyJ', item), 'next')
assert.equal(deck.deckKey('KeyJ', undefined), 'next')
for (const code of ['KeyY', 'KeyA', 'KeyN', 'Digit1', 'Enter', 'Space']) assert.equal(deck.deckKey(code, undefined), undefined)
item = deck.deckItem(menu({ options: many }))
assert.equal(deck.deckKey('Digit9', item).id, 9)
assert.equal(deck.deckKey('Numpad2', item).id, 2)
for (const code of ['Digit0', 'KeyY', 'KeyA', 'KeyN', 'F1']) assert.equal(deck.deckKey(code, item), undefined)
assert.equal(deck.deckKey('Digit5', deck.deckItem(menu())), undefined)  // the own-answer row
assert.equal(deck.deckKey('KeyA', deck.deckItem(permission([{ id: 1, text: 'Yes' }, { id: 2, text: 'No' }]))), undefined)
// Keys are off while you type.
for (const tagName of ['TEXTAREA', 'INPUT', 'SELECT']) assert.equal(deck.isTyping({ tagName }), true)
assert.equal(deck.isTyping({ tagName: 'DIV', isContentEditable: true }), true)
for (const target of [{ tagName: 'BUTTON' }, { tagName: 'BODY' }, { tagName: 'DIALOG' }, {}, null]) assert.equal(deck.isTyping(target), false)
const source = readFileSync(path.join(__dirname, 'src/ActionDeck.tsx'), 'utf8')
assert.match(source, /event\.repeat \|\| event\.ctrlKey \|\| event\.metaKey \|\| event\.altKey \|\| event\.shiftKey \|\| isTyping\(event\.target/)
assert.doesNotMatch(source.replace(/\/\/.*$/gm, ''), /undo/i)  // a sent key cannot be taken back, so the deck offers no undo
console.log('deck keys: Y, A, N, 1-9 and J; off for missing answers and while typing')

// The status line names the answer.
item = deck.deckItem(permission(three))
assert.equal(deck.answerName(item, item.yes), 'Yes')
assert.equal(deck.answerName(item, item.no), 'No')
assert.equal(deck.answerName(item, item.always), '"Yes, and don\'t ask again for npm comman…"')
assert.equal(deck.answerName(deck.deckItem(menu()), many[1]), '"Option 2"')

// The full text of the ask.
assert.equal(deck.askText(permission(three)), 'Bash command\n\n  npm install\n  Install the packages')
assert.equal(deck.askText(menu({ text: 'I can ship it two ways.\nWhich one?\n❯ 1. Option 1\n  2. Option 2' })), 'I can ship it two ways.\nWhich one?')
assert.equal(deck.askText(menu({ text: '❯ 1. Option 1\n  2. Option 2' })), 'Which one?')
assert.equal(deck.askText(menu({ reason: 'prompt', options: undefined, text: 'Press Enter to continue' })), 'Press Enter to continue')
assert.equal(deck.askText(pane()), '')
const marks = (text, kind = 'approval') => Array.from(deck.askLines(text, kind), line => line.mark)
// Diff marks: only numbered edit lines inside a block of numbered lines.
assert.deepEqual(marks('Edit file\nsrc/a.js\n\n 11  same\n 12 -const a = 1\n 12 +const a = 2\n 13 +const b = 3\n 14  same\n\nDo you want to make this edit?'), ['', '', '', '', 'remove', 'add', 'add', '', '', ''])
assert.deepEqual(marks('Create file\n 1 +first\n 2 +second'), ['', 'add', 'add'])
// A Bash command is never a diff: lines that start with - or +, also after a lone number, stay plain.
assert.deepEqual(marks('Bash command\n\n  grep \\\n  -rn "retries" \\\n  --include=*.js \\\n  +x src\n  - removed\n  3 - 1\n\n  Search the sources'), ['', '', '', '', '', '', '', '', '', ''])
assert.deepEqual(marks('+added\n- removed\n--- a/file\n+++ b/file\n 7 -alone'), ['', '', '', '', ''])
assert.deepEqual(marks('# Plan\n- Step one\n+ Step two', 'plan'), ['', '', ''])
assert.deepEqual(Array.from(deck.askLines('a\n\nb', 'question'), line => line.text), ['a', '', 'b'])
console.log('deck text: approval text, question without its option rows, diff marks only in approvals')

// The shared buttons (answers.tsx): which ones a card shows.
const buttons = found => renderToStaticMarkup(React.createElement(AnswerActions, { api: async () => {}, item: found, label: 'Docs pass', waiting: false, onAnswer() {}, onFail() {} }))
let html = buttons(deck.deckItem(permission(three)))
assert.match(html, /Yes <kbd>Y<\/kbd>.*don&#x27;t ask again for npm commands.*<kbd>A<\/kbd>.*No <kbd>N<\/kbd>/s)
assert.equal((html.match(/<button/g) ?? []).length, 3)
html = buttons(deck.deckItem(permission([{ id: 1, text: 'Yes' }, { id: 2, text: 'No' }])))
assert.equal((html.match(/<button/g) ?? []).length, 2)
assert.doesNotMatch(html, /<kbd>A<\/kbd>/)
assert.equal(buttons(deck.deckItem(plan)), '')  // the shared buttons never answer a plan
// The deck draws the plan buttons itself: full text, no key hint.
assert.match(source, /plans\.map\(option => <button type="button" key=\{option\.id\} disabled=\{busy\} onClick=\{\(\) => choosePlan\(option\)\}>\{option\.text\}<\/button>\)/)
assert.equal(buttons(deck.deckItem(pane({ state: 'Approval', approval: { id: null, actionable: false, text: 'Allow?' } }))), '')
assert.doesNotMatch(buttons(home.find(found => found.kind === 'approval')), /<kbd>A<\/kbd>/)
console.log('deck buttons: Yes, Always and No for a permission, numbers for a question, none for a plan or when unchecked')

// Answers go through the checked routes. Home's sendAnswer is unchanged: approval by id, anything else by menu option and label.
;(async () => {
  const calls = []
  const api = async (endpoint, body) => { calls.push([endpoint, body]) }
  item = deck.deckItem(permission(three))
  await sendAnswer(api, item, item.always)
  item = deck.deckItem(menu())
  await sendAnswer(api, item, item.menu[2])
  // Home's route for a plan item stays the menu route, as before the deck (Home offers no plan options).
  await sendAnswer(api, deck.deckItem(plan), planOptions[1])
  // The deck's own plan route: the checked approval, by id.
  await deck.sendPlanChoice(api, plan, planOptions[1])
  await assert.rejects(deck.sendPlanChoice(api, planPane({ actionable: false }), planOptions[1]), /plan changed/)
  await assert.rejects(deck.sendPlanChoice(api, planMenu, planOptions[1]), /plan changed/)
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['approval', { session: '%1', thread: 't1', approval: 'permit', choice: 2 }],
    ['menu', { session: '%1', thread: 't1', choice: 3, label: 'Option 3' }],
    ['menu', { session: '%1', thread: 't1', choice: 2, label: 'Yes, manually approve edits' }],
    ['approval', { session: '%1', thread: 't1', approval: 'plan-1', choice: 2 }],
  ])
  // Home's items for the same panes: a plan has no answers, a question keeps options 1 to 3, a permission has no Always.
  const homePlan = needs.inboxItems([plan], {})[0]
  assert.deepEqual([homePlan.kind, homePlan.yes, homePlan.no, homePlan.always, homePlan.menu], ['plan', undefined, undefined, undefined, undefined])
  assert.equal(buttons(homePlan), '')
  assert.doesNotMatch(readFileSync(path.join(__dirname, 'src/useAnswers.ts'), 'utf8'), /plan/)
  console.log('deck answers: approval by id, question by menu option; plan only from the deck by approval id; Home unchanged')
})().catch(error => { console.error(error); process.exitCode = 1 })
