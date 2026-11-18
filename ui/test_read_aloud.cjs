// Read aloud uses the real chat UI and Web Audio, with fixture-only API routes.
const http = require('node:http'), fs = require('node:fs/promises'), path = require('node:path')
const assert = require('node:assert/strict')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const sentences = [
  'The numbers add up.',
  'The balance sheet balances across all statements, and the figures that appear in more than one place agree with the figures in the source records.',
  'The cash flow statement reconciles with the opening balance, the closing balance, the transfers, and all the payments recorded during the quarter.',
  'The notes explain the adjustments that were made after the quarter closed and identify which entries the reviewer needs to sign off this week.',
]
const reply = sentences.join(' ')
const fixture = {
  sessions: { sessions: [{ id: '%1', tmux_session: '$1', label: 'project:0.1', pane_name: 'Read aloud', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null, ask: null }], default_thread: 't1' },
  chat: { messages: [{ id: 'u1', role: 'user', text: 'Check the statements', time: new Date().toISOString() }, { id: 'a1', role: 'assistant', text: reply, time: new Date().toISOString() }], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  prefs: { prefs: {}, version: 1 },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  'browser/tabs': { tabs: [] }, 'work/board': { revision: 1, projects: [], items: [], stages: [] },
}

function silenceWav() {
  const sampleRate = 24000, bytesPerSample = 2
  const dataLength = 0.2 * sampleRate * bytesPerSample
  const wav = Buffer.alloc(44 + dataLength)
  wav.write('RIFF', 0)
  wav.writeUInt32LE(wav.length - 8, 4)
  wav.write('WAVEfmt ', 8)
  wav.writeUInt32LE(16, 16)
  wav.writeUInt16LE(1, 20) // PCM
  wav.writeUInt16LE(1, 22) // Mono
  wav.writeUInt32LE(sampleRate, 24)
  wav.writeUInt32LE(sampleRate * bytesPerSample, 28)
  wav.writeUInt16LE(bytesPerSample, 32)
  wav.writeUInt16LE(8 * bytesPerSample, 34)
  wav.write('data', 36)
  wav.writeUInt32LE(dataLength, 40)
  return wav
}

;(async () => {
  const server = http.createServer((q, r) => { r.writeHead(404); r.end() })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://localhost:${server.address().port}`
  const browser = await getBrowser()
  const context = await browser.newContext({ viewport: { width: 320, height: 844 }, serviceWorkers: 'block' })
  const page = await context.newPage()
  const requests = []
  const wav = silenceWav()
  let gate, failureAt = -1, failure
  const hold = () => {
    let release, receive
    const pending = new Promise(resolve => { release = resolve })
    const received = new Promise(resolve => { receive = resolve })
    gate = { pending, receive }
    return { release, wait: async () => {
      let timer
      try {
        await Promise.race([received, new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('No TTS request reached the held route')), 5000)
        })])
      } finally { clearTimeout(timer) }
    } }
  }
  let passed = 0
  const check = (name, action) => { action(); passed++; console.log(`ok ${name}`) }
  try {
    page.setDefaultTimeout(5000)
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const ep = url.pathname.slice(12)
        if (ep === 'tts') {
          const request = route.request()
          requests.push({ text: request.postDataJSON().text, method: request.method(), headers: request.headers() })
          if (requests.length === failureAt) {
            if (failure === 'network') return route.abort('failed')
            return route.fulfill({ status: failure, json: { error: 'Natural voice unavailable' } })
          }
          const held = gate
          gate = undefined
          if (held) { held.receive(); await held.pending }
          // The browser may discard the route after it aborts a held request.
          return route.fulfill({ contentType: 'audio/wav', body: wav }).catch(error => {
            if (!held) throw error
          })
        }
        if (ep === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[ep] || {} })
      }
      if (url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.join(DIST, decodeURIComponent(url.pathname.slice(5)))
        try { return await route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort()
    })
    await page.addInitScript(() => {
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
      window.__voice = { events: [], utterances: [], primes: 0, unlocked: false }
      const { events, utterances } = window.__voice
      const fetch = window.fetch
      window.fetch = (input, init) => {
        if (input === '/mobile/api/tts') {
          events.push('fetch')
          init.signal?.addEventListener('abort', () => events.push('abort'), { once: true })
        }
        return fetch(input, init)
      }
      const start = AudioBufferSourceNode.prototype.start
      AudioBufferSourceNode.prototype.start = function (...args) {
        events.push('start')
        this.addEventListener('ended', () => events.push('end'), { once: true })
        return start.apply(this, args)
      }
      Object.defineProperty(window, 'speechSynthesis', { value: {
        getVoices: () => [
          { name: 'Robotic', lang: 'en-US', localService: true },
          { name: 'Google', lang: 'en-US', localService: true },
          { name: 'Premium', lang: 'en-GB', localService: false },
          { name: 'Natural Premium', lang: 'fr-FR', localService: true },
        ],
        cancel: () => events.push('cancel'),
        speaking: false, pending: false,
        speak: utterance => {
          // Safari: speech starts only after one speak() made during a tap; before that, and with refuse set,
          // it drops the utterance and fires no event.
          if (navigator.userActivation.isActive) window.__voice.unlocked = true
          if (!utterance.text) { window.__voice.primes++; return }
          utterances.push({ text: utterance.text, voice: utterance.voice?.name, lang: utterance.lang })
          if (window.__voice.refuse || !window.__voice.unlocked) return
          queueMicrotask(() => { utterance.onstart?.(); utterance.onend?.() })
        },
      } })
      window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text } }
    })

    const actions = page.locator('.chat-page .assistant-message .message-actions').last()
    const read = actions.getByRole('button', { name: 'Read aloud' })
    const stop = actions.getByRole('button', { name: 'Stop reading' })
    const load = async (text = reply) => {
      fixture.chat.messages[1].text = text
      await page.goto(`${origin}/chat/?session=%251`)
      await read.waitFor()
      await page.evaluate(() => { window.__voice.events.length = 0 })
    }
    const voice = () => page.evaluate(() => window.__voice)
    const waitForFetches = count => page.waitForFunction(count => window.__voice.events.filter(event => event === 'fetch').length >= count, count, { timeout: 5000 })

    await load()
    const firstClip = hold()
    await read.click()
    await waitForFetches(1)
    await firstClip.wait()
    await stop.waitFor()
    check('Stop reading shows while the first clip is held', () => assert.equal(requests.length, 1))
    check('first request uses one short sentence and the TTS write headers', () => {
      const { text, method, headers } = requests[0]
      assert.equal(text, sentences[0])
      assert.ok(text.length <= 120)
      assert.equal(method, 'POST')
      assert.equal(headers['content-type'], 'application/json')
      assert.equal(headers['x-codex-mobile'], '1')
    })
    const secondClip = hold()
    firstClip.release()
    await secondClip.wait()
    await page.waitForFunction(() => window.__voice.events.includes('end'), null, { timeout: 5000 })
    check('prefetch stays one clip ahead while the second response is held', () => assert.equal(requests.length, 2))
    secondClip.release()
    await read.waitFor()
    const success = await voice()
    check('the second request starts during playback of the first clip', () => {
      assert.deepEqual(success.events.slice(0, 5), ['cancel', 'fetch', 'start', 'fetch', 'end'])
    })
    check('all four sentences are requested and played in order, within the size limits', () => {
      const parts = requests.map(request => request.text)
      assert.deepEqual(parts, [sentences[0], sentences.slice(1, 3).join(' '), sentences[3]])
      assert.ok(parts.every(part => part.length <= 600))
      assert.equal(success.events.filter(event => event === 'end').length, parts.length)
    })
    check('server playback uses no device utterance and resets the button on completion', () => {
      assert.deepEqual(success.utterances, [])
      assert.equal(success.events.at(-1), 'end')
    })

    await load()
    requests.length = 0
    const stoppedClip = hold()
    await read.click()
    await stop.waitFor()
    await stoppedClip.wait()
    const aborted = page.waitForEvent('requestfailed', { predicate: request => request.url().endsWith('/mobile/api/tts'), timeout: 5000 })
    await stop.click()
    await read.waitFor()
    await aborted
    stoppedClip.release()
    await page.waitForTimeout(300) // Give a stale clip enough time to start and end.
    const stopped = await voice()
    check('Stop aborts the held request and late audio cannot play or fetch another clip', () => {
      assert.equal(requests.length, 1)
      assert.deepEqual(stopped.events, ['cancel', 'fetch', 'abort', 'cancel'])
      assert.deepEqual(stopped.utterances, [])
    })

    const longReply = `${'The figure for this quarter includes all confirmed entries and adjustments '.repeat(10).trim()}. The review is complete.`
    await load(longReply)
    requests.length = 0
    failureAt = 2
    failure = 'network'
    await read.click()
    await stop.waitFor()
    await read.waitFor()
    const partial = await voice()
    check('a long opening sentence splits at a word and a later network failure reads only the unread text', () => {
      const first = requests[0].text
      assert.ok(first.length >= 100 && first.length <= 120)
      assert.equal(longReply[first.length], ' ')
      assert.equal([first, ...partial.utterances.map(utterance => utterance.text)].join(' '), longReply)
      assert.ok(partial.utterances.every(utterance => utterance.text.length <= 300))
      assert.equal(partial.events.filter(event => event === 'end').length, 1)
    })
    check('the tap unlocked the device voice once, so the fallback after the tap can speak', () => {
      assert.equal(partial.primes, 1)
    })

    failureAt = requests.length + 1
    failure = 503
    await read.click()
    await waitForFetches(3)
    await read.waitFor()
    const after503 = await voice()
    const fallback = after503.utterances.slice(partial.utterances.length)
    check('a network failure permits retry, and a 503 falls back to the best English voice', () => {
      assert.equal(requests.length, 3)
      assert.equal(fallback.map(utterance => utterance.text).join(' '), longReply)
      assert.ok(fallback.every(utterance => utterance.voice === 'Premium' && utterance.lang === 'en-GB'))
    })
    await read.click()
    await page.waitForFunction(count => window.__voice.utterances.length === count, after503.utterances.length + fallback.length, { timeout: 5000 })
    await read.waitFor()
    const repeated = await voice()
    check('a second tap after 503 uses the device voice with no more TTS requests', () => {
      assert.equal(requests.length, 3)
      assert.deepEqual(repeated.utterances.slice(after503.utterances.length), fallback)
    })

    // Safari refuses device speech that no tap started and fires no event: the button must not stay on Stop.
    await page.evaluate(() => { window.__voice.refuse = true })
    await read.click()
    await stop.waitFor()
    const refusedAt = Date.now()
    await read.waitFor({ timeout: 6000 })
    const refused = await voice()
    check('a refused device voice resets the button after a few seconds', () => {
      assert.ok(Date.now() - refusedAt < 4500)
      assert.equal(requests.length, 3)
      assert.equal(refused.utterances.length, repeated.utterances.length + 1)
    })
  } finally { await context.close(); server.close() }
  console.log(`read aloud: all ${passed} checks pass`)
  process.exit(0)
})().catch(error => { console.error('FAIL', error.stack); process.exit(1) })
