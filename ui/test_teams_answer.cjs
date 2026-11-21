// An answer given in Teams shows as your own bubble with a quiet "Answered from Teams" line, without the typed prefix.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const output = {}
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
const PARSERS = ['./reviewText', './statusText', './injectedPrompt']
const parser = name => {
  const module = {}
  runInNewContext(compile(readFileSync(`src/${name.slice(2)}.ts`, 'utf8')), { exports: module, require: next => parser(next) })
  return module
}
const Root = ({ className, children, ...rest }) => React.createElement('div', { className, 'data-msg-id': rest['data-msg-id'] }, children)
runInNewContext(compile(readFileSync('src/Messages.tsx', 'utf8')), {
  exports: output,
  require(name) {
    if (['react', 'react/jsx-runtime', 'lucide-react'].includes(name)) return require(name)
    if (name === '@assistant-ui/react') return { MessagePrimitive: { Root, Parts: () => 'PARTS' } }
    if (name === './shellCommand') return { shellCommand: () => null }
    if (name === './SentImages') return { sentContent: () => null }
    // Marked prompts still have to pass the real parsers.
    if (PARSERS.includes(name)) return parser(name)
    return {}
  },
})
const render = (text, props = {}) => renderToStaticMarkup(React.createElement(output.Message, { id: '7', role: 'user', text, api() {}, session: '%fixture', ...props }))

const html = render('[Answer from Teams] you do it\nand push <b>')
assert(html.includes('class="user-message via-teams"'), html)
assert(html.includes('data-msg-id="7"'))
assert(html.includes('Answered from Teams'))
assert(html.includes('you do it\nand push &lt;b&gt;'), 'the answer shows as plain text, with its line breaks')
assert(!html.includes('[Answer from Teams]'), 'the typed prefix is not shown')
assert(/<svg[^>]*aria-hidden="true"/.test(html), 'the icon is decorative')

// Other messages keep their form: the prefix in the middle, an empty answer, and an agent's reply.
for (const text of ['I saw [Answer from Teams] in the log', '[Answer from Teams] ', 'plain']) assert(!render(text).includes('Answered from Teams'), text)
assert(!render('[Answer from Teams] x', { role: 'assistant', actions: false }).includes('Answered from Teams'))

// A review request bound to a commit (B245): the card names the full hash in the reply format and shows the rules line.
const HASH = '3f9c2a7b41d6e8f05a1c9b2d7e4f6a8c0b1d2e3f'
const card = render(`[Tether review] The codex tab “W” (%7) finished a turn. Review its work: check the changes and the claims, then list what is wrong or missing. Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. Review id: 0a1b2c3d. Commit under review: ${HASH} in /w. `
  + `Read the review rules first: \`git -C /w show origin/main:REVIEW.md\`. An approval counts only for this commit: start it with \`APPROVED ${HASH}\` (the full hash). `
  + 'Its last reply:\n\nDone.\n\nTranscript: /t.jsonl', { source: 'tether', prompt: 'review' })
assert(card.includes(`Reply <code>APPROVED ${HASH}</code> (the full hash)`), card)
assert(card.includes('<code>git -C /w show origin/main:REVIEW.md</code>'), card)
assert(!card.includes('Reply APPROVED,'), card)
assert.equal(card.split(`APPROVED ${HASH}`).length, 2, 'the reply format shows once')

const { parseReview } = parser('./reviewText')
const otherHash = 'a'.repeat(40)
const approve = (covers, reply, footer) => `[Tether review] Approved by the reviewer tab (%7)${covers}:\n\n${reply}\n\nThe review is complete. ${footer}`
const next = 'the next step your instructions give for an approved review, then stop.'
const unbound = `Do ${next}`
const dirty = 'It covered uncommitted changes, so no commit is approved yet: commit the work '
  + `in this turn, and Tether asks the reviewer to confirm that commit. ${unbound}`
const clean = `The approval is for commit ${HASH} only. Before you push, deploy or merge, `
  + `check that HEAD in this tab's worktree (\`git rev-parse HEAD\`) is ${HASH}. `
  + `If HEAD is different or you cannot read it, do not act on this approval. Stop and wait for the next review. If HEAD is that hash, do ${next}`
for (const [covers, reply, footer] of [
  ['', 'approved', unbound],
  ['', `APPROVED ${HASH}`, unbound],
  [` for commit ${HASH}`, `APPROVED ${HASH}`, clean],
  [` for commit ${HASH}`, `Approved — for commit \x60${HASH.toUpperCase()}\x60`, clean],
  [` for commit ${HASH} and its uncommitted changes`, `Approved ${HASH}`, dirty],
]) assert.equal(parseReview(approve(covers, reply, footer))?.kind, 'approved')
for (const [covers, reply, footer] of [
  [` for commit ${HASH}`, 'I have not reviewed this.', unbound],
  [` for commit ${HASH}`, `APPROVED ${HASH}`, unbound],
  [` for commit ${HASH}`, `APPROVED ${HASH}`, dirty],
  [` for commit ${HASH} and its uncommitted changes`, `APPROVED ${HASH}`, clean],
  ['', 'Reviewed and approved.', unbound],
]) assert.equal(parseReview(approve(covers, reply, footer)), null)
for (const approval of [
  approve(` for commit ${HASH}`, `APPROVED ${otherHash}`, clean),
  approve(` for commit ${HASH} and its uncommitted changes`, `Approved: for commit **${'B'.repeat(64)}**`, dirty),
  approve(` for commit ${HASH}`, `APPROVED ${HASH.slice(0, 12)}`, clean),
]) {
  assert.equal(parseReview(approval), null, 'the approval names a different commit')
  assert(render(approval, { source: 'tether', prompt: 'review' }).includes('class="user-message"'))
}
for (const [reply, expected] of [
  ['APPROVED. The regression tests pass.', null],
  [`APPROVED.\n\nI checked ${HASH}.`, null],
  [`APPROVED ${HASH.slice(0, 39)}`, null],
  [`APPROVED ${HASH}é`, null],
  [`aPpRoVeD\u0085for COMMIT **${HASH.toUpperCase()}**`, 'approved'],
]) {
  const approval = approve(` for commit ${HASH}`, reply, clean)
  assert.equal(parseReview(approval)?.kind ?? null, expected, reply)
  if (expected === null) assert(render(approval, { source: 'tether', prompt: 'review' }).includes('class="user-message"'))
}
assert.equal(parseReview(approve(` for commit ${HASH} and its uncommitted changes`, 'APPROVED', dirty)), null)
assert.equal(parseReview(approve(` for commit ${HASH.slice(0, 12)}`, 'APPROVED', unbound))?.kind, 'approved')

const samples = JSON.parse(readFileSync('../server/fixtures/injected_prompts/other.json', 'utf8'))
const wrongScript = samples.review_notice.replaceAll('/agent_tab_cli.py', '/sample.py')
assert.equal(parseReview(wrongScript), null, 'both commands must run agent_tab_cli.py')
assert(render(wrongScript, { source: 'tether', prompt: 'review' }).includes('class="user-message"'))
assert.equal(parseReview(samples.review_notice.replace('after 3 rounds', 'after 5 rounds')), null)
assert.equal(parseReview(samples.review_summary.replace('3 review rounds', '5 review rounds')), null)
assert.equal(parseReview(samples.review_stopped.replace('after 3 review rounds', 'after 5 review rounds'))?.rounds, 5)
const correction = (approved, head) => `[Tether review] Correction: the approval above is for commit ${approved} only. `
  + `This tab is now at ${head}. Do not push, deploy or merge on that approval. Wait for the next review.`
assert.equal(parseReview(correction(HASH, otherHash))?.kind, 'correction')
assert.equal(parseReview(correction(HASH, HASH)), null)
assert.equal(parseReview(correction(HASH.slice(0, 12), otherHash)), null)
assert.equal(parseReview(correction(HASH, otherHash.slice(0, 12))), null)

const legacy = agent => `[Tether review] The ${agent} tab “W” (%7) finished a turn. Review its work: Its last reply:\n\nDone.\n\nTranscript: /srv/project/chat.jsonl`
for (const agent of ['Claude', 'Codex', 'claude', 'codex']) assert.equal(parseReview(legacy(agent))?.kind, 'request')
for (const agent of ['fictional', 'Antigravity']) assert.equal(parseReview(legacy(agent)), null)
const request = legacy('Codex').replace('Review its work: ', 'Review its work: check the changes and the claims, then list what is wrong or missing. '
  + 'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. Review id: 0a1b2c3d. ')
assert.equal(parseReview(request)?.kind, 'request')
assert.equal(parseReview(request.replace('0a1b2c3d', '0a1b2c3z')), null)
assert.equal(parseReview(request.replace('Tether sends your reply to that tab.', 'Send it yourself.')), null)

// Words that are also Object.prototype keys stay the person's bubble; only the exact interrupt lines become notice lines.
for (const word of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) assert(render(word).includes('class="user-message"'), word)
assert(render('[Request interrupted by user]').includes('<p class="notice-line" data-msg-id="7">'))
assert(render('[Request interrupted by user] and more').includes('class="user-message"'))

const css = readFileSync('src/App.css', 'utf8')
assert(/\.via-label \{[^}]*color-mix\(in srgb, var\(--text\) 78%, transparent\)/.test(css), 'the label keeps a 4.5:1 colour on the bubble')
console.log('Teams answer: quiet label, no prefix, other messages unchanged')
