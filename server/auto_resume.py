"""Remember open conversations and resume them once after a tmux server loss.

All terminal access is injected. The store lock covers only snapshot access;
discovery, terminal commands, transcript reads and sleeps run outside it.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import groups
import history
import paths
import prefs_sync
import side_shells
from terminal_input import UUID, new_window_after, resume_command, resume_session

TICK = 2
CLOSE_AFTER = 3
VERIFY_SECS = 60
MAX_PASS = 20
GAP = 1.5
OPTIONS = ('@chat_name', '@chat_name_at', '@chat_icon', '@chat_icon_by',
           '@project_rev', '@worktree', '@group', '@tether_role', '@group_by')
SESSION_OPTIONS = ('@project', '@groups')  # @groups: the session's groups (groups.py), so restored @group labels resolve
_STATES = ('open', 'pending', 'resuming', 'failed', 'deferred')
_FORMAT = '\t'.join('#{' + field + '}' for field in
                    ('pane_id', *OPTIONS, *SESSION_OPTIONS))
_lock = threading.Lock()
_wake = threading.Event()
_tombstones = {}
_cache = (None, {})


def _path():
    return paths.CACHE / 'open-tabs.json'


def _empty():
    return {'version': 1, 'server': None, 'tabs': {}}


def _number(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def _valid_tab(thread, tab):
    if not (isinstance(thread, str) and UUID.fullmatch(thread) and isinstance(tab, dict)
            and tab.get('thread') == thread and tab.get('agent') in ('claude', 'codex')
            and tab.get('state') in _STATES):
        return False
    strings = ('pane', 'session', 'session_id', 'folder', 'name', 'icon', 'error', 'transcript')
    return (all(isinstance(tab.get(key), str) for key in strings)
            and (tab.get('profile') is None or isinstance(tab['profile'], str))
            and isinstance(tab.get('order'), list) and len(tab['order']) == 2
            and all(type(value) is int for value in tab['order'])
            and all(type(tab.get(key)) is bool for key in ('working', 'stopped'))
            and _number(tab.get('started'))
            and (tab.get('missing_since') is None or _number(tab['missing_since']))
            and type(tab.get('offset')) is int and tab['offset'] >= 0
            and all(isinstance(tab.get(key), dict) and all(
                opt in allowed and isinstance(value, str) and value
                for opt, value in tab[key].items())
                for key, allowed in (('options', OPTIONS), ('session_options', SESSION_OPTIONS))))


def _read():
    try:
        with _path().open(encoding='utf-8') as stream:
            saved = json.load(stream)
        if not (isinstance(saved, dict) and type(saved.get('version')) is int and saved['version'] == 1
                and isinstance(saved.get('tabs'), dict)
                and (saved.get('server') is None or isinstance(saved['server'], str)
                     and re.fullmatch(r'\d+:\d+', saved['server']))):
            return _empty()
        tabs = {thread: tab for thread, tab in saved['tabs'].items() if _valid_tab(thread, tab)}
        for tab in tabs.values():
            if tab['state'] == 'deferred':
                tab.update(state='pending', error='')
        return dict(version=1, server=saved.get('server'), tabs=tabs)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _empty()


def _write(saved):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(saved, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _edit(change):
    with _lock:
        saved = _read()
        before = json.dumps(saved, sort_keys=True)
        result = change(saved)
        if json.dumps(saved, sort_keys=True) != before:
            _write(saved)
        return result


def _tab(thread):
    with _lock:
        return _read()['tabs'].get(thread)


def _update(thread, **fields):
    def change(saved):
        tab = saved['tabs'].get(thread)
        if tab is not None:
            tab.update(fields)
            return True
        return False
    return _edit(change)


def _tmux(run, *args, check=False):
    return run([paths.TMUX, *args], check=check, capture_output=True, text=True, timeout=5)


def server_id(*, run=subprocess.run):
    try:
        result = _tmux(run, 'display-message', '-p', '#{pid}:#{start_time}')
        identifier = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r'\d+:\d+', identifier):
            return 'up', identifier
        if result.returncode and any(text in result.stderr.lower() for text in ('no server running', 'error connecting')):
            return 'none', None
    except (OSError, subprocess.SubprocessError):
        pass
    return 'unknown', None


def enabled():
    return prefs_sync.load()['prefs'].get('autoResume', True) is not False


def _entries(entries):
    return entries() if callable(entries) else entries


def _session(entry):
    return entry.get('label', '').rsplit(':', 1)[0]


def _eligible(entry):
    thread = entry.get('thread')
    return (entry.get('agent') in ('claude', 'codex') and entry.get('writable') is True
            and isinstance(thread, str) and UUID.fullmatch(thread)
            and not entry.get('pending_conversation') and _session(entry) != side_shells.SESSION)


def _prune(now):
    for key in list(_tombstones):
        if _tombstones[key] <= now:
            del _tombstones[key]


def _blocked(entry):
    return any(key in _tombstones for key in (entry.get('id'), entry.get('thread'), entry.get('tmux_session')))


def _archived(entry, prefs, display_dir):
    archived = prefs.get('archived', {})
    if archived.get(entry.get('tmux_session')) == _session(entry):
        return True
    tab = prefs.get('archivedTabs', {}).get(entry.get('id'))
    return (isinstance(tab, dict) and tab.get('dir') == display_dir(entry)
            and (not tab.get('thread') or not entry.get('thread') or tab['thread'] == entry['thread']))


def _pane_rows(*, run):
    result = _tmux(run, 'list-panes', '-a', '-F', _FORMAT, check=True)
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) == len(OPTIONS) + len(SESSION_OPTIONS) + 1:
            pane, *values = fields
            rows[pane] = dict(zip((*OPTIONS, *SESSION_OPTIONS), values))
    return rows


def _order(position):
    try:
        window, pane = position.split('.')
        return [int(window), int(pane)]
    except (AttributeError, ValueError):
        return [0, 0]


def tick(*, entries, folder_of, status_of, display_dir, run=subprocess.run,
         now=time.time, sleep=time.sleep, on_change=lambda: None):
    kind, identifier = server_id(run=run)
    if kind == 'unknown':
        return
    stamp = now()
    active = enabled()

    def loss(saved):
        if saved['server'] is not None and (kind == 'none' or saved['server'] != identifier):
            if not active:
                saved['tabs'].clear()
            else:
                for thread, tab in list(saved['tabs'].items()):
                    tab['pane'] = ''
                    tab['session_id'] = ''
                    if (tab['state'] == 'open' and tab['missing_since'] is not None
                            and stamp - tab['missing_since'] > CLOSE_AFTER):
                        del saved['tabs'][thread]
                    elif tab['state'] in ('open', 'resuming'):
                        tab.update(state='pending', missing_since=None, stopped=tab['stopped'] or tab['working'])
            saved['server'] = identifier
        elif saved['server'] is None and kind == 'up':
            saved['server'] = identifier
    _edit(loss)  # Persist the loss before any command can start a conversation.

    def turned_off(saved):
        # Off means nothing waits to come back: no notice, and a Retry cannot leave a tab pending for ever.
        for thread, tab in list(saved['tabs'].items()):
            if tab['state'] != 'open':
                del saved['tabs'][thread]
    if not active:
        _edit(turned_off)

    if kind == 'up':
        live = _entries(entries)
        if server_id(run=run) != ('up', identifier):
            return
        rows = _pane_rows(run=run)
        prefs = prefs_sync.load()['prefs']
        archived_sessions = {_session(entry) for entry in live
                             if prefs.get('archived', {}).get(entry.get('tmux_session')) == _session(entry)}
        refreshed, archived = {}, set()
        for entry in live:
            if not _eligible(entry):
                continue
            thread = entry['thread']
            if _archived(entry, prefs, display_dir):
                archived.add(thread)
                continue
            row = rows.get(entry['id'], {})
            refreshed[thread] = dict(
                thread=thread, agent=entry['agent'], profile=entry.get('codex_profile'), pane=entry['id'],
                session=_session(entry), session_id=entry.get('tmux_session', ''),
                order=_order(entry.get('position')),
                folder=folder_of(entry) or '', name=row.get('@chat_name') or entry.get('pane_name') or '',
                icon=entry.get('pane_icon') or '', options={opt: row[opt] for opt in OPTIONS if row.get(opt)},
                session_options={opt: row[opt] for opt in SESSION_OPTIONS if row.get(opt)},
                working=status_of(entry).get('state') == 'Working', state='open', error='', started=0,
                missing_since=None, stopped=False, offset=0, transcript=entry.get('transcript') or '')

        # Terminal probes and cleanup must stay outside the snapshot lock.
        with _lock:
            verifying = [tab for tab in _read()['tabs'].values() if tab['state'] == 'resuming'
                         and tab['thread'] not in refreshed]
        failed = {}
        for tab in verifying:
            pane = tab['pane']
            age = stamp - tab['started']
            if pane not in rows:
                failed[tab['thread']] = 'The resumed pane has ended.'
            elif age >= VERIFY_SECS:
                entry = next((entry for entry in live if entry['id'] == pane), None)
                if entry and entry.get('agent') == 'shell':
                    screen = _tmux(run, 'capture-pane', '-p', '-t', pane).stdout
                    lines = [line.strip() for line in screen.splitlines() if line.strip()]
                    failed[tab['thread']] = lines[-1][:160] if lines else 'The resume command exited.'
                    _tmux(run, 'kill-pane', '-t', pane)
                elif age >= 5 * VERIFY_SECS or not entry or not entry.get('pending_conversation'):
                    failed[tab['thread']] = 'Could not verify the resumed conversation.'

        def sync(saved):
            _prune(stamp)
            for thread in archived:
                saved['tabs'].pop(thread, None)
            for thread, fresh in refreshed.items():
                entry = next(entry for entry in live if entry.get('thread') == thread)
                if _blocked(entry):
                    continue
                old = saved['tabs'].get(thread, {})
                fresh.update(stopped=old.get('stopped', False), offset=old.get('offset', 0),
                             started=old.get('started', 0))
                if fresh['stopped']:
                    fresh['working'] = False
                saved['tabs'][thread] = fresh
            for thread, tab in list(saved['tabs'].items()):
                if tab['state'] == 'pending' and tab['session'] in archived_sessions:
                    del saved['tabs'][thread]
                    continue
                if thread in refreshed:
                    continue
                if tab['state'] == 'open':
                    if tab['missing_since'] is None:
                        tab['missing_since'] = stamp
                    elif stamp - tab['missing_since'] >= CLOSE_AFTER:
                        del saved['tabs'][thread]
                elif tab['state'] == 'resuming' and thread in failed:
                    tab.update(state='failed', error=failed[thread])
        _edit(sync)

    if active and launch(entries=entries, run=run, now=now, sleep=sleep):
        on_change()


def _options(tab, pane, session_id, *, run):
    # Set the name first: the tmux_names rename hook must see the saved name.
    options = tab['options']
    name = options.get('@chat_name') or tab['name']
    if name:
        _tmux(run, 'set-option', '-p', '-t', pane, '@chat_name', name, check=True)
    if tab['session_options'].get('@groups'):
        try:  # before the labels, so @group points to a real group
            groups.restore(session_id, tab['session_options']['@groups'], run)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass  # the tab still resumes; it shows in Unstacked
    for opt in OPTIONS[1:]:
        if options.get(opt):
            _tmux(run, 'set-option', '-p', '-t', pane, opt, options[opt], check=True)
    if tab['session_options'].get('@project'):
        _tmux(run, 'set-option', '-t', session_id, '@project', tab['session_options']['@project'], check=True)
    if tab['name']:
        _tmux(run, 'rename-window', '-t', pane, tab['name'], check=True)
        _tmux(run, 'set-option', '-w', '-t', pane, 'automatic-rename', 'off', check=True)


def launch(*, entries, run=subprocess.run, now=time.time, sleep=time.sleep):
    def select(saved):
        stamp = now()
        if any(tab['state'] == 'resuming' and 0 <= stamp - tab['started'] < VERIFY_SECS
               for tab in saved['tabs'].values()):
            return []
        pending = sorted((tab for tab in saved['tabs'].values() if tab['state'] == 'pending'),
                         key=lambda tab: (tab['session'], tab['order']))
        return [tab['thread'] for tab in pending[:MAX_PASS]]
    try:
        selected = _edit(select)
    except Exception:
        return False  # Without a durable claim, no conversation may start.
    launched = False
    for index, thread in enumerate(selected):
        pane = None
        cleanup = False
        try:
            tab = _tab(thread)
            if tab is None or tab['state'] != 'pending':
                continue
            try:
                listed = _entries(entries)
            except (OSError, subprocess.SubprocessError):
                continue  # Discovery failed, so nothing shows the thread is closed. The tab stays pending for the next tick.
            # discover reports a 'saved' row with no pane when tmux is down: that is not an open tab.
            live = next((entry for entry in listed if entry.get('thread') == thread and entry.get('id') != 'saved'), None)
            if live is not None:
                _update(thread, state='open', error='', missing_since=None, pane=live['id'],
                        session=_session(live), session_id=live.get('tmux_session', tab['session_id']))
                continue
            if tab['pane'] and tab['pane'] in _pane_rows(run=run):
                entry = next((entry for entry in listed if entry.get('id') == tab['pane']), None)
                if entry is None or entry.get('agent') != 'shell':
                    _update(thread, state='resuming', started=now(), error='')
                    continue
            if not os.path.isdir(tab['folder']):
                raise ValueError('Folder not found: ' + tab['folder'])
            chat = history.find_chat(thread, agent=tab['agent'])
            # The chat list hides some threads that can still resume, such as `codex exec` ones.
            if chat is None and not os.path.isfile(tab['transcript']):
                raise ValueError('Conversation not found.')
            command = resume_command(thread, agent=tab['agent'], profile=tab['profile'])
            if not _update(thread, state='resuming', started=now(), error=''):
                continue
            existing = _tmux(run, 'list-panes', '-s', '-t', '=' + tab['session'] + ':', '-F', '#{pane_id}')
            panes = existing.stdout.splitlines() if existing.returncode == 0 else []
            if panes:
                pane = new_window_after(panes[-1], run, cwd=tab['folder'], group=tab['options'].get('@group', ''))
                cleanup = True
            else:
                pane = resume_session(tab['session'], tab['folder'], thread, run=run,
                                      agent=tab['agent'], profile=tab['profile'])
                _update(thread, pane=pane)
            session_id = _tmux(run, 'display-message', '-p', '-t', pane, '#{session_id}', check=True).stdout.strip()
            # For a new session resume_session already sent the keys. The shell
            # buffers them during startup; the launcher reads @project afterwards.
            _options(tab, pane, session_id, run=run)
            if panes:
                # The project launcher needs the options before these shell keys.
                if _tab(thread) is None:
                    _tmux(run, 'kill-pane', '-t', pane)
                    continue
                _tmux(run, 'send-keys', '-t', pane, '-l', command, check=True)
                _tmux(run, 'send-keys', '-t', pane, 'Enter', check=True)
            transcript = tab['transcript'] or (chat or {}).get('transcript') or ''
            try:
                offset = os.path.getsize(transcript)
            except OSError:
                offset = 0
            _update(thread, pane=pane, session_id=session_id, transcript=transcript, offset=offset)
            launched = True
        except Exception as exc:
            try:
                _update(thread, state='failed', error=(str(exc).strip() or type(exc).__name__)[:160])
            except Exception:
                pass  # A failed save must not start the command or stop other tabs.
            if cleanup and pane:
                try:
                    _tmux(run, 'kill-pane', '-t', pane)
                except Exception:
                    pass
        finally:
            # The first new-session may have created a different server.
            try:
                kind, identifier = server_id(run=run)
                if kind != 'unknown':
                    _edit(lambda saved: saved.update(server=identifier))
            except Exception:
                pass
        if index < len(selected) - 1:
            sleep(GAP)
    return launched


def _forget(*, pane=None, session_id=None, session_name=None, panes=(), now):
    def change(saved):
        _prune(now)
        keys = {key for key in (pane, session_id, *panes) if key}
        sessions = {tab['session'] for tab in saved['tabs'].values()
                    if session_id is not None and tab['session_id'] == session_id}
        if session_name is not None:
            sessions.add(session_name)
        for thread, tab in list(saved['tabs'].items()):
            if ((pane and tab['pane'] == pane)
                    or session_id is not None and tab['session_id'] == session_id
                    or tab['state'] == 'pending' and tab['session'] in sessions):
                keys.update((thread, tab['pane']))
                del saved['tabs'][thread]
        _tombstones.update({key: now + 30 for key in keys})
    _edit(change)


def forget_pane(pane, *, run=subprocess.run, now=time.time):
    try:
        _forget(pane=pane, now=now())
    except (OSError, subprocess.SubprocessError):
        pass


def forget_session(session_id, *, run=subprocess.run, now=time.time):
    # Install the session tombstone before the terminal lookup can race discovery.
    try:
        _forget(session_id=session_id, now=now())
        result = _tmux(run, 'display-message', '-p', '-t', session_id + ':', '#{session_name}')
        session_name = (result.stdout.strip() if result.returncode == 0 else '') or None
        panes = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{pane_id}').stdout.splitlines()
        _forget(session_id=session_id, session_name=session_name, panes=panes, now=now())
    except (OSError, subprocess.SubprocessError):
        pass


def _claude_prompt(record):
    if record.get('type') != 'user' or any(record.get(flag) is True for flag in (
            'isMeta', 'isSidechain', 'isCompactSummary', 'isVisibleInTranscriptOnly')):
        return False
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') != 'tool_result' for block in content)


def new_turn(*, agent, transcript, offset):
    try:
        with open(transcript, 'rb') as stream:
            if stream.seek(0, 2) < offset:
                return False
            # An offset may point into an unfinished record. Only whole lines count.
            if offset:
                stream.seek(offset - 1)
                boundary = stream.read(1) == b'\n'
            else:
                boundary = True
            stream.seek(offset)
            data = stream.read(4 * 1024 * 1024)
        lines = data.split(b'\n')[:-1]
        if not boundary:
            lines = lines[1:]
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            if agent == 'claude' and _claude_prompt(record):
                return True
            payload = record.get('payload')
            if (agent == 'codex' and record.get('type') == 'event_msg'
                    and isinstance(payload, dict) and payload.get('type') == 'task_started'):
                return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def overlay(entry, status):
    global _cache
    with _lock:
        try:
            info = _path().stat()
            key = (str(_path()), info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            key = None
        if _cache[0] != key:
            _cache = (key, _read()['tabs'])
        tab = _cache[1].get(entry.get('thread')) if key is not None else None
    if not tab or not tab['stopped']:
        return status
    if new_turn(agent=tab['agent'], transcript=tab['transcript'], offset=tab['offset']):
        def clear(saved):
            current = saved['tabs'].get(tab['thread'])
            if current and current['offset'] == tab['offset']:
                current['stopped'] = False
        _edit(clear)
        return status
    if status.get('state') in ('Approval', 'Input needed'):
        return status
    return dict(status, state='Stopped')


def notice():
    with _lock:
        tabs = [{key: tab[key] for key in ('thread', 'agent', 'name', 'folder', 'error', 'state')}
                for tab in _read()['tabs'].values() if tab['state'] == 'failed']
    return {'failed': len(tabs), 'tabs': tabs}


def retry():
    def change(saved):
        tabs = [tab for tab in saved['tabs'].values() if tab['state'] == 'failed']
        for tab in tabs:
            tab.update(state='pending', error='')
        return len(tabs)
    count = _edit(change)
    _wake.set()
    return count


def dismiss():
    def change(saved):
        threads = [thread for thread, tab in saved['tabs'].items() if tab['state'] == 'failed']
        for thread in threads:
            del saved['tabs'][thread]
        return len(threads)
    return _edit(change)


def watch(*, entries, folder_of, status_of, display_dir, on_change, run=subprocess.run):
    """Service-only watch. Tests call tick with fake terminal access instead."""
    while True:
        _wake.clear()
        try:
            tick(entries=entries, folder_of=folder_of, status_of=status_of,
                 display_dir=display_dir, on_change=on_change, run=run)
        except Exception as exc:
            print('Auto-resume: ' + str(exc), file=sys.stderr, flush=True)
        _wake.wait(TICK)
