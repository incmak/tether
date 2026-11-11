// The chat ask card: kind chip, waited time, title and detail lines. Run: node test_ask_card.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const read = name => readFileSync(path.join(__dirname, 'src', name), 'utf8')
const code = ts.transpileModule(read('askCard.ts'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const card = {}
runInNewContext(code, { exports: card })
const { askKind, waited, splitApproval, planParts, questionTitle, KIND_LABEL } = card
const plain = value => JSON.parse(JSON.stringify(value))
const texts = parts => plain(parts.lines.map(line => line.text))
const signs = parts => plain(parts.lines.map(line => line.sign))

// Kind chip
assert.equal(KIND_LABEL[askKind(false, true, null)], 'Needs permission')
assert.equal(KIND_LABEL[askKind(true, true, null)], 'Plan to review')
assert.equal(KIND_LABEL[askKind(false, false, { reason: 'menu', options: [{ id: 1, text: 'Postgres' }] })], 'Question')
assert.equal(askKind(false, false, { reason: 'prompt', askBridge: { id: 'a' } }), 'question')
assert.equal(askKind(false, false, { reason: 'no-input-box', ask: { id: 'a', questions: [] } }), 'question')
for (const reason of ['panel', 'picker', 'info', 'permissions']) assert.equal(askKind(false, false, { reason, options: [{ id: 1 }] }), null, reason)
assert.equal(askKind(false, false, { reason: 'prompt', login: { url: 'https://example.com/login' } }), null)
assert.equal(askKind(false, false, { reason: 'prompt' }), null)
assert.equal(askKind(false, false, null), null)
console.log('ask card: permission, plan and question get a kind chip; panels, pickers and sign-in keep their own cards')

// Waited time
assert.equal(waited(1000, 1030), 'Just asked')
assert.equal(waited(1000, 1000 + 12 * 60 + 5), 'Waiting 12m')
assert.equal(waited(1000, 1000 + 3 * 3600), 'Waiting 3h')
assert.equal(waited(1000, 1000 + 2 * 86400), 'Waiting 2d')
assert.equal(waited(1000, 900), 'Just asked')
console.log('ask card: the header says how long the ask has waited')

// Bash command: the tool is the title, the command and its note stay in the box.
const bash = splitApproval('Bash command\n\n   npm test -- src/auth/login.test.ts --runInBand\n   Run the login tests')
assert.equal(bash.title, 'Bash command')
assert.equal(bash.format, 'command')
assert.deepEqual(texts(bash), ['npm test -- src/auth/login.test.ts --runInBand', 'Run the login tests'])
assert.equal(bash.question, '')
// A command that starts with "-" or "+" is never a diff row.
assert.ok(splitApproval('Bash command\n\n   find . -name "*.log" \\\n   -delete').lines.every(line => line.sign === ''))
// The boxed form of older dialogs.
const boxed = splitApproval('╭──────────────────────╮\n│ Bash command         │\n│   git status --short │\n│   Show changed files │\n╰──────────────────────╯')
assert.equal(boxed.title, 'Bash command')
assert.deepEqual(texts(boxed), ['git status --short', 'Show changed files'])
// Codex: "Command" then the command.
const codex = splitApproval('Command\ntouch example.txt')
assert.deepEqual([codex.title, codex.format, texts(codex)], ['Command', 'command', ['touch example.txt']])
console.log('ask card: a command approval has a title and the full command')

// File edit: the title names the file, the diff rows carry their sign, the closing question stays.
const edit = splitApproval(['Edit file', 'src/auth/session.ts', '', ' 12    const now = Date.now()', ' 13 -  return cookie.expires < now', ' 13 +  // expires is in seconds', ' 14 +  return cookie.expires * 1000 < now', ' 15  }', '', 'Do you want to make this edit to session.ts?'].join('\n'))
assert.equal(edit.title, 'Edit file · src/auth/session.ts')
assert.equal(edit.format, 'diff')
assert.deepEqual(signs(edit), ['', '-', '+', '+', ''])
assert.equal(edit.lines[1].text, '13 -  return cookie.expires < now')
assert.equal(edit.question, 'Do you want to make this edit to session.ts?')
// Plain "+" and "-" rows count under a file header only.
assert.deepEqual(signs(splitApproval('Update file\nnotes/todo.md\n\n-old line\n+new line\n same')), ['-', '+', ''])
assert.equal(splitApproval('Fetch\n\n- first point\n- second point').format, 'text')
// A new file has numbered rows and no signs: plain text, nothing coloured.
const create = splitApproval('Create file\ndocs/plan.md\n\n  1 # Plan\n  2 - Ship it')
assert.deepEqual([create.title, create.format], ['Create file · docs/plan.md', 'text'])
console.log('ask card: an edit approval names the file and marks added and removed lines')

// One line: it is the title. A question with nothing else: it is the title.
assert.deepEqual(plain(splitApproval('Do you want to approve network access to "api.example.com"?')), { title: 'Do you want to approve network access to "api.example.com"?', format: 'text', lines: [], question: '' })
// A read-only approval (the screen tail): the first line is the title, the rest is kept.
const tail = splitApproval('Codex is waiting for your approval.\n\nshell\n$ rm -r build\n› 1. Yes\n  2. No')
assert.equal(tail.title, 'Codex is waiting for your approval.')
assert.deepEqual(texts(tail), ['shell', '$ rm -r build', '› 1. Yes', '  2. No'])
// A long first line is not a title: everything stays in the box.
const long = 'x'.repeat(80)
assert.deepEqual([splitApproval(`${long}\nmore`).title, texts(splitApproval(`${long}\nmore`))], ['', [long, 'more']])
assert.deepEqual(plain(splitApproval('')), { title: '', format: 'text', lines: [], question: '' })
// No line of a request is lost: title, lines and question together hold every word.
for (const sample of ['Bash command\n\n   rm -r build\n   Remove the old build folder', 'Edit file\nsrc/app.ts\n 1 -a\n 1 +b\nDo you want to make this edit to app.ts?', 'Read file\n\n  Read(/work/notes.md)']) {
  const parts = splitApproval(sample), shown = [parts.title, ...texts(parts), parts.question].join('\n')
  for (const word of sample.split(/\s+/).filter(Boolean)) assert.ok(shown.includes(word), `${word} in ${sample}`)
}
console.log('ask card: short, read-only and unknown approvals keep all their text')

// Captured screens (server/test_fixtures/narrow/screens.json): every approval record, for Claude, Codex and Antigravity at 20 to 80 columns.
// The card gets the text that terminal_input.approval() keeps: the rows above the first option, without rule lines and
// without a one-row "Do you want to proceed?".
const approvalText = screen => {
  const rows = screen.replace(/\x1b\[[0-9;]*m/g, '').split('\n')
  const first = rows.findIndex(row => /^\s*[>❯›]?\s*\d+\.\s+\S/.test(row))
  return rows.slice(0, first).filter(row => !/^\s*(?:[─━═╌╍┄┅\-_]{3,}|Do you want to proceed\?)\s*$/.test(row)).join('\n').replace(/\n{3,}/g, '\n\n').trim()
}
const screens = JSON.parse(readFileSync(path.join(__dirname, '../server/test_fixtures/narrow/screens.json'), 'utf8')).filter(record => /approval/.test(record.kind))
assert.ok(screens.length >= 30 && ['claude', 'codex', 'antigravity'].every(agent => screens.some(record => record.agent === agent)))
for (const record of screens) {
  const parts = splitApproval(approvalText(record.screen)), where = `${record.agent} ${record.width} ${record.kind}`
  assert.equal(parts.title, 'Command', where)
  assert.equal(parts.format, 'command', where)
  // At 20 and 22 columns the question wraps ("Do you want to" / "proceed?"): it must not land in the command box.
  // A question the pane cut ("Do you want to proc?") is not the generic line: it stays, as the closing question.
  assert.deepEqual(texts(parts), ['touch example.txt'], where)
  assert.equal(parts.question, record.kind === 'clipped_approval' && record.width < 30 ? approvalText(record.screen).split('\n').pop() : '', where)
}
console.log(`ask card: ${screens.length} captured approval screens (Claude, Codex, Antigravity; 20 to 80 columns) split into "Command" and the command`)

// The approval samples of the server tests, as approval() hands them to the card.
// test_needs.py BASH
const rm = splitApproval('Bash command\n\n   rm -r build\n   Remove the old build folder')
assert.deepEqual([rm.title, rm.format, texts(rm), rm.question], ['Bash command', 'command', ['rm -r build', 'Remove the old build folder'], ''])
// test_session_controls.py: Claude's countdown notice stays in view, under the command.
const countdown = splitApproval('Bash command\n\n   rm -rf build\n\n Claude Code will automatically block this request in 0:57, to avoid holding up an unattended session')
assert.equal(countdown.title, 'Bash command')
assert.deepEqual(texts(countdown), ['  rm -rf build', '', 'Claude Code will automatically block this request in 0:57, to avoid holding up an unattended session'])
// test_push.py: the boxed dialog.
const pushed = splitApproval('╭──╮\n│ Bash command │\n│   git push -q example-user HEAD │\n│ Push the branch │')
assert.deepEqual([pushed.title, pushed.format, texts(pushed)], ['Bash command', 'command', ['  git push -q example-user HEAD', 'Push the branch']])
// test_session_controls.py: a network request is one question and nothing else.
assert.equal(splitApproval('Do you want to approve network access to "api.example.com"?').title, 'Do you want to approve network access to "api.example.com"?')
// test_plan_review.py SCREEN: the plan chooser's screen text. The card shows the transcript's plan (planParts below); this text is the fallback.
const chooser = splitApproval("Here is Claude's plan:\n# A plan\n\nWould you like to proceed?")
assert.deepEqual([chooser.title, texts(chooser), chooser.question], ["Here is Claude's plan:", ['# A plan'], 'Would you like to proceed?'])
// test_plan_review.py: the plan texts of the ExitPlanMode records.
assert.deepEqual(plain(planParts('# A plan\n\n- step')), { title: 'A plan', body: '- step' })
console.log('ask card: the server tests\' Bash, boxed, countdown, network and plan samples keep their title and text')

// Plan and question titles
assert.deepEqual(plain(planParts('# Add the inbox panel\n\n1. Add the route\n2. Add the list\n')), { title: 'Add the inbox panel', body: '1. Add the route\n2. Add the list\n' })
assert.deepEqual(plain(planParts('## Fix login ##\nSteps')), { title: 'Fix login', body: 'Steps' })
assert.deepEqual(plain(planParts('First do this.\n\n# Later heading')), { title: '', body: 'First do this.\n\n# Later heading' })
assert.equal(questionTitle(' Which database should the service use? '), 'Which database should the service use?')
assert.equal(questionTitle('Line one\nLine two'), '')
assert.equal(questionTitle('y'.repeat(201)), '')
console.log('ask card: a plan takes its first heading as the title; a one-line question is the title')

// The dock uses the helpers and keeps the one answer path. A plan gets no primary button.
const dock = read('Dock.tsx')
assert.match(dock, /splitApproval\(approval\.text\)/)
assert.match(dock, /primary = !plan && approval/)
// One kind label: the chip above the card (Chat.tsx). The card itself adds only the wait.
assert.doesNotMatch(dock, /KIND_LABEL/)
assert.match(read('Chat.tsx'), /askLabel = askCardKind \? KIND_LABEL\[askCardKind\] : undefined/)
assert.equal((dock.match(/onChoose\(/g) ?? []).length, 1, 'options answer through the one existing onChoose call')
console.log('ask card: the dock shows the parts and answers through the existing path')
