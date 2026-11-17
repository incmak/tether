import { useSyncExternalStore } from 'react'

/* The name and icon this server gives its app (Settings › Appearance › Installed app). The sidebar shows them, so two
   Tether servers look different inside the app too. The server writes the saved name into index.html as
   <meta name="apple-mobile-web-app-title"> and serves /chat/icon.svg in the saved colour or picture
   (server/app_identity.py). A save in Settings calls brandChanged(), so the page follows without a reload. */

export const DEFAULT_NAME = 'Tether'
const ICON = '/chat/icon.svg'

type Brand = { name: string; icon: string }
const titled = () => document.querySelector<HTMLMetaElement>('meta[name="apple-mobile-web-app-title"]')?.content.trim()
let brand: Brand = { name: (typeof document !== 'undefined' && titled()) || DEFAULT_NAME, icon: ICON }
let saves = 0
const listeners = new Set<() => void>()

function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }

/** The app name and the address of its icon. */
export function useBrand() { return useSyncExternalStore(subscribe, () => brand, () => brand) }

/** After a save: show the new name, and load the icon again (the server sends it with no-cache). */
export function brandChanged(name: string) {
  const icon = `${ICON}?v=${++saves}`
  brand = { name: name.trim() || DEFAULT_NAME, icon }
  document.title = brand.name
  document.querySelector<HTMLLinkElement>('link[rel="icon"]')?.setAttribute('href', icon)
  listeners.forEach(listener => listener())
}
