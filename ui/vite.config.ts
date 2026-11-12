import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'

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

export default defineConfig({
  base: '/chat/',
  plugins: [liveGuard, react(), tailwindcss(), vadAssets, bodyStylesheet],
  server: {
    proxy: { '/mobile': { target: 'http://127.0.0.1:7683', changeOrigin: false } },
  },
})
