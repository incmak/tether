# Tether browser extension

Load this folder as an unpacked Manifest V3 extension in Chrome 118 or newer.
Open its options. Enter your Tether HTTPS origin and a fresh code from Tether's browser settings.
Accept Chrome's request for access to that Tether origin. Pin the extension for its Stop control.

Pairing survives Chrome restarts. The server stores only a hash of the device token.
Revoke the device in Tether to remove its access. Each task needs a separate Allow decision in Tether.
The optional **Also confirm on this Mac** setting adds a local confirmation window.

The extension opens a new task tab. It attaches the debugger to that tabId only.
The toolbar badge shows **STOP**. Click it, cancel Chrome's debugger banner, or close the task tab to stop.
Tether records whether you canceled the debugger or Chrome closed the connection to the tab.
If Chrome supplies no recognized reason, Tether reports that the debugging connection ended.
Expiry and connection loss also stop control. Reconnect never restores a task grant.
Stop leaves the task tab open for you, unless you close it yourself.

Page commands follow the exact list in `policy.mjs` and `server/mac_browser.py`.
Cookie CDP APIs, file-input uploads, browser-wide commands, and target switches are refused.
Page JavaScript still has the page's normal site access. An approved task can perform site actions.
Popups, workers, and separate iframe targets do not join the grant. Extra agent-created tabs are refused.
A site may open a popup, but the extension does not attach its debugger to that popup.

The server must expose `/mobile/api/browser/extension/connect` with WebSocket Upgrade support.
Only `connect` and `exchange` use device authentication instead of Tether browser sign-in.
The nginx example under `server/deploy/examples/` shows those exact routes.
Keep all pairing-code creation, device lists, revocation, and Allow routes behind normal Tether sign-in.

## Open Tether search from any app

The command **Open Tether search** shows your Tether tab and opens its search menu, also when Chrome is in the background.
Its suggested key is ⌘⇧9 on a Mac (Ctrl+Shift+9 elsewhere). Chrome allows only Ctrl+Shift plus a digit as a suggested global key.
To use ⌥Space, as inside Tether:

1. Open `chrome://extensions/shortcuts`, or click **Change the key** in the extension options.
2. Find **Tether browser** › **Open Tether search** and click the pencil.
3. Press ⌥Space.
4. Set the scope to **Global**.

If Raycast or another app already uses ⌥Space, that app keeps it. Pick another key, or free it in that app first.
⌘Space stays with Spotlight.

The command uses no new permission. It finds the tab at your paired origin under `/chat/`, focuses its window, and adds `#search` to its address.
The page opens search and removes `#search` again. It adds no Back step and closes no open dialog.
If no Tether tab is open, it opens `/chat/?go=search`. It never touches the tab under a debugger task.
Web pages cannot send this command. Only the key press starts it.

No remote script, content script, local relay, or native helper is used.
No host permission is required at install time. Pairing requests only the chosen HTTPS origin.
The extension sends page events and images to that Tether server while a grant is active.
It does not send browsing history or inspect other tabs through the debugger.
