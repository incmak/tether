import { useEffect, useRef, useState } from 'react'
import type { Api } from './api'
import { listBackgrounds, type BackgroundItem } from './backgrounds'
import { DEFAULT_LOOK, PHOTO_PLACEHOLDER, PHOTO_URL, type Look } from './layouts'
import type { Prefs } from './prefs'

/** Waits before each new try while the photo or its small copies are not there yet. */
const RETRY_MS = [4000, 15000, 60000]

type Update = (change: (current: Prefs) => Partial<Prefs>) => void

/** The photo's mean colour (weighted to its saturated parts) and whether it is light, from a 32×18 copy. */
async function photoPalette(url: string): Promise<{ tint: string; light: boolean }> {
  const image = new Image()
  image.src = url
  await image.decode()
  const canvas = document.createElement('canvas')
  canvas.width = 32; canvas.height = 18
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) throw new Error('No canvas')
  context.drawImage(image, 0, 0, 32, 18)
  const data = context.getImageData(0, 0, 32, 18).data
  let r = 0, g = 0, b = 0, weight = 0, light = 0
  for (let at = 0; at < data.length; at += 4) {
    const red = data[at], green = data[at + 1], blue = data[at + 2]
    const w = 0.08 + (Math.max(red, green, blue) - Math.min(red, green, blue)) / 255
    r += red * w; g += green * w; b += blue * w; weight += w
    light += (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
  }
  const hex = (value: number) => Math.round(value / weight).toString(16).padStart(2, '0')
  return { tint: `#${hex(r)}${hex(g)}${hex(b)}`, light: light / (data.length / 4) > 0.55 }
}
const darkPage = () => getComputedStyle(document.documentElement).colorScheme.includes('dark') || matchMedia('(prefers-color-scheme: dark)').matches && document.documentElement.dataset.theme !== 'light'

/** A photo brings a matching tint and a fuller panel fill when its brightness differs from the page. */
export async function applyPhoto(item: BackgroundItem, update: Update): Promise<void> {
  if (!PHOTO_URL.test(item.url)) return
  const set = (patch: Partial<Look>) => update(state => ({ look: { ...state.look ?? DEFAULT_LOOK, ...patch } }))
  const placeholder = item.placeholder && PHOTO_PLACEHOLDER.test(item.placeholder) ? item.placeholder : undefined
  set({ backdrop: 'photo', photo: { id: item.id, url: item.url, ...placeholder ? { placeholder } : {} } })
  try {
    // The placeholder needs no network; its mean colour is close enough for the tint.
    const { tint, light } = await photoPalette(placeholder ?? item.thumb ?? item.url)
    set({ tint, fill: light === darkPage() ? 0.8 : 0.6 })
  } catch { /* The photo still shows; the tint stays as it was. */ }
}

/** The photo for the Glass stage. The full photo shows only after it has loaded and decoded, so a slow link never
 *  draws it from top to bottom. Until then the tiny placeholder shows, also while a dropped link retries. A look saved before placeholders existed (or
 *  before the server made small copies) is brought up to date once from the backgrounds list. */
export function usePhotoStage(api: Api, photo: Look['photo'] | undefined, update: Update): { '--g-photo'?: string; '--g-photo-ph'?: string } {
  const url = photo && PHOTO_URL.test(photo.url) ? photo.url : undefined
  const placeholder = photo?.placeholder && PHOTO_PLACEHOLDER.test(photo.placeholder) ? photo.placeholder : undefined
  const [ready, setReady] = useState<string>()
  useEffect(() => {
    if (!url) return
    let live = true, timer: number | undefined, tries = 0, attempt = 0
    const load = () => {
      // A new try (from the timer or the 'online' event) makes any older try stale.
      window.clearTimeout(timer)
      const mine = ++attempt
      const image = new Image()
      image.src = url
      image.decode().then(() => { if (live && mine === attempt) setReady(url) }, () => {
        if (!live || mine !== attempt) return
        // Loaded but not decodable here: the browser can still paint it, and it is complete, so it never draws in rows.
        if (image.complete && image.naturalWidth > 0) { setReady(url); return }
        // Not loaded (often a dropped link): keep the placeholder and try again later, or when the link returns.
        if (tries < RETRY_MS.length) timer = window.setTimeout(load, RETRY_MS[tries++])
      })
    }
    load()
    window.addEventListener('online', load)
    return () => { live = false; window.clearTimeout(timer); window.removeEventListener('online', load) }
  }, [url])
  const checked = useRef<string | undefined>(undefined)
  const id = photo?.id
  useEffect(() => {
    if (!id || !url || placeholder || checked.current === id) return
    checked.current = id
    let live = true, timer: number | undefined, tries = 0
    // The server makes the small copies in the background after it starts, so an early answer can lack them.
    const later = () => { if (live && tries < RETRY_MS.length) timer = window.setTimeout(check, RETRY_MS[tries++]) }
    const check = () => listBackgrounds(api).then(items => {
      if (!live) return
      const item = items.find(entry => entry.id === id)
      if (!item) return
      if (!PHOTO_URL.test(item.url) || !item.placeholder || !PHOTO_PLACEHOLDER.test(item.placeholder)) { later(); return }
      const fresh = { id, url: item.url, placeholder: item.placeholder }
      update(state => ({
        ...state.look?.photo?.id === id ? { look: { ...state.look, photo: fresh } } : {},
        ...state.myLook?.photo?.id === id ? { myLook: { ...state.myLook, photo: fresh } } : {},
      }))
    }, later)
    void check()
    return () => { live = false; window.clearTimeout(timer); if (checked.current === id) checked.current = undefined }
  }, [api, id, url, placeholder, update])
  return {
    ...url && ready === url ? { '--g-photo': `url("${url}")` } : {},
    ...placeholder ? { '--g-photo-ph': `url("${placeholder}")` } : {},
  }
}
