"""Paired outbound Chrome transport and one-tab signed grants. No local Chrome process."""
import base64
from collections import deque
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import browser
import browser_cdp as cdp
import paths
import signed_access as signed

PREFIX = '/mobile/api/browser/extension/'
ORIGIN = re.compile(r'chrome-extension://[a-p]{32}')
CHUNK_CHARS = 1_000_000
MAX_REPLY = 64 * 1024 * 1024
# A chunk may give up its last UTF-16 code unit to keep a surrogate pair together.
MAX_CHUNKS = (MAX_REPLY + CHUNK_CHARS - 2) // (CHUNK_CHARS - 1)
MAX_ASSEMBLY = MAX_REPLY + 1024 * 1024
PAGE_ALLOWED = (signed.PAGE_ALLOWED - {'Network.getCookies', 'DOM.setFileInputFiles'}) | {
    'Page.startScreencast', 'Page.stopScreencast', 'Page.screencastFrameAck'}
# Passive event domains share the extension's single debugger attachment. Fetch has
# command-specific interception settings and must still reach Chrome unchanged.
SHARED_DOMAINS = {'Runtime', 'Page', 'DOM', 'Network', 'Log', 'Performance', 'Accessibility'}


def check_command(method, params):
    if method not in PAGE_ALLOWED or not isinstance(params, dict):
        raise browser.BrowserError('Not allowed through your Chrome access.')
    # A debugger session can only address the approved tab. Never forward routing selectors.
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('targetId', 'browserContextId', 'sessionId') and not (
                        method == 'Page.screencastFrameAck' and key == 'sessionId' and value is params):
                    raise browser.BrowserError('Outside this Chrome tab.')
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(params)
    if method == 'Page.navigate':
        browser.normalize_url(params.get('url'))


class Broker:
    def __init__(self, store=None, clock=time.time):
        self.store = store or paths.CONFIG / 'browser-devices.json'
        self.clock = clock
        self.lock = threading.RLock()
        self.codes, self.connections = {}, {}
        self.devices = None  # Lazy: importing the server does not touch real configuration.
        self.browser = MacBrowser(self)
        self.access = None

    def load(self):
        if self.devices is None:
            try:
                self.devices = json.loads(self.store.read_text())
                if not isinstance(self.devices, dict):
                    raise ValueError('Invalid device store.')
                self.store.chmod(0o600)
            except FileNotFoundError:
                self.devices = {}
        return self.devices

    def pair(self):
        with self.lock:
            now = self.clock()
            self.codes = {code: expiry for code, expiry in self.codes.items() if expiry > now}
            if len(self.codes) >= 16:
                raise ValueError('Too many pairing codes. Wait five minutes.')
            code = secrets.token_hex(8).upper()
            self.codes[code] = now + 300
            return {'code': code, 'expires': now + 300}

    def exchange(self, code, origin, name):
        if not isinstance(origin, str) or not ORIGIN.fullmatch(origin):
            raise ValueError('Invalid extension Origin.')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Device name must contain 1 to 80 characters.')
        with self.lock:
            if not isinstance(code, str) or self.codes.pop(code, 0) <= self.clock():
                raise ValueError('Pairing code expired or already used.')
            devices = self.load()
            if len(devices) >= 16:
                raise ValueError('Revoke an old browser first.')
            ident, token = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
            devices[ident] = {'id': ident, 'name': name.strip(), 'origin': origin,
                              'hash': hashlib.sha256(token.encode()).hexdigest(), 'lastSeen': self.clock(), 'chrome': ''}
            cdp.save_config(self.store, devices)
            return {'id': ident, 'token': token}

    def authenticate(self, token, origin):
        if not isinstance(token, str) or len(token) != 43 or not isinstance(origin, str) or not ORIGIN.fullmatch(origin):
            raise ValueError('Invalid device credentials.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = next((row for row in self.load().values() if row['origin'] == origin
                        and hmac.compare_digest(row['hash'], digest)), None)
            if row is None:
                raise ValueError('Invalid device credentials.')
            return row['id']

    def rows(self):
        with self.lock:
            return [{'id': row['id'], 'name': row['name'], 'connected': row['id'] in self.connections,
                     'lastSeen': row['lastSeen'], 'chrome': row['chrome']} for row in self.load().values()]

    def revoke(self, ident):
        with self.lock:
            if not isinstance(ident, str) or ident not in self.load():
                raise ValueError('Choose a paired browser.')
            self.devices.pop(ident)
            cdp.save_config(self.store, self.devices)
            connection = self.connections.get(ident)
        if connection:
            connection.close()
        return {'devices': self.rows()}

    def connected(self):
        with self.lock:
            return next((conn for conn in reversed(list(self.connections.values())) if not conn.closed), None)

    def register(self, ident, transport, chrome):
        with self.lock:
            if ident not in self.load():
                raise ValueError('Device revoked.')
            old = self.connections.get(ident)
        if old:
            old.close()
        with self.lock:
            if ident not in self.load():
                raise ValueError('Device revoked.')
            conn = Device(self, ident, transport)
            self.connections[ident] = conn
            self.devices[ident].update(lastSeen=self.clock(), chrome=str(chrome)[:200])
            cdp.save_config(self.store, self.devices)
            return conn

    def disconnected(self, conn):
        with self.lock:
            if self.connections.get(conn.ident) is conn:
                self.connections.pop(conn.ident)
            if conn.ident in self.load():
                self.devices[conn.ident]['lastSeen'] = self.clock()
        if self.access:
            self.access.disconnect(conn)

    def stop(self):
        for conn in list(self.connections.values()):
            conn.close()
        if self.access:
            self.access.stop()


class Device:
    def __init__(self, broker, ident, transport):
        self.broker, self.ident, self.transport = broker, ident, transport
        self.epoch = secrets.token_urlsafe(16)
        self.lock = threading.RLock()
        self.waiters = {}
        self.assemblies = {}
        self.assembly_bytes = 0
        self.ended = set()
        self.closed = False

    def send(self, message):
        if self.closed or self.transport.closed:
            raise browser.BrowserError('Your Chrome disconnected. Ask again.')
        self.transport.message(dict(message, epoch=self.epoch))

    def request(self, message, timeout=15):
        ident = message.get('id') or secrets.token_urlsafe(16)
        now = time.monotonic()
        event = threading.Event()
        slot = {'event': event, 'reply': None, 'grant': message.get('grant', ident), 'timeout': timeout,
                'deadline': now + timeout, 'hard_deadline': now + max(timeout, 120)}
        with self.lock:
            if len(self.waiters) >= 128:
                raise browser.BrowserError('Too many Chrome commands.')
            self.waiters[ident] = slot
        try:
            self.send(dict(message, id=ident))
            while not event.is_set():
                with self.lock:
                    left = slot['deadline'] - time.monotonic()
                    if event.is_set():
                        break
                if left <= 0:
                    # Uncertain delivery must never leave authority available for a retry.
                    self.close()
                    raise browser.BrowserError('Chrome command timed out. Ask again.')
                event.wait(left)
            reply = slot['reply']
            if reply and reply.get('type') == 'error' and reply.get('code') == 'TOO_LARGE':
                raise browser.BrowserError(str(reply.get('message') or 'Chrome reply is too large.')[:200])
            if not reply or reply.get('type') in ('error', 'ended'):
                raise browser.BrowserError('Chrome command refused or grant ended.')
            return reply
        finally:
            with self.lock:
                self.waiters.pop(ident, None)
                self._drop_assembly(ident)

    def _drop_assembly(self, ident):
        assembly = self.assemblies.pop(ident, None)
        if assembly:
            self.assembly_bytes -= assembly['bytes']

    def _chunk(self, message):
        # Called with Device.lock held, including delivery of the completed reply.
        ident = message.get('id')
        if not isinstance(ident, str):
            raise ValueError('Invalid chunk id.')
        slot = self.waiters.get(ident)
        now = time.monotonic()
        if not slot or slot['event'].is_set() or now >= slot['deadline']:
            return None
        seq, total, data = message.get('seq'), message.get('total'), message.get('data')
        if (type(seq) is not int or type(total) is not int or not 1 < total <= MAX_CHUNKS
                or not isinstance(data, str) or len(data) > CHUNK_CHARS):
            raise ValueError('Invalid reply chunk.')
        assembly = self.assemblies.get(ident)
        if assembly is None:
            assembly = {'total': total, 'parts': [], 'bytes': 0}
        if total != assembly['total'] or seq != len(assembly['parts']):
            raise ValueError('Out-of-order reply chunk.')
        size = len(data.encode('utf-8'))
        if self.assembly_bytes + size > MAX_ASSEMBLY:
            raise ValueError('Reply assembly exceeds the byte limit.')
        self.assemblies[ident] = assembly
        assembly['parts'].append(data)
        assembly['bytes'] += size
        self.assembly_bytes += size
        slot['deadline'] = min(slot['hard_deadline'], max(slot['deadline'], now + slot['timeout']))
        if seq + 1 < total:
            return None
        reply = json.loads(''.join(assembly['parts']))
        self._drop_assembly(ident)
        if (not isinstance(reply, dict) or reply.get('type') != 'result'
                or reply.get('id') != ident or reply.get('epoch') != self.epoch):
            raise ValueError('Invalid assembled reply.')
        return reply

    def cancel_grant(self, ident):
        with self.lock:
            self.ended.add(ident)
            for slot in self.waiters.values():
                if slot['grant'] == ident:
                    slot['reply'] = {'type': 'ended'}
                    slot['event'].set()

    def handle(self, message):
        if not isinstance(message, dict) or message.get('epoch') != self.epoch:
            raise ValueError('Invalid connection epoch.')
        kind = message.get('type')
        if kind == 'ping':
            with self.broker.lock:
                if self.ident in self.broker.load():
                    self.broker.devices[self.ident]['lastSeen'] = self.broker.clock()
            self.send({'type': 'pong'})
        elif kind in ('result', 'granted', 'error', 'ended', 'chunk'):
            with self.lock:
                if kind == 'chunk':
                    message = self._chunk(message)
                    if message is None:
                        return
                if kind == 'ended':
                    self.cancel_grant(message.get('id'))
                slot = self.waiters.get(message.get('id'))
                if slot:
                    slot['reply'] = message
                    slot['event'].set()
            if kind == 'ended' and self.broker.access:
                self.broker.access.extension_end(self, message.get('id'), message.get('reason'))
        elif kind == 'event':
            self.broker.browser.conn.event(self, message)
        elif kind == 'tab':
            target = self.broker.browser.native_targets.get(message.get('tab'))
            tab = self.broker.browser.tabs.get(target)
            grant = tab.agent_client if tab else None
            if grant and grant.device is self and message.get('grant') == grant.row['id']:
                tab.url, tab.title = str(message.get('url', tab.url))[:8192], str(message.get('title', tab.title))[:1000]
                self.broker.browser._bump()
        else:
            raise ValueError('Invalid extension message.')

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.assemblies.clear()
            self.assembly_bytes = 0
            for slot in self.waiters.values():
                slot['event'].set()
        self.transport.abort()
        self.broker.disconnected(self)


class Connection:
    """Synthesize browser targets; all real commands address one extension-created tabId."""
    closed = False

    def __init__(self, owner):
        self.owner = owner
        self.sessions, self.sinks = {}, {}
        self.lock = threading.RLock()
        self.domain_lock = threading.RLock()  # Never held by the extension event reader.
        self.enabled, self.contexts, self.runtime_started = {}, {}, set()
        self.log_entries, self.log_started = {}, set()

    def register_sink(self, session, sink):
        with self.lock:
            self.sinks[session] = sink

    def unregister_sink(self, session):
        with self.lock:
            self.sinks.pop(session, None)

    def detach(self, session):
        with self.domain_lock, self.lock:
            self.sessions.pop(session, None)
            self.sinks.pop(session, None)
            self.enabled.pop(session, None)

    def domain_command(self, method, params, session, target, request):
        domain, action = method.split('.', 1)
        # Keep Runtime warm until grant removal, even between clients. Otherwise
        # navigation while nobody subscribes could leave a stale context cache.
        with self.domain_lock:
            with self.lock:
                enabled = self.enabled.setdefault(session, set())
                if domain in ('Runtime', 'Log') and action == 'enable' and domain in enabled:
                    return {}
                shared = any(domain in domains and self.sessions.get(other) == target
                             for other, domains in self.enabled.items() if other != session)
                started = target in (self.runtime_started if domain == 'Runtime' else self.log_started)
            if action == 'disable':
                # Keep Runtime and Log on in Chrome: a real Log.disable then enable makes
                # Chrome replay its own history on top of the relay cache.
                result = {} if domain in ('Runtime', 'Log') or shared else request()
                with self.lock:
                    enabled.discard(domain)
                return result
            local = domain in ('Runtime', 'Log') and started
            result = {} if local else request()
            with self.lock:
                enabled.add(domain)
                if domain == 'Runtime':
                    self.runtime_started.add(target)
                    sink = self.sinks.get(session)
                    # Replay and live delivery use the same lock. A destruction or
                    # creation event cannot overtake this snapshot, or appear twice.
                    if sink:
                        for context in self.contexts.get(target, {}).values():
                            sink({'method': 'Runtime.executionContextCreated',
                                  'params': {'context': context}, 'sessionId': session})
                elif domain == 'Log':
                    self.log_started.add(target)
                    sink = self.sinks.get(session)
                    if sink:
                        for entry in self.log_entries.get(target, ()):
                            sink({'method': 'Log.entryAdded', 'params': entry, 'sessionId': session})
            return result

    def info(self, target):
        tab = self.owner.tab(target)
        return {'targetId': target, 'type': 'page', 'title': tab.title, 'url': tab.url,
                'attached': True, 'browserContextId': 'signed-default', 'canAccessOpener': False}

    def call(self, method, params=None, session=None, timeout=15):
        params = params or {}
        if method == 'Browser.getVersion':
            conn = self.owner.broker.connected()
            chrome = self.owner.broker.devices.get(conn.ident, {}).get('chrome', '') if conn else ''
            product = re.search(r'(?:Chrome|Chromium)/[0-9.]+', chrome)
            return {'protocolVersion': '1.3', 'product': product[0] if product else 'Chrome/118.0.0.0', 'revision': '',
                    'userAgent': chrome or 'Chrome/118.0.0.0', 'jsVersion': ''}
        if method == 'Target.getTargetInfo':
            return {'targetInfo': self.info(params.get('targetId') or self.sessions.get(session))}
        if method == 'Target.attachToTarget':
            self.owner.tab(params.get('targetId'))
            ident = secrets.token_urlsafe(12)
            self.sessions[ident] = params['targetId']
            return {'sessionId': ident}
        if method == 'Target.detachFromTarget':
            self.detach(params.get('sessionId'))
            return {}
        if method in ('Target.closeTarget', 'Target.activateTarget'):
            target = params.get('targetId')
            method = 'Page.close' if method == 'Target.closeTarget' else 'Page.bringToFront'
        else:
            target = self.sessions.get(session)
        tab = self.owner.tab(target)
        if method in ('Page.close', 'Page.bringToFront'):
            params = {}
        check_command(method, params)
        grant = tab.agent_client
        if (not grant or not self.owner.agent_proxy.valid(grant) or grant.device.closed
                or grant.row['id'] in grant.device.ended):
            raise browser.BrowserError('That Chrome grant has ended.')
        if method in ('Page.close', 'Page.bringToFront'):
            params = {}
        def request():
            reply = grant.device.request({'type': 'command', 'grant': grant.row['id'], 'tab': grant.tab_id,
                                          'method': method, 'params': params}, timeout)
            return reply.get('result') or {}
        domain, _, action = method.partition('.')
        if domain in SHARED_DOMAINS and action in ('enable', 'disable'):
            return self.domain_command(method, params, session, target, request)
        if method == 'Log.clear':
            with self.domain_lock:
                with self.lock:
                    self.log_entries.pop(target, None)
                return request()
        if method == 'Page.setLifecycleEventsEnabled':
            with self.domain_lock:
                with self.lock:
                    enabled = self.enabled.setdefault(session, set())
                    shared = any('lifecycle' in domains and self.sessions.get(other) == target
                                 for other, domains in self.enabled.items() if other != session)
                result = {} if not params.get('enabled') and shared else request()
                with self.lock:
                    if params.get('enabled'):
                        enabled.add('lifecycle')
                    else:
                        enabled.discard('lifecycle')
                return result
        return request()

    def send(self, method, params=None, session=None):
        if method == 'Target.detachFromTarget':
            self.detach((params or {}).get('sessionId'))
            return
        # Only housekeeping uses fire-and-forget; no thread or command queue is needed for ACKs.
        target = self.sessions.get(session)
        tab = self.owner.tabs.get(target)
        grant = tab.agent_client if tab else None
        if grant and self.owner.agent_proxy.valid(grant):
            check_command(method, params or {})
            try:
                grant.device.send({'type': 'command', 'id': secrets.token_urlsafe(16), 'grant': grant.row['id'],
                                   'tab': grant.tab_id, 'method': method, 'params': params or {}})
            except browser.BrowserError:
                pass

    def event(self, device, message):
        target = self.owner.native_targets.get(message.get('tab'))
        tab = self.owner.tabs.get(target)
        grant = tab.agent_client if tab else None
        if (not grant or grant.device is not device or message.get('grant') != grant.row['id']
                or not self.owner.agent_proxy.valid(grant)):
            return
        method, params = message.get('method'), message.get('params')
        if not isinstance(method, str) or not isinstance(params, dict) or method.startswith(('Target.', 'Browser.')):
            return
        # Existing p18 path acknowledges on receipt and replaces the single newest-frame slot.
        self.owner._event(method, params, tab.session)
        with self.lock:
            if self.owner.tabs.get(target) is not tab or not self.owner.agent_proxy.valid(grant):
                return  # Grant removal can race the reader after the initial identity check.
            contexts = self.contexts.setdefault(target, {})
            if method == 'Runtime.executionContextCreated':
                context = params.get('context') or {}
                if type(context.get('id')) is int:
                    contexts[context['id']] = context
            elif method == 'Runtime.executionContextDestroyed':
                contexts.pop(params.get('executionContextId'), None)
                unique = params.get('executionContextUniqueId')
                if unique:
                    for ident, context in list(contexts.items()):
                        if context.get('uniqueId') == unique:
                            contexts.pop(ident)
            elif method == 'Runtime.executionContextsCleared':
                contexts.clear()
            elif method == 'Log.entryAdded':
                self.log_entries.setdefault(target, deque(maxlen=100)).append(params)
            sinks = [(session, sink) for session, sink in self.sinks.items() if self.sessions.get(session) == target]
            for session, sink in sinks:
                domain = method.split('.', 1)[0]
                if domain in ('Runtime', 'Log') and domain not in self.enabled.get(session, set()):
                    continue
                if method != 'Page.screencastFrame':  # panel owns the cast and its acknowledgments
                    sink({'method': method, 'params': params, 'sessionId': session})


class MacBrowser(browser.Browser):
    def __init__(self, broker):
        super().__init__(signed=True)
        self.broker = broker
        self.conn = Connection(self)
        self.native_targets = {}

    def running(self):
        return self.broker.connected() is not None

    def _bump(self):
        super()._bump()
        if hasattr(self, 'peer_browser'):
            self.peer_browser._bump()

    def ensure(self):
        if not self.running():
            raise browser.BrowserError('Your Chrome is disconnected.')

    def list_tabs(self):
        return self.state()  # never create a tab outside an Allow decision

    def refresh(self, tab):
        """Read real metadata through the granted page; Target.getTargetInfo is synthetic on the relay."""
        try:
            if 'Page' not in self.conn.enabled.get(tab.session, set()):
                self.conn.call('Page.enable', session=tab.session, timeout=5)
            history = self.conn.call('Page.getNavigationHistory', session=tab.session, timeout=5)
            entries, index = history.get('entries') or [], history.get('currentIndex', -1)
        except Exception:  # metadata only; a failed read must never end the grant
            return
        if not 0 <= index < len(entries) or self.tabs.get(tab.target) is not tab:
            return
        entry = entries[index]
        url, title = entry.get('url') or tab.url, entry.get('title') or ''
        if (tab.url, tab.title) != (url, title):
            tab.url, tab.title = url, title
            self._bump()

    def new_tab(self, *args, **kwargs):
        raise ValueError('Ask for your Chrome access to open a tab.')

    def close_tab(self, target):
        self.conn.call('Target.closeTarget', {'targetId': target})
        return self.state()

    def stop(self):
        self.broker.stop()

    def remove(self, target):
        with self.lock:
            tab = self.tabs.pop(target, None)
            if tab:
                self.native_targets = {native: known for native, known in self.native_targets.items() if known != target}
                self.by_session.pop(tab.session, None)
                self.order.remove(target)
                for session, attached in list(self.conn.sessions.items()):
                    if attached == target:
                        self.conn.detach(session)
                with self.conn.lock:
                    self.conn.contexts.pop(target, None)
                    self.conn.log_entries.pop(target, None)
                    self.conn.log_started.discard(target)
                    self.conn.runtime_started.discard(target)
                with tab.changed:
                    tab.changed.notify_all()
                self._bump()


class MacAccess(signed.Access):
    browser_id = 'chrome'
    page_allowed = PAGE_ALLOWED - {'Page.startScreencast', 'Page.stopScreencast', 'Page.screencastFrameAck'}

    def __init__(self, broker, **kwargs):
        super().__init__(broker.browser, **kwargs)
        self.broker = broker
        broker.access = self

    def ask(self, reason, pane, duration=15, url=None):
        if not self.broker.connected():
            raise ValueError('Your Chrome is disconnected. Ask again or choose server.')
        return super().ask(reason, pane, duration, url)

    def request_binding(self):
        device = self.broker.connected()
        if not device:
            raise ValueError('Your Chrome is disconnected. Ask again.')
        return device

    def allow(self, ident, duration=None):
        with self.browser.lock:
            with self.lock:
                slot = self._request(ident)
                duration = slot['row']['minutes'] if duration is None else signed.minutes(duration)
                if duration > slot['row']['minutes']:
                    raise ValueError('Allow may shorten the requested time only.')
                if self.grants or self.reserved:
                    raise signed.GrantLimit('End the active Chrome grant first.')
                device = slot['binding']
                if device.closed:
                    raise browser.BrowserError('Your Chrome disconnected. Ask again.')
                self.reserved.add(ident)
            try:
                self._listen()
                reply = device.request({'type': 'grant', 'id': ident, 'reason': slot['row']['reason'],
                                        'minutes': duration, 'url': slot['row']['url']}, timeout=65)
                if (type(reply.get('tab')) is not int or not isinstance(reply.get('target'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', reply['target'])):
                    raise browser.BrowserError('Invalid Chrome tab.')
                target = reply['target']  # Playwright requires root target id == the main frame id.
                grant = signed.Grant(slot['row'], target, duration, self.clock(), auto=slot['auto'])
                grant.row['expires'] = min(grant.row['expires'], reply.get('expires', 0) / 1000)
                grant.device, grant.tab_id = device, reply['tab']
                with self.lock, self.history.lock:
                    if (slot['event'].is_set() or time.monotonic() >= slot['deadline'] or self.stopped or device.closed
                            or ident in device.ended or grant.row['expires'] <= self.clock()):
                        raise browser.BrowserError('That Chrome request has ended.')
                    self._check_hold(slot)
                    session = secrets.token_urlsafe(12)
                    tab = browser.Tab(target, session, mode='agent')
                    tab.url = slot['row']['url'] or tab.url
                    self.browser.tabs[target] = self.browser.by_session[session] = tab
                    self.browser.conn.sessions[session] = target
                    self.browser.native_targets[reply['tab']] = target
                    self.browser.order.append(target)
                    self.grants[ident] = grant
                    self._mark(grant, tab)
                    grant.timer = threading.Timer(max(0, grant.row['expires'] - self.clock()), self.end, args=(ident, 'expired'))
                    grant.timer.daemon = True
                    grant.timer.start()
                    self._grant_answer(slot, grant)
                self.browser.refresh(tab)  # a CDP round trip: never under the access lock
            except Exception:
                if not device.closed:
                    device.send({'type': 'end', 'id': ident})
                with self.lock:
                    if not slot['auto'] and not slot['event'].is_set():
                        self._answer(slot, {'ok': False, 'reason': 'denied'})
                raise
            finally:
                with self.lock:
                    self.reserved.discard(ident)
        return self.state()

    def end(self, ident, reason='tether_end'):
        if not isinstance(ident, str):
            raise ValueError('Choose a grant.')
        # Revoke authority and enqueue detach before taking Browser.lock. A slow page
        # command must not delay local Stop, expiry, or disconnect invalidation.
        with self.lock:
            grant = self.grants.pop(ident, None)
            if grant:
                self._retire(grant, reason)
                grant.timer.cancel()
                if grant.watch:
                    grant.watch.cancel()
            clients = [client for client in self.clients if client.grant is grant]
            self.browser._bump()
        for client in clients:
            client.revoke()
        if grant and not grant.device.closed:
            grant.device.cancel_grant(ident)
            try:
                grant.device.send({'type': 'end', 'id': ident})
            except browser.BrowserError:
                pass
        if grant:
            self.browser.remove(grant.row['tab'])
        return self.state()

    def extension_end(self, device, ident, reason='unknown'):
        with self.lock:
            grant = self.grants.get(ident)
            if not grant or grant.device is not device:
                return
            self._retire(grant, reason)
            for client in list(self.clients):
                if client.grant is grant:
                    client.revoke()
        # The reader cannot wait on a lock held by a command that awaits a reply.
        threading.Thread(target=self.end, args=(ident, reason), daemon=True, name='mac-grant-end').start()

    def disconnect(self, device):
        with self.lock:
            ids = [ident for ident, grant in self.grants.items() if grant.device is device]
            for slot in list(self.pending.values()):
                if slot.get('binding') is device:
                    self._answer(slot, {'ok': False, 'reason': 'denied'})
        for ident in ids:
            self.extension_end(device, ident, 'connection_lost')


class RoutedAccess:
    """Choose the browser at request time. Never move an existing request or grant."""
    def __init__(self, server, mac):
        self.server, self.mac = server, mac
        self.browser = server.browser
        server.panel_state = self.state
        mac.panel_state = self.state
        mac.browser.peer_browser = server.browser
        self.allow_lock = threading.RLock()
        request_lock = threading.RLock()
        for access in (server, mac):
            access.request_lock, access.request_peers = request_lock, [server, mac]
        mac.history = server.history

    def ask(self, reason, pane, duration=15, url=None, browser='auto', verified=False):
        browser = 'chrome' if browser == 'mac' else browser
        if browser not in ('auto', 'chrome', 'server'):
            raise ValueError('Choose auto, chrome, or server.')
        access = self.mac if browser == 'chrome' or browser == 'auto' and self.mac.broker.connected() else self.server
        if verified and self.server.auto():
            with self.allow_lock:
                identity = self.server._identity(pane)
                key = signed.process_key(pane, identity)
                if key is not None and not self.server.history.is_held(key):
                    # Serialize lookup and creation so simultaneous asks from one chat share the grant.
                    for owner in (self.server, self.mac):
                        with owner.lock:
                            for grant in owner.grants.values():
                                if (grant.row['pane'] == pane and owner.valid(grant) and grant.identity is not None
                                        and signed.same_chat(grant.identity, identity)):
                                    return owner.grant_result(grant)
                    slot = access._request_slot(reason, pane, duration, url, auto=True, identity=identity)
                    return self._allow_automatically(access, slot)
        return access.ask(reason, pane, duration, url)

    def _allow_automatically(self, access, slot):
        try:
            if not access._queue_request(slot):
                raise browser.BrowserError('Signed access stopped.')
            self.allow(slot['row']['id'])
        except Exception as exc:
            message = ' '.join(str(exc).split())
            # Browser and OS errors can contain local paths or capability URLs.
            if not message or re.search(r'[/\\]|[A-Za-z0-9_-]{22,}', message):
                message = 'The signed-in browser could not open a tab.'
            reason = 'busy' if isinstance(exc, signed.GrantLimit) else 'failed'
            with access.lock:
                access._answer(slot, {'ok': False, 'reason': reason, 'error': message[:180]})
        return slot['result']

    def state(self):
        states = [self.server.state(), self.mac.state()]
        return {**{key: [row for state in states for row in state[key]] for key in ('requests', 'grants')},
                'ended': self.server.history.state(), 'auto': states[0]['auto']}

    def dismiss(self, ident):
        self.mac.dismiss(ident)  # MacBrowser._bump also wakes the server panel stream.
        return self.state()

    def _owner(self, ident):
        if not isinstance(ident, str):
            raise ValueError('Choose a request or grant.')
        with self.mac.lock:
            return self.mac if ident in self.mac.pending or ident in self.mac.grants else self.server

    def allow(self, ident, duration=None):
        with self.allow_lock:
            if len(self.server.grants) + len(self.mac.grants) >= 2:
                raise signed.GrantLimit('End a grant first (the limit is 2).')
            self._owner(ident).allow(ident, duration)
        return self.state()

    def deny(self, ident):
        self._owner(ident).deny(ident)
        return self.state()

    def end(self, ident):
        self._owner(ident).end(ident)
        return self.state()

    def stop(self):
        self.mac.broker.stop()
        self.server.stop()


def upgrade(handler, broker=None):
    """Only this device-authenticated route bypasses cookie auth. Token never enters a URL."""
    broker = broker or BROKER
    handler.close_connection = True
    origin = handler.headers.get('Origin', '')
    key = handler.headers.get('Sec-WebSocket-Key', '')
    try:
        valid = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid = False
    if (not ORIGIN.fullmatch(origin) or len(handler.headers.get_all('Origin', [])) != 1):
        handler.respond(403, {'error': 'Invalid extension Origin.'})
        return
    if (not valid or handler.headers.get('Upgrade', '').lower() != 'websocket'
            or 'upgrade' not in [part.strip() for part in handler.headers.get('Connection', '').lower().split(',')]
            or handler.headers.get('Sec-WebSocket-Version') != '13'):
        handler.respond(400, {'error': 'WebSocket required.'})
        return
    transport = device = None
    try:
        handler.protocol_version = 'HTTP/1.1'
        handler.send_response(101)
        handler.send_header('Upgrade', 'websocket')
        handler.send_header('Connection', 'Upgrade')
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        handler.send_header('Sec-WebSocket-Accept', accept)
        handler.end_headers()
        handler.wfile.flush()
        handler.close_connection = True
        handler.connection.settimeout(5)
        transport = cdp.Transport(handler.connection)
        messages = cdp.websocket_messages(handler.rfile, transport.send, limit=cdp.MAX_MESSAGE)
        hello = json.loads(next(messages))
        if not isinstance(hello, dict) or hello.get('type') != 'hello':
            raise ValueError('Authenticate first.')
        ident = broker.authenticate(hello.get('token'), origin)
        device = broker.register(ident, transport, hello.get('chrome', ''))
        device.send({'type': 'ready', 'chunks': True})
        handler.connection.settimeout(30)
        for raw in messages:
            device.handle(json.loads(raw))
    except (ValueError, TypeError, EOFError, StopIteration, OSError, cdp.WebSocketError, browser.BrowserError):
        pass
    finally:
        if device:
            device.close()
        if transport:
            transport.abort()
            transport.writer.join(timeout=2)


def exchange(handler, broker=None):
    broker = broker or BROKER
    handler.close_connection = True
    try:
        origin = handler.headers.get('Origin', '')
        if not ORIGIN.fullmatch(origin) or len(handler.headers.get_all('Origin', [])) != 1:
            raise ValueError('Invalid extension Origin.')
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid pairing request.')
        handler.connection.settimeout(5)
        body = json.loads(handler.rfile.read(size))
        result = broker.exchange(body.get('code'), origin, body.get('name', 'Mac Chrome'))
        handler.respond(200, result)
    except (ValueError, AttributeError, UnicodeError):
        handler.respond(403, {'error': 'Pairing refused. Use a fresh code from Tether.'})


BROKER = Broker()
