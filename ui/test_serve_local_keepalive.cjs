// serveLocal must not reuse a keep-alive socket to the test's local server (B285).
// Playwright fetches routed requests through one keep-alive agent shared by every context. Node's http server
// closes an idle socket after keepAliveTimeout (5 s). A request sent on that socket as the server closes it fails
// with ECONNRESET while the page is still open. This server makes that race certain: it destroys any socket that
// carries a second request.
const assert = require('node:assert/strict')
const http = require('node:http')
const test = require('node:test')
const { getBrowser, serveLocal } = require('./browser_test_helper.cjs')

const pages = {
  // A script, then a fetch after it loads: the second request goes out once the first socket is idle.
  '/chat/': '<!doctype html><script src="/chat/a.js"></script><script>fetch("/chat/b.js").then(r => r.text()).then(t => { document.title = t })</script>',
  // The real failures were Chrome prefetches of app chunks.
  '/chat/prefetch': '<!doctype html><link rel="prefetch" href="/chat/c.js"><link rel="prefetch" href="/chat/d.js"><script src="/chat/a.js"></script>',
}

test('serveLocal survives a server that closes reused sockets', async t => {
  const served = [], connectionHeaders = []
  const server = http.createServer((request, response) => {
    const socket = request.socket
    socket.requests = (socket.requests ?? 0) + 1
    if (socket.requests > 1) return socket.destroy()
    connectionHeaders.push(request.headers.connection)
    served.push(request.url)
    if (pages[request.url]) return response.writeHead(200, { 'Content-Type': 'text/html' }).end(pages[request.url])
    response.writeHead(200, { 'Content-Type': 'text/javascript' }).end(request.url === '/chat/b.js' ? 'loaded' : '')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections() }))
  const origin = `http://127.0.0.1:${server.address().port}`
  const browser = await getBrowser()
  t.after(() => browser.close())

  for (const [name, path, expected] of [['script then fetch', '/chat/', ['/chat/', '/chat/a.js', '/chat/b.js']],
    ['prefetch', '/chat/prefetch', ['/chat/prefetch', '/chat/a.js', '/chat/c.js', '/chat/d.js']]]) {
    await t.test(name, async () => {
      served.length = 0
      connectionHeaders.length = 0
      const context = await browser.newContext({ serviceWorkers: 'block' })
      const failures = []
      await context.route('**/*', route => serveLocal(route).catch(error => { failures.push(error.message); return route.abort() }))
      try {
        const page = await context.newPage()
        await page.goto(origin + path)
        if (path === '/chat/') await page.waitForFunction(() => document.title === 'loaded')
        else await page.waitForFunction(count => performance.getEntriesByType('resource').filter(entry => /\/[cd]\.js$/.test(entry.name)).length === count, 2)
        assert.deepEqual(failures, [], 'serveLocal fetches must not fail')
        assert.deepEqual([...served].sort(), [...expected].sort())
        assert(connectionHeaders.every(value => value === 'close'), `each fetch closes its socket: ${connectionHeaders}`)
      } finally { await context.close() }
    })
  }
})
