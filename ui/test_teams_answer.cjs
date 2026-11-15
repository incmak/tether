// An answer given in Teams shows as your own bubble with a quiet "Answered from Teams" line, without the typed prefix.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const output = {}
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
const Root = ({ className, children, ...rest }) => React.createElement('div', { className, 'data-msg-id': rest['data-msg-id'] }, children)
runInNewContext(compile(readFileSync('src/Messages.tsx', 'utf8')), {
  exports: output,
  require(name) {
    if (['react', 'react/jsx-runtime', 'lucide-react'].includes(name)) return require(name)
    if (name === '@assistant-ui/react') return { MessagePrimitive: { Root, Parts: () => 'PARTS' } }
    if (name === './shellCommand') return { shellCommand: () => null }
    if (name === './SentImages') return { sentContent: () => null }
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

const css = readFileSync('src/App.css', 'utf8')
assert(/\.via-label \{[^}]*color-mix\(in srgb, var\(--text\) 78%, transparent\)/.test(css), 'the label keeps a 4.5:1 colour on the bubble')
console.log('Teams answer: quiet label, no prefix, other messages unchanged')
