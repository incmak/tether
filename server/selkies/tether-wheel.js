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
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || event.data?.tether !== 'wheel-mode') return
    relay = event.data.relay !== false
    if (event.data.smooth === true) (document.head || document.documentElement).appendChild(smooth)
    else smooth.remove()
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
