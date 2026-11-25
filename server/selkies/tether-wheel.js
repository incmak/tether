// Tether: wheel and trackpad scrolling for the panel's live view.
// Selkies turns each wheel event into whole X11 wheel clicks of about 120 px, so a Mac trackpad, which sends many
// small events, scrolled about 12 times too far. This listener runs first (capture on the window) and hands each turn
// to the Tether panel that frames this page. The panel sends it to Chromium over DevTools with the exact pixels.
// Pinch zoom (ctrlKey) stays with Selkies. The message carries no secrets, so '*' is a safe target; the panel
// accepts it only from this frame and this origin.
(function () {
  if (window.parent === window) return
  var relay = true
  // Selkies sets image-rendering: pixelated inline on its canvas. When the panel scales this frame down, that drops
  // pixel columns from thin text, so the panel asks for smooth sampling; !important wins over the inline value.
  var smooth = document.createElement('style')
  smooth.textContent = 'canvas, video { image-rendering: auto !important; }'
  var pictures = {
    sharp: { video_fullcolor: true, framerate: 60, video_bitrate: 8000 },
    standard: { video_fullcolor: false, framerate: 60, video_bitrate: 8000 },
    light: { video_fullcolor: false, framerate: 30, video_bitrate: 3000 },
  }
  // The pinned Selkies client saves these keys on a settings message. If they survive a page load, its
  // handshake can restore a stale preset to the shared display. Use the same primary-display key prefix.
  try {
    var prefix = (location.origin + location.pathname).replace(/[^a-zA-Z0-9._-]/g, '_')
    Object.keys(pictures.sharp).forEach(function (name) {
      localStorage.removeItem(prefix + '_' + name)
      localStorage.removeItem(prefix + '_' + name + '_pick')
    })
  } catch { /* Storage can be disabled. */ }
  var pictureReady = false
  var pendingPicture = null
  var picture
  var panelOrigin
  var observedTransport = null
  var streamMessages = null
  var streamSize = ''
  function applyPicture(settings) {
    window.postMessage({ type: 'settings', settings: settings }, location.origin)
  }
  function postPicture() {
    if (panelOrigin && picture !== undefined) window.parent.postMessage({ tether: 'picture-state', preset: picture }, panelOrigin)
  }
  function reportPicture(fullcolor, fps, bitrate) {
    picture = Object.keys(pictures).find(function (id) {
      var settings = pictures[id]
      return settings.video_fullcolor === fullcolor && settings.framerate === fps && settings.video_bitrate === bitrate
    }) ?? null
    postPicture()
  }
  function reportServerPicture(settings) {
    // The server_settings snapshot stores booleans in value and current range settings in default.
    reportPicture(settings?.video_fullcolor?.value, settings?.framerate?.default, settings?.video_bitrate?.default)
  }
  function readStreamMessage(data) {
    try {
      var update = JSON.parse(data)
      if (update?.type === 'server_settings') {
        reportServerPicture(update.settings)
        return
      }
      if (update?.type === 'display_settings' && update.displayId === 'primary') {
        var settings = update.settings
        reportPicture(settings?.video_fullcolor, settings?.framerate, settings?.video_bitrate)
        return
      }
      // Selkies reports the realized output size on its transport. A local canvas resize can happen earlier.
      if (update?.type !== 'stream_resolution' || update.displayId !== 'primary'
          || !Number.isInteger(update.width) || update.width <= 0 || !Number.isInteger(update.height) || update.height <= 0) return
      var size = update.width + 'x' + update.height
      if (size === streamSize) return
      streamSize = size
      window.parent.postMessage({ tether: 'live-resize', width: update.width, height: update.height }, '*')
    } catch { /* Ignore malformed stream messages. */ }
  }
  function onStreamMessage(event) {
    var data = event.data
    // The pinned transport prefixes gzip control messages with byte 5. Large server_settings use this path.
    var gzip = data instanceof ArrayBuffer && data.byteLength > 1 && new Uint8Array(data, 0, 1)[0] === 5
    if (!gzip && (typeof data !== 'string' || !/"(?:stream_resolution|server_settings|display_settings)"/.test(data))) return
    if (!gzip && !streamMessages) {
      readStreamMessage(data)
      return
    }
    var transport = observedTransport
    // Keep a later text report behind an earlier compressed report. Drop queued work from a replaced socket.
    var pending = (streamMessages ?? Promise.resolve()).then(async function () {
      if (transport !== observedTransport) return
      if (gzip) {
        var stream = new Blob([data.slice(1)]).stream().pipeThrough(new DecompressionStream('gzip'))
        data = await new Response(stream).text()
      }
      if (transport === observedTransport) readStreamMessage(data)
    }).catch(function () { /* Ignore malformed compressed control messages. */ })
    streamMessages = pending
    void pending.then(function () { if (streamMessages === pending) streamMessages = null })
  }
  window.addEventListener('message', function (event) {
    var message = event.data
    // The iframe can load before Selkies installs its settings receiver. Its first settings announce means
    // the receiver and socket are ready. Later announces may be responses to picture changes.
    if (event.source === window && event.origin === location.origin && message?.type === 'serverSettings') {
      var transport = window.selkiesTransport
      if (!observedTransport || transport !== observedTransport) {
        observedTransport?.removeEventListener('message', onStreamMessage)
        observedTransport = typeof transport?.addEventListener === 'function' ? transport : null
        streamMessages = null
        streamSize = ''
        observedTransport?.addEventListener('message', onStreamMessage)
        // Once subscribed, use the transport reports in order. display_settings can make Selkies
        // re-post its old serverSettings snapshot, so the window message can be stale.
        reportServerPicture(message.payload)
      }
      if (!pictureReady) {
        pictureReady = true
        if (pendingPicture) applyPicture(pendingPicture)
        pendingPicture = null
      }
      return
    }
    if (event.source !== window.parent) return
    if (message?.tether === 'wheel-mode') {
      // The live iframe has its own port and no referrer. The parent's setup message supplies its origin.
      if (event.origin !== 'null' && panelOrigin !== event.origin) {
        panelOrigin = event.origin
        postPicture()
      }
      relay = message.relay !== false
      if (message.smooth === true) (document.head || document.documentElement).appendChild(smooth)
      else smooth.remove()
    }
    if (message?.tether === 'picture' && typeof message.preset === 'string' && Object.prototype.hasOwnProperty.call(pictures, message.preset)) {
      // Selkies persists these values and resends them when its socket reconnects.
      if (pictureReady) applyPicture(pictures[message.preset])
      else pendingPicture = pictures[message.preset]
    }
  })
  window.addEventListener('wheel', function (event) {
    if (!relay || event.ctrlKey) return
    event.preventDefault()
    event.stopImmediatePropagation()
    var step = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1
    window.parent.postMessage({
      tether: 'wheel',
      x: event.clientX / Math.max(1, window.innerWidth),
      y: event.clientY / Math.max(1, window.innerHeight),
      dx: event.deltaX * step,
      dy: event.deltaY * step,
      alt: event.altKey, meta: event.metaKey, shift: event.shiftKey,
    }, '*')
  }, { capture: true, passive: false })
})()
