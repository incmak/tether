// Text contrast on accent fills (WCAG 2.2 AA, 1.4.3: 4.5:1), for each color scheme and theme.
// The values come from the CSS and from schemes.ts, so a later token edit that fails contrast fails here. Run: node test_contrast.cjs
const { strict: assert } = require('node:assert')
const { readFileSync, readdirSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const load = (file, require) => {
  const code = ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}
  runInNewContext(code, { exports, require })
  return exports
}
const tones = load('src/tones.ts')
const schemes = load('src/schemes.ts', name => { assert.equal(name, './tones'); return tones })
const { contrast, mix, accentFill, HOVER_SHARE } = tones

// One palette: { accent, accentText, fill }.
function check(name, p) {
  const at = (what, a, b, low) => assert.ok(contrast(a, b) >= low, `${name}: ${what} is ${contrast(a, b).toFixed(2)}:1 (${a} on ${b}), needs ${low}:1`)
  at('text on a filled control', p.accentText, p.fill, 4.5)
  at('text on a hovered filled control', p.accentText, mix(p.fill, '#000000', HOVER_SHARE), 4.5)
}
const vars = text => Object.fromEntries([...text.matchAll(/--([a-z-]+):\s*(#[0-9a-f]{6})\b/g)].map(m => [m[1], m[2]]))
const palette = v => ({ accent: v.accent, accentText: v['accent-text'], fill: v['accent-fill'] })
let checked = 0

// 1. The built-in palette, as index.css states it.
const css = readFileSync('src/index.css', 'utf8')
const light = vars(css.match(/^:root \{([^}]*)\}/m)[1])
const darkAuto = vars(css.match(/:root:not\(\[data-theme="light"\]\) \{([^}]*)\}/)[1])
const darkSet = vars(css.match(/^:root\[data-theme="dark"\] \{([^}]*)\}/m)[1])
assert.deepEqual(darkAuto, darkSet, 'index.css: the two dark blocks hold the same tokens')
for (const [name, v] of [['claude light (index.css)', light], ['claude dark (index.css)', { ...light, ...darkSet }]]) { check(name, palette(v)); checked++ }

// 2. Each scheme, as schemes.ts writes it to the page.
for (const scheme of schemes.SCHEMES) for (const mode of ['light', 'dark']) {
  const t = scheme[mode], made = vars(schemes.tones(t))
  check(`${scheme.id} ${mode}`, { ...t, fill: made['accent-fill'] }); checked++
  // "claude" adds no style: index.css must hold the same values.
  if (scheme.id === 'claude') {
    const own = palette(mode === 'light' ? light : { ...light, ...darkSet })
    assert.deepEqual(own, { accent: t.accent, accentText: t.accentText, fill: made['accent-fill'] }, `index.css and schemes.ts differ for claude ${mode}`)
  }
}

// 3. The built-in layout themes: applyLayoutTheme makes the fill with the same function.
const themes = [...readFileSync('src/layouts.ts', 'utf8').matchAll(/theme: \{ (bg: '#[^}]+) \}/g)].map(m => Object.fromEntries([...m[1].matchAll(/(\w+): '(#[0-9a-f]{6})'/g)].map(x => [x[1], x[2]])))
assert.ok(themes.length >= 3, 'found the layout themes')
themes.forEach((t, index) => {
  check(`layout theme ${index + 1}`, { ...t, fill: accentFill(t.accent, t.accentText) }); checked++
})

// 4. The check is real: the old pair fails, and the functions change only what fails.
assert.throws(() => check('old claude light', { ...palette(light), fill: '#c96442' }), /text on a filled control is 3\.76:1/)
assert.equal(accentFill('#3b5bab', '#ffffff'), '#3b5bab', 'a passing accent stays as it is')
assert.notEqual(accentFill('#c96442', '#fffaf5'), '#c96442')

// 5. The rules that use the tokens: a fill under --accent-text is --accent-fill.
const share = Number(readFileSync('src/controls.css', 'utf8').match(/--accent-hover: color-mix\(in srgb, var\(--accent-fill\) (\d+)%, #000\)/)[1]) / 100
assert.equal(share, HOVER_SHARE, 'controls.css --accent-hover and tones.ts HOVER_SHARE match')
for (const file of readdirSync('src').filter(name => name.endsWith('.css'))) {
  for (const [, selector, body] of readFileSync(`src/${file}`, 'utf8').matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const where = `${file}: ${selector.trim().slice(0, 80)}`
    if (/(^|[^-\w])color:\s*var\(--accent-text\)/.test(body)) assert.ok(!/background(-color)?:\s*var\(--accent\)/.test(body), `${where}: text on an accent fill must use --accent-fill`)
  }
}

console.log(`contrast: ${checked} palettes pass`)
