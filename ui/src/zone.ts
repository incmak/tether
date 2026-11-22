import { useSyncExternalStore } from 'react'

export const DEFAULT_ZONE = 'Asia/Kolkata'
let current = DEFAULT_ZONE
let currentConfirmed = false
const listeners = new Set<() => void>()
const formatters = new Map<string, Intl.DateTimeFormat | null>()

function formatter(zone: string, options: Intl.DateTimeFormatOptions, locale?: string) {
  const key = JSON.stringify([zone, locale, Object.entries(options).filter(([, value]) => value !== undefined).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)])
  let cached = formatters.get(key)
  if (cached === undefined) {
    try {
      cached = new Intl.DateTimeFormat(locale, { ...options, timeZone: zone })
    } catch (error) {
      if (!(error instanceof RangeError)) throw error
      cached = null
    }
    formatters.set(key, cached)
  }
  return cached
}

export const tetherZone = () => current
export const tetherZoneConfirmed = () => currentConfirmed
/** confirmed: the server answered with this zone; an old name applies when its renamed copy formats. */
export function setTetherZone(name: string, confirmed = false) {
  if (!name || (name === current && confirmed === currentConfirmed) || !(confirmed ? canFormat(name) : acceptsZone(name))) return
  current = name
  currentConfirmed = confirmed
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function useTetherZone() {
  return useSyncExternalStore(subscribe, tetherZone, tetherZone)
}

export function useTetherZoneConfirmed() {
  return useSyncExternalStore(subscribe, tetherZoneConfirmed, tetherZoneConfirmed)
}

const RENAMED: Record<string, string> = {
  'Asia/Calcutta': 'Asia/Kolkata', 'Asia/Katmandu': 'Asia/Kathmandu', 'Asia/Rangoon': 'Asia/Yangon', 'Asia/Saigon': 'Asia/Ho_Chi_Minh',
  'Europe/Kiev': 'Europe/Kyiv', 'Atlantic/Faeroe': 'Atlantic/Faroe', 'America/Godthab': 'America/Nuuk', 'Pacific/Enderbury': 'Pacific/Kanton',
  'America/Buenos_Aires': 'America/Argentina/Buenos_Aires', 'America/Indianapolis': 'America/Indiana/Indianapolis', 'America/Louisville': 'America/Kentucky/Louisville',
  'Pacific/Truk': 'Pacific/Chuuk', 'Pacific/Ponape': 'Pacific/Pohnpei', 'Asia/Ulan_Bator': 'Asia/Ulaanbaatar',
}

function acceptsZone(name: string) {
  return formatter(name, {}, 'en') !== null
}

/** This browser can show times in the zone, itself or by its renamed copy. */
export const canFormat = (name: string) => (!!RENAMED[name] && acceptsZone(RENAMED[name])) || acceptsZone(name)

export function pickZone(...names: (string | undefined)[]) {
  for (const name of names) {
    if (!name) continue
    const renamed = RENAMED[name]
    if (renamed && acceptsZone(renamed)) return renamed
    if (acceptsZone(name)) return name
  }
  return DEFAULT_ZONE
}

export function deviceZone() {
  try { const name = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; return RENAMED[name] ?? name } catch { return '' }
}

export function zoneList() {
  const intl: typeof Intl & { supportedValuesOf?: (key: 'timeZone') => string[] } = Intl
  // ICU (and so Chrome) still lists some old names, such as Asia/Calcutta; show the current IANA name instead.
  if (intl.supportedValuesOf) return [...new Set([...intl.supportedValuesOf('timeZone').map(name => RENAMED[name] ?? name), 'UTC', DEFAULT_ZONE])].sort()
  return [...new Set(['UTC', DEFAULT_ZONE, 'America/New_York', 'America/Los_Angeles', 'Europe/London', 'Europe/Paris', 'Asia/Tokyo', 'Australia/Sydney', current, deviceZone()].filter(Boolean))]
}

const DATE: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'numeric', day: 'numeric' }
const TIME: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: 'numeric', second: 'numeric' }
const DATE_TIME = { ...DATE, ...TIME }

export const validDateMs = (ms: number) => !Number.isNaN(new Date(ms).getTime())

function zoneFormatter(zone: string, options: Intl.DateTimeFormatOptions, locale?: string) {
  const renamed = RENAMED[zone]
  return (renamed ? formatter(renamed, options, locale) : null) ?? formatter(zone, options, locale)
}

function format(at: Date | number, options: Intl.DateTimeFormatOptions, zone = current) {
  const ms = typeof at === 'number' ? at : at.getTime()
  return (validDateMs(ms) ? zoneFormatter(zone, options)?.format(ms) : undefined) ?? 'Invalid Date'
}

export const fmtTime = (at: Date | number, options = TIME) => format(at, options)
export const fmtDate = (at: Date | number, options = DATE) => format(at, options)
export const fmtDateTime = (at: Date | number, options = DATE_TIME) => format(at, options)

export function dayParts(day: string | undefined) {
  if (typeof day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return
  const [year, month, date] = day.split('-').map(Number)
  const value = new Date(utcTime(year, month - 1, date, 0, 0))
  if (value.getUTCFullYear() === year && value.getUTCMonth() === month - 1 && value.getUTCDate() === date) {
    return { year, month: month - 1, date }
  }
}

export function fmtDay(day: string | undefined, options = DATE) {
  const parts = dayParts(day)
  return parts ? format(utcTime(parts.year, parts.month, parts.date, 0, 0), options, 'UTC') : 'Invalid Date'
}

function zoneName(zone: string, at: number, timeZoneName: 'short' | 'shortOffset') {
  if (!validDateMs(at)) return zone
  return zoneFormatter(zone, { timeZoneName }, 'en-US')?.formatToParts(at).find(part => part.type === 'timeZoneName')?.value ?? zone
}

export const offsetLabel = (zone: string, at = Date.now()) => zoneName(zone, at, 'shortOffset')
export const zoneLabel = (zone: string, at = Date.now()) => zoneName(zone, at, 'short')

const PARTS: Intl.DateTimeFormatOptions = {
  ...DATE, ...TIME, weekday: 'short', hourCycle: 'h23', calendar: 'gregory', numberingSystem: 'latn',
}
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function fieldsAt(ms: number, zone: string): Record<string, string> {
  // A bad timestamp gives NaN parts, as Date math did, instead of a RangeError that stops the whole page.
  if (!validDateMs(ms)) return {}
  const parts = zoneFormatter(zone, PARTS, 'en-US')?.formatToParts(ms) ?? []
  return Object.fromEntries(parts.map(({ type, value }) => [type, value]))
}

export function zoneParts(ms: number, zone = current) {
  const fields = fieldsAt(ms, zone)
  return {
    year: Number(fields.year), month: Number(fields.month) - 1, date: Number(fields.day),
    weekday: fields.weekday === undefined ? NaN : WEEKDAYS.indexOf(fields.weekday),
    hour: Number(fields.hour), minute: Number(fields.minute),
  }
}

export function dayKey(ms: number, zone = current) {
  const { year, month, date } = zoneParts(ms, zone)
  const two = (n: number) => String(n).padStart(2, '0')
  return `${String(year).padStart(4, '0')}-${two(month + 1)}-${two(date)}`
}

export const sameDay = (a: number, b: number, zone = current) => dayKey(a, zone) === dayKey(b, zone)

function utcTime(year: number, month: number, date: number, hour: number, minute: number, second = 0) {
  if (year < 0 || year >= 100) return Date.UTC(year, month, date, hour, minute, second)
  // Date.UTC treats years 0–99 as 1900–1999.
  const value = new Date(0)
  value.setUTCFullYear(year, month, date)
  value.setUTCHours(hour, minute, second)
  return value.getTime()
}

function offsetMs(zone: string, at: number) {
  const { year, month, day, hour, minute, second } = fieldsAt(at, zone)
  return utcTime(+year, +month - 1, +day, +hour, +minute, +second) - Math.floor(at / 1000) * 1000
}

export const offsetMinutes = (zone: string, at = Date.now()) => offsetMs(zone, at) / 60000

/** The instants for a wall time, earliest first. In a gap, return the time shifted forward. */
export function zonedTimes(year: number, month: number, date: number, hour: number, minute: number, zone = current) {
  const wall = utcTime(year, month, date, hour, minute)
  const day = 86400000
  // Include both sides of a nearby offset transition. Iteration alone can miss one side of a fold.
  const offsets = new Set([offsetMs(zone, wall - day), offsetMs(zone, wall + day)])
  let instant = wall
  for (let i = 0; i < 2; i++) {
    const offset = offsetMs(zone, instant)
    offsets.add(offset)
    instant = wall - offset
  }
  const matches: number[] = []
  let after = Infinity
  let gap = Infinity
  for (const offset of offsets) {
    const candidate = wall - offset
    const shift = offsetMs(zone, candidate) - offset
    if (shift === 0) matches.push(candidate)
    if (shift > 0 && shift < gap) { gap = shift; after = candidate }
  }
  return matches.length ? matches.sort((a, b) => a - b) : [after]
}

export function zonedTime(year: number, month: number, date: number, hour: number, minute: number, zone = current) {
  return zonedTimes(year, month, date, hour, minute, zone)[0]
}

export function zonedTimeLatest(year: number, month: number, date: number, hour: number, minute: number, zone = current) {
  const times = zonedTimes(year, month, date, hour, minute, zone)
  return times[times.length - 1]
}

export function startOfDay(ms: number, zone = current) {
  const { year, month, date } = zoneParts(ms, zone)
  return zonedTime(year, month, date, 0, 0, zone)
}
