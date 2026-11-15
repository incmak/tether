// The "Open Tether search" command: pick the Tether tab to show, or the page to open.
// Pure functions, so they can be tested without Chrome (ui/test_extension_search.cjs).

/** The paired origin, checked as connect() checks it. null when it is missing or not a bare HTTPS origin. */
export function pairedOrigin(origin) {
  if (typeof origin !== 'string' || !origin) return null;
  let u;
  try { u = new URL(origin); } catch { return null; }
  if (u.protocol !== 'https:' || u.origin !== origin || u.username || u.password) return null;
  return u.origin;
}

/** A tab of the Tether chat UI: the exact paired origin and a path under /chat/. */
export function isTetherTab(tab, origin) {
  if (typeof tab?.url !== 'string') return false;
  let u;
  try { u = new URL(tab.url); } catch { return false; }
  return u.origin === origin && u.pathname.startsWith('/chat/');
}

/**
 * What the command does. tabs: chrome.tabs.query results. skip: the tab under a debugger grant, never touched.
 * Returns {tab, windowId, url} to focus that tab (url null: it already asks for search), or {create} for a new tab.
 * Only the hash of the tab's own address changes: the page opens search and removes the hash (ui/src/backNav.ts).
 */
export function searchTarget(origin, tabs, skip) {
  const ok = pairedOrigin(origin);
  if (!ok) return null;
  const found = tabs.filter(tab => Number.isInteger(tab.id) && tab.id !== skip && isTetherTab(tab, ok));
  if (!found.length) return {create: `${ok}/chat/?go=search`};
  const tab = found.reduce((best, next) => (next.lastAccessed ?? 0) > (best.lastAccessed ?? 0) ? next : best);
  const u = new URL(tab.url);
  // Already #search: the page has not handled it yet. Navigating to the same address would replace that entry.
  const url = u.hash === '#search' ? null : (u.hash = 'search', u.href);
  return {tab: tab.id, windowId: tab.windowId, url};
}
