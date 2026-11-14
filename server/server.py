"""Mobile transcript and message bridge to one existing Codex CLI thread."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
from pathlib import Path
import subprocess
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from agents import claude_consume, claude_snapshot, discover, usage
from agents import records
import agents
import codex_messages
import continuations
import pane_icons
from antigravity import snapshot as antigravity_snapshot, transcript_message as antigravity_message, window_active
from terminal_input import NAME_HELP, valid_name, fork_chat, new_window_after, DraftError, card_key, menu_choose, unqueue, clear_draft as terminal_clear_draft, type_text, live_status, capture, capture_screen, submit as terminal_submit, respond_approval, plan_menu, start_agent, create_session, shell_snapshot, shell_submit, verify
from quota_notices import error_message, merge as merge_quota_notices
import tool_details
import subagents
import subagent_stop
import activity
import broadcast
import catalog
import claude_probe
import claude_hooks
import ask_bridge
import footer
import accounts
import live_usage
import login_flow
import saved_accounts
import history
import chat_hidden
import live as live_state
from terminal_input import resume_command, resume_session, rename_agent
from terminal_input import stash_quiet_draft, restore_check, clear_unsent, prompt_draft
import model_picker
import prompt_actions
import push
import shortcuts
import prefs_sync
import codex_provider
import delegate
import worktrees
import agent_tabs
import review_links
import foundry_spend
import transcribe
import tts
import voice_log
import friday_proxy
import live_mode
import attention
import suggest_groups
import thread_started
import shell_log
import terminal_replies
import side_question
import reply_preview
import bang_run
import uploads
import media_files
import repo_panel
import files
import office_addon
import auth
import needs
import select
import sys
import socket
import paths
import side_shells
import host_stats
import tmux_merge
import tmux_names
import auto_resume
import window_size
import projects_api
import work_api
import work_jira
import groups
import helper_close
import groups_api
import codex_level
import autonomy
import approval
import custom_layouts
import backgrounds
import codex_runs
import browser_api
import mac_browser
import browser_cdp
import updates

ROOT = Path(__file__).resolve().parent
KEEPALIVE = 10
UI_ASSETS = {name: (ROOT / name).read_text() for name in ('index.html', 'app.js')}
STATIC_ASSETS = {'/mobile/' + p.name: p.read_bytes() for p in [*(ROOT / 'vendor').glob('*.js'), ROOT / 'sw.js', ROOT / 'manifest.webmanifest', *ROOT.glob('*.png')]}
TERMINAL_ASSETS = {'html': (ROOT / 'termius.html').read_text(), 'js': (ROOT / 'termius.js').read_text()}
CONFIG = paths.private_file('default_conversation.json')
TTYD_PORT = int(os.environ.get('MOBILE_TMUX_TTYD_PORT', '7682'))
SEND_LOCK = threading.Lock()
side_question.SEND_LOCK = SEND_LOCK  # the copy key waits for a message that Tether types
WATCHING = {}
WATCHED = {}
WATCHING_LOCK = threading.Lock()


def chat_folder(value):
    """Validate an explicit working folder before a tmux write."""
    if (not isinstance(value, str) or len(value) > 1024
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError('Folder not found.')
    if value == '~' or value.startswith('~/'):
        value = str(paths.HOME) + value[1:]
    if not os.path.isabs(value):
        raise ValueError('Folder not found.')
    try:
        folder = os.path.realpath(value)
        if os.path.isdir(folder):
            return folder
    except (OSError, ValueError):
        pass
    raise ValueError('Folder not found.')


def watching(session):
    with WATCHING_LOCK:
        return WATCHING.get(session, 0) > 0 or time.monotonic() - WATCHED.get(session, float('-inf')) < 10


def ask_watching(session, window):
    with WATCHING_LOCK:
        return WATCHING.get(session, 0) > 0 or time.monotonic() - WATCHED.get(session, float('-inf')) < window


ask_bridge.pending.watching = ask_watching


def notify(payload, urgent):
    threading.Thread(target=push.send_all, args=(payload, urgent), daemon=True).start()


HELPER_CLOSER = helper_close.Closer()
NOTIFIER = push.Notifier(notify, watching, levels=push.pane_levels, delivery=push.delivery_settings, context=lambda entry: notification_context(entry))
WORK_WATCHER = work_api.Watcher(notify, delivery=push.delivery_settings)


def review_alert(title, body, session, review=None):
    payload = {'title': title, 'body': body, 'tag': 'review-' + session, 'session': session,
               'priority': 'urgent', 'silent': False, 'url': '/mobile/#session=' + quote(session)}
    if review is not None:
        payload['review'] = review
    notify(payload, True)


def review_work_mark(entry):
    cwd = entry.get('cwd')
    if not cwd:
        return None
    try:
        outputs = []
        for args in (['git', 'rev-parse', 'HEAD'], ['git', 'status', '--porcelain'], ['git', 'diff', 'HEAD']):
            result = subprocess.run(args, cwd=cwd, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
                                    capture_output=True, timeout=5)
            if result.returncode:
                return None
            outputs.append(result.stdout)
        return hashlib.sha256(b''.join(outputs)).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def monitor_notices():
    """Keep native quota notices even when the phone page is closed."""
    while True:
        try:
            entries = sessions()
        except Exception as exc:
            print(f'session discovery failed: {exc!r}', file=sys.stderr)
            time.sleep(2)
            continue
        for entry in entries:
            try:
                if entry.get('owner_pid') and entry.get('writable'):
                    NOTIFIER.observe(entry, shared_status(entry))
            except Exception:  # one odd pane must never stop the monitor for everyone
                continue
        review_links.observe(entries, shared_status, review_send, last_reply,
                             alert=review_alert, work_mark=review_work_mark, review_reply=review_reply)
        try:
            attention.STORE.observe(entries, shared_status, notification_reply, lambda entry: timeline(entry['id'])['finished'])
            digest = attention.STORE.digest_due()
            if digest:
                notify(digest, False)
        except Exception as exc:  # Attention mode must never stop the monitor
            print(f'attention observe failed: {exc!r}', file=sys.stderr)
        try:
            WORK_WATCHER.observe()
        except Exception as exc:  # the work board must never stop the monitor
            print(f'work watcher failed: {exc!r}', file=sys.stderr)
        try:
            work_jira.MOVER.observe()
        except Exception as exc:  # Jira on Done must never stop the monitor
            print(f'work jira failed: {exc!r}', file=sys.stderr)
        try:
            HELPER_CLOSER.observe(entries, shared_status, subprocess.run, watching, lock=SEND_LOCK, fresh=fresh_status)
        except Exception as exc:  # closing idle helper tabs must never stop the monitor
            print(f'helper close failed: {exc!r}', file=sys.stderr)
        time.sleep(2)


def display_text(text):
    text = continuations.unwrap(text)
    def fence(value, language=''):
        delimiter = '`' * max(3, max((len(part) + 1 for part in re.findall(r'`+', value)), default=3))
        return delimiter + language + '\n' + value + '\n' + delimiter

    def shell_command(match):
        command, result = match[1].strip(), match[2].strip()
        ticks = '`' * max(1, max((len(part) + 1 for part in re.findall(r'`+', command)), default=1))
        rendered = '! ' + ticks + ' ' + command + ' ' + ticks if '\n' not in command else '!\n\n' + fence(command, 'sh')
        exit_code = re.search(r'(?m)^Exit code:\s*(-?\d+)', result)
        output_header = re.search(r'(?m)^Output:[ \t]*(?:\r?\n)?', result)
        output = result[output_header.end():].strip('\r\n') if output_header else ''
        if output:
            rendered += '\n\n' + fence(output, 'text')
        if exit_code and exit_code[1] != '0':
            rendered += '\n\nExit code: ' + exit_code[1]
        elif not output_header and not exit_code and result:
            rendered += '\n\n' + fence(result, 'text')
        return rendered

    text = re.sub(r'<user_shell_command>\s*<command>(.*?)</command>\s*<result>(.*?)</result>\s*</user_shell_command>',
                  shell_command, text, flags=re.DOTALL)
    def answer(match):
        try:
            reply = json.loads(match.group(1))
            return '\n'.join(item['answer'] for item in reply
                             if isinstance(item, dict) and isinstance(item.get('answer'), str))
        except (ValueError, TypeError):
            return ''
    return re.sub(r'<send_user_message_question_reply>(.*?)</send_user_message_question_reply>',
                  answer, text, flags=re.DOTALL)


def settings():
    """The saved default conversation (optional: a fresh install has none)."""
    try:
        saved = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        saved = {}
    return {'thread': saved.get('thread'), 'transcript': saved.get('transcript')}


STATUS_CACHE = {}  # entry id -> (when, status)
STATUS_LOCK = threading.Lock()
STATUS_PANE_LOCKS = {}


def shared_status(entry):
    """live_status for the session list and the monitor, which would otherwise each re-read the pane every few seconds."""
    with STATUS_LOCK:
        pane_lock = STATUS_PANE_LOCKS.setdefault(entry['id'], threading.Lock())
    with pane_lock:
        when, status = STATUS_CACHE.get(entry['id'], (0.0, None))
        if time.monotonic() - when > 1:
            try:
                status = auto_resume.overlay(entry, live_status(entry))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):  # a pane that just closed must not fail the whole tab list
                status = {'state': 'Unavailable', 'approval': None}
            if (status.get('attention') or {}).get('askBridge'):
                STATUS_CACHE.pop(entry['id'], None)  # Keep question payloads in the bounded bridge store only.
            else:
                STATUS_CACHE[entry['id']] = (time.monotonic(), status)
            track(entry['id'], status['state'])
        return status


def fresh_status(entry):
    """shared_status without the one-second cache: for a check that must see a prompt sent a moment ago."""
    STATUS_CACHE.pop(entry['id'], None)
    return shared_status(entry)


TRANSITIONS = {}  # entry id -> {'state', 'since', 'finished'}: wall-clock times, so the phone can sort and mark finished turns


ACTIVE_STATES = ('Working', 'Approval', 'Input needed')  # the turn is not over while the agent waits on you


def track(identifier, state, now=None):
    """Remember when a pane changed state, and when its last turn ended (Working, Approval or Input needed, then anything calmer)."""
    now = now or time.time()
    seen = TRANSITIONS.get(identifier)
    if not seen:
        TRANSITIONS[identifier] = {'state': state, 'since': now, 'finished': None}
    elif seen['state'] != state:
        if seen['state'] in ACTIVE_STATES and state not in ACTIVE_STATES:
            seen['finished'] = now
        seen.update(state=state, since=now)


def timeline(identifier):
    seen = TRANSITIONS.get(identifier) or {}
    return {'since': seen.get('since'), 'finished': seen.get('finished')}


SESSIONS_CACHE = [0.0, []]
SESSIONS_LOCK = threading.Lock()
SESSIONS_GENERATION = [0]  # counts tmux writes (forget_sessions); a list built across one is not cached


def pane_identity(pane):
    """The chat that runs in a tmux pane now, for signed browser grants: (agent, pid, start, thread or None while
    the chat id is not known yet), or ('gone',) when the pane has no agent. signed_access.same_chat compares them."""
    entry = next((entry for entry in sessions() if entry['id'] == pane), None)
    if not entry or entry.get('agent') in (None, 'shell'):
        return ('gone',)
    thread = None if entry.get('pending_conversation') else entry.get('thread')
    return (entry.get('agent'), entry.get('owner_pid'), entry.get('process_start'), thread)


def sessions():
    """Discovery shells out (ps, tmux, sqlite); every open page and the monitor ask every 2s, so share one result for a second."""
    with SESSIONS_LOCK:
        if time.monotonic() - SESSIONS_CACHE[0] > 1:
            SESSIONS_CACHE[1] = discover(settings(), subprocess.run)
            window_size.apply(subprocess.run, entries=SESSIONS_CACHE[1])
            SESSIONS_CACHE[0] = time.monotonic()
        return SESSIONS_CACHE[1]


# Writes that add, remove or rename tmux panes or sessions. After one succeeds, the next list must come from tmux,
# so a page that refreshes right away sees the new pane (or no longer sees the closed one).
TMUX_WRITES = {'/mobile/api/split', '/mobile/api/fork', '/mobile/api/delegate', '/mobile/api/agent-tab', '/mobile/api/continue', '/mobile/api/review-link', '/mobile/api/resume', '/mobile/api/shell', '/mobile/api/session',
               '/mobile/api/tmux-sessions', '/mobile/api/rename', '/mobile/api/pane-name', '/mobile/api/pane-icon', '/mobile/api/pane-icon/draw', '/mobile/api/side-shell',
               '/mobile/api/side-shell/end', '/mobile/api/start', '/mobile/api/merge', '/mobile/api/auto-resume', '/mobile/api/review-stalled'} | groups_api.WRITES


def forget_sessions():
    with SESSIONS_LOCK:
        SESSIONS_CACHE[0] = 0.0
        SESSIONS_GENERATION[0] += 1


def build_listing():
    rows = session_rows(sessions())
    try:
        links = review_links.reviewed_by()
        paused = review_links.paused()
    except Exception:  # a review-store read error must not hide the session list
        links = {}
        paused = {}
    for row in rows:
        if links.get(row['id']):
            row['reviewers'] = links[row['id']]
        row['reviewers_paused'] = paused.get(row['id'], [])
    listing = {'sessions': rows, 'default_thread': settings()['thread']}
    try:
        found_groups = groups.all_groups()
    except Exception:  # a group read error must not hide the session list
        found_groups = {}
    if found_groups:
        listing['groups'] = found_groups
    resume_notice = auto_resume.notice()
    if resume_notice['failed']:
        listing['auto_resume'] = resume_notice
    return listing


LISTING_CACHE = {'at': 0.0, 'generation': None, 'body': b'', 'tag': ''}
LISTING_LOCK = threading.Lock()


def sessions_listing():
    """The session list as JSON bytes and a weak ETag. Every open page polls it, so pages that ask within a second of
    the last build share it, and a build in progress serves the pages that wait on it (B24). A tmux write
    (forget_sessions) makes the next list new at once, and a list built while a write happened is not kept."""
    with LISTING_LOCK:
        generation = SESSIONS_GENERATION[0]
        if LISTING_CACHE['generation'] == generation and time.monotonic() - LISTING_CACHE['at'] <= 1:
            return LISTING_CACHE['body'], LISTING_CACHE['tag']
        body = json.dumps(build_listing()).encode()
        tag = 'W/"%s"' % hashlib.sha256(body).hexdigest()[:20]
        if SESSIONS_GENERATION[0] == generation:
            LISTING_CACHE.update(at=time.monotonic(), generation=generation, body=body, tag=tag)
        return body, tag


def etag_matches(header, tag):
    """If-None-Match against one weak tag (weak comparison, RFC 9110 13.1.2)."""
    if not header or not tag:
        return False
    plain = tag.removeprefix('W/')
    return any(part.strip() == '*' or part.strip().removeprefix('W/') == plain for part in header.split(','))


SESSION_ROW_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix='session-row')


def session_row(entry):
    """One pane in the session list. `needs` is a one-line summary when the pane waits on the user (the desktop "Needs you" list)."""
    status = shared_status(entry)
    return dict({key: entry.get(key) for key in ('id', 'tmux_session', 'label', 'agent', 'thread', 'writable', 'pane_name', 'tmux_pane_name', 'pane_icon', 'agent_name', 'codex_profile', 'worktree', 'group', 'role')},
                dir=home_path(tab_folder(entry)), **status, **timeline(entry['id']), needs=needs.summary(status),
                ask=attention.STORE.ask(entry['id'], entry.get('thread')))


def session_rows(entries):
    """Build session rows concurrently, preserving discovery order."""
    return list(SESSION_ROW_POOL.map(session_row, entries))


def files_sessions():
    """Live panes and their folders, for the Files explorer's Places."""
    return [{'session': entry['id'], 'name': entry.get('pane_name') or entry.get('label') or entry.get('tmux_session'),
             'agent': entry.get('agent'), 'cwd': tab_folder(entry)} for entry in sessions()]


# Shown in a pane's top border when its window holds several panes. Claude writes "✳ topic" over the pane title,
# so the border reads Tether's own name first.
BORDER_FORMAT = ' #{?@chat_name,#{@chat_name},#{pane_title}} '


def name_in_tmux(pane, name):
    """Show a pane's Tether name in tmux too: a window with only this pane takes the name (and stops renaming itself);
    a window with several panes shows each pane's name in its border. An empty name gives the window back to tmux."""
    try:
        result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{window_panes}'], capture_output=True, text=True, timeout=5)
        count = int(result.stdout.strip())
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return
    if count == 1:
        command = (['rename-window', '-t', pane, name, ';', 'set-option', '-w', '-t', pane, 'automatic-rename', 'off'] if name
                   else ['set-option', '-w', '-t', pane, '-u', 'automatic-rename'])
    elif name:
        command = ['set-option', '-w', '-t', pane, 'pane-border-status', 'top', ';', 'set-option', '-w', '-t', pane, 'pane-border-format', BORDER_FORMAT]
    else:
        return
    subprocess.run([paths.TMUX, *command], capture_output=True, timeout=5)


def name_together(pane, name, from_session):
    """Keep one name per pane: the agent gets `/rename NAME`, and in a tmux session with only this pane the session
    and the pane share the name too. from_session: the user renamed the session (else the pane). Returns a note or ''."""
    entries = sessions()
    entry = next((e for e in entries if e['id'] == pane), None)
    if not entry or not name:
        return ''
    alone = sum(e.get('tmux_session') == entry.get('tmux_session') for e in entries) == 1
    if alone and from_session:
        subprocess.run([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', name, ';', 'set-option', '-p', '-t', pane,
                        '@chat_name_at', str(int(time.time() * 1000))], capture_output=True, timeout=5)
    elif alone and valid_name(name) and name != entry.get('label'):
        subprocess.run([paths.TMUX, 'rename-session', '-t', pane, name], capture_output=True, timeout=5)  # a taken name keeps the old one
    if from_session and not alone:
        return ''
    return rename_agent(entry, name)


def choose(identifier=None):
    entries = sessions()
    if identifier is None:
        if not entries:
            raise ValueError('No tmux sessions found. Start one (tmux new -s work) and run an agent in it.')
        return next((entry for entry in entries if entry.get('thread') and entry.get('thread') == settings()['thread']), entries[0])
    entry = next((entry for entry in entries if entry['id'] == identifier), None)
    if not entry:
        raise ValueError('That session has ended. Select another tab.')
    return entry


CODEX_COMMAND = re.compile(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")')
QUIET_CALLS = {'wait', 'sleep'}


def codex_action_lines(payload):
    """One short line per command or tool call in a Codex response item."""
    name = payload.get('name') or 'tool'
    raw = payload.get('input') if payload.get('type') == 'custom_tool_call' else payload.get('arguments')
    if not isinstance(raw, str):
        return ['Ran a script' if name == 'exec' else name]
    commands = []
    for found in CODEX_COMMAND.findall(raw):
        try:
            commands.append(json.loads(found))
        except ValueError:
            continue
    if not commands and payload.get('type') == 'function_call':
        try:
            arguments = json.loads(raw)
            commands = [arguments.get('cmd') or arguments.get('command')] if isinstance(arguments, dict) else []
        except ValueError:
            pass
    commands = [command for command in commands if isinstance(command, str) and command.strip()]
    if commands:
        return ['Ran ' + command.strip().split('\n')[0] for command in commands]
    return [] if name in QUIET_CALLS else ['Ran a script' if name == 'exec' else name]


def codex_state():
    return {'messages': [], 'dropped': 0, 'busy': False, 'usage': None, 'model': '', 'effort': '', 'credits': None}


def codex_consume(state, index, entry):
    """Fold one rollout record into the chat messages, busy flag and latest usage figures."""
    messages = state['messages']
    payload = entry.get('payload', {})
    kind = payload.get('type')
    if entry.get('type') == 'turn_context' and payload.get('model'):
        state['model'] = str(payload['model'])
        state['effort'] = payload.get('effort') or payload.get('reasoning_effort') or ''
    if entry.get('type') == 'event_msg':
        if kind in ('error', 'warning'):
            text = error_message(payload)
            if text:
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice',
                                 'text': text, 'time': entry.get('timestamp')})
        if kind == 'token_count':
            info = payload.get('info') or {}
            tokens = (info.get('last_token_usage') or {}).get('total_tokens')
            limit = info.get('model_context_window')
            rates = payload.get('rate_limits') or {}
            windows = [{'label': f"{value['window_minutes'] / 60:g}h", 'percent': value.get('used_percent'),
                        'resets_at': value.get('resets_at')}
                       for key in ('primary', 'secondary') if isinstance((value := rates.get(key)), dict)
                       and isinstance(value.get('window_minutes'), (int, float)) and value.get('used_percent') is not None]
            previous = state['usage'] or ({}, [], None)
            context = {'percent': tokens / limit * 100 if tokens is not None and limit else None,
                       'tokens': tokens, 'limit': limit, 'total_token_usage': info.get('total_token_usage')}
            state['usage'] = (context if info else previous[0], windows if rates else previous[1], entry.get('timestamp'))
            if rates:
                state['credits'] = rates.get('credits')
        if kind == 'task_started':
            state['busy'] = True
        elif kind in ('task_complete', 'turn_aborted'):
            state['busy'] = False
    if entry.get('type') == 'compacted' or (entry.get('type') == 'response_item' and kind == 'compaction'):
        messages.append(activity.notice(index, entry.get('timestamp'), 'Context compacted.'))
        return
    if entry.get('type') == 'event_msg' and kind == 'turn_aborted':
        messages.append(activity.notice(index, entry.get('timestamp'), 'Turn ' + str(payload.get('reason') or 'aborted') + '.'))
        return
    if entry.get('type') == 'response_item' and kind in ('custom_tool_call', 'function_call'):
        lines = codex_action_lines(payload)
        item = activity.action(f'{index}.0', entry.get('timestamp'), '; '.join(lines))
        item['tool'] = tool_details.summary(payload)
        if entry.get('timestamp'):
            item['tool']['started'] = entry['timestamp']
        messages.append(item)
        return
    if entry.get('type') == 'response_item' and kind in ('function_call_output', 'custom_tool_call_output'):
        tool_details.mark_result(messages, payload.get('call_id'), payload, time=entry.get('timestamp'))
        return
    if entry.get('type') == 'response_item' and kind == 'agent_message':
        peer = dict(payload, role='assistant')
        for at, part in enumerate(codex_messages.parts(peer)):
            messages.append({'id': f'{index}.peer.{at}', 'role': 'assistant', 'kind': 'peer',
                             'title': 'Agent message', 'text': part['text'], 'time': entry.get('timestamp')})
        return
    if entry.get('type') != 'response_item' or kind != 'message':
        return
    role = payload.get('role')
    if role not in ('user', 'assistant'):
        return
    for at, part in enumerate(codex_messages.parts(payload)):
        explicit_user = part.pop('_user', False)
        text = continuations.unwrap(part['text']) if explicit_user else display_text(part['text'])
        if not text.strip() or (not explicit_user and text.startswith('# AGENTS.md instructions')):
            continue
        messages.append(dict(part, id=str(index) if at == 0 else f'{index}.{at}', text=text,
                             time=entry.get('timestamp'), phase=payload.get('phase')))


def claude_cwd(entry):
    """Where a Claude or Antigravity session is working now: its transcript records the folder, which changes as it moves around."""
    try:
        with open(entry['transcript'], 'rb') as handle:
            size = handle.seek(0, 2)
            window = 131072
            pattern = rb'"Cwd":"(?:\\")?(/[^"\\]+)' if entry.get('agent') == 'antigravity' else rb'"cwd":"((?:[^"\\]|\\.)*)"'
            while True:  # a long run of tool output can push the last folder record far from the end: widen until found
                handle.seek(max(0, size - window))
                found = re.findall(pattern, handle.read())
                if found or window >= size or window >= 33554432:
                    break
                window *= 4
        return json.loads('"' + found[-1].decode('utf-8', 'ignore') + '"') if found else None
    except (OSError, KeyError, ValueError):
        return None


def codex_cwd(entry):
    """Where a Codex session works now: each turn records its folder, so a /cd shows from the next turn on."""
    try:
        with open(entry['transcript'], 'rb') as handle:
            size = handle.seek(0, 2)
            window = 262144
            while True:  # long tool output can push the last turn record far from the end, so widen the search until it is found
                handle.seek(max(0, size - window))
                found = re.findall(rb'"type":"turn_context","payload":\{[^\n]*?"cwd":"((?:[^"\\]|\\.)*)"', handle.read())
                if found or window >= size or window >= 33554432:
                    break
                window *= 4
        return json.loads('"' + found[-1].decode('utf-8', 'ignore') + '"') if found else None
    except (OSError, KeyError, ValueError):
        return None


def antigravity_model(entry, usage):
    """agy's status report can be minutes behind a model change, but its history notes every change on the next message
    ("changed setting `Model Selection` from A to B"); when that is newer than the report, show B."""
    try:
        reported = (usage or {}).get('reported_at') or 0
        path = entry['transcript']
        if os.stat(path).st_mtime <= reported:
            return None
        with open(path, 'rb') as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - 262144))
            found = re.findall(rb'Model Selection` from .{1,120}? to (.{1,120}?)\.\s+No need', handle.read())
        return found[-1].decode('utf-8', 'ignore') if found else None
    except (OSError, KeyError, ValueError, TypeError):
        return None


def antigravity_cwd(entry):
    """Antigravity reports its folder with every status update; its transcript only sometimes records one."""
    try:
        reported = json.loads((paths.CACHE / 'antigravity' / (entry['thread'] + '.json')).read_text())
        return reported.get('cwd') or None
    except (OSError, KeyError, ValueError):
        return None


def agent_folder(entry):
    """Where the agent itself says it is working."""
    agent = entry.get('agent')
    found = (claude_cwd(entry) if agent in ('claude', 'antigravity') else codex_cwd(entry) if agent == 'codex' else None) \
        or (antigravity_cwd(entry) if agent == 'antigravity' else None)
    return found or entry.get('thread_cwd') or entry.get('cwd')


def tab_folder(entry):
    """The folder a tab shows: where the agent works, except that a `!cd` made in the tab sticks until the agent moves somewhere else
    (agents often snap back to their start folder after a command, which must not undo the user's cd)."""
    folder = agent_folder(entry)
    shell = shell_log.moved(entry['id'])
    if shell and shell[3] in (None, str(entry.get('process_start'))) and (shell[2] is None or shell[2] == folder):
        return shell[0]
    return folder


def edit_file_path(body):
    """Resolve editor paths relative to the tab's folder, as chat file reads do."""
    text = body.get('path')
    if isinstance(text, str) and text and not text.startswith(('/', '~')):
        session = body.get('session')
        folder = tab_folder(choose(session if isinstance(session, str) else None)) or str(paths.HOME)
        text = os.path.join(folder, text)
    return text


def home_path(path):
    """A folder as shown to people: <home>/x becomes ~/x."""
    if not path:
        return ''
    home = str(Path.home())
    return '~' + path[len(home):] if path == home or path.startswith(home + '/') else path


RATES_FILE = paths.CACHE / 'codex_rates.json'
LAST_RATES = [None]


def recent_rates(limit=6, tail=400_000):
    """The newest allowance report found in the most recent Codex history files, as (windows, reported_at)."""
    files = sorted((paths.CODEX / 'sessions').glob('**/rollout-*.jsonl'), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
    for path in files:
        try:
            with open(path, 'rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - tail))
                lines = stream.read().decode('utf-8', 'ignore').splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            rates = (entry.get('payload') or {}).get('rate_limits') or {}
            windows = [{'label': f"{value.get('window_minutes', 0) / 60:g}h", 'percent': value.get('used_percent'), 'resets_at': value.get('resets_at')}
                       for key in ('primary', 'secondary') if isinstance((value := rates.get(key)), dict)]
            if windows:
                return windows, entry.get('timestamp')
    return [], None


def usage_summary(now=None, live_age=None):
    """Account allowances for the sidebar: Claude from the newest status-line report, Codex from the newest rollout report.
    With live_age, a live Codex check no older than that many seconds replaces a saved report that is older."""
    now = now or time.time()
    claude = None
    claude_switched = saved_accounts.switched_at('claude')
    codex_switched = saved_accounts.switched_at('codex')
    from agents import METRICS
    try:
        files = sorted(METRICS.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)[:20]
    except OSError:
        files = []
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if claude_switched and (epoch_of(data.get('reported_at')) or 0) < claude_switched:
            continue
        rates = data.get('rate_limits') or {}
        windows = [{'label': {'five_hour': '5h', 'seven_day': '7d'}.get(name, name.replace('_', ' ')),
                    'percent': value.get('used_percentage'), 'resets_at': value.get('resets_at')}
                   for name, value in rates.items() if isinstance(value, dict) and value.get('used_percentage') is not None]
        if windows:
            claude = {'windows': windows, 'reported_at': data.get('reported_at')}
            break
    codex = None
    try:
        saved = json.loads(RATES_FILE.read_text())
        windows, reported = saved['windows'], saved.get('reported_at')
    except (OSError, ValueError, KeyError, TypeError):
        windows, reported = recent_rates()
    if windows and (not codex_switched or (epoch_of(reported) or 0) >= codex_switched):
        codex = {'windows': [dict(w, label={'168h': '7d'}.get(w.get('label'), w.get('label'))) for w in windows], 'reported_at': reported}
    errors = {}
    if live_age is not None:
        # Claude: a live check through Claude Code's SDK mode (no prompt, no cost). Runs beside the Codex check.
        claude_live = {}
        worker = threading.Thread(target=lambda: claude_live.update(claude_probe.check_usage(live_age)), daemon=True)
        worker.start()
        result = live_usage.check(live_age)
        worker.join(45)
        report = claude_live.get('report')
        if report and report['windows'] and report['reported_at'] >= claude_switched and (epoch_of((claude or {}).get('reported_at')) or 0) <= report['reported_at']:
            claude = dict(report, windows=[dict(window) for window in report['windows']])
        if claude_live.get('error'):
            errors['claude'] = claude_live['error']
        report = result.get('report')
        if report and report['windows'] and report['reported_at'] >= codex_switched and (epoch_of((codex or {}).get('reported_at')) or 0) <= report['reported_at']:
            codex = dict(report, windows=[dict(window) for window in report['windows']], source='live')
        if result.get('error'):
            errors['codex'] = result['error']
    if claude is None and claude_switched:
        for account in saved_accounts.snapshot()['claude']:
            if account.get('active') and account.get('windows'):
                claude = {'windows': [dict(window, resets_at=epoch_of(window.get('resets_at'))) for window in account['windows']],
                          'reported_at': claude_switched or time.time(), 'source': 'account'}
                break
    for report in (claude, codex):  # a window that has reset since the report is back at 0%
        for window in (report or {}).get('windows', []):
            if isinstance(window.get('resets_at'), (int, float)) and window['resets_at'] <= now:
                window.update(percent=0, resets_at=None)
    return {'claude': claude, 'codex': codex, 'accounts': accounts.summary(), 'live_errors': errors}


def epoch_of(value):
    if isinstance(value, (int, float)) or value is None:
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def fresh_codex_screen(screen):
    """True when the pane shows Codex's start banner and nothing said yet (a new session, or one just cleared with /clear)."""
    lines = [line.strip() for line in screen.splitlines()]
    if not any('OpenAI Codex' in line for line in lines):
        return False
    return not any(line.startswith('•') for line in lines) and sum(line.startswith('›') for line in lines) <= 1


def codex_metrics(config, reported_usage, model='', effort='', credits=None):
    """Rings for a Codex tab. Allowances are account-wide, so a tab that has not reported yet reuses the last report seen;
    the context figure falls back to the terminal footer ("Context 97% left") until the first turn is recorded."""
    context, windows, reported = reported_usage or ({}, [], None)
    inherited = False
    try:
        newest = json.loads(RATES_FILE.read_text())
        newest['windows']
    except (OSError, ValueError, KeyError, TypeError):
        newest = None
    if windows and newest and newest['windows'] and (epoch_of(newest.get('reported_at')) or 0) > (epoch_of(reported) or 0):
        # An idle tab still holds the report from its last turn, maybe days old. The newest report wins, for the saved
        # file and for this tab's allowance rings; the context ring stays this tab's own.
        windows, reported, inherited = newest['windows'], newest.get('reported_at'), True
    elif windows:
        saved = json.dumps({'windows': windows, 'reported_at': reported})
        if (str(RATES_FILE), saved) != LAST_RATES[0] or not RATES_FILE.exists():  # rewrite if the file moved or was deleted
            try:
                RATES_FILE.parent.mkdir(parents=True, exist_ok=True)
                RATES_FILE.write_text(saved)
                LAST_RATES[0] = (str(RATES_FILE), saved)
            except OSError:
                pass
    elif newest:
        windows, reported, inherited = newest['windows'], newest.get('reported_at'), True
    else:
        windows, reported = recent_rates()
        inherited = bool(windows)
    try:
        screen = capture(config)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        screen = ''
    shown = footer.codex_screen(screen)
    if fresh_codex_screen(screen):
        context = {}  # /clear leaves the previous rollout in place until the next turn.
    if not model or model == shown.get('model'):
        effort = effort or shown.get('effort', '')
    model = model or shown.get('model', '')
    if context.get('percent') is None:
        context = shown.get('context', context)
    if not windows:
        windows = shown.get('windows', [])
    report = usage(context, windows, reported)
    report.update(model=model, effort=effort, credits=credits if credits is not None else shown.get('credits'))
    if inherited:
        stamp = reported
        try:
            stamp = datetime.fromisoformat(str(reported).replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
        report.update(allowance_inherited=any(report[key] for key in ('five_hour', 'weekly')), allowance_reported_at=stamp)
    return report


NOTIFICATION_TAIL_BYTES = 256 * 1024


def notification_records(entry):
    """Read a bounded tail without tmux or history cache locks."""
    if entry.get('agent') not in ('claude', 'codex', 'antigravity') or entry.get('ambiguous'):
        return []
    try:
        with open(entry['transcript'], 'rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            start = max(0, size - NOTIFICATION_TAIL_BYTES)
            stream.seek(start)
            tail = stream.read(NOTIFICATION_TAIL_BYTES)
        if start:
            tail = tail.partition(b'\n')[2]  # discard a record clipped by the byte limit
        records = []
        for line in tail.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records
    except (OSError, KeyError, ValueError):
        return []


def notification_context(entry):
    records = notification_records(entry)
    return notification_reply(entry, records), records


def notification_reply(entry, records=None):
    """Use the chat parsers to extract the last reply from the bounded tail."""
    records = notification_records(entry) if records is None else records
    try:
        latest_step, latest_text = None, ''
        for index, record in reversed(list(enumerate(records))):
            if entry['agent'] == 'antigravity':
                message = antigravity_message(record, index, display_text)
                if message and message['role'] == 'assistant':
                    step = record.get('step_index')
                    step = step if isinstance(step, int) else index
                    if latest_step is None or step > latest_step:
                        latest_step, latest_text = step, message['text']
                continue  # the chat orders by step; the newest version of a step wins
            state = codex_state()
            if entry['agent'] == 'claude':
                claude_consume(state['messages'], index, record, display_text)
            else:
                codex_consume(state, index, record)
            for message in reversed(state['messages']):
                if message.get('role') == 'assistant' and not message.get('kind'):
                    return message['text']
        return latest_text
    except Exception:  # A missing, malformed or changing transcript must not break the poll loop.
        return ''


def codex_footer(config, metrics):
    result = footer.codex(metrics)
    goal = live_state.codex_goal(config)
    if goal:
        result.update(goal=goal['status'], goal_status=goal['status'])
    return result


def snapshot(config=None):
    config = config or settings()
    if config.get('agent') == 'claude':
        terminal_replies.capture_banner(config, lambda entry: capture_screen(entry, 2000))  # the banner scrolls away in a long session
        data = claude_snapshot(config, display_text)
        data = dict(data, messages=terminal_replies.merge(config, data['messages']))
        return dict(data, thread=config['thread'], agent='claude', writable=config['writable'], session=config['id'])
    if config.get('agent') == 'antigravity' and config.get('transcript'):
        terminal_replies.capture_banner(config, capture)
        data = antigravity_snapshot(config, display_text, records, usage)
        shown = antigravity_model(config, data.get('usage'))
        if shown:
            windows = [dict(w, active=window_active(w['label'], shown)) for w in data['usage']['windows']]
            data = dict(data, usage=dict(usage(data['usage']['context'], windows, data['usage']['reported_at']), model=shown))
        return dict(data, messages=terminal_replies.merge(config, data['messages'])) if isinstance(data.get('messages'), list) else data
    if config.get('agent') in ('shell', 'antigravity') or config.get('ambiguous'):
        return {'messages': [], 'thread': None, 'agent': config['agent'], 'busy': False,
                'writable': False, 'session': config['id'], 'usage': usage(),
                'unsupported': ('Multiple Codex sessions share this directory; their conversation mapping is ambiguous. Use Termius until each is mapped explicitly.'
                                if config.get('ambiguous') else ''
                                if config.get('agent') == 'shell' else 'This session does not expose a supported chat transcript. Use Termius for this session.')}
    terminal_replies.capture_banner(config, capture)
    path = Path(config['transcript'])
    metrics = usage()
    if not path.is_file():
        metrics = codex_metrics(config, None)
        return {'messages': terminal_replies.merge(config, merge_quota_notices(dict(config, agent='codex'), [])), 'busy': False, 'thread': config['thread'], 'usage': metrics, 'footer': codex_footer(config, metrics),
                'agent': 'codex', 'writable': config.get('writable', True), 'session': config.get('id')}
    state = history.read(path, codex_state, codex_consume)
    messages, busy = state['messages'], state['busy']
    try:
        cleared = fresh_codex_screen(capture(config))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        cleared = False
    if cleared:  # /clear starts a new conversation that has no history file yet; the old one must not show through
        messages, busy = [], False
    metrics = codex_metrics(config, state['usage'], state.get('model', ''), state.get('effort', ''), state.get('credits'))
    return {'messages': terminal_replies.merge(config, merge_quota_notices(dict(config, agent='codex'), activity.group_actions(activity.merge(list(messages))))), 'dropped': state['dropped'], 'busy': busy, 'thread': config['thread'], 'usage': metrics,
            'agent': 'codex', 'writable': config.get('writable', True), 'session': config.get('id'), 'footer': codex_footer(config, metrics)}


def last_reply(entry):
    """The most recent assistant text for a review request; missing history is harmless."""
    try:
        for message in reversed(snapshot(entry)['messages']):
            text = message.get('text')
            if message.get('role') == 'assistant' and isinstance(text, str) and text.strip():
                return text
    except Exception:
        pass
    return ''


def review_reply(entry, request_id):
    """The last assistant text in the turn anchored by this review request."""
    try:
        messages = snapshot(entry)['messages']
        marker = f'Review id: {request_id}.'
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            text = message.get('text')
            if message.get('role') == 'user' and isinstance(text, str) and marker in text:
                reply = ''
                for message in messages[index + 1:]:
                    if message.get('role') == 'user':
                        break
                    text = message.get('text')
                    if message.get('role') == 'assistant' and isinstance(text, str) and text.strip():
                        reply = text
                return reply
    except Exception:
        pass
    return None


def submit(message, config=None):
    config = config or settings()
    if not config.get('writable', True):
        raise RuntimeError('Messaging is unavailable for this session. Use Termius.')
    with SEND_LOCK:
        wrapped, marker = continuations.prepare(config, message)
        if marker:
            continuations.stage(config, wrapped, marker)
        terminal_submit(config, wrapped)
        if marker:
            continuations.sent(config, marker)


def send_text(config, message, state=None):
    """The chat composer's guarded send to one pane. Returns 'queued' when the agent was working (it takes typed input
    while busy; Tether keeps no queue of its own), else 'submitted'. state: a status the caller just read."""
    state = state or live_status(config)['state']
    if config['agent'] == 'codex' and re.match(r'/ls(\s|$)', message.strip()):
        message = '!' + message.strip()[1:]   # Codex has no /ls; "!ls" runs it in the shell
    wants_reply = (config['agent'] == 'codex' and state != 'Working'  # while it works, new lines are its answer, not the command's
                   and re.match(r'^/[a-z][a-z0-9_-]*(?:\s|$)', message.strip(), re.I) and message.strip() != '/model')
    before = capture(config) if wants_reply else ''
    note = '' if message.strip().startswith(('/', '!')) else shell_log.folder_note(config['id'], agent_folder(config))
    submit(note + message.strip(), config)
    if wants_reply:  # Codex answers its slash commands on the terminal only
        threading.Thread(target=terminal_replies.record, args=(config, before, capture), daemon=True).start()
    return 'queued' if state == 'Working' else 'submitted'


def review_send(config, message, state=None):
    """Deliver a review around a quiet Claude draft, preserving ordinary send guards."""
    config = dict(config, _review=True)
    stashed = stash_quiet_draft(config) if config.get('agent') == 'claude' else None
    try:
        result = send_text(config, message, state)
    except Exception as exc:
        try:
            if 'reached the agent input' in str(exc):
                terminal_clear_draft(config)
            else:
                clear_unsent(config, message)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
        if stashed:
            try:
                if not any(prompt_draft(config)):
                    verify(config)
                    subprocess.run([paths.TMUX, 'send-keys', '-t', config['id'], 'C-s'],
                                   check=True, capture_output=True, timeout=5)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                pass
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise RuntimeError('Could not deliver the review message.') from exc
        raise
    if stashed:
        restore_check(config, stashed)
    return result


def broadcast_message(body):
    """POST /mobile/api/broadcast: one guarded broker for every multi-pane send (the Agents page and `tether broadcast`)."""
    return broadcast.deliver(body.get('message'), body.get('panes'), via=body.get('via') or 'direct', master=body.get('master'), exclude=body.get('exclude'),
                             entries=sessions, status=live_status, send=send_text, cli=str(ROOT.parent / 'tether'))


DEFAULT_WINDOW = 150
MAX_WINDOW = 2000


def window_limit(query):
    try:
        return max(20, min(int(query.get('limit', [DEFAULT_WINDOW])[0]), MAX_WINDOW))
    except ValueError:
        return DEFAULT_WINDOW


def with_version(payload):
    """A frozen copy of the chat payload plus `version`, a short hash of it. A client that already shows this version
    passes it to the stream as `have`, and the stream answers `event: same` instead of sending the payload again (B63).
    The payload is serialized once: cached message objects can change in place while another thread reads them,
    so the version, the body sent and the delta base (B66) must all come from the same snapshot."""
    text = json.dumps(payload)
    frozen = json.loads(text)
    frozen['version'] = hashlib.sha1(text.encode()).hexdigest()[:20]
    return frozen


def chat_delta(old, new):
    """What changed from payload `old` to payload `new`, both with versions, for a stream that opted in (B66).
    The client applies it only to the screen that shows version `base`:
      set: top-level keys with a new value; unset: keys that went away;
      messages: new list = old[drop:drop + keep] + tail (absent when the list did not change).
    Values compare as JSON text: Python's == treats True and 1 as equal, the page does not."""
    delta = {'base': old['version'], 'version': new['version'], 'set': {}, 'unset': [key for key in old if key not in new]}
    before, after = old.get('messages'), new.get('messages')
    lists = isinstance(before, list) and isinstance(after, list)
    for key, value in new.items():
        if key == 'version' or (key == 'messages' and lists):
            continue
        if key not in old or json.dumps(old[key]) != json.dumps(value):
            delta['set'][key] = value
    if lists:
        was, now = [json.dumps(message) for message in before], [json.dumps(message) for message in after]
        if was != now:
            # The window slides: find where the new list starts in the old one, then how far the two agree.
            drop = was.index(now[0]) if now and now[0] in was else len(was)
            keep = 0
            while keep < len(now) and drop + keep < len(was) and was[drop + keep] == now[keep]:
                keep += 1
            delta['messages'] = {'drop': drop, 'keep': keep, 'tail': after[keep:]}
    return delta


def chat_payload(config, shell_mode=False, limit=DEFAULT_WINDOW, preview=False):
    """One chat view: the latest `limit` messages plus how many older ones were left out."""
    before = change_signature(config) if preview else None
    shell = config['agent'] == 'shell' and shell_mode
    state = {'state': 'Shell', 'approval': None} if shell else auto_resume.overlay(config, live_status(config))
    data = shell_snapshot(config) if shell else snapshot(config)
    if not shell and isinstance(data.get('messages'), list):
        data = dict(data, messages=shell_log.merge(config['id'], data['messages']))
    if data.get('busy') and state['state'] == 'Idle':
        state['state'] = 'Working'
    if not (config['agent'] == 'claude' and plan_menu(state.get('approval') or state.get('attention'))):
        data.pop('plan', None)
    ask = data.pop('ask', None) if isinstance(data, dict) else None
    if ask and state.get('attention') and not state['attention'].get('options'):
        # The screen shows only the bottom of a tall question: show the whole question from the transcript.
        state = dict(state, attention=dict(state['attention'], ask=ask))
    data = continuations.decorate(config, data)
    messages = data.get('messages')
    earlier = max(0, len(messages) - limit) if isinstance(messages, list) else 0
    dropped = data.pop('dropped', 0) if isinstance(data, dict) else 0
    if earlier:
        data = dict(data, messages=messages[-limit:])
    result = dict(data, earlier=earlier + dropped, limit=limit, max_limit=MAX_WINDOW, started=thread_started.started(config), **state)
    if config['agent'] == 'claude' and state['state'] == 'Working':
        text = None
        try:
            screen, width = reply_preview.sample(config)
            bang = bang_run.extract(screen)
            if bang is not None:
                result['bang'] = bang
            if preview:
                text = reply_preview.extract(screen, width, messages or [])
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            text = None
        # Do not pair an old transcript snapshot with a block that just committed.
        if text and before == change_signature(config):
            result['preview'] = {'text': text}
    return result


def find_messages(messages, query, limit=60):
    """Messages whose text holds every word of the query (any case), newest first. `from_end` is the chat window
    needed to show the message (1 = the newest), so the UI can load it and scroll to it."""
    words = query.lower().split()
    if not words or not isinstance(messages, list):
        return {'matches': [], 'total': 0}
    found = []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        text = str(message.get('text') or '')
        lower = text.lower()
        if all(word in lower for word in words):
            at = lower.find(words[0])
            start = max(0, at - 60)
            snippet = ' '.join(text[start:at + len(words[0]) + 120].split())
            found.append({'id': message.get('id'), 'role': message.get('role'), 'kind': message.get('kind'), 'time': message.get('time'),
                          'from_end': len(messages) - index, 'snippet': ('…' if start else '') + snippet})
    return {'matches': found[:limit], 'total': len(found)}


def search_payload(config, query, shell_mode=False):
    shell = config['agent'] == 'shell' and shell_mode
    data = shell_snapshot(config) if shell else snapshot(config)
    messages = data.get('messages')
    if not shell and isinstance(messages, list):
        messages = shell_log.merge(config['id'], messages)
    return dict(find_messages(messages, query), max_limit=MAX_WINDOW)


def change_signature(config):
    """Cheap fingerprint of everything a chat view is built from: history file and agent status file; no tmux subprocess."""
    parts = []
    for path in (config.get('transcript'), config.get('registry')):
        try:
            info = os.stat(path)
            parts.append((info.st_ino, info.st_mtime_ns, info.st_size))
        except (OSError, TypeError):
            parts.append(None)
    return tuple(parts)


CHAT_UI_ROOT = Path(os.environ.get('TMUX_CHAT_DIST', str(Path(__file__).resolve().parent.parent / 'ui' / 'dist')))
CHAT_UI_TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png',
                 '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
                 '.wasm': 'application/wasm', '.mjs': 'text/javascript', '.onnx': 'application/octet-stream'}
CHAT_UI_TEXT_TYPES = {'.html', '.js', '.mjs', '.css', '.svg', '.json', '.webmanifest'}
CHAT_UI_GZIP_CACHE = {}
CHAT_UI_GZIP_LOCK = threading.Lock()
CHAT_UI_GZIP_LIMIT = 64 * 1024 * 1024
# Text bodies of at least GZIP_MIN bytes go out gzip-compressed when the client accepts it:
# API JSON is 4-6x smaller, which matters on a phone link. Only same-origin requests get gzip
# (Sec-Fetch-Site same-origin or none, or no header from curl and CLI clients). Tailscale-identity
# and proxy sign-in work without the SameSite=Strict cookie, so a cross-site page could otherwise
# send signed-in requests that reflect its input (files/search q) and time compressed sizes (BREACH).
# 'same-site' is not trusted: sibling hosts under the tailnet domain are other machines.
GZIP_SITES = (None, 'same-origin', 'none')
GZIP_TYPES = {'application/json', 'application/manifest+json', 'image/svg+xml',
              'text/html', 'text/javascript', 'text/css', 'text/plain', 'text/markdown'}
GZIP_MIN = 1024


def accepts_gzip(header, coding='gzip'):
    """True when an Accept-Encoding header allows `coding` (gzip by default), by name or through '*', with q > 0."""
    encodings = {}
    for token in (header or '').split(','):
        encoding, *parameters = token.split(';')
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition('=')
            if name.lower() == 'q':
                try:
                    quality = float(value) if separator else 0.0
                except ValueError:
                    quality = 0.0
        encodings[encoding.strip().lower()] = quality
    return 0 < encodings.get(coding, encodings.get('*', 0)) <= 1


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # a client that stalls mid-request must not hold a thread forever

    def respond(self, status, body, content_type='application/json', cache='no-store', headers=None):
        data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        if (self.command in ('POST', 'DELETE') and status < 300 and self.path.split('?', 1)[0] in TMUX_WRITES
                and not getattr(self, 'keep_sessions', False)):
            forget_sessions()
        headers = dict(headers or {})
        if content_type in GZIP_TYPES and 'Content-Encoding' not in headers and status not in (204, 304):
            headers.setdefault('Vary', 'Accept-Encoding, Sec-Fetch-Site')
            request = getattr(self, 'headers', None) or {}
            if (len(data) >= GZIP_MIN and request.get('Sec-Fetch-Site') in GZIP_SITES
                    and accepts_gzip(request.get('Accept-Encoding'))):
                data = gzip.compress(data, compresslevel=6, mtime=0)
                headers['Content-Encoding'] = 'gzip'
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        if status != 304:  # a 304 may only repeat the 200's length (RFC 9110 8.6), so it sends none
            self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, kind):
        """A media file with Range support, so a phone video can seek without loading the whole file."""
        size = path.stat().st_size
        try:
            span = media_files.byte_range(self.headers.get('Range'), size)
        except ValueError:
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % size)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        start, end = span or (0, size - 1)
        self.send_response(206 if span else 200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('Accept-Ranges', 'bytes')
        if span:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Cache-Control', 'private, no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Disposition', files.disposition(path.name) if kind == media_files.DOWNLOAD else 'inline')
        if kind == media_files.TEXT:
            self.send_header('Content-Security-Policy', 'sandbox')
        self.end_headers()
        with open(path, 'rb') as stream:
            stream.seek(start)
            left = end - start + 1
            try:
                while left > 0 and (chunk := stream.read(min(left, 262144))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):  # the player stopped or seeked
                pass

    def stream_chat(self, identifier, shell_mode, limit=DEFAULT_WINDOW, lifetime=600, preview=False, have='', deltas=False):
        """Server-sent events: push a fresh chat payload whenever something the chat view depends on changes.
        have: the version the client already shows. When the first payload has that version, send `event: same`.
        deltas: after the first event, send `event: delta` (see chat_delta) when it is shorter than the payload."""
        config = choose(identifier)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        # One gzip stream per connection, flushed after each event: repeated keys and messages compress
        # well across events, and a sync flush hands each event to the client at once (B62).
        packer = None
        if accepts_gzip(self.headers.get('Accept-Encoding')) and self.headers.get('Sec-Fetch-Site') in GZIP_SITES:
            packer = zlib.compressobj(6, zlib.DEFLATED, 31)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding, Sec-Fetch-Site')
        self.end_headers()
        self.close_connection = True

        def send(data):
            self.wfile.write(packer.compress(data) + packer.flush(zlib.Z_SYNC_FLUSH) if packer else data)
        watched = config.get('id')
        with WATCHING_LOCK:
            WATCHING[watched] = WATCHING.get(watched, 0) + 1
        started = discovered = full = wrote = time.monotonic()
        signature = last_body = last_payload = screen = None
        sampled = 0.0
        working = False
        try:
            send(b'retry: 2000\n\n')
            while time.monotonic() - started < lifetime:
                now = time.monotonic()
                try:
                    if now - discovered >= 2:
                        config, discovered = choose(identifier), now
                    current = change_signature(config)
                    screen_changed = False
                    if now - sampled >= (.3 if working else 1):
                        latest = reply_preview.sample(config)[0] if preview and working and config['agent'] == 'claude' else capture(config)
                        screen_changed, screen, sampled = latest != screen, latest, now
                    if current != signature or screen_changed or now - full >= 5:
                        signature, full = current, now
                        payload = chat_payload(config, shell_mode, limit, preview=True) if preview else chat_payload(config, shell_mode, limit)
                        working = payload.get('state') == 'Working'
                        payload = with_version(payload)  # one frozen snapshot: version, body and delta base agree
                        body = json.dumps(payload)
                        if body != last_body:
                            sent = payload if deltas else None
                            if last_body is None and have and payload['version'] == have:
                                event = 'event: same\ndata: ' + json.dumps({'version': have})
                            else:
                                event = 'data: ' + body
                                if last_payload is not None:
                                    change = json.dumps(chat_delta(last_payload, sent))
                                    if len(change) < len(body):
                                        event = 'event: delta\ndata: ' + change
                            last_body, last_payload = body, sent
                            send((event + '\n\n').encode())
                            wrote = now
                except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
                    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                        raise
                    send(b'event: unavailable\ndata: {}\n\n')
                    wrote = now
                    time.sleep(1)
                if now - wrote >= KEEPALIVE:  # a named event: the page counts it as proof the stream is alive (comments are invisible to it)
                    send(b'event: idle\ndata: {}\n\n')
                    wrote = now
                self.wfile.flush()
                time.sleep(.1)  # stat is cheap; pane reads have a separate bounded cadence
            if packer:  # end the gzip stream so the client sees a clean close, not a cut one
                self.wfile.write(packer.flush())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        finally:
            with WATCHING_LOCK:
                WATCHING[watched] = max(0, WATCHING.get(watched, 1) - 1)

    def deny(self):
        """Answer and return True when the request is not signed in."""
        if auth.signed_in(self.headers.get('Cookie')) or auth.tailscale_signed_in(self.headers.get('Tailscale-User-Login'), self.client_address[0]):
            return False
        if self.command == 'GET' and not self.path.startswith('/mobile/api/'):
            self.send_response(302)
            self.send_header('Location', '/mobile/login')
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.respond(401, {'error': 'Sign in required.'})
        return True

    def login(self, error=''):
        self.respond(200 if not error else 401, auth.LOGIN_PAGE.replace('__ERROR__', error), 'text/html')

    def do_login(self):
        if not auth.same_origin(self.headers.get('Origin', ''), self.headers.get('Host', '')):
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0') or 0)
        except ValueError:
            size = 0
        if not 0 < size <= 1024:
            self.respond(400, {'error': 'Invalid request.'})
            return
        candidate = parse_qs(self.rfile.read(size).decode('utf-8', 'ignore')).get('token', [''])[0]
        outcome = auth.attempt(auth.client_id(self.client_address[0], self.headers.get('X-Forwarded-For')), candidate)
        if outcome == 'ok':
            secure = self.headers.get('X-Forwarded-Proto') == 'https' or self.headers.get('Origin', '').startswith('https://')
            self.send_response(303)
            self.send_header('Set-Cookie', auth.cookie_header(secure))
            self.send_header('Location', '/chat/')  # the tmux-chat UI is the default; the old UI stays at /mobile/chat/
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.login('Too many attempts. Try again in a few minutes.' if outcome == 'blocked' else 'That token is not right.')

    def proxy_terminal(self):
        """Pass /terminal/live/ (ttyd, the full interactive terminal) through to the local ttyd, WebSocket included, after sign-in."""
        origin = self.headers.get('Origin')
        if origin and not auth.same_origin(origin, self.headers.get('Host', '')):
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        try:
            upstream = socket.create_connection(('127.0.0.1', TTYD_PORT), timeout=5)
        except OSError:
            self.respond(503, {'error': 'The live terminal is not running. Start ttyd (see the README) to use Interact.'})
            return
        upgrading = 'upgrade' in self.headers.get('Connection', '').lower()
        lines = ['%s %s HTTP/1.1' % (self.command, self.path)]
        lines += ['%s: %s' % (key, value) for key, value in self.headers.items()
                  if key.lower() not in ('cookie', 'connection') or (key.lower() == 'connection' and upgrading)]
        if not upgrading:
            lines.append('Connection: close')
        self.pipe_upstream(upstream, lines)

    def pipe_upstream(self, upstream, lines, body=False):
        """Relay an HTTP request and then pipe bytes, including WebSocket frames."""
        with upstream:
            upstream.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
            if body:
                # Headers may prefetch body bytes into rfile. Drain them before raw recv.
                try:
                    if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                        while True:
                            line = self.rfile.readline(8192)
                            size = int(line.split(b';', 1)[0], 16)
                            if size < 0 or not line.endswith(b'\r\n'):
                                return
                            upstream.sendall(line)
                            if size == 0:
                                while True:
                                    line = self.rfile.readline(8192)
                                    if not line or not line.endswith(b'\r\n'):
                                        return
                                    upstream.sendall(line)
                                    if line == b'\r\n':
                                        break
                                break
                            left = size + 2
                            while left:
                                data = self.rfile.read(min(left, 65536))
                                if not data:
                                    return
                                upstream.sendall(data)
                                left -= len(data)
                    else:
                        left = int(self.headers.get('Content-Length', '0'))
                        if left < 0:
                            return
                        while left:
                            data = self.rfile.read(min(left, 65536))
                            if not data:
                                return
                            upstream.sendall(data)
                            left -= len(data)
                except (ValueError, OSError):
                    return
            upstream.settimeout(None)
            pair = {self.connection: upstream, upstream: self.connection}
            try:
                while True:
                    ready, _, _ = select.select(list(pair), [], [], 600)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        pair[source].sendall(data)
            except OSError:
                return

    def proxy_office(self):
        config = office_addon.load()
        if config is None:
            self.respond(404, {'error': 'The Office editor add-on is not installed.', 'code': 'office-off'})
            return
        origin = self.headers.get('Origin')
        if origin and not auth.same_origin(origin, self.headers.get('Host', '')):
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        target = urlsplit(config['url'])
        try:
            upstream = socket.create_connection((target.hostname, target.port), timeout=5)
        except OSError:
            self.respond(503, {'error': 'The Office editor is not running.'})
            return
        lines = office_addon.office_upstream_head(self.command, self.path, self.headers, self.client_address[0])
        self.pipe_upstream(upstream, lines, body=True)

    def serve_chat_ui(self, path):
        """Serve the Tether UI from CHAT_UI_ROOT; API requests still need the sign-in cookie."""
        root = CHAT_UI_ROOT.resolve()
        target = (root / unquote(path[len('/chat/'):])).resolve()
        if root not in target.parents and target != root:
            self.respond(404, {'error': 'Not found'})
            return
        if not target.is_file():
            target = root / 'index.html'  # single-page app: unknown paths load the app
        if not target.is_file():
            self.respond(404, {'error': 'The chat UI is not built yet.'})
            return
        kind = CHAT_UI_TYPES.get(target.suffix, 'application/octet-stream')
        cache = 'max-age=31536000, immutable' if target.parent.name == 'assets' else 'no-cache'
        headers = {'Vary': 'Accept-Encoding'} if target.suffix in CHAT_UI_TEXT_TYPES else {}
        accept = self.headers.get('Accept-Encoding')
        brotli = target.with_name(target.name + '.br')
        # B26: the build writes .br files next to hashed assets (vite.config.ts). Brotli is about 20% smaller than
        # gzip for JS. Only hashed assets: the .br file is made from the same build, so it cannot go stale.
        if (target.suffix in CHAT_UI_TEXT_TYPES and target.parent.name == 'assets' and brotli.is_file()
                and accepts_gzip(accept, 'br')):
            data = brotli.read_bytes()
            headers['Content-Encoding'] = 'br'
        elif (target.suffix in CHAT_UI_TEXT_TYPES and accepts_gzip(accept)
                and target.stat().st_size >= GZIP_MIN):
            with CHAT_UI_GZIP_LOCK:
                info = target.stat()
                key = (target.resolve(), info.st_mtime_ns, info.st_size)
                data = CHAT_UI_GZIP_CACHE.get(key)
                if data is None:
                    data = gzip.compress(target.read_bytes(), compresslevel=6, mtime=0)
                    if sum(map(len, CHAT_UI_GZIP_CACHE.values())) + len(data) > CHAT_UI_GZIP_LIMIT:
                        CHAT_UI_GZIP_CACHE.clear()
                    if len(data) <= CHAT_UI_GZIP_LIMIT:
                        CHAT_UI_GZIP_CACHE[key] = data
            headers['Content-Encoding'] = 'gzip'
        else:
            data = target.read_bytes()
        self.respond(200, data, kind, cache, headers=headers)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == mac_browser.PREFIX + 'connect':
            mac_browser.upgrade(self)
            return
        if path in ('/chat', '/', '/mobile', '/mobile/'):  # old home-screen icons open /mobile/: send them to the new UI
            self.send_response(301)
            self.send_header('Location', '/chat/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path.startswith('/chat/'):
            self.serve_chat_ui(path)
            return
        if path.startswith('/terminal/live/'):
            if not self.deny():
                self.proxy_terminal()
            return
        if path == '/mobile/login':
            if auth.tailscale_signed_in(self.headers.get('Tailscale-User-Login'), self.client_address[0]):
                self.send_response(302)  # already signed in by Tailscale: skip the token page
                self.send_header('Location', '/chat/')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                self.login()
            return
        if path not in ('/mobile/manifest.webmanifest', '/mobile/icon-192.png', '/mobile/icon-512.png', '/mobile/apple-touch-icon.png') and self.deny():
            return
        try:
            if path in ('/mobile/terminal/', '/mobile/terminal'):
                self.respond(200, TERMINAL_ASSETS['html'], 'text/html')
            elif path == '/mobile/termius.js':
                self.respond(200, TERMINAL_ASSETS['js'], 'text/javascript')
            elif path in ('/mobile/chat/', '/mobile/chat'):
                self.respond(200, UI_ASSETS['index.html'], 'text/html')
            elif path == '/mobile/app.js':
                self.respond(200, UI_ASSETS['app.js'], 'text/javascript')
            elif path.startswith('/mobile/vendor/') and '/mobile/' + path.rsplit('/', 1)[1] in STATIC_ASSETS:
                self.respond(200, STATIC_ASSETS['/mobile/' + path.rsplit('/', 1)[1]], 'text/javascript', 'max-age=86400')
            elif path == '/mobile/sw.js':  # no cache: browsers must see service-worker updates
                self.respond(200, STATIC_ASSETS[path], 'text/javascript')
            elif path == '/mobile/manifest.webmanifest':
                self.respond(200, STATIC_ASSETS[path], 'application/manifest+json', 'max-age=86400')
            elif path in ('/mobile/icon-192.png', '/mobile/icon-512.png', '/mobile/apple-touch-icon.png'):
                self.respond(200, STATIC_ASSETS[path], 'image/png', 'max-age=86400')
            elif path.startswith('/mobile/api/icon/') and path.endswith('.svg'):
                svg = pane_icons.read(path[len('/mobile/api/icon/'):-4])
                self.respond(200, svg, 'image/svg+xml', 'max-age=300') if svg else self.respond(404, {'error': 'No such icon.'})
            elif path == '/mobile/api/shortcuts':
                self.respond(200, {'shortcuts': shortcuts.load()})
            elif path == '/mobile/api/prefs':
                self.respond(200, prefs_sync.load())
            elif path == '/mobile/api/auto-resume':
                self.respond(200, auto_resume.notice())
            elif path == '/mobile/api/codex-provider':
                self.respond(200, codex_provider.status())
            elif path == '/mobile/api/update': self.respond(200, updates.payload())
            elif path == '/mobile/api/host':
                self.respond(200, {'name': socket.gethostname().split('.')[0], 'resources': host_stats.enabled(), 'terminal': TTYD_PORT > 0, 'office': office_addon.enabled()})
            elif path == '/mobile/api/office/copy':
                if not office_addon.enabled():
                    self.respond(404, {'error': 'The Office editor add-on is not installed.', 'code': 'office-off'})
                else:
                    key = parse_qs(urlsplit(self.path).query).get('key', [''])[0]
                    self.respond(200, {'copy': office_addon.copy_for(key)})
            elif path == '/mobile/api/host-stats':
                result = host_stats.payload()
                self.respond(200, result) if result is not None else self.respond(404, {'error': 'Host resources are disabled.'})
            elif path == '/mobile/api/foundry-spend':
                self.respond(200, foundry_spend.report(codex_provider.status()['model']))
            elif path in ('/mobile/api/catalog', '/mobile/api/files'):
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                folder = tab_folder(config)
                if path == '/mobile/api/catalog':
                    self.respond(200, catalog.catalog(config['agent'], folder, config.get('transcript')))
                else:
                    self.respond(200, {'files': catalog.find_files(folder, query.get('q', [''])[0][:200])})
            elif path == '/mobile/api/footer':
                config = choose(parse_qs(urlsplit(self.path).query).get('session', [None])[0])
                screen_read_at = time.time()  # before capture: hooks during the read still win
                screen = capture(config, styled=True) if config['agent'] == 'claude' else ''
                result = footer.parse(screen) if config['agent'] == 'claude' else {}
                result = claude_hooks.overlay(config, result, screen_read_at=screen_read_at)
                if config['agent'] == 'codex' and not config.get('ambiguous'):
                    state = history.read(config['transcript'], codex_state, codex_consume)
                    result = codex_footer(config, codex_metrics(config, state['usage'], state['model'], state.get('effort', ''), state.get('credits')))
                    live = claude_hooks.codex_live(config)
                    if live is not None:
                        result.update(spinner=None, running=None)
                        if live['busy'] and not live['waiting']:
                            now = time.time()
                            result['spinner'] = {'verb': 'Working…', 'detail': claude_hooks.elapsed(live['started'], now)}
                            if live['tool']:
                                tool = live['tool']
                                result['running'] = {'title': tool['title'], 'command': tool['command'],
                                                     'elapsed': claude_hooks.elapsed(tool['started'], now)}
                if config['agent'] == 'claude' and not config.get('ambiguous'):
                    result['heads_up'] = footer.card_note(result.get('heads_up'), config.get('thread'), screen=screen)
                    result['model'] = agents.claude_model(config.get('thread'))
                self.respond(200, result)
            elif path == '/mobile/api/shell-output':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                try:
                    self.respond(200, live_state.shell_output(config.get('thread') if config['agent'] == 'claude' else None, query.get('task', [None])[0]))
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
            elif path == '/mobile/api/live':
                config = choose(parse_qs(urlsplit(self.path).query).get('session', [None])[0])
                self.respond(200, live_state.for_thread(config.get('thread')) if config['agent'] == 'claude' else {'goal': live_state.codex_goal(config), 'tasks': []})
            elif path == '/mobile/api/accounts':
                try:
                    self.respond(200, saved_accounts.snapshot())
                except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    self.respond(502, {'error': str(exc) if isinstance(exc, RuntimeError) else 'Could not read saved accounts.'})
            elif path == '/mobile/api/login/state':
                try:
                    self.respond(200, login_flow.runner.state(parse_qs(urlsplit(self.path).query).get('agent', [None])[0]))
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
            elif path == '/mobile/api/usage':
                # Only the sheet's Refresh starts a Codex check (at most one per 30 s); the 60 s sidebar poll reuses its result.
                self.respond(200, usage_summary(live_age=30 if parse_qs(urlsplit(self.path).query).get('live') == ['1'] else float('inf')))
            elif path == '/mobile/api/history/hidden':
                hidden = chat_hidden.load()
                index = history.all_chats()
                by_key = {chat['agent'] + ':' + chat['id']: chat for chat in index}
                directories = {}
                for chat in index:
                    folder = home_path(chat['cwd'])
                    directories[folder] = directories.get(folder, 0) + 1
                dirs = [{'dir': folder, 'count': directories.get(folder, 0)} for folder, _ in
                        sorted(hidden['dirs'].items(), key=lambda item: item[1], reverse=True)]
                chats = []
                for key, _ in sorted(hidden['chats'].items(), key=lambda item: item[1], reverse=True):
                    chat = by_key.get(key)
                    if chat is not None:
                        chats.append(dict(id=chat['id'], agent=chat['agent'], title=chat['title'],
                                          dir=home_path(chat['cwd']) or None, time=chat['time']))
                    if len(chats) == 200:
                        break
                self.respond(200, {'temporary': hidden['temporary'],
                                   'temporaryCount': sum(chat_hidden.is_temporary(chat['cwd']) for chat in index),
                                   'dirs': dirs, 'chats': chats})
            elif path == '/mobile/api/history':
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                try:
                    limit = int(query.get('limit', [30])[0])
                    if not 1 <= limit <= 100:
                        raise ValueError('Invalid limit.')
                except ValueError:
                    self.respond(400, {'error': 'Use a limit from 1 to 100.'})
                    return
                try:
                    offset = int(query.get('offset', [0])[0])
                    if offset < 0:
                        raise ValueError('Invalid offset.')
                except ValueError:
                    self.respond(400, {'error': 'Use an offset of 0 or more.'})
                    return
                agent = query.get('agent', ['all'])[0]
                state = query.get('state', ['all'])[0]
                if agent not in ('all', 'claude', 'codex') or state not in ('all', 'open', 'closed'):
                    self.respond(400, {'error': 'Use agent all, claude, or codex and state all, open, or closed.'})
                    return
                live = {entry['thread']: entry['id'] for entry in sessions() if entry.get('thread')}
                index = history.all_chats()
                hidden = chat_hidden.load()
                hidden_count = (sum(chat['agent'] + ':' + chat['id'] in hidden['chats'] for chat in index)
                                + len(hidden['dirs']) + (hidden['temporary'] == 'hidden'
                                and any(chat_hidden.is_temporary(chat['cwd']) for chat in index)))
                index = [chat for chat in index if chat['id'] in live or not (
                    chat['agent'] + ':' + chat['id'] in hidden['chats']
                    or home_path(chat['cwd']) in hidden['dirs']
                    or hidden['temporary'] == 'hidden' and chat_hidden.is_temporary(chat['cwd']))]
                filters = dict(q=query.get('q', [''])[0][:200], state=state, live=live, display_dir=home_path)
                directory = query.get('dir', [None])[0]
                matches = history.search_chats(index, agent=agent, dir=directory, **filters)
                count_matches = history.search_chats(index, dir=directory, **filters)
                counts = {'all': len(count_matches),
                          'claude': sum(chat['agent'] == 'claude' for chat in count_matches),
                          'codex': sum(chat['agent'] == 'codex' for chat in count_matches)}
                directories = {}
                temporary_dirs = set()
                for chat in history.search_chats(index, agent=agent, **filters):
                    folder = home_path(chat['cwd'])
                    if folder:
                        directories[folder] = directories.get(folder, 0) + 1
                        if chat_hidden.is_temporary(chat['cwd']):
                            temporary_dirs.add(folder)
                dirs = [{'dir': folder, 'count': count, 'temporary': folder in temporary_dirs} for folder, count in
                        sorted(directories.items(), key=lambda item: (-item[1], item[0]))]
                chats = [dict(id=chat['id'], agent=chat['agent'], title=chat['title'],
                              dir=home_path(chat['cwd']) or None, time=chat['time'],
                              live=chat['id'] in live, pane=live.get(chat['id']))
                         for chat in matches[offset:offset + limit]]
                self.respond(200, {'chats': chats, 'total': len(matches),
                                   'next': offset + limit if offset + limit < len(matches) else None,
                                   'counts': counts, 'dirs': dirs, 'temporary': hidden['temporary'],
                                   'hiddenCount': hidden_count})
            elif path == '/mobile/api/push/config':
                self.respond(200, {'supported': push.available(), 'publicKey': push.vapid_keys()[1] if push.available() else None, 'muted': push.muted(), 'levels': push.pane_levels(), 'delivery': push.delivery_settings()})
            elif path == '/mobile/api/search':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                self.respond(200, search_payload(config, query.get('q', [''])[0][:200], query.get('mode') == ['shell']))
            elif path == '/mobile/api/file':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                try:
                    found, kind = media_files.resolve(query.get('path', [''])[0], tab_folder(config))
                except ValueError as exc:
                    self.respond(404, {'error': str(exc)})
                    return
                self.send_file(found, kind)
            elif path == '/mobile/api/worktree-info':
                query = parse_qs(urlsplit(self.path).query)
                try:
                    if 'session' in query:
                        folder = agent_folder(choose(query['session'][0]))
                        folder = folder if folder and os.path.isdir(folder) else str(paths.HOME)
                    else:
                        folder = prefs_sync.session_folder()
                    repo = worktrees.info(folder)
                except ValueError:
                    repo = None
                self.respond(200, {'repo': True, 'base': home_path(repo['base'])} if repo else {'repo': False})
            elif path in ('/mobile/api/repo/changes', '/mobile/api/repo/diff', '/mobile/api/repo/tree'):
                # The Changes and Files side panels. folder: the panel's folder (~/x); without it, the tab's own folder.
                query = parse_qs(urlsplit(self.path).query)
                folder = query.get('folder', [''])[0] or tab_folder(choose(query.get('session', [None])[0]))
                try:
                    if path == '/mobile/api/repo/changes':
                        self.respond(200, repo_panel.changes(folder))
                    elif path == '/mobile/api/repo/diff':
                        self.respond(200, repo_panel.diff(folder, query.get('repo', ['.'])[0], query.get('path', [''])[0]))
                    else:
                        self.respond(200, repo_panel.listing(folder, query.get('path', [''])[0]))
                except ValueError as exc:
                    self.respond(404, {'error': str(exc)})
            elif path.startswith(files.PREFIX):
                files.serve_get(self, path, files_sessions)
            elif path == '/mobile/api/tool-image':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                try:
                    kind, data = tool_details.image(config, query.get('id', [''])[0], int(query.get('n', ['0'])[0]))
                except ValueError as exc:
                    self.respond(404, {'error': str(exc)})
                    return
                # The image of a finished tool call never changes; the browser may keep it.
                self.respond(200, data, kind, 'private, max-age=86400')
            elif path == '/mobile/api/message-image':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                try:
                    kind, data = tool_details.message_image(config, query.get('key', [''])[0], int(query.get('n', ['0'])[0]))
                except ValueError as exc:
                    self.respond(404, {'error': str(exc)})
                    return
                self.respond(200, data, kind, 'private, max-age=86400')
            elif path == '/mobile/api/tool':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                self.respond(200, tool_details.lookup(config, query.get('id', [''])[0]))
            elif path == '/mobile/api/subagents':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                self.respond(200, subagents.listing(config))
            elif path == '/mobile/api/subagent':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                self.respond(200, subagents.detail(config, query.get('id', [''])[0]))
            elif path == '/mobile/api/subagent-task':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                self.respond(200, subagents.task(config, query.get('id', [''])[0]))
            elif path == '/mobile/api/subagent-tool':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                self.respond(200, subagents.tool(config, query.get('agent', [''])[0], query.get('id', [''])[0]))
            elif path == '/mobile/api/chat':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                if query.get('watch') == ['1']:
                    with WATCHING_LOCK:
                        WATCHED[config['id']] = time.monotonic()
                self.respond(200, with_version(chat_payload(config, query.get('mode') == ['shell'], window_limit(query), preview=query.get('preview') == ['1'] and query.get('watch') == ['1'])))
            elif path == '/mobile/api/screen':
                query = parse_qs(urlsplit(self.path).query)
                config = choose(query.get('session', [None])[0])
                try:
                    lines = int(query.get('lines', [80])[0])
                except ValueError:
                    lines = 80
                self.respond(200, {'text': capture_screen(config, lines), 'session': config['id'], 'agent': config['agent']})
            elif browser_api.handles(path):
                browser_api.get(self)
            elif projects_api.handles(path):
                projects_api.get(self)
            elif work_api.handles(path):
                work_api.get(self)
            elif groups_api.handles(path):
                groups_api.get(self)
            elif codex_level.handles(path):
                codex_level.get(self)
            elif autonomy.handles(path):
                autonomy.get(self)
            elif approval.handles(path):
                approval.get(self)
            elif custom_layouts.handles(path):
                custom_layouts.get(self)
            elif backgrounds.handles(path):
                backgrounds.get(self)
            elif codex_runs.handles(path):
                codex_runs.get(self, choose)
            elif tts.handles(path):
                tts.get(self)
            elif friday_proxy.handles(path):
                friday_proxy.get(self)
            elif live_mode.handles(path):
                live_mode.get(self, lambda: session_rows(sessions()))
            elif attention.handles(path):
                attention.get(self)
            elif path == '/mobile/api/stream':
                query = parse_qs(urlsplit(self.path).query)
                self.stream_chat(query.get('session', [None])[0], query.get('mode') == ['shell'], window_limit(query), preview=query.get('preview') == ['1'], have=query.get('have', [''])[0][:64],
                                 deltas=query.get('delta') == ['1'])
            elif path == '/mobile/api/sessions':
                body, tag = sessions_listing()
                headers = {'ETag': tag, 'Vary': 'Accept-Encoding, Sec-Fetch-Site'}
                if etag_matches(self.headers.get('If-None-Match'), tag):
                    self.respond(304, b'', cache='no-cache', headers=headers)
                else:
                    self.respond(200, body, cache='no-cache', headers=headers)
            elif path == '/mobile/api/side-shells':
                self.respond(200, {'shells': side_shells.list_shells()})
            elif path == '/mobile/api/tmux-sessions':
                result = subprocess.run([paths.TMUX, 'list-sessions', '-F', '#{session_id}\t#{session_name}\t#{session_windows}'],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode:
                    self.respond(200, {'sessions': []})
                else:
                    active = [line.split('\t', 2) for line in result.stdout.splitlines()]
                    self.respond(200, {'sessions': [{'id': item[0], 'name': item[1], 'windows': int(item[2])}
                                                     for item in active if len(item) == 3 and re.fullmatch(r'\$[0-9]+', item[0]) and item[1] != side_shells.SESSION]})
            else:
                self.respond(404, {'error': 'Not found'})
        except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            self.respond(503, {'error': 'The conversation is currently unavailable.'})

    def do_POST(self):
        if self.path in ('/mobile/api/office/open', '/mobile/api/files/save'):
            text_save = self.path == '/mobile/api/files/save'
            if self.deny():
                return
            if not auth.same_origin(self.headers.get('Origin', ''), self.headers.get('Host', '')) or self.headers.get('X-Codex-Mobile') != '1':
                self.respond(403, {'error': 'Reload the page and try again.'})
                return
            if not text_save and not office_addon.enabled():
                self.respond(404, {'error': 'The Office editor add-on is not installed.', 'code': 'office-off'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if text_save and size <= 0:
                    self.close_connection = True
                    raise files.invalid('Send a JSON body.')
                if not 0 < size <= (3 * 1024 * 1024 if text_save else 65536):
                    if text_save:
                        self.close_connection = True
                        raise files.FilesError(413, 'too-large', 'That request is too large.')
                    self.respond(413, {'error': 'Request is too large.'})
                    return
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or (not text_save and type(body.get('mobile', False)) is not bool):
                    raise ValueError('Invalid request.')
                text = edit_file_path(body)
                if text_save:
                    self.respond(200, files.save_text(text, body.get('text'), body.get('base')))
                else:
                    self.respond(200, office_addon.open_document(text, os.environ.get('USER', ''), body.get('mobile', False),
                                                               body.get('lang', 'en'), body.get('theme', 'dark')))
            except files.FilesError as exc:
                self.respond(exc.status, {'error': str(exc), 'code': exc.code})
            except ValueError:
                if text_save:
                    files.answer_error(self, files.invalid('Send a valid JSON object with path, text and base.'))
                else:
                    self.respond(400, {'error': 'Invalid request.'})
            except OSError as exc:
                error = files.disk_error(exc)
                self.respond(error.status, {'error': str(error), 'code': error.code})
            return
        if self.path == mac_browser.PREFIX + 'exchange':
            mac_browser.exchange(self)
            return
        if self.path == '/mobile/login':
            self.do_login()
            return
        if self.path.startswith(files.PREFIX):  # the Files explorer: same sign-in and CSRF checks as below
            if not self.deny():
                if auth.same_origin(self.headers.get('Origin', ''), self.headers.get('Host', '')) and self.headers.get('X-Codex-Mobile') == '1':
                    files.serve_post(self, self.path)
                else:
                    self.close_connection = True
                    self.respond(403, {'error': 'Reload the page and try again.'})
            return
        if not backgrounds.handles(self.path) and self.path not in ('/mobile/api/update', '/mobile/api/history/hide', '/mobile/api/file-check', '/mobile/api/ask-hook', '/mobile/api/ask-answer', '/mobile/api/ask-engage', '/mobile/api/message', '/mobile/api/broadcast', '/mobile/api/approval', '/mobile/api/menu', '/mobile/api/prompt-action', '/mobile/api/start', '/mobile/api/session', '/mobile/api/tmux-sessions', '/mobile/api/key', '/mobile/api/clear-draft', '/mobile/api/unqueue', '/mobile/api/mode', '/mobile/api/type',
                         '/mobile/api/push/subscribe', '/mobile/api/push/unsubscribe', '/mobile/api/push/test', '/mobile/api/push/mute', '/mobile/api/push/level', '/mobile/api/push/settings', '/mobile/api/shortcuts', '/mobile/api/prefs', '/mobile/api/codex-provider', '/mobile/api/task-models', '/mobile/api/model-options', '/mobile/api/model', '/mobile/api/subagent-stop', '/mobile/api/rename', '/mobile/api/merge', suggest_groups.PATH, '/mobile/api/shell', '/mobile/api/upload', '/mobile/api/transcribe', '/mobile/api/split', '/mobile/api/fork', '/mobile/api/delegate', '/mobile/api/agent-tab', '/mobile/api/continue-preview', '/mobile/api/continue', '/mobile/api/review-stop', '/mobile/api/review-stalled', '/mobile/api/review-link', '/mobile/api/resume', '/mobile/api/auto-resume', '/mobile/api/pane-name', '/mobile/api/pane-icon', '/mobile/api/pane-icon/draw', '/mobile/api/stop-task', '/mobile/api/side-shell', '/mobile/api/side-shell/end', '/mobile/api/login/start', '/mobile/api/login/code', '/mobile/api/login/cancel', '/mobile/api/accounts/refresh', '/mobile/api/accounts/switch') and not browser_api.handles(self.path) and not projects_api.handles(self.path) and not work_api.handles(self.path) and not groups_api.handles(self.path) and not codex_level.handles(self.path) and not autonomy.handles(self.path) and not approval.handles(self.path) and not custom_layouts.handles(self.path) and not tts.handles(self.path) and not voice_log.handles(self.path) and not friday_proxy.handles(self.path) and not live_mode.handles(self.path) and not attention.handles(self.path):
            self.respond(404, {'error': 'Not found'})
            return
        # Reject unauthenticated and cross-origin submissions.
        if self.deny():
            return
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if not auth.same_origin(origin, host) or self.headers.get('X-Codex-Mobile') != '1':
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        if browser_api.handles(self.path):
            browser_api.post(self)
            return
        if backgrounds.handles(self.path):
            backgrounds.post(self)
            return
        if projects_api.handles(self.path):
            projects_api.post(self)
            return
        if work_api.handles(self.path):
            work_api.post(self, sessions=sessions, status=shared_status, send=review_send, run=subprocess.run, lock=SEND_LOCK)
            return
        if groups_api.handles(self.path):
            groups_api.post(self)
            return
        if codex_level.handles(self.path):
            codex_level.post(self)
            return
        if autonomy.handles(self.path):
            autonomy.post(self)
            return
        if custom_layouts.handles(self.path):
            custom_layouts.post(self)
            return
        if tts.handles(self.path):
            tts.post(self)
            return
        if voice_log.handles(self.path):
            voice_log.post(self)
            return
        if friday_proxy.handles(self.path):
            friday_proxy.post(self)
            return
        if live_mode.handles(self.path):
            live_mode.post(self)
            return
        if self.path == suggest_groups.PATH:
            suggest_groups.post(self)
            return
        if self.path == '/mobile/api/task-models':
            self.respond(200, delegate.task_models())
            return
        if self.path == '/mobile/api/upload':
            self.do_upload()
            return
        if self.path == '/mobile/api/transcribe':
            self.do_transcribe()
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= (16384 if approval.handles(self.path) else 262144 if self.path in ('/mobile/api/shortcuts', '/mobile/api/prefs', '/mobile/api/delegate', '/mobile/api/agent-tab') else 65536):
                self.respond(413, {'error': 'Message is too large.'})
                return
            body = json.loads(self.rfile.read(size))
            message = body.get('message') if isinstance(body, dict) else None
            if not isinstance(body, dict):
                raise ValueError('Invalid request.')
            if approval.handles(self.path):
                approval.post(self, body)
                return
            if self.path == '/mobile/api/update':
                try:
                    self.respond(202, {'run': updates.start(body.get('version'))})
                except updates.UpdateRefused as error:
                    self.respond(error.status, {'error': str(error)})
                return
            if self.path == '/mobile/api/auto-resume':
                action = body.get('action')
                if action not in ('retry', 'dismiss'):
                    raise ValueError('Choose retry or dismiss.')
                count = auto_resume.retry() if action == 'retry' else auto_resume.dismiss()
                self.respond(200, {'count': count})
                return
            if self.path == '/mobile/api/history/hide':
                kind = body.get('kind')
                if kind == 'chat':
                    chat_hidden.set_chat(body.get('agent'), body.get('id'), body.get('hidden'))
                elif kind == 'dir':
                    chat_hidden.set_dir(body.get('dir'), body.get('hidden'))
                elif kind == 'temporary':
                    chat_hidden.set_temporary(body.get('mode'))
                else:
                    raise ValueError('Choose chat, dir, or temporary for kind.')
                self.respond(200, {'ok': True})
                return
            if self.path == agent_tabs.PATH:
                agent_tabs.serve(self, body, sessions=sessions, folder=agent_folder, snapshot=snapshot,
                                 last_reply=last_reply, run=subprocess.run, lock=SEND_LOCK,
                                 status=shared_status, send=review_send, timeline=timeline)
                return
            if self.path == '/mobile/api/file-check':
                names = body.get('paths')
                if not isinstance(names, list) or len(names) > 200:
                    raise ValueError('Paths must be a list of at most 200 items.')
                self.respond(200, {'results': media_files.openable(names, tab_folder(choose(body.get('session'))))})
                return
            if self.path == '/mobile/api/prompt-action':
                with SEND_LOCK:
                    status, result = prompt_actions.ledger.perform(body, choose)
                self.respond(status, result)
                return
            if ask_bridge.handles(self.path):
                ask_bridge.serve(self, body, choose)
                return
            if attention.handles(self.path):
                attention.serve(self, body, choose)
                return
            if self.path == '/mobile/api/message' and (not isinstance(message, str) or not message.strip()):
                self.respond(400, {'error': 'Enter a message first.'})
                return
            if self.path == '/mobile/api/broadcast':
                result = broadcast_message(body)
                for row in result.get('results') or []:
                    if row.get('result') in ('sent', 'sent_busy'):
                        attention.STORE.clear(row.get('pane'))  # you answered that tab
                self.respond(200, result)
                return
            if self.path == '/mobile/api/accounts/refresh':
                self.respond(200, saved_accounts.listing())
                return
            if self.path == '/mobile/api/accounts/switch':
                self.respond(200, saved_accounts.switch(body.get('agent'), body.get('number')))
                return
            if self.path.startswith('/mobile/api/login/'):
                action, agent = self.path.rsplit('/', 1)[1], body.get('agent')
                if action == 'start':
                    result = login_flow.runner.start(agent, body.get('method'), body.get('email'))
                elif action == 'code':
                    result = login_flow.runner.code(agent, body.get('code'))
                else:
                    result = login_flow.runner.cancel(agent)
                self.respond(200, result)
                return
            if self.path == '/mobile/api/prefs':
                try:
                    self.respond(200, prefs_sync.save(body.get('changes')))
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
                return
            if self.path == '/mobile/api/codex-provider':
                try:
                    self.respond(200, codex_provider.choose(body.get('provider')))
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
                return
            if self.path == '/mobile/api/shortcuts':
                try:
                    self.respond(200, {'shortcuts': shortcuts.save(body.get('shortcuts'))})
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
                return
            if self.path == '/mobile/api/stop-task':
                config = choose(body.get('session'))
                if config['agent'] != 'claude':
                    raise ValueError('Select a Claude conversation.')
                self.respond(200, live_state.stop_task(config.get('thread'), body.get('task')))
                return
            if self.path == '/mobile/api/shell':
                command = body.get('command')
                if not isinstance(command, str):
                    raise ValueError('Type a command after the !.')
                config = choose(body.get('session'))
                start = agent_folder(config)
                result = shell_log.run(config['id'], command, start, agent_folder(config), str(config.get('process_start')))
                self.respond(200, {'accepted': True, 'cwd': shell_log.short(shell_log.current(config['id'], start) or '')})
                return
            if self.path == '/mobile/api/rename':
                pane, name = body.get('pane'), body.get('name')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    self.respond(400, {'error': 'Select an active chat tab.'})
                    return
                if not valid_name(name):
                    self.respond(400, {'error': NAME_HELP})
                    return
                with SEND_LOCK:
                    result = subprocess.run([paths.TMUX, 'rename-session', '-t', pane, name], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    self.respond(409, {'error': 'That name is already used.' if 'duplicate' in result.stderr else 'Could not rename the session.'})
                    return
                with SEND_LOCK:
                    note = name_together(pane, name, from_session=True)
                self.respond(200, {'session': pane, 'name': name, **({'note': note} if note else {})})
                return
            if self.path == '/mobile/api/merge':  # sidebar drag and drop: merge sessions, move or break out a pane
                with SEND_LOCK:
                    moved = tmux_merge.merge(body.get('kind'), body.get('source'), body.get('target'), keep=body.get('keep'))
                self.respond(200, moved)
                return
            if self.path in ('/mobile/api/session', '/mobile/api/tmux-sessions'):
                try:
                    if self.path == '/mobile/api/tmux-sessions' and 'cwd' in body:
                        if 'worktree' in body:
                            raise ValueError('Use cwd or worktree, not both.')
                        session_folder = chat_folder(body['cwd'])
                    else:
                        session_folder = prefs_sync.session_folder()
                except ValueError as exc:
                    self.respond(400, {'error': str(exc)})
                    return
            if self.path == '/mobile/api/session':
                with SEND_LOCK:
                    pane = create_session(body.get('name'), session_folder)
                self.respond(200, {'session': pane, 'label': body['name']})
                return
            if self.path == '/mobile/api/tmux-sessions':
                if 'worktree' in body:
                    if not valid_name(body.get('name')):
                        raise ValueError(NAME_HELP)
                    taken = subprocess.run([paths.TMUX, 'has-session', '-t', '=' + body['name']],
                                           capture_output=True, text=True, timeout=5)
                    if taken.returncode == 0:
                        raise ValueError('A session with that name already exists.')
                made = worktrees.from_body(body, session_folder)
                with SEND_LOCK, worktrees.keeping(made):
                    pane = create_session(body.get('name'), made['path'] if made else session_folder)
                    if made:
                        subprocess.run([paths.TMUX, 'set-option', '-p', '-t', pane, '@worktree', made['branch']],
                                       check=True, capture_output=True, timeout=5)
                    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'],
                                            check=True, capture_output=True, text=True, timeout=5)
                self.respond(201, {'session': result.stdout.strip(), 'name': body['name'],
                                   **({'worktree': made} if made else {})})
                return
            if self.path == '/mobile/api/pane-name':
                pane, name = body.get('pane'), body.get('name')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    raise ValueError('Select a pane.')
                if not isinstance(name, str) or len(name.strip()) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    raise ValueError('Use up to 40 characters for the pane name.')
                name = name.strip()
                # The time lets the newest name win against a later /rename in Claude or Codex (agents.list_panes).
                command = (['set-option', '-p', '-t', pane, '@chat_name', name, ';', 'set-option', '-p', '-t', pane, '@chat_name_at', str(int(time.time() * 1000))]
                           if name else ['set-option', '-p', '-t', pane, '-u', '@chat_name', ';', 'set-option', '-p', '-t', pane, '-u', '@chat_name_at'])
                with SEND_LOCK:
                    result = subprocess.run([paths.TMUX, *command], capture_output=True, text=True, timeout=5)
                    if result.returncode:
                        raise RuntimeError('Could not rename the pane. It may have closed.')
                    name_in_tmux(pane, name)
                    note = name_together(pane, name, from_session=False)
                self.respond(200, {'pane': pane, 'name': name, **({'note': note} if note else {})})
                return
            if self.path == '/mobile/api/pane-icon/draw':
                # Claude draws a new icon (pane_icons.draw, a small model, about 10 s) and the pane wears it.
                pane, label, hint = body.get('pane'), body.get('label'), body.get('hint', '')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    raise ValueError('Select a pane.')
                if not isinstance(label, str) or not isinstance(hint, str) or len(hint) > 200:
                    raise ValueError('Use up to 200 characters to describe the icon.')
                icon = 'svg-' + pane_icons.draw(label, hint)
                with SEND_LOCK:
                    result = subprocess.run([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_icon', icon, ';',
                                             'set-option', '-p', '-t', pane, '@chat_icon_by', 'user'], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    raise RuntimeError('Could not set the icon. The pane may have closed.')
                forget_sessions()
                self.respond(200, {'pane': pane, 'icon': icon})
                return
            if self.path == '/mobile/api/pane-icon':
                # icon: a lucide icon name or svg-<name> (pane_icons.py); '' shows the agent's own icon again.
                pane, icon = body.get('pane'), body.get('icon')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    raise ValueError('Select a pane.')
                if not isinstance(icon, str) or (icon and not agents.ICON.fullmatch(icon)):
                    raise ValueError('Pick an icon from the list.')
                # @chat_icon_by tells the agent the person picked it, so it leaves the icon alone (projects.render).
                command = (['set-option', '-p', '-t', pane, '@chat_icon', icon, ';', 'set-option', '-p', '-t', pane, '@chat_icon_by', 'user'] if icon
                           else ['set-option', '-p', '-t', pane, '-u', '@chat_icon', ';', 'set-option', '-p', '-t', pane, '-u', '@chat_icon_by'])
                with SEND_LOCK:
                    result = subprocess.run([paths.TMUX, *command], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    raise RuntimeError('Could not set the icon. The pane may have closed.')
                forget_sessions()
                self.respond(200, {'pane': pane, 'icon': icon})
                return
            if self.path == '/mobile/api/side-shell':
                # A plain shell in the pane's folder, in Tether's own tmux session. pane: the shell the panel shows now.
                # folder: a Files explorer path ("Terminal here"); it must be a folder inside the files root, not a secret one.
                shell, new, folder = body.get('pane'), body.get('new') is True, body.get('folder')
                program = body.get('program')
                if program is not None and (program != 'btop' or not host_stats.enabled() or TTYD_PORT <= 0):
                    raise ValueError('Host terminal is unavailable.')
                if shell is not None and not (isinstance(shell, str) and side_shells.PANE.fullmatch(shell)):
                    raise ValueError('Select a shell.')
                folder = str(paths.HOME) if program else side_shells.folder_in_root(folder) if folder is not None else agent_folder(choose(body.get('session')))
                with SEND_LOCK:
                    pane = side_shells.open_shell(folder, shell, new, program=program, compact=body.get('compact') is True)
                self.respond(200, {'pane': pane})
                return
            if self.path == '/mobile/api/side-shell/end':
                with SEND_LOCK:
                    if 'pane' in body:
                        side_shells.end_shell(body.get('pane'))
                        response = {'ended': True}
                    else:
                        response = {'ended': True, 'panes': side_shells.end_shells(body.get('which'))}
                self.respond(200, response)
                return
            if self.path == '/mobile/api/split':
                pane, direction = body.get('session'), body.get('direction')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane) or direction not in ('right', 'down'):
                    raise ValueError('Select a pane and split right or down.')
                cwd = chat_folder(body['cwd']) if 'cwd' in body else None
                choose(pane)
                group = body.get('group')  # "+" in a group: "" = Unstacked, or a group id of the pane's session
                if group is not None:
                    try:
                        if group != '':
                            groups.find_in_session_of(pane, group)
                    except groups.GroupError as exc:
                        self.respond(exc.status, {'error': str(exc)})
                        return
                with SEND_LOCK:
                    new_pane = new_window_after(pane, subprocess.run, group=group,
                                                **({'cwd': cwd} if cwd is not None else {}))
                self.respond(201, {'pane': new_pane})
                return
            if self.path == '/mobile/api/review-link':
                with SEND_LOCK:
                    result = agent_tabs.link_pair(body, sessions(), last_reply)
                self.respond(200, result)
                return
            if self.path == '/mobile/api/review-stop':
                worker, reviewer = agent_tabs.review_pair(body)
                self.respond(200, {'stopped': review_links.remove(worker, reviewer)})
                return
            if self.path == '/mobile/api/review-stalled':
                reviewer = None
                if body.get('action') == 'latest':
                    reviewer = next((entry for entry in sessions() if entry['id'] == body.get('reviewer')), None)
                review_links.resume_stalled(body.get('worker'), body.get('reviewer'), body.get('id'),
                                            body.get('action'), last_reply=last_reply, reviewer_entry=reviewer)
                self.respond(200, {'ok': True})
                return
            if self.path in ('/mobile/api/continue-preview', '/mobile/api/continue'):
                identifier = body.get('session')
                if not isinstance(identifier, str) or not re.fullmatch(r'%[0-9]+', identifier):
                    raise ValueError('Select a chat to continue.')
                with SEND_LOCK:
                    entries = sessions()
                    entry = next((item for item in entries if item['id'] == identifier), None)
                    if entry is None:
                        raise ValueError('That session has ended.')
                    if body.get('thread') != entry.get('thread'):
                        raise ValueError('The conversation changed. Reload before continuing.')
                    if self.path == '/mobile/api/continue':
                        existing = continuations.receipt(entry, body, entries)
                        if existing:
                            self.respond(200, {'pane': existing})
                            return
                    state = live_status(entry)['state']
                    data = continuations.decorate(entry, snapshot(entry))
                    if data.get('busy'):
                        state = 'Working'
                    continuations.validate(entry, body, state)
                    goal = live_state.codex_goal(entry) if entry['agent'] == 'codex' else live_state.for_thread(entry.get('thread')).get('goal')
                    preview = continuations.preview(entry, body['target'], data.get('messages') or [], goal)
                    if self.path.endswith('preview'):
                        self.respond(200, preview)
                        return
                    if body.get('version') != preview['version']:
                        raise ValueError('The chat changed after the preview. Preview the handoff again.')
                    # Repeat process identity and live state immediately before opening the destination.
                    verify(entry)
                    if live_status(entry)['state'] != 'Idle':
                        raise ValueError('The chat started work. Wait until it is idle.')
                    fresh = choose(identifier)
                    if (fresh.get('owner_pid'), fresh.get('process_start'), fresh.get('thread')) != (entry.get('owner_pid'), entry.get('process_start'), entry.get('thread')):
                        raise ValueError('The agent changed. Reload before continuing.')
                    continuations.check_goal(live_state.codex_goal(entry) if entry['agent'] == 'codex' else live_state.for_thread(entry.get('thread')).get('goal'))
                    folder = agent_folder(entry)
                    new_pane = continuations.begin(entry, body, folder if folder and os.path.isdir(folder) else str(paths.HOME),
                        data.get('messages') or [], preview['brief'], subprocess.run)
                self.respond(201, {'pane': new_pane})
                return
            if self.path == '/mobile/api/delegate':
                entries = sessions()
                entry = delegate.validate(body, entries)
                folder = agent_folder(entry)
                made = worktrees.from_body(body, folder if folder and os.path.isdir(folder) else str(paths.HOME))
                with SEND_LOCK, worktrees.keeping(made):
                    if made:
                        entry = delegate.validate(body, sessions())
                    pane, name = delegate.start(entry, body, folder if folder and os.path.isdir(folder) else str(paths.HOME),
                                                run=subprocess.run, recent=lambda: snapshot(entry).get('messages') or [],
                                                **({'cwd': made['path'], 'worktree': made['branch']} if made else {}))
                self.respond(201, {'pane': pane, 'name': name, **({'worktree': made} if made else {})})
                return
            if self.path == '/mobile/api/fork':
                pane = body.get('session')
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    raise ValueError('Select a chat to copy.')
                entries = sessions()
                entry = next((item for item in entries if item['id'] == pane), None)
                if entry is None:
                    raise ValueError('That session has ended. Select another tab.')
                folder = agent_folder(entry)
                with SEND_LOCK:
                    new_pane, name = fork_chat(entry, entries, folder if folder and os.path.isdir(folder) else str(paths.HOME))
                self.respond(201, {'pane': new_pane, 'name': name})
                return
            if self.path == '/mobile/api/resume':
                agent = body.get('agent', 'claude')
                if agent not in ('claude', 'codex'):
                    raise ValueError('Choose Claude or Codex.')
                if ('name' in body) == ('after' in body):
                    raise ValueError('Choose exactly one target: name or after.')
                if 'name' in body and not valid_name(body['name']):
                    raise ValueError(NAME_HELP)
                chat = history.find_chat(body.get('id'), agent=agent)
                if chat is None:
                    self.respond(404, {'error': 'Chat not found.'})
                    return
                cwd = chat['cwd'] if chat['cwd'] and os.path.isdir(chat['cwd']) else str(paths.HOME)
                with SEND_LOCK:
                    entries = sessions()
                    live = next((entry for entry in entries if entry.get('thread') == chat['id']), None)
                    if live is not None:
                        self.respond(409, {'error': 'That chat is already open.', 'pane': live['id']})
                        return
                    if 'after' in body and not any(entry['id'] == body['after'] for entry in entries):
                        raise ValueError('Choose a pane from the live session list to open the chat after.')
                    profile = codex_provider.launch_profile() if agent == 'codex' else None
                    command = resume_command(chat['id'], agent=agent, profile=profile)
                    try:
                        if 'name' in body:
                            name = body['name']
                            pane = resume_session(name, cwd, chat['id'], run=subprocess.run, agent=agent, profile=profile)
                        else:
                            name = chat['title'][:60]
                            pane = new_window_after(body['after'], subprocess.run, cwd=cwd)
                            subprocess.run([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', name, ';',
                                            'set-option', '-p', '-t', pane, '@chat_name_at', str(int(time.time() * 1000))],
                                           check=True, capture_output=True, timeout=5)
                            subprocess.run([paths.TMUX, 'send-keys', '-t', pane, '-l', command],
                                           check=True, capture_output=True, timeout=5)
                            subprocess.run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'],
                                           check=True, capture_output=True, timeout=5)
                    except RuntimeError as exc:
                        if 'already exists' not in str(exc):
                            raise
                        self.respond(409, {'error': str(exc)})
                        return
                    result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'],
                                            check=True, capture_output=True, text=True, timeout=5)
                try:
                    chat_hidden.set_chat(agent, chat['id'], False)
                except OSError as exc:  # The chat is open by now, so this must not report a failed resume.
                    print(f'Could not unhide a resumed chat: {exc}', file=sys.stderr, flush=True)
                self.respond(201, {'session': result.stdout.strip(), 'pane': pane, 'name': name})
                return
            if self.path == '/mobile/api/push/mute':
                self.respond(200, {'muted': push.set_muted(body.get('session'), body.get('muted'))})
                return
            if self.path == '/mobile/api/push/level':
                self.respond(200, {'levels': push.set_level(body.get('session'), body.get('level'))})
                return
            if self.path == '/mobile/api/push/settings':
                self.respond(200, {'delivery': push.set_delivery(body.get('delivery'))})
                return
            if self.path.startswith('/mobile/api/push/'):
                if not push.available():
                    raise RuntimeError('Push notifications are not installed on the server.')
                if self.path.endswith('/subscribe'):
                    push.subscribe(body.get('subscription'))
                elif self.path.endswith('/unsubscribe'):
                    if isinstance(body.get('endpoint'), str):
                        push.unsubscribe(body['endpoint'])
                else:
                    delivered = push.send_all({'title': 'Tether', 'body': 'Notifications are working.', 'tag': 'test', 'priority': 'important', 'url': '/mobile/'})
                    self.respond(200, {'accepted': True, 'delivered': delivered})
                    return
                self.respond(200, {'accepted': True})
                return
            config = choose(body.get('session'))
            if (self.path != '/mobile/api/subagent-stop' or 'thread' in body) and body.get('thread') != config.get('thread'):
                self.respond(409, {'error': 'This session changed conversations. Reload its tab before sending.'})
                return
            if self.path == '/mobile/api/message' and config['agent'] == 'shell' and body.get('mode') == 'shell':
                with SEND_LOCK:
                    shell_submit(config, message.strip())
                self.respond(200, {'accepted': True, 'delivery': 'shell'})
                return
            if self.path == '/mobile/api/start':
                # New Codex panes use the chosen provider: `codex -p foundry` for Azure Foundry, plain `codex` for the plan.
                profile = codex_provider.launch_profile() if body.get('agent') == 'codex' else None
                with SEND_LOCK:
                    start_agent(config, body.get('agent'), profile=profile)
                self.respond(200, {'accepted': True})
                return
            if not config.get('writable'):
                raise RuntimeError('Messaging is unavailable for this session.')
            if self.path == '/mobile/api/subagent-stop':
                with SEND_LOCK:
                    result = subagent_stop.stop(config, body.get('id'))
                self.respond(200, result)
                return
            if self.path in ('/mobile/api/model-options', '/mobile/api/model'):
                with SEND_LOCK:
                    if self.path == '/mobile/api/model-options':
                        result = model_picker.options(config)
                    else:
                        model, effort = body.get('model'), body.get('effort')
                        if not isinstance(model, str) or not model or not (effort is None or isinstance(effort, str)):
                            raise ValueError('Choose a model first.')
                        result = model_picker.apply(config, model, effort or None)
                self.respond(200, result)
                return
            if self.path == '/mobile/api/mode':
                if config['agent'] != 'claude':
                    raise ValueError('Modes are for Claude sessions.')
                with SEND_LOCK:
                    verify(config)
                    def read_mode():
                        return footer.parse(capture(config, styled=True))
                    result = footer.set_mode(read_mode,
                                             lambda: subprocess.run([paths.TMUX, 'send-keys', '-t', config['id'], 'BTab'], check=True, capture_output=True, timeout=5),
                                             body.get('mode'))
                self.respond(200, result)
                return
            if self.path == '/mobile/api/key':
                key = body.get('key')
                if key not in ('C-c', 'Up', 'Down', 'Left', 'Right', 'Enter', 'Escape', 'Tab', 'BTab', 'Space', 'y', 'n', 'r', 'f', *'0123456789'):
                    self.respond(400, {'error': 'Invalid key.'})
                    return
                with SEND_LOCK:
                    if body.get('card') is True and config['agent'] == 'claude' and key in '0123456789':
                        card_key(config, key)
                    else:
                        verify(config)
                        if key == 'r':
                            import permissions_dialog
                            styled = capture(config, styled=True) if config['agent'] == 'claude' else ''
                            dialog = permissions_dialog.parse(styled, styled)
                            if not dialog or dialog['tab'] != 'Recently blocked':
                                self.respond(400, {'error': 'Open Recently blocked first.'})
                                return
                        if key == 'f' and not (config['agent'] == 'claude' and side_question.open_on(config)):
                            self.respond(400, {'error': 'Open a finished side question first.'})
                            return
                        if key == 'Escape' and body.get('stop') is True and config['agent'] == 'claude':
                            from terminal_input import stop_turn
                            self.respond(200, {'accepted': True, **stop_turn(config)})
                            return
                        from terminal_input import drop_double_escape
                        if key == 'Escape' and drop_double_escape(config):
                            print(f'/mobile/api/key dropped double Escape for Claude pane {config["id"]}', file=sys.stderr, flush=True)
                        else:
                            subprocess.run([paths.TMUX, 'send-keys', '-t', config['id'], key],
                                           check=True, capture_output=True, timeout=5)
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/menu':
                choice, label, answer = body.get('choice'), body.get('label'), body.get('answer')
                if (not isinstance(choice, int) or isinstance(choice, bool) or not (label is None or isinstance(label, str))
                        or not (answer is None or isinstance(answer, str) and answer.strip())):
                    raise ValueError('Choose an option first.')
                if answer is not None and any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in answer):
                    raise ValueError('The answer contains terminal control characters.')
                with SEND_LOCK:
                    menu_choose(config, choice, label=label, answer=answer)
                attention.STORE.clear(config['id'], config.get('thread'))
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/type':
                with SEND_LOCK:
                    type_text(config, body.get('text'), enter=body.get('enter') is True, login_url=body.get('login_url'))
                if body.get('enter') is True:
                    attention.STORE.clear(config['id'], config.get('thread'))
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/unqueue':
                with SEND_LOCK:
                    text = unqueue(config)
                self.respond(200, {'text': text})
                return
            if self.path == '/mobile/api/clear-draft':
                with SEND_LOCK:
                    terminal_clear_draft(config)
                self.respond(200, {'accepted': True})
                return
            if self.path == '/mobile/api/approval':
                with SEND_LOCK:
                    respond_approval(config, body.get('approval'), body.get('action'), choice=body.get('choice'), feedback=body.get('feedback'))
                attention.STORE.clear(config['id'], config.get('thread'))
                self.respond(200, {'accepted': True})
            else:
                delivery = send_text(config, message)
                attention.STORE.clear(config['id'], config.get('thread'))  # you answered the tab
                self.respond(200, {'accepted': True, 'delivery': delivery})
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {'error': str(exc) if self.path in ('/mobile/api/history/hide', '/mobile/api/session', '/mobile/api/tmux-sessions', '/mobile/api/broadcast', '/mobile/api/merge', '/mobile/api/model', '/mobile/api/shell', '/mobile/api/split', '/mobile/api/fork', '/mobile/api/delegate', '/mobile/api/agent-tab', '/mobile/api/continue-preview', '/mobile/api/continue', '/mobile/api/review-stop', '/mobile/api/review-stalled', '/mobile/api/review-link', '/mobile/api/resume', '/mobile/api/auto-resume', '/mobile/api/pane-name', '/mobile/api/pane-icon', '/mobile/api/pane-icon/draw', '/mobile/api/mode', '/mobile/api/stop-task', '/mobile/api/side-shell', '/mobile/api/side-shell/end', '/mobile/api/menu', '/mobile/api/push/mute', '/mobile/api/push/level', '/mobile/api/push/settings', '/mobile/api/login/start', '/mobile/api/login/code', '/mobile/api/login/cancel', '/mobile/api/accounts/refresh', '/mobile/api/accounts/switch') else 'Invalid message.'})
        except (OSError, RuntimeError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            # The journal keeps why a send was refused; the access log shows only "503".
            print(f'{self.path} refused: {type(exc).__name__}: {str(exc)[:200]}', file=sys.stderr, flush=True)
            self.respond(503, {'error': str(exc) if isinstance(exc, RuntimeError)
                               else 'Could not confirm delivery. Check the terminal before retrying.',
                               'draft': isinstance(exc, DraftError)})

    def do_PUT(self):
        self.respond(404, {'error': 'Not found'})

    def do_upload(self):
        """A raw file body for one session; the path of the saved copy goes back so the page can put it in the message."""
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= uploads.LIMIT:
                self.close_connection = True
                self.respond(413, {'error': 'That file is empty or too large (the limit is %d MB).' % (uploads.LIMIT // 1024 // 1024)})
                return
            config = choose(self.headers.get('X-Session'))
            if (self.headers.get('X-Thread') or None) != (config.get('thread') or None):  # tmux-chat sends '' for no thread
                self.respond(409, {'error': 'This session changed conversations. Reload its tab before attaching.'})
                return
            if not config.get('writable') or config['agent'] == 'shell':
                raise RuntimeError('Attachments need a running agent in this session.')
            data = self.rfile.read(size)
            target = uploads.save(unquote(self.headers.get('X-Filename', '')), data)
            self.respond(200, {'path': str(target), 'name': target.name, 'size': len(data)})
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {'error': str(exc)})
        except (OSError, RuntimeError) as exc:
            self.respond(503, {'error': str(exc) if isinstance(exc, RuntimeError) else 'Could not save that file.'})

    def do_transcribe(self):
        """A raw audio body from the mic button; the text goes back for the draft. Nothing is typed into a pane."""
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= transcribe.LIMIT:
                self.close_connection = True
                self.respond(413, {'error': 'The recording is empty or too long.'})
                return
            audio = self.rfile.read(size)
            self.respond(200, transcribe.transcribe(audio, self.headers.get('Content-Type', ''), live=self.headers.get('X-Live') == '1', recording=self.headers.get('X-Recording', ''), phrase=self.headers.get('X-Live-Phrase') == '1'))
        except ValueError as exc:
            self.respond(400, {'error': str(exc)})
        except RuntimeError as exc:
            self.respond(503, {'error': str(exc)})

    def do_PATCH(self):
        if self.path.split('?', 1)[0] != friday_proxy.OFFER:
            self.respond(404, {'error': 'Not found'})
            return
        if self.deny():
            return
        if not auth.same_origin(self.headers.get('Origin', ''), self.headers.get('Host', '')) or self.headers.get('X-Codex-Mobile') != '1':
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        friday_proxy.patch(self)

    def do_DELETE(self):
        if self.path not in ('/mobile/api/tmux-sessions', '/mobile/api/session'):
            self.respond(404, {'error': 'Not found'})
            return
        if self.deny():
            return
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if not auth.same_origin(origin, host) or self.headers.get('X-Codex-Mobile') != '1':
            self.respond(403, {'error': 'Reload the page and try again.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                self.respond(413, {'error': 'Invalid request.'})
                return
            body = json.loads(self.rfile.read(size))
            if self.path == '/mobile/api/session':
                pane = body.get('pane') if isinstance(body, dict) else None
                if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
                    self.respond(400, {'error': 'Select an active chat tab.'})
                    return
                with SEND_LOCK:
                    active_panes = subprocess.run([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}'],
                                                  check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
                    if pane not in active_panes:
                        self.respond(404, {'error': 'That chat tab has already ended.'})
                        return
                    auto_resume.forget_pane(pane, run=subprocess.run)
                    subprocess.run([paths.TMUX, 'kill-pane', '-t', pane], check=True,
                                   capture_output=True, timeout=5)
                self.respond(200, {'closed': pane})
                return
            identifier = body.get('session') if isinstance(body, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r'\$[0-9]+', identifier):
                self.respond(400, {'error': 'Select an active tmux session.'})
                return
            with SEND_LOCK:
                active = subprocess.run([paths.TMUX, 'list-sessions', '-F', '#{session_id}'],
                                        check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
                if identifier not in active:
                    self.respond(404, {'error': 'That tmux session has already ended.'})
                    return
                auto_resume.forget_session(identifier, run=subprocess.run)
                subprocess.run([paths.TMUX, 'kill-session', '-t', identifier], check=True,
                               capture_output=True, timeout=5)
            self.respond(200, {'closed': identifier})
        except (ValueError, UnicodeError):
            self.respond(400, {'error': 'Invalid request.'})
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            self.respond(503, {'error': 'Could not close the tmux session.'})


    # Keep Share dispatch together: the existing routes remain the fallback.
    _get_without_share = do_GET
    _post_without_share = do_POST

    def do_GET(self):
        import share_api
        if not share_api.dispatch(self):
            self._get_without_share()

    def do_POST(self):
        import share_api
        if not share_api.dispatch(self):
            self._post_without_share()

    def do_HEAD(self):
        import share_api
        if not share_api.dispatch(self):
            self.send_error(404)


class OfficeOriginHandler(Handler):
    """Only proxy the editor; never serve Tether pages or APIs on this origin."""

    def do_GET(self):
        if not (auth.signed_in(self.headers.get('Cookie'))
                or auth.tailscale_signed_in(self.headers.get('Tailscale-User-Login'), self.client_address[0])):
            self.respond(401, {'error': 'Sign in required.'})
            return
        self.proxy_office()

    do_POST = do_PUT = do_HEAD = do_DELETE = do_OPTIONS = do_PATCH = do_GET


def start_office_origin(host):
    config = office_addon.load()
    if config is None:
        return None
    try:
        http = ThreadingHTTPServer((host, config['editor_port']), OfficeOriginHandler)
    except OSError as exc:
        logging.getLogger(__name__).warning('Office editor listener did not start: %s', type(exc).__name__)
        return None
    threading.Thread(target=http.serve_forever, name='office-origin-listener', daemon=True).start()
    return http


if __name__ == '__main__':
    import share_api
    share_api.get_store()  # Reconcile private storage before accepting requests.
    import atexit
    import browser
    import pane_view
    import detection_rules
    detection_rules.save_last_good()
    atexit.register(browser.stop_all)
    pane_view.zoom_repair_all()
    paths.SCOPE_TMUX = bool(os.environ.get('INVOCATION_ID'))
    # A tmux server that this service starts takes its default-shell from $SHELL. systemd gives /bin/bash, so new panes
    # ran bash without the user's zsh setup (the claude/codex project launchers). Use the account's login shell.
    os.environ['SHELL'] = shell_log.login_shell()
    host = os.environ.get('MOBILE_TMUX_HOST', '127.0.0.1')
    port = int(os.environ.get('MOBILE_TMUX_PORT', '7683'))
    auth.check_startup(host)
    browser_cdp.start(notify, agent_finder=lambda pane: next(
        (entry['agent'] for entry in sessions() if entry['id'] == pane), 'agent'), identity_finder=pane_identity)
    if auth.MODE == 'token':
        print('Mobile Tmux listening on http://%s:%d/mobile/ -- sign in with the token in %s' % (host, port, auth.TOKEN_FILE), flush=True)
        auth.token()
    threading.Thread(target=monitor_notices, daemon=True).start()
    threading.Thread(target=tmux_names.watch, daemon=True).start()  # tmux renames flow back into pane names
    threading.Thread(target=auto_resume.watch, kwargs={
        'entries': lambda: (forget_sessions(), sessions())[1],
        'folder_of': agent_folder, 'status_of': shared_status,
        'display_dir': lambda entry: home_path(tab_folder(entry)),
        'on_change': forget_sessions,
    }, daemon=True).start()
    threading.Thread(target=window_size.watch, daemon=True).start()
    backgrounds.upgrade_soon()  # small copies for photos saved before they existed
    office_addon.start_listener()
    start_office_origin(host)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
