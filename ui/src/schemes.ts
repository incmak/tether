// App color schemes. Each has a light and a dark set of the tokens in index.css; the Theme setting picks which set shows.
// "claude" is the built-in palette in index.css, so it adds no style.
import { accentFill, accentInk } from './tones'

type Tokens = { bg: string; sidebar: string; surface: string; bubble: string; text: string; muted: string; line: string; hover: string; accent: string; accentText: string; attention?: string; error?: string }
export interface Scheme { id: string; name: string; light: Tokens; dark: Tokens }

const t = (bg: string, sidebar: string, surface: string, bubble: string, text: string, muted: string, line: string, hover: string, accent: string, accentText: string, extra: Partial<Tokens> = {}): Tokens =>
  ({ bg, sidebar, surface, bubble, text, muted, line, hover, accent, accentText, ...extra })

export const SCHEMES: Scheme[] = [
  { id: 'claude', name: 'Claude', light: t('#f7f5f0', '#eeece5', '#fcfbf8', '#eae7de', '#302f2b', '#67655d', '#d9d6cd', '#e6e3da', '#c96442', '#fffaf5'), dark: t('#262624', '#20201e', '#30302c', '#383832', '#e9e6df', '#b1ada3', '#48473f', '#363630', '#c96442', '#fffaf5') },
  { id: 'paper', name: 'Paper', light: t('#f8f8f6', '#efefec', '#ffffff', '#ebebe7', '#2b2c2e', '#616367', '#dcdcd8', '#e6e6e2', '#3b5bab', '#ffffff'), dark: t('#1f2023', '#1a1b1d', '#28292d', '#323338', '#e6e6e8', '#a9abb1', '#3e4046', '#303136', '#8aa4e8', '#101522') },
  { id: 'sage', name: 'Sage', light: t('#f4f6f1', '#e9ede5', '#fbfcf9', '#e3e8dd', '#2a3027', '#596254', '#d3dacb', '#dde3d6', '#4a7550', '#f7fbf5'), dark: t('#1e221e', '#191c19', '#272c27', '#30362f', '#e3e8df', '#a6b0a1', '#3d453b', '#2d332c', '#8fbf8c', '#132013') },
  { id: 'moss', name: 'Moss', light: t('#f5f4ec', '#ebe9dd', '#fcfbf5', '#e5e2d2', '#2d2e22', '#5c5d53', '#d8d5c4', '#e0ddcc', '#5f6d22', '#fbfbef', { attention: '#7a5c1a' }), dark: t('#1d1f17', '#181a13', '#26291f', '#2f3226', '#e6e6d6', '#aeae98', '#404334', '#2c2f23', '#b3c46a', '#1a1d0c') },
  { id: 'tide', name: 'Tide', light: t('#f3f6f7', '#e8edef', '#fafcfc', '#e0e8ea', '#23303a', '#56656f', '#cfdade', '#d9e3e6', '#1b6f80', '#f2fbfd'), dark: t('#1a2126', '#151b1f', '#222b31', '#2a353c', '#dfe8ec', '#9fb0b9', '#37444c', '#26313a', '#5fc0d0', '#0b1f24') },
  { id: 'fjord', name: 'Fjord', light: t('#f2f4f8', '#e6eaf1', '#fafbfd', '#e1e6ee', '#2e3440', '#596276', '#d3d9e4', '#dbe0ea', '#4c6f9c', '#f7f9fd', { attention: '#82641e' }), dark: t('#242933', '#1e222b', '#2c323e', '#343b49', '#e5e9f0', '#a7b0c2', '#434c5e', '#323a47', '#88c0d0', '#14202a') },
  { id: 'plum', name: 'Plum', light: t('#f7f4f6', '#eee8ec', '#fdfbfc', '#ebe3e8', '#302833', '#675c6c', '#dcd2da', '#e5dce3', '#7b4a8c', '#fcf7fd'), dark: t('#241f26', '#1e1a20', '#2d2730', '#362f39', '#ebe4ee', '#b3a8b8', '#483f4c', '#342d37', '#c79bd8', '#22152a') },
  { id: 'rose', name: 'Rosé', light: t('#faf5f4', '#f2e9e7', '#fffbfa', '#efe3e0', '#3a2c2c', '#6e5d5a', '#e4d4d0', '#ecdedb', '#a84a5d', '#fff7f8'), dark: t('#271f20', '#211a1b', '#312728', '#3b2f30', '#f0e4e3', '#bba9a7', '#4d3e3f', '#382d2e', '#e48a9c', '#2a1217') },
  { id: 'dune', name: 'Dune', light: t('#f8f4ea', '#efe8d8', '#fdfaf3', '#ebe2cd', '#33291a', '#6a5f4a', '#ddd1b6', '#e6dcc5', '#8a5f12', '#fffaf0', { attention: '#7a5a14' }), dark: t('#25211a', '#1f1c16', '#2f2a21', '#383228', '#efe6d4', '#b8ab92', '#4a4234', '#352f25', '#e0b25a', '#2a1d05') },
  { id: 'graphite', name: 'Graphite', light: t('#f6f6f6', '#ececec', '#fcfcfc', '#e8e8e8', '#262626', '#606060', '#d6d6d6', '#e2e2e2', '#3d3d3d', '#ffffff'), dark: t('#1c1c1c', '#171717', '#252525', '#2e2e2e', '#ebebeb', '#a8a8a8', '#3c3c3c', '#2b2b2b', '#d4d4d4', '#161616') },
  { id: 'ink', name: 'Ink', light: t('#ffffff', '#f5f5f7', '#ffffff', '#f0f0f3', '#18181b', '#5f5f6b', '#e4e4e7', '#ececf0', '#4f46e5', '#ffffff'), dark: t('#0f0f12', '#0b0b0d', '#18181c', '#222228', '#ededf0', '#a1a1aa', '#2e2e35', '#1f1f25', '#8b85ff', '#0f0d24') },
  { id: 'midnight', name: 'Midnight', light: t('#f4f6fb', '#e9edf6', '#fbfcfe', '#e3e8f4', '#1f2640', '#586079', '#d3d9ea', '#dce2f0', '#3d55c9', '#f6f8ff'), dark: t('#151a2c', '#111526', '#1c2238', '#242b44', '#e3e7f5', '#a3abc7', '#343c5a', '#222944', '#8ea2ff', '#0f1530') },
]

export const DEFAULT_SCHEME = 'claude'
export const schemeById = (id: string | undefined) => SCHEMES.find(scheme => scheme.id === id) ?? SCHEMES[0]

/** The tones of the accent that carry text: see tones.ts. index.css holds the same two values for "claude". */
export const tones = (tokens: Tokens) =>
  `--accent-fill:${accentFill(tokens.accent, tokens.accentText)};--accent-ink:${accentInk(tokens.accent, [tokens.bg, tokens.sidebar, tokens.surface, tokens.bubble, tokens.hover])};`

const LIGHT_EXTRA = { attention: '#8c6b22', error: '#a43b31' }, DARK_EXTRA = { attention: '#d9b761', error: '#f09888' }
const block = (tokens: Tokens, extra: { attention: string; error: string }) => {
  const all = { ...extra, ...tokens }
  return `color:${all.text};background:${all.bg};--bg:${all.bg};--sidebar:${all.sidebar};--surface:${all.surface};--bubble:${all.bubble};--text:${all.text};--muted:${all.muted};--line:${all.line};--hover:${all.hover};--accent:${all.accent};--accent-text:${all.accentText};${tones(all)}--attention:${all.attention};--error:${all.error};`
}

/** Apply a scheme: one style tag after the app CSS, with the same selectors as index.css. Also sets the browser bar color. */
export function applyScheme(id: string | undefined) {
  const scheme = schemeById(id)
  let style = document.getElementById('tether-scheme')
  if (scheme.id === DEFAULT_SCHEME) style?.remove()
  else {
    if (!style) { style = document.createElement('style'); style.id = 'tether-scheme' }
    document.head.appendChild(style)  // keep it last, so it wins over the app CSS
    style.textContent = `:root{${block(scheme.light, LIGHT_EXTRA)}}`
      + `@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){${block(scheme.dark, DARK_EXTRA)}}}`
      + `:root[data-theme="dark"]{${block(scheme.dark, DARK_EXTRA)}}`
  }
  document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => {
    meta.content = meta.media.includes('dark') ? scheme.dark.bg : scheme.light.bg
  })
}

/** Use one browser theme color until the returned function runs. For surfaces that fill the window, such as the media viewer. */
let held = 0
export function holdThemeColor(color: string) {
  const metas = [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')]
  const before = metas.map(meta => meta.content)
  metas.forEach(meta => { meta.content = color })
  held++
  return () => { held--; metas.forEach((meta, at) => { meta.content = before[at] }) }
}
/** True while a full-window surface holds the browser theme color. */
export const themeColorHeld = () => held > 0
