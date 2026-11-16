import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ChevronLeft, ChevronRight, Ellipsis, FolderOpen, X, ZoomIn, ZoomOut } from 'lucide-react'
import { useModal } from './hooks'
import { holdThemeColor } from './schemes'
import './ImageViewer.css'
import { wheelSideways } from './wheelSideways'

export type Media = { name: string; url: string; kind?: 'image' | 'video' | 'audio'; error?: string; downloadUrl?: string; thumbnailUrl?: string }

// One viewer for chat images, attachments, Files and Share. Navigation stays in the caller's group.
export function ImageViewer({ image, close, onShow, onDetails, items = [], onSelect }: {
  image: Media | null; close: () => void; onShow?: () => void; onDetails?: () => void; items?: Media[]; onSelect?: (image: Media) => void
}) {
  const { ref, onCancel } = useModal(!!image, close)
  // The installed app draws its window controls in the theme color: match the dark surface while it shows.
  const showing = !!image
  useEffect(() => showing ? holdThemeColor('#0b0c0e') : undefined, [showing])
  const index = image ? items.findIndex(item => item.url === image.url) : -1
  const grouped = index >= 0 && items.length > 1 && !!onSelect
  const move = (delta: number) => { const next = items[index + delta]; if (grouped && next) onSelect?.(next) }
  return <dialog ref={ref} onCancel={onCancel} className="image-viewer" aria-label={image?.name ?? 'Media viewer'}
    onTouchStart={event => event.stopPropagation()} onTouchMove={event => event.stopPropagation()}
    onTouchEnd={event => event.stopPropagation()} onTouchCancel={event => event.stopPropagation()}
    onKeyDown={event => {
      if (event.altKey || event.ctrlKey || event.metaKey || (event.target as HTMLElement).closest('video, audio, .viewer-stage.zoomed')) return
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); event.stopPropagation(); move(event.key === 'ArrowLeft' ? -1 : 1) }
    }}>{image && <ViewerContent image={image} close={close} onShow={onShow} onDetails={onDetails}
      move={move} index={grouped ? index : 0} total={grouped ? items.length : 1} items={grouped ? items : []} onSelect={onSelect} />}
  </dialog>
}

function ViewerContent({ image, close, onShow, onDetails, move, index, total, items, onSelect }: {
  image: Media; close: () => void; onShow?: () => void; onDetails?: () => void
  move: (delta: number) => void; index: number; total: number; items: Media[]; onSelect?: (image: Media) => void
}) {
  const [zoom, setZoom] = useState({ url: image.url, value: false })
  const zoomed = zoom.url === image.url && zoom.value
  const [failure, setFailure] = useState('')
  const failed = failure === image.url
  const [dimensions, setDimensions] = useState({ url: image.url, width: 0, height: 0 })
  const natural = dimensions.url === image.url ? dimensions : { width: 0, height: 0 }
  const [size, setSize] = useState({ width: 0, height: 0 })
  const stage = useRef<HTMLDivElement>(null)
  const touch = useRef<{ x: number; y: number; time: number; lastX: number; lastY: number; lastAt: number; speed: number; axis: 'wait' | 'x' | 'y' } | null>(null)
  const lastTap = useRef<{ x: number; y: number; time: number } | null>(null)
  const track = useRef<HTMLDivElement>(null)
  const background = useRef<HTMLDivElement>(null)
  const motion = useRef<{ timer?: ReturnType<typeof setTimeout>; frame?: number; selected?: string }>({})
  const selection = useRef({ url: image.url, index })
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
  const stopMotion = () => {
    clearTimeout(motion.current.timer)
    if (motion.current.frame) cancelAnimationFrame(motion.current.frame)
    motion.current.timer = undefined; motion.current.frame = undefined
  }
  const translate = (x: number, duration = 0, y = 0) => {
    const node = track.current
    if (!node) return
    // Neighbor slides stay hidden while the photo shrinks and snaps back.
    if (y && !node.dataset.dismiss) node.dataset.dismiss = 'true'
    else if (!y && (x || !duration)) delete node.dataset.dismiss
    const progress = Math.min(1, Math.max(0, y / (stage.current?.clientHeight || 1)))
    const curve = `${duration}ms cubic-bezier(.32,.72,0,1)`
    node.style.transition = duration ? `transform ${curve}, opacity ${curve}` : 'none'
    node.style.transform = `translate3d(${x}px,${y}px,0) scale(${1 - progress * .15})`
    node.style.opacity = `${1 - progress}`
    if (background.current) {
      background.current.style.transition = duration ? `opacity ${curve}` : 'none'
      background.current.style.opacity = `${1 - progress}`
    }
  }
  const horizontalX = (x: number) => {
    if (!items[index + (x < 0 ? 1 : -1)]) return x * .25
    return Math.abs(x) > size.width ? Math.sign(x) * (size.width + (Math.abs(x) - size.width) * .25) : x
  }
  const finishSwipe = (dx: number, speed: number) => {
    const delta = dx < 0 ? 1 : -1
    const next = items[index + delta]
    const fling = !reduced() && Math.abs(speed) > .35 && Math.sign(speed) === Math.sign(dx)
    if (!next || !(Math.abs(dx) > size.width * .3 || fling)) { translate(0, reduced() ? 0 : 220); return }
    if (reduced()) { move(delta); return }
    const remaining = Math.abs(Math.sign(dx) * size.width - horizontalX(dx))
    const duration = Math.round(Math.min(320, Math.max(140, remaining / Math.max(Math.abs(speed), 1.2))))
    motion.current.selected = next.url
    translate(Math.sign(dx) * size.width, duration)
    motion.current.timer = setTimeout(() => { motion.current.timer = undefined; move(delta) }, duration)
  }
  const finishDown = (dy: number, speed: number) => {
    const height = stage.current?.clientHeight || 1
    if (!(dy > height * .25 || !reduced() && speed > .5)) { translate(0, reduced() ? 0 : 220); return }
    if (reduced()) { close(); return }
    const duration = Math.round(Math.min(320, Math.max(140, Math.abs(height - dy) / Math.max(Math.abs(speed), 1.2))))
    translate(0, duration, Math.max(height, dy))
    motion.current.timer = setTimeout(() => { motion.current.timer = undefined; close() }, duration)
  }
  useLayoutEffect(() => {
    const previous = selection.current
    selection.current = { url: image.url, index }
    stopMotion()
    if (motion.current.selected === image.url || previous.url === image.url || Math.abs(index - previous.index) !== 1 || reduced()) translate(0)
    else {
      translate(Math.sign(index - previous.index) * (stage.current?.clientWidth ?? 0))
      motion.current.frame = requestAnimationFrame(() => { motion.current.frame = requestAnimationFrame(() => translate(0, 220)) })
    }
    motion.current.selected = undefined
    // The animation reads the committed URL and index; stage size comes from its live box.
  }, [image.url, index])
  useLayoutEffect(() => () => stopMotion(), [])
  const measure = useCallback((natural: { width: number; height: number }) => setDimensions({ url: image.url, ...natural }), [image.url])
  const origin = useRef({ x: .5, y: .5 })
  if (zoom.url !== image.url) { setZoom({ url: image.url, value: false }); setFailure('') }
  const kind = image.kind ?? 'image'
  useLayoutEffect(() => { touch.current = null; lastTap.current = null }, [image.url])
  useLayoutEffect(() => {
    const box = stage.current
    if (!box) return
    const observer = new ResizeObserver(() => setSize({ width: box.clientWidth, height: box.clientHeight }))
    observer.observe(box)
    return () => observer.disconnect()
  }, [])
  const fit = natural.width && size.width ? Math.min(size.width / natural.width, size.height / natural.height, 1) : 0
  const width = natural.width * fit * (zoomed ? 2.5 : 1)
  const height = natural.height * fit * (zoomed ? 2.5 : 1)
  useLayoutEffect(() => {
    const box = stage.current
    if (box) {
      box.scrollLeft = zoomed ? Math.max(0, origin.current.x * width - box.clientWidth / 2) : 0
      box.scrollTop = zoomed ? Math.max(0, origin.current.y * height - box.clientHeight / 2) : 0
    }
  }, [zoomed, width, height])
  useLayoutEffect(() => {
    const box = stage.current
    if (!box) return
    const move = (event: TouchEvent) => {
      if (!zoomed && touch.current && touch.current.axis !== 'wait' && event.touches.length === 1 && event.cancelable) event.preventDefault()
    }
    box.addEventListener('touchmove', move, { passive: false })
    return () => box.removeEventListener('touchmove', move)
  }, [zoomed])
  const toggle = (x?: number, y?: number) => {
    stopMotion(); translate(0); motion.current.selected = undefined
    const img = stage.current?.querySelector('[data-current="true"] img')
    if (img && x !== undefined && y !== undefined) {
      const rect = img.getBoundingClientRect()
      origin.current = { x: Math.max(0, Math.min(1, (x - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (y - rect.top) / rect.height)) }
    } else origin.current = { x: .5, y: .5 }
    setZoom({ url: image.url, value: !zoomed })
  }
  return <>
    <div ref={background} className="viewer-background" aria-hidden="true" />
    <header className="viewer-head">
      <button type="button" className="viewer-button viewer-close" onClick={close} aria-label="Close viewer" title="Close viewer" autoFocus><X size={21} strokeWidth={1.8} /></button>
      <div className="viewer-title"><p className="viewer-name" title={image.name}>{image.name.split('/').pop()}</p>
        {total > 1 && <span className="viewer-count" aria-live="polite">{index + 1} / {total}</span>}
      </div>
      <div className="viewer-actions" role="toolbar" aria-label="Media actions">
        {kind === 'image' && !image.error && !failed && <button type="button" className="viewer-button" onClick={() => toggle()} aria-label={zoomed ? 'Zoom out' : 'Zoom in'} aria-pressed={zoomed} title={zoomed ? 'Zoom out' : 'Zoom in'}>{zoomed ? <ZoomOut size={21} strokeWidth={1.8} /> : <ZoomIn size={21} strokeWidth={1.8} />}</button>}
        {!image.error && <a className="viewer-button" href={image.downloadUrl ?? image.url} download={image.name.split('/').pop()} aria-label="Download" title="Download"><ArrowDownToLine size={21} strokeWidth={1.8} /></a>}
        {onShow && <button type="button" className="viewer-button" onClick={onShow} aria-label="Show in Files" title="Show in Files"><FolderOpen size={21} strokeWidth={1.8} /></button>}
        {onDetails && <button type="button" className="viewer-button" onClick={onDetails} aria-label="File details and actions" title="File details and actions"><Ellipsis size={21} /></button>}
      </div>
    </header>
    <div ref={stage} className={`viewer-stage${kind === 'image' ? ' viewer-photo' : ''}${zoomed ? ' zoomed' : ''}`} tabIndex={zoomed ? 0 : undefined} aria-label={zoomed ? 'Zoomed image. Scroll to pan.' : undefined}
      onTouchStart={event => {
        const matrix = track.current ? new DOMMatrix(getComputedStyle(track.current).transform) : new DOMMatrix()
        stopMotion(); motion.current.selected = undefined
        const finger = event.touches[0]
        if (kind !== 'image' || event.touches.length !== 1) { touch.current = null; lastTap.current = null; translate(0); return }
        const x = zoomed ? 0 : matrix.m41, y = zoomed ? 0 : matrix.m42
        const rawX = !items[index + (x < 0 ? 1 : -1)] ? x * 4
          : Math.abs(x) > size.width ? Math.sign(x) * (size.width + (Math.abs(x) - size.width) * 4) : x
        translate(x, 0, y)
        touch.current = { x: finger.clientX - rawX, y: finger.clientY - (y < 0 ? y * 4 : y), time: Date.now(), lastX: finger.clientX, lastY: finger.clientY, lastAt: performance.now(), speed: 0, axis: Math.abs(y) > .5 ? 'y' : Math.abs(x) > .5 ? 'x' : 'wait' }
      }}
      onTouchMove={event => {
        const start = touch.current
        if (!start || zoomed || event.touches.length !== 1) return
        const finger = event.touches[0], dx = finger.clientX - start.x, dy = finger.clientY - start.y
        if (start.axis === 'wait') {
          if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return
          if (Math.abs(dy) > Math.abs(dx) * 1.2) start.axis = 'y'
          else if (Math.abs(dx) > Math.abs(dy) * 1.5) start.axis = 'x'
          else return
          lastTap.current = null
        }
        const now = performance.now()
        if (now > start.lastAt) start.speed = start.speed * .4 + ((start.axis === 'x' ? finger.clientX - start.lastX : finger.clientY - start.lastY) / (now - start.lastAt)) * .6
        start.lastX = finger.clientX; start.lastY = finger.clientY; start.lastAt = now
        if (start.axis === 'y') translate(0, 0, dy < 0 ? dy * .25 : dy)
        else translate(horizontalX(dx))
      }}
      onTouchCancel={() => { touch.current = null; lastTap.current = null; stopMotion(); translate(0, reduced() ? 0 : 220) }}
      onTouchEnd={event => {
        const start = touch.current; touch.current = null
        if (!start || event.touches.length) return
        const end = event.changedTouches[0], dx = end.clientX - start.x, dy = end.clientY - start.y
        if (!zoomed && start.axis !== 'wait') {
          lastTap.current = null
          const speed = performance.now() - start.lastAt > 100 ? 0 : start.speed
          if (start.axis === 'y') finishDown(dy, speed)
          else finishSwipe(dx, speed)
          return
        }
        if (kind !== 'image' || Math.abs(dx) > 12 || Math.abs(dy) > 12 || Date.now() - start.time > 300) { lastTap.current = null; return }
        const previous = lastTap.current
        if (previous && Date.now() - previous.time < 350 && Math.hypot(end.clientX - previous.x, end.clientY - previous.y) < 30) {
          event.preventDefault(); lastTap.current = null; toggle(end.clientX, end.clientY)
        } else lastTap.current = { x: end.clientX, y: end.clientY, time: Date.now() }
      }}>
      {image.error || failed ? <p className="viewer-error" role="alert" tabIndex={0}>{image.error || 'This media could not load.'}</p>
        : kind === 'video' ? <video src={image.url} controls autoPlay playsInline onError={() => setFailure(image.url)} />
        : kind === 'audio' ? <audio src={image.url} controls autoPlay onError={() => setFailure(image.url)} />
        : <div ref={track} className="viewer-track" style={{ width: zoomed ? Math.max(size.width, width) : size.width, height: zoomed ? Math.max(size.height, height) : size.height }}>
          {(items.length ? items.slice(Math.max(0, index - 1), index + 2) : [image]).filter(item => !zoomed || item.url === image.url).map(item => {
            const current = item.url === image.url
            const offset = current ? 0 : items.findIndex(candidate => candidate.url === item.url) - index
            return <div className="viewer-slide" key={item.url} data-current={current ? 'true' : undefined} aria-hidden={!current} style={{ transform: `translateX(${offset * 100}%)` }}>
              <GalleryImage image={item} size={size} zoomed={current && zoomed} active={current} onMeasure={measure}
                onError={() => { if (current) setFailure(image.url) }} onDoubleClick={event => { if (current) toggle(event.clientX, event.clientY) }} />
            </div>
          })}
        </div>}

    </div>
    {total > 1 && <footer className="viewer-footer">
      <nav className="viewer-filmstrip" aria-label="Media navigation">
        <button type="button" className="viewer-button viewer-previous" onClick={() => move(-1)} disabled={index === 0} aria-label="Previous image" title="Previous image"><ChevronLeft size={22} strokeWidth={1.8} /></button>
        <div ref={wheelSideways} className="viewer-thumbnails">{items.slice(Math.max(0, Math.min(index - 2, items.length - 5)), Math.max(5, index + 3)).map(item => <button
          type="button" className="viewer-thumbnail" key={item.url} aria-label={`View ${item.name}`} aria-current={item.url === image.url ? 'true' : undefined}
          title={item.name} onClick={() => onSelect?.(item)}><img src={item.thumbnailUrl ?? item.url} alt="" loading="lazy" draggable={false} /></button>)}</div>
        <button type="button" className="viewer-button viewer-next" onClick={() => move(1)} disabled={index === total - 1} aria-label="Next image" title="Next image"><ChevronRight size={22} strokeWidth={1.8} /></button>
      </nav>
    </footer>}
  </>
}

// Each URL owns a DOM image and fitted dimensions. Neighbors decode before they enter the viewport.
function GalleryImage({ image, size, zoomed, active, onMeasure, onError, onDoubleClick }: {
  image: Media; size: { width: number; height: number }; zoomed: boolean; active: boolean
  onMeasure: (size: { width: number; height: number }) => void; onError: () => void; onDoubleClick: (event: React.MouseEvent<HTMLImageElement>) => void
}) {
  const [natural, setNatural] = useState({ width: 0, height: 0 })
  const [failed, setFailed] = useState(false)
  useLayoutEffect(() => { if (active && failed) onError() }, [active, failed, onError])
  const fit = natural.width ? Math.min(size.width / natural.width, size.height / natural.height, 1) * (zoomed ? 2.5 : 1) : 0
  useLayoutEffect(() => { if (active && natural.width) onMeasure(natural) }, [active, natural, onMeasure])
  return <>
    {!natural.width && active && <span className="viewer-loading" role="status" aria-label="Loading image" />}
    <img src={image.url} alt={active ? image.name : ''} draggable={false} decoding="async"
      style={{ width: natural.width * fit, height: natural.height * fit, visibility: natural.width ? 'visible' : 'hidden' }}
      onLoad={event => setNatural({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
      onError={() => setFailed(true)} onDoubleClick={onDoubleClick} />
  </>
}
