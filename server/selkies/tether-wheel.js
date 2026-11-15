// Tether: wheel and trackpad scrolling for the panel's live view.
// Selkies turns each wheel event into whole X11 wheel clicks of about 120 px, so a Mac trackpad, which sends many
// small events, scrolled about 12 times too far. This listener runs first (capture on the window) and hands each turn
// to the Tether panel that frames this page. The panel sends it to Chromium over DevTools with the exact pixels.
// Pinch zoom (ctrlKey) stays with Selkies. The message carries no secrets, so '*' is a safe target; the panel
// accepts it only from this frame and this origin.
(function () {
  if (window.parent === window) return
  window.addEventListener('wheel', function (event) {
    if (event.ctrlKey) return
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
