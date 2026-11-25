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
  var pictureReady = false
  var pendingPicture = null
  var observedTransport = null
  var streamSize = ''
  function applyPicture(settings) {
    window.postMessage({ type: 'settings', settings: settings }, location.origin)
  }
  function onStreamResolution(event) {
    // Selkies reports the realized output size on its transport. A local canvas resize can happen earlier.
    if (typeof event.data !== 'string' || !event.data.includes('"stream_resolution"')) return
    try {
      var update = JSON.parse(event.data)
      if (update?.type !== 'stream_resolution' || update.displayId !== 'primary'
          || !Number.isInteger(update.width) || update.width <= 0 || !Number.isInteger(update.height) || update.height <= 0) return
      var size = update.width + 'x' + update.height
      if (size === streamSize) return
      streamSize = size
      window.parent.postMessage({ tether: 'live-resize', width: update.width, height: update.height }, '*')
    } catch { /* Ignore malformed stream messages. */ }
  }
  window.addEventListener('message', function (event) {
    var message = event.data
    // The iframe can load before Selkies installs its settings receiver. Its first settings announce means
    // the receiver and socket are ready. Later announces may be responses to picture changes.
    if (event.source === window && event.origin === location.origin && message?.type === 'serverSettings') {
      var transport = window.selkiesTransport
      if (typeof transport?.addEventListener === 'function' && transport !== observedTransport) {
        observedTransport?.removeEventListener('message', onStreamResolution)
        observedTransport = transport
        streamSize = ''
        transport.addEventListener('message', onStreamResolution)
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
