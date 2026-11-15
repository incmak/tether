const $ = id => document.getElementById(id);
const saved = await chrome.storage.local.get(['origin', 'confirm']);
$('origin').value = saved.origin || '';
$('confirm').checked = saved.confirm === true;
document.querySelector('form').onsubmit = async e => {
  e.preventDefault();
  try {
    const u = new URL($('origin').value.trim());
    if (u.protocol !== 'https:' || u.username || u.password || u.search || u.hash || u.pathname !== '/')
      throw Error('Enter an HTTPS origin without a path.');
    // Must run in this user gesture, before fetch or runtime messaging.
    if (!await chrome.permissions.request({origins: [u.origin + '/*']}))
      throw Error('Allow access to your Tether address to pair.');
    const result = await chrome.runtime.sendMessage({type: 'pair', origin: u.origin,
      code: $('code').value.trim().toUpperCase(), confirm: $('confirm').checked});
    $('code').value = '';
    $('status').textContent = result.error ? result.message : 'Paired. Chrome will reconnect automatically.';
  } catch (error) { $('status').textContent = error.message; }
};
$('save').onclick = async () => {
  await chrome.storage.local.set({confirm: $('confirm').checked});
  $('status').textContent = 'Confirmation setting saved.';
};
// Chrome does not promise the suggested key: another extension can hold it. Show what is set now.
const [search] = (await chrome.commands.getAll()).filter(item => item.name === 'open-search');
$('key').textContent = search?.shortcut
  ? `${search.shortcut} shows your Tether tab and opens its search, from any app.`
  : 'No key is set. Choose one to show your Tether tab and open its search from any app.';
$('keys').onclick = () => chrome.tabs.create({url: 'chrome://extensions/shortcuts'});
setInterval(async () => {
  const s = await chrome.runtime.sendMessage({type: 'state'});
  if (s.connected) $('status').textContent = s.grant ? 'Connected. One task tab is active.' : 'Connected. Allow each task in Tether.';
}, 2000);
