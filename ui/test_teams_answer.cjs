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
    // The pure parsers load for real: Message() asks each one whether the text is an injected prompt.
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
  + 'Its last reply:\n\nDone.\n\nTranscript: /t.jsonl')
assert(card.includes(`Reply <code>APPROVED ${HASH}</code> (the full hash)`), card)
assert(card.includes('<code>git -C /w show origin/main:REVIEW.md</code>'), card)
assert(!card.includes('Reply APPROVED,'), card)
assert.equal(card.split(`APPROVED ${HASH}`).length, 2, 'the reply format shows once')

// Words that are also Object.prototype keys stay the person's bubble; only the exact interrupt lines become notice lines.
for (const word of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) assert(render(word).includes('class="user-message"'), word)
assert(render('[Request interrupted by user]').includes('<p class="notice-line" data-msg-id="7">'))
assert(render('[Request interrupted by user] and more').includes('class="user-message"'))

const css = readFileSync('src/App.css', 'utf8')
assert(/\.via-label \{[^}]*color-mix\(in srgb, var\(--text\) 78%, transparent\)/.test(css), 'the label keeps a 4.5:1 colour on the bubble')
console.log('Teams answer: quiet label, no prefix, other messages unchanged')
