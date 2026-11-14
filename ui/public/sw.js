// Tether service worker: notifications and files from Android's Share sheet.
// The manifest share_target posts them to /chat/share; we keep them in a cache until the app attaches them.
const INBOX = 'tether-share-inbox'
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'POST' || url.pathname !== '/chat/share') return
  event.respondWith((async () => {
    const form = await event.request.formData()
    const cache = await caches.open(INBOX)
    let index = 0
    for (const file of form.getAll('media')) {
      if (!(file instanceof File)) continue
      const key = `/chat/share-inbox/${Date.now()}-${index++}`
      await cache.put(key, new Response(file, { headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Name': encodeURIComponent(file.name || 'shared') } }))
    }
    return Response.redirect('/chat/?shared=1', 303)
  })())
})

// One notification per pane, so each update alerts on its own and keeps its own tap, Approve/Deny or Urgent only.
// Android folds 4+ notifications of one app into a group whose tap opens only one pane, so a plain update also offers
// "All agents", which opens the Agents page. An older Tether folded updates into one summary (tag tether-summary);
// a new push closes that leftover.
const SUMMARY = 'tether-summary'
const ICONS = { icon: '/chat/icon-192.png', badge: '/chat/badge-96.png' }
self.addEventListener('push', event => {
  let payload = {}
  try { payload = event.data?.json() || {} } catch { /* still show a notification for an empty push */ }
  event.waitUntil(notify(payload))
})
async function notify(payload) {
  const title = payload.title || 'Tether', body = payload.body || ''
  for (const item of await self.registration.getNotifications({ tag: SUMMARY })) item.close()
  await self.registration.showNotification(title, { ...ICONS, body, tag: payload.tag, silent: payload.silent === true,
    data: { session: payload.session, thread: payload.thread, approval: payload.approval, body: payload.body, review: payload.review, go: payload.go },
    // A plain yes/no prompt gets Approve and Deny (the server picked it as safe to answer from here); others get Urgent only.
    actions: payload.approval ? [{ action: 'approve', title: 'Approve' }, { action: 'deny', title: 'Deny' }]
      : payload.review ? [{ action: 'resend', title: 'Ask again' }, { action: 'latest', title: 'Send this reply' }]
      : payload.session ? [{ action: 'mute', title: 'Urgent only' }, { action: 'agents', title: 'All agents' }] : [],
  })
}
// Answer the prompt by its id. The server checks the prompt on screen is still that one, so an old notification cannot
// approve a newer request. The result replaces the notification quietly; a refusal says why and opens the pane on tap.
async function answer(notification, approve) {
  const { session, thread, approval, body } = notification.data || {}
  let error = ''
  try {
    const response = await fetch('/mobile/api/approval', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ session, thread, approval, action: approve ? 'approve' : 'reject' }) })
    if (!response.ok) error = (await response.json().catch(() => ({}))).error || `Error ${response.status}`
  } catch { error = 'Could not reach the server.' }
  const title = error ? `${notification.title}: not ${approve ? 'approved' : 'denied'}` : `${notification.title}: ${approve ? 'Approved' : 'Denied'}`
  await self.registration.showNotification(title, { body: error ? `${error} Tap to open.` : (body || ''), tag: notification.tag, silent: !error,
    icon: '/chat/icon-192.png', badge: '/chat/badge-96.png', data: { session } })
  if (!error) {
    await new Promise(resolve => setTimeout(resolve, 4000))
    for (const shown of await self.registration.getNotifications({ tag: notification.tag })) if (shown.title === title) shown.close()
  }
}
// A stalled review: ask the reviewer again, or send its latest reply. The server checks the review id, so an old
// notification does nothing. A refusal says why and opens the worker tab on tap.
async function resumeReview(notification, action) {
  const { session, review } = notification.data || {}
  let error = ''
  try {
    const response = await fetch('/mobile/api/review-stalled', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ worker: review.worker, reviewer: review.reviewer, id: review.id, action }) })
    if (!response.ok) error = (await response.json().catch(() => ({}))).error || `Error ${response.status}`
  } catch { error = 'Could not reach the server.' }
  const done = action === 'resend' ? 'Asked the reviewer again' : 'Sent the reply'
  const title = error ? 'Review stalled: not done' : done
  await self.registration.showNotification(title, { body: error ? `${error} Tap to open.` : '', tag: notification.tag, silent: !error,
    icon: '/chat/icon-192.png', badge: '/chat/badge-96.png', data: { session } })
  if (!error) {
    await new Promise(resolve => setTimeout(resolve, 4000))
    for (const shown of await self.registration.getNotifications({ tag: notification.tag })) if (shown.title === title) shown.close()
  }
}
self.addEventListener('notificationclick', event => {
  event.notification.close()
  const session = event.notification.data?.session
  if ((event.action === 'resend' || event.action === 'latest') && event.notification.data?.review) {
    event.waitUntil(resumeReview(event.notification, event.action))
    return
  }
  if ((event.action === 'approve' || event.action === 'deny') && event.notification.data?.approval) {
    event.waitUntil(answer(event.notification, event.action === 'approve'))
    return
  }
  if (event.action === 'mute' && session) {
    // One tap from the notification: no window opens.
    event.waitUntil(fetch('/mobile/api/push/level', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' }, body: JSON.stringify({ session, level: 'urgent' }) }))
    return
  }
  const go = event.action === 'agents' ? 'agents' : event.notification.data?.go
  event.waitUntil(go === 'agents' || go === 'work' ? openPage(go) : openPane(session))
})
// "All agents" (or an old summary) opens the Agents page; a work item question opens Needs you. In the open Tether
// window when there is one, else in a new window.
async function openPage(go) {
  const chat = await lastWindow()
  if (chat) {
    chat.postMessage({ go })
    try { await chat.focus(); return } catch { /* fall through and open a window */ }
  }
  await self.clients.openWindow(`/chat/?go=${go}`)
}
async function lastWindow() {
  const windows = (await self.clients.matchAll({ type: 'window', includeUncontrolled: true })).filter(client => {
    const at = new URL(client.url)
    return at.origin === self.location.origin && at.pathname.startsWith('/chat/')
  })
  return windows.find(client => client.focused) ?? windows.find(client => client.visibilityState === 'visible') ?? windows[0]
}
// Open the pane from the notification. Prefer the Tether window the person last used: focused, then visible, then any.
// Send the pane first: focus() can be refused (another macOS Space, or Chrome in the background), and a refusal must not
// leave the window on the old pane. If no window takes focus, open a new one on the pane.
async function openPane(session) {
  const url = '/chat/' + (session ? `?session=${encodeURIComponent(session)}` : '')
  const chat = await lastWindow()
  if (chat) {
    if (session) chat.postMessage({ session })
    try { await chat.focus(); return } catch { /* fall through and open a window */ }
  }
  await self.clients.openWindow(url)
}
