import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, extname, resolve } from 'node:path'
import { brotliCompressSync, constants } from 'node:zlib'

const liveGuard: Plugin = {
  name: 'live-guard',
  apply: 'build',
  configResolved(config) {
    const outDir = resolve(config.root, config.build.outDir)
    if (existsSync(resolve(outDir, '.live-ui'))) {
      throw new Error('This folder is the live Tether UI. Build to another folder (vite build --outDir <dir>), then run scripts/deploy-ui.py <dir> ui/dist.')
    }
  },
}

// Copy from the installed versions so dev and production use the same local assets.
const vadAssets = {
  name: 'vad-assets',
  configResolved(config: { publicDir: string }) {
    const require = createRequire(import.meta.url)
    const vad = require.resolve('@ricky0123/vad-web')
    const runtime = createRequire(vad)
    const target = resolve(config.publicDir, 'vad')
    mkdirSync(target, { recursive: true })
    for (const name of ['silero_vad_v5.onnx', 'vad.worklet.bundle.min.js']) {
      copyFileSync(resolve(dirname(vad), name), resolve(target, name))
    }
    for (const name of ['ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.mjs']) {
      copyFileSync(runtime.resolve(`onnxruntime-web/${name}`), resolve(target, name))
    }
  },
}

// Move the app stylesheet from <head> to the end of <body>. A head stylesheet blocks all paint, so on 3G the
// boot screen in index.html stayed blank until the CSS arrived. A body stylesheet still holds the module
// script until it loads, so React never renders unstyled.
const bodyStylesheet = {
  name: 'body-stylesheet',
  apply: 'build' as const,
  transformIndexHtml: {
    order: 'post' as const,
    handler(html: string) {
      const links: string[] = []
      const out = html.replace(/\s*<link rel="stylesheet"[^>]*>/g, (tag) => { links.push(tag.trim()); return '' })
      return out.replace('</body>', `  ${links.join('\n    ')}\n  </body>`)
    },
  },
}

// B26: write a .br file next to each hashed text asset of 1 KB or more. The server sends it to browsers that accept br
// (server.py serve_chat_ui). Built once at the highest quality, so the server never compresses these files.
const BROTLI_TYPES = new Set(['.js', '.mjs', '.css', '.svg', '.json'])
export function brotliFiles(dir: string) {
  const written: string[] = []
  if (!existsSync(dir)) return written
  for (const name of readdirSync(dir)) {
    const file = resolve(dir, name)
    if (!BROTLI_TYPES.has(extname(name)) || statSync(file).size < 1024) continue
    const data = readFileSync(file)
    const packed = brotliCompressSync(data, { params: { [constants.BROTLI_PARAM_QUALITY]: 11, [constants.BROTLI_PARAM_SIZE_HINT]: data.length } })
    if (packed.length < data.length) { writeFileSync(`${file}.br`, packed); written.push(name) }
  }
  return written
}
const brotliAssets: Plugin = {
  name: 'brotli-assets',
  apply: 'build',
  writeBundle(options) { if (options.dir) brotliFiles(resolve(options.dir, 'assets')) },
}

// B26: packages that the first screen loads go to one `vendor` chunk. A deploy changes the app code, not the packages,
// so phones keep the cached vendor chunk and download only the app chunk again. Packages that only lazy chunks use
// (pdf.js, xlsx, voice) stay out of it, so the first load does not grow. lucide-react stays out too: its index imports
// every icon, and the Sheets icon search loads all of them lazily, so a vendor chunk would pull 1.2 MB of icons in.
type ModuleInfo = { isEntry: boolean; importers: readonly string[] } | null
export function vendorChunk(id: string, getModuleInfo: (id: string) => ModuleInfo, seen = new Map<string, boolean>()): string | undefined {
  if (!id.includes('/node_modules/') || id.includes('/node_modules/lucide-react/')) return undefined
  const fromEntry = (module: string): boolean => {
    const known = seen.get(module)
    if (known !== undefined) return known
    seen.set(module, false)  // a cycle does not prove a static path
    const info = getModuleInfo(module)
    const result = !!info && (info.isEntry || info.importers.some(fromEntry))
    seen.set(module, result)
    return result
  }
  return fromEntry(id) ? 'vendor' : undefined
}
const vendorSeen = new Map<string, boolean>()

// B26: write the files of each lazy panel into index.html, so lazyPanels.ts can prefetch them into the HTTP cache with
// <link rel="prefetch">. A prefetch does not run Vite's import helper, so a failed prefetch cannot fire
// `vite:preloadError` (a page reload) or leave a stylesheet that Vite thinks it already loaded.
export const PANELS: Record<string, string> = { explorer: '/src/Explorer.tsx', explorerPhone: '/src/ExplorerPhone.tsx', share: '/src/Share.tsx' }
type OutChunk = { type: 'chunk'; fileName: string; isEntry: boolean; facadeModuleId: string | null; imports: string[]; viteMetadata?: { importedCss: Set<string> } }
type OutBundle = Record<string, OutChunk | { type: 'asset' }>
export function panelFiles(bundle: OutBundle, base: string) {
  const chunks = new Map<string, OutChunk>()
  for (const item of Object.values(bundle)) if (item.type === 'chunk') chunks.set(item.fileName, item)
  const closure = (start: string[], into = new Set<string>()) => {
    for (const name of start) {
      if (into.has(name) || !chunks.has(name)) continue
      into.add(name)
      closure(chunks.get(name)!.imports, into)
    }
    return into
  }
  const boot = closure([...chunks.values()].filter(chunk => chunk.isEntry).map(chunk => chunk.fileName))
  const out: Record<string, string[]> = {}
  for (const [key, source] of Object.entries(PANELS)) {
    const panel = [...chunks.values()].find(chunk => chunk.facadeModuleId?.endsWith(source))
    if (!panel) continue
    const files: string[] = []
    for (const name of closure([panel.fileName])) {
      if (boot.has(name)) continue
      files.push(name, ...(chunks.get(name)!.viteMetadata?.importedCss ?? []))
    }
    out[key] = [...new Set(files)].map(name => base + name)
  }
  return out
}
const panelManifest: Plugin = {
  name: 'panel-manifest',
  apply: 'build',
  transformIndexHtml: {
    order: 'post',
    handler(html, context) {
      if (!context.bundle) return html
      const json = JSON.stringify(panelFiles(context.bundle as unknown as OutBundle, '/chat/')).replaceAll('<', '\\u003c')
      return html.replace('</head>', `  <script type="application/json" id="panel-files">${json}</script>\n  </head>`)
    },
  },
}

export default defineConfig({
  base: '/chat/',
  plugins: [liveGuard, react(), tailwindcss(), vadAssets, bodyStylesheet, panelManifest, brotliAssets],
  build: {
    rollupOptions: { output: { manualChunks: (id, { getModuleInfo }) => vendorChunk(id, getModuleInfo, vendorSeen) } },
  },
  server: {
    proxy: { '/mobile': { target: 'http://127.0.0.1:7683', changeOrigin: false } },
  },
})
