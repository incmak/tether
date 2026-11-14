"""Discover live tmux chats and use each agent's own transcript and message route."""
import json
import os
from pathlib import Path
import html
import re
import sqlite3
import time
from datetime import datetime
from antigravity import live_entry as antigravity_entry, process_start
from quota_notices import error_message, merge as merge_quota_notices
import codex_provider
import tool_details
import activity
import history
import paths
import procinfo
import pane_view
from side_shells import SESSION as SIDE_SHELLS

HOME = paths.HOME
ICON = re.compile(r"[a-z0-9][a-z0-9-]{0,43}")  # tmux option @chat_icon: a lucide icon name, or svg-<name> (pane_icons.py)
METRICS = paths.CACHE / 'usage'


def records(path):
    if not Path(path).is_file():
        return  # A newly launched agent may not have written its first message yet.
    with Path(path).open() as stream:
        for index, line in enumerate(stream):
            try:
                yield index, json.loads(line)
            except json.JSONDecodeError:
                continue


def descendants(pid, processes):
    found = {pid}
    while True:
        children = {child for child, (parent, _) in processes.items() if parent in found}
        if children <= found:
            return found
        found |= children


UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


FOOTER_PATH = re.compile(r'·\s+(~(?:/[^\s·]*)?|/[^\s·]+)\s+·')


def footer_cwd(run, pane):
    """The working directory Codex shows in its footer line (it changes with /cd; the tmux pane's path does not)."""
    try:
        screen = run([paths.TMUX, 'capture-pane', '-p', '-J', '-t', pane, '-S', '-6'], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in reversed(screen.splitlines()):
        match = FOOTER_PATH.search(line)
        if match:
            path = match[1]
            return str(HOME) + path[1:] if path.startswith('~') else path
    return None


def started_ms(ticks):
    """Wall-clock start of a process, in milliseconds, from its platform start token."""
    return procinfo.start_ms(ticks)


def codex_thread(connection, pid, ticks, cwd):
    """Prefer the process's open rollout, then explicit resume arguments, then the directory/time fallback."""
    # The open rollout identifies the process even when several panes share a folder.
    # Subagent rollouts can be open in the same process; only interactive threads qualify.
    try:
        candidates = set()
        for _, target in procinfo.fd_targets(pid):
            if not Path(target).name.startswith('rollout-') or not target.endswith('.jsonl'):
                continue
            row = connection.execute(
                "SELECT id,rollout_path FROM threads WHERE rollout_path=? AND archived=0 AND source IN ('cli','vscode')",
                (target,)).fetchone()
            if row:
                candidates.add(tuple(row))
        if len(candidates) == 1:
            return candidates.pop()
    except OSError:
        pass
    try:
        arguments = [arg.decode('utf-8', 'ignore') for arg in procinfo.argv(pid)]
    except OSError:
        arguments = []
    for index, argument in enumerate(arguments[:-1]):
        if argument == 'resume' and UUID.match(arguments[index + 1]):
            row = connection.execute('SELECT id,rollout_path FROM threads WHERE id=?', (arguments[index + 1],)).fetchone()
            if row:
                return row
    since = started_ms(ticks) - 60_000
    return connection.execute(
        "SELECT id,rollout_path FROM threads WHERE cwd=? AND archived=0 AND source IN ('cli','vscode') "
        "AND (created_at_ms>=? OR updated_at_ms>=?) ORDER BY updated_at_ms DESC LIMIT 1", (cwd, since, since)).fetchone()


def match_unresolved_codex(connection, entries):
    """A Codex pane whose folder cannot be read (a narrow pane cuts its footer off) is matched by time instead: it owns the
    conversations begun after it started and before the next Codex process did."""
    codex = sorted((e for e in entries if e['agent'] == 'codex'), key=lambda e: (float if procinfo.DARWIN else int)(e.get('process_start') or 0))
    for index, entry in enumerate(codex):
        if not entry.get('pending_conversation'):
            continue
        since = started_ms(entry['process_start']) - 60_000
        later = codex[index + 1]['process_start'] if index + 1 < len(codex) else None
        until = started_ms(later) - 60_000 if later else None
        row = connection.execute(
            "SELECT id,rollout_path,cwd FROM threads WHERE archived=0 AND source IN ('cli','vscode') AND created_at_ms>=? "
            "AND (? IS NULL OR created_at_ms<?) ORDER BY updated_at_ms DESC LIMIT 1", (since, until, until)).fetchone()
        if row:
            entry.pop('pending_conversation', None)
            entry.update(thread=row[0], transcript=row[1], thread_cwd=row[2])


def codex_database():
    """Codex's thread index (newest state_N.sqlite), read-only; an empty stand-in when Codex has never run on this machine."""
    found = sorted((paths.CODEX).glob('state_*.sqlite'), key=lambda p: [int(n) for n in re.findall(r'\d+', p.name)])
    if found:
        try:
            return sqlite3.connect('file:' + str(found[-1]) + '?mode=ro', uri=True)
        except sqlite3.Error:
            pass
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INT, source TEXT, created_at_ms INT, updated_at_ms INT)')
    return connection


def discover(config, run):
    pane_view.zoom_repair_all()
    result = run([paths.TMUX, 'list-panes', '-a', '-F',
                  '#{pane_id}\t#{session_id}\t#{session_name}\t#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_path}\t#{@chat_name_at}\t#{@chat_name}\t#{@chat_icon}\t#{@worktree}\t#{@group}\t#{@zoom_for}\t#{@tether_role}'],
                 capture_output=True, text=True, timeout=5)
    if result.returncode:
        return ([{'id': 'saved', 'label': 'Last chat', 'agent': 'codex',
                  'thread': config['thread'], 'transcript': config['transcript'], 'writable': False}] if config.get('thread') else [])
    positions = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) > 12:
            fields.pop()  # @tether_role
        if len(fields) > 9 and fields[-1]:
            positions[(fields[1], fields[-1])] = fields[3]
    processes = procinfo.table(run)
    claude = []
    for path in (HOME / '.claude/sessions').glob('*.json'):
        try:
            registry = json.loads(path.read_text())
            if registry.get('kind') == 'interactive' and registry.get('pid') in processes:
                registry['registry'] = str(path)
                claude.append(registry)
        except (OSError, ValueError):
            continue
    connection = codex_database()
    entries = []
    try:
        for line in result.stdout.splitlines():
            fields = line.split('\t')
            # @tether_role (delegate.ROLES): a helper tab's job, such as "advisor". Older callers send no such field.
            role = fields.pop() if len(fields) > 12 else ''
            if len(fields) > 9 and fields.pop():
                continue  # @zoom_for identifies the placeholder, never a chat.
            group = fields.pop() if len(fields) > 10 else ''  # @group (groups.py); older callers send no such field
            worktree = fields.pop() if len(fields) > 9 else ''
            pane, tmux_session, label, position, pid = fields[:5]
            position = positions.get((tmux_session, pane), position)
            if label == SIDE_SHELLS:  # the desktop terminal panel's own shells are not sessions
                continue
            # The last fields are when and what the pane was named in tmux-chat (tmux options @chat_name_at, @chat_name).
            # Older callers send six or seven fields.
            named_at = ''
            # A ninth field is the pane's icon (tmux option @chat_icon), set in Tether or by the agent itself.
            icon = fields.pop() if len(fields) > 8 else ''
            if len(fields) > 7:
                cwd, named_at, pane_name = '\t'.join(fields[5:-2]), fields[-2], fields[-1]
            elif len(fields) > 6:
                cwd, pane_name = '\t'.join(fields[5:-1]), fields[-1]
            else:
                cwd, pane_name = '\t'.join(fields[5:]), ''
            tree = descendants(int(pid), processes)
            names = {processes[p][1] for p in tree if p in processes}
            entry = {'id': pane, 'tmux_session': tmux_session, 'label': label, 'position': position,
                     'agent': 'shell', 'writable': False, 'worktree': worktree.strip()[:200], 'group': group.strip()[:16],
                     'pane_pid': int(pid), 'pane_start': process_start(int(pid)), 'cwd': cwd, 'pane_name': pane_name.strip()[:40], 'tmux_pane_name': pane_name.strip()[:40], 'pane_icon': icon if ICON.fullmatch(icon) else '',
                     'pane_named_at': int(named_at) if named_at.isdigit() else 0}
            if role in ('advisor', 'second-advisor'):
                entry['role'] = 'advisor'
            live = next((r for r in claude if r['pid'] in tree and r.get('tmux', '').endswith('.' + pane)), None)
            if live:
                project = re.sub(r'[^a-zA-Z0-9]', '-', live['cwd'])
                transcript = HOME / '.claude/projects' / project / (live['sessionId'] + '.jsonl')
                entry.update(agent='claude', thread=live['sessionId'], transcript=str(transcript),
                             registry=live['registry'], writable=True, owner_pid=live['pid'],
                             process_start=process_start(live['pid']),
                             busy=live.get('status') == 'busy')
                # A name the user gave (/rename or claude --name). Claude's own derived names ("projects-40") are not shown.
                if live.get('nameSource') == 'user' and isinstance(live.get('name'), str) and live['name'].strip():
                    entry['agent_name'] = ' '.join(live['name'].split())
                    entry['agent_named_at'] = live.get('nameSince') if isinstance(live.get('nameSince'), int) else 0
            elif 'claude' in names:
                owner = next(p for p in tree if p in processes and processes[p][1] == 'claude')
                started = process_start(owner)
                pending = 'pending-' + str(owner) + '-' + started
                transcript = HOME / '.claude/projects' / 'pending' / (pending + '.jsonl')
                entry.update(agent='claude', thread=pending, transcript=str(transcript),
                             writable=True, owner_pid=owner, process_start=started,
                             pending_conversation=True)
            elif 'codex' in names:
                owner = next(p for p in tree if p in processes and processes[p][1] == 'codex')
                started = process_start(owner)
                entry['codex_profile'] = codex_provider.pane_profile(owner)  # 'foundry' for `codex -p foundry`; None = the plan
                row = codex_thread(connection, owner, started, cwd)
                thread_cwd = cwd
                if not row:  # after /cd, Codex records its conversation under the new folder, not the pane's
                    shown = footer_cwd(run, pane)
                    if shown and shown != cwd:
                        row, thread_cwd = codex_thread(connection, owner, started, shown), shown
                if row:
                    entry.update(agent='codex', thread=row[0], transcript=row[1], writable=True, cwd=cwd, thread_cwd=thread_cwd,
                                 owner_pid=owner, process_start=started)
                else:
                    # A brand-new Codex session has no conversation yet: show an empty chat, never another session's history.
                    pending = 'pending-' + str(owner) + '-' + started
                    entry.update(agent='codex', thread=pending, transcript=str(HOME / '.codex/sessions/pending' / (pending + '.jsonl')),
                                 writable=True, cwd=cwd, owner_pid=owner, process_start=started, pending_conversation=True)
            elif 'agy' in names or any(processes[p][1] in ('agy', 'exe') for p in tree if p in processes):
                entry['agent'] = 'antigravity'
                entry.update(antigravity_entry([p for p in tree if p in processes and processes[p][1] in ('agy', 'exe')], cwd=cwd))
            entries.append(entry)
        match_unresolved_codex(connection, entries)
    finally:
        connection.close()
    counts = {e['label']: sum(other['label'] == e['label'] for other in entries) for e in entries}
    for entry in entries:
        if counts[entry['label']] > 1:
            entry['label'] += ':' + entry['position']
    # Two Codex processes can never be one conversation: the older process keeps it, the newer one starts empty until it has its own.
    claimed = set()
    for entry in sorted((e for e in entries if e['agent'] == 'codex' and not e.get('pending_conversation')), key=lambda e: (float if procinfo.DARWIN else int)(e.get('process_start') or 0)):
        if entry['thread'] in claimed:
            pending = 'pending-' + str(entry['owner_pid']) + '-' + str(entry['process_start'])
            entry.update(thread=pending, transcript=str(HOME / '.codex/sessions/pending' / (pending + '.jsonl')), pending_conversation=True)
        claimed.add(entry['thread'])
    names = codex_names() if any(e['agent'] == 'codex' and not e.get('pending_conversation') for e in entries) else {}
    for entry in entries:
        if entry['agent'] == 'codex' and not entry.get('pending_conversation') and names.get(entry['thread']):
            entry['agent_name'], entry['agent_named_at'] = names[entry['thread']]
    for entry in entries:
        # The newest name wins: a /rename in the agent replaces an older Tether name, and a later Tether name replaces it again.
        # A Tether name saved before tmux-chat kept the time counts as older.
        if entry.get('agent_name') and entry.get('pane_name') and entry.get('agent_named_at', 0) >= entry['pane_named_at']:
            entry['pane_name'] = ''
    return entries


_codex_names = {'key': None, 'names': {}}


def iso_ms(text):
    """'2026-11-01T14:12:08.30724454Z' -> epoch milliseconds; 0 when missing or unreadable."""
    if not isinstance(text, str):
        return 0
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)', text.strip())
    if not match:
        return 0
    try:
        stamp = datetime.fromisoformat(match[1] + (match[3] if match[3] != 'Z' else '+00:00'))
    except ValueError:
        return 0
    return int(stamp.timestamp() * 1000) + int((match[2] or '0')[:3].ljust(3, '0'))


def codex_names():
    """Codex thread names (/rename, or the name Codex gives a thread), from ~/.codex/session_index.jsonl. The last line for a thread wins.
    Re-read only when the file changes."""
    index = paths.CODEX / 'session_index.jsonl'
    try:
        info = index.stat()
    except OSError:
        return {}
    key = (info.st_mtime_ns, info.st_size)
    if _codex_names['key'] != key:
        names = {}
        try:
            with index.open(encoding='utf-8', errors='replace') as lines:
                for line in lines:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get('id'), str) and isinstance(record.get('thread_name'), str):
                        names[record['id']] = (' '.join(record['thread_name'].split()), iso_ms(record.get('updated_at')))
        except OSError:
            return {}
        _codex_names.update(key=key, names=names)
    return _codex_names['names']


def usage(context=None, rates=None, reported=None):
    context = context or {}
    rates = rates or []
    percent = context.get('percent')
    if percent is not None:
        context['percent'] = max(0, min(100, float(percent)))
    eligible = [r for r in rates if r.get('percent') is not None and r.get('active', True)
                and (not r.get('resets_at') or r['resets_at'] > time.time())]
    account = max(eligible, key=lambda r: r['percent']) if eligible else {}
    five_hour = next((r for r in eligible if r.get('period') == 'five_hour' or r.get('window_minutes') == 300 or r.get('label') in ('5h', 'five hour')), {})
    weekly = next((r for r in eligible if r.get('period') == 'weekly' or r.get('window_minutes') == 10080 or r.get('label') in ('168h', 'seven day', 'weekly')), {})
    return {'context': context, 'account': account, 'five_hour': five_hour, 'weekly': weekly,
            'windows': rates, 'reported_at': reported}


COMMAND = re.compile(r'<command-name>\s*(.*?)\s*</command-name>(?:.*?<command-args>(.*?)</command-args>)?', re.DOTALL)
COMMAND_OUTPUT = re.compile(r'<local-command-stdout>(.*?)</local-command-stdout>', re.DOTALL)


# System lines Claude Code prints in the terminal between turns, by record subtype, with the title Tether shows.
# compact_boundary is left out: the summary user record after it already shows as a "Conversation compacted" row.
SYSTEM_NOTICES = {'away_summary': 'While you were away', 'informational': 'Notice'}
TASK_FIELD = re.compile(r'<(summary|event|status|task-id)>(.*?)</\1>', re.DOTALL)


PEER = re.compile(r'<(cross-session-message|agent-message)\b([^>]*)>\n?(.*?)\n?</\1>', re.DOTALL)
HANDBACK = re.compile(r'^\[Subagent hand-back\].*?The report follows:\n', re.DOTALL)
BASH = re.compile(r'<bash-(input|stdout|stderr)>(.*?)</bash-\1>', re.DOTALL)
PASTED = re.compile(r'\n?</?pasted_content(?: id="[^"]*")?>\n?')
ARTIFACT_NOTE = re.compile(r'^\s*<artifact-content-authored-by-others/>\n[^\n]*\n')


def peer_text(body):
    """A subagent report starts with a safety preamble and has every line indented by two spaces; drop both."""
    report = HANDBACK.sub('', body)
    if report != body:
        report = '\n'.join(line[2:] if line.startswith('  ') else line for line in report.split('\n'))
    return report.strip()


def task_notice(text, index, time):
    """Prompts Claude Code writes itself (a finished background task, a message from another session or a subagent,
    a `!` shell command): show them as what they are, not as the user's words. None for anything else."""
    if not isinstance(text, str):
        return None
    if text.lstrip().startswith('<task-notification>'):
        fields = {key: ' '.join(value.split()) for key, value in TASK_FIELD.findall(text)}
        line = fields.get('summary') or fields.get('event') or ' '.join(
            part for part in ('Task', fields.get('task-id'), fields.get('status')) if part)
        notice = {'id': str(index), 'role': 'assistant', 'kind': 'notice', 'title': 'Background task', 'text': line, 'time': time}
        if (re.search(r'^\[?\s*Monitor expired after\b.*?\bno events\b', fields.get('event', ''), re.IGNORECASE)
                and fields.get('status', '').lower() not in ('failed', 'killed', 'error', 'stopped')
                and not re.search(r'exit code (?:[1-9]|-)', ' '.join(text.split()), re.IGNORECASE)):
            label = re.search(r'^Monitor (?:event:\s*)?"([^"]*)"', fields.get('summary', ''), re.IGNORECASE)
            notice.update(quiet='watch', watch=label[1].strip()[:120] if label else 'Watch')
        return notice
    peer = PEER.search(text) if text.lstrip().startswith(('<cross-session-message', '<agent-message', 'Another Claude session sent a message:')) else None
    if peer:
        name = re.search(r'from-name="([^"]*)"', peer[2])
        title = 'Subagent report' if peer[1] == 'agent-message' else 'From ' + (name[1] if name else 'another session')
        return {'id': str(index), 'role': 'assistant', 'kind': 'peer', 'title': title, 'text': peer_text(peer[3]), 'time': time}
    if text.lstrip().startswith('<bash-'):
        parts = dict((kind, html.unescape(activity.ANSI.sub('', value)).strip()) for kind, value in BASH.findall(text))
        if 'input' in parts:
            return {'id': str(index), 'role': 'user', 'text': '! ' + parts['input'], 'time': time}
        output = '\n'.join(part for part in (parts.get('stdout'), parts.get('stderr')) if part)
        return {'id': str(index), 'role': 'assistant', 'kind': 'terminal', 'text': output or '(no output)', 'time': time}
    return None


def tidy_user_text(text):
    """Drop wrapper tags around the user's own words: pasted text, and the note Claude puts before a compact summary."""
    return PASTED.sub('\n', ARTIFACT_NOTE.sub('', text)).strip('\n') if '<' in text else text


def claude_consume(messages, index, record, clean):
    """Fold one transcript record into the list of chat messages."""
    if record.get('isSidechain') or record.get('isMeta'):
        return
    if record.get('type') == 'attachment':
        # A message sent while Claude is busy is delivered as an attachment, not as a user record.
        attachment = record.get('attachment') or {}
        if attachment.get('type') == 'hook_blocking_error':
            # A hook stopped Claude ("Goal not met: …", a failed check after an edit). The terminal shows it; so do we.
            error = attachment.get('blockingError')
            error = error.get('blockingError') if isinstance(error, dict) else error
            if isinstance(error, str) and error.strip():
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice', 'title': f"{attachment.get('hookName') or 'Hook'} hook blocked",
                                 'text': activity.ANSI.sub('', error).strip(), 'time': record.get('timestamp')})
            return
        prompt = attachment.get('prompt') if attachment.get('type') == 'queued_command' else None
        notice = task_notice(prompt, index, record.get('timestamp'))
        if notice:
            messages.append(notice)
            return
        if isinstance(prompt, str) and clean(prompt).strip():
            messages.append({'id': str(index), 'role': 'user', 'text': tidy_user_text(clean(prompt)), 'time': record.get('timestamp'),
                             'delivery': 'queued'})
        return
    message = record.get('message', {})
    role = message.get('role')
    # Debug records such as api-request-blob also carry a user/assistant message; they are copies, not turns.
    if record.get('type') not in ('user', 'assistant') or role not in ('user', 'assistant'):
        # CLI 2.1.3xx writes a slash command's reply ("Can't restart while ...") as a system record, not a user record.
        # It also writes the command itself there when it is typed in the terminal ("/model").
        local = record.get('content') if record.get('type') == 'system' and record.get('subtype') == 'local_command' and isinstance(record.get('content'), str) else ''
        command, output = COMMAND.search(local), COMMAND_OUTPUT.search(local)
        if command:
            messages.append({'id': str(index), 'role': 'user', 'text': (command[1] + ' ' + (command[2] or '').strip()).strip(),
                             'time': record.get('timestamp')})
            return
        result = activity.ANSI.sub('', output[1]).strip() if output else ''
        if result:
            messages.append({'id': str(index), 'role': 'assistant', 'kind': 'terminal', 'text': result, 'time': record.get('timestamp')})
            return
        notice = SYSTEM_NOTICES.get(record.get('subtype')) if record.get('type') == 'system' else None
        content = record.get('content')
        if notice and isinstance(content, str) and content.strip():
            text = re.sub(r'\s*\(disable recaps in /config\)\s*$', '', activity.ANSI.sub('', content)).strip()
            # Socket bookkeeping between sessions is noise, and a notice that repeats a recent one adds nothing.
            if text.startswith('A process claiming the address ') or any(
                    item.get('title') == notice and item.get('text') == text for item in messages[-30:]):
                return
            messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice', 'title': notice,
                             'text': text, 'time': record.get('timestamp')})
            return
        if record.get('type') == 'system' and record.get('subtype') in ('error', 'api_error'):
            text = error_message(record)
            if text:
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice',
                                 'text': text, 'time': record.get('timestamp')})
        return
    content = message.get('content', '')
    notice = task_notice(content if isinstance(content, str) else '\n'.join(
        part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text'),
        index, record.get('timestamp')) if role == 'user' and isinstance(content, (str, list)) else None
    if notice:
        messages.append(notice)
        return
    if role == 'user' and isinstance(content, str):
        command, output = COMMAND.search(content), COMMAND_OUTPUT.search(content)
        if command:
            messages.append({'id': str(index), 'role': 'user', 'text': (command[1] + ' ' + (command[2] or '').strip()).strip(),
                             'time': record.get('timestamp')})
            return
        if output:
            result = activity.ANSI.sub('', output[1]).strip()
            if result:
                messages.append({'id': str(index), 'role': 'assistant', 'kind': 'terminal', 'text': result,
                                 'time': record.get('timestamp')})
            return
    text = content if isinstance(content, str) else '\n'.join(
        item.get('text', '') for item in content if item.get('type') == 'text')
    text = clean(text)
    if role == 'user':
        text = tidy_user_text(text)
    if text.strip() and not text.startswith(('<system-reminder>', '<local-command', '<command-name>')):
        item = {'id': str(index), 'role': role, 'text': text, 'time': record.get('timestamp')}
        if role == 'user' and isinstance(content, list):
            count = len(tool_details.images({'content': content}))
            if count > 0 and isinstance(record.get('uuid'), str):
                item['images'] = count
                item['image_key'] = record['uuid']
        if role == 'assistant' and record.get('isApiErrorMessage') and error_message(dict(record, message=text)):
            item['kind'] = 'notice'
        messages.append(item)
    if role == 'user' and isinstance(content, list):
        for part in content:
            if part.get('type') == 'tool_result':
                tool_details.mark_result(messages, part.get('tool_use_id'), part, time=record.get('timestamp'))
    if role == 'assistant' and isinstance(content, list):
        for position, part in enumerate(content):
            if part.get('type') == 'tool_use':
                item = activity.action(f'{index}.{position}', record.get('timestamp'),
                                       activity.claude_tool_line(part.get('name', 'Tool'), part.get('input')))
                item['tool'] = tool_details.summary(part)
                if record.get('timestamp'):
                    item['tool']['started'] = record['timestamp']
                messages.append(item)


def ask_questions(part):
    """An open AskUserQuestion from the transcript: its questions and options as Claude numbers them on screen.
    A question taller than the pane hides its top rows, so the screen alone cannot show it."""
    text = lambda value, limit: value.strip()[:limit] if isinstance(value, str) else ''
    questions = []
    for item in ((part.get('input') or {}).get('questions') or [])[:4]:
        if not isinstance(item, dict) or not text(item.get('question'), 2000):
            continue
        options = [{'label': text(option.get('label'), 300), 'description': text(option.get('description'), 1000)}
                   for option in (item.get('options') or [])[:8] if isinstance(option, dict) and text(option.get('label'), 300)]
        questions.append({'question': text(item.get('question'), 2000), 'header': text(item.get('header'), 60),
                          'multi': bool(item.get('multiSelect')), 'options': options})
    return {'id': part.get('id'), 'questions': questions} if questions and part.get('id') else None


def claude_model(thread):
    """Claude's model name from its status-line JSON (claude_usage.py). Empty when no Tether status line ran."""
    if not isinstance(thread, str) or not re.fullmatch(r'[a-zA-Z0-9-]+', thread):
        return ''
    try:
        model = json.loads((METRICS / (thread + '.json')).read_text()).get('model')
    except (OSError, ValueError, AttributeError):
        return ''
    return str(model.get('display_name') or model.get('id') or '') if isinstance(model, dict) else ''


def claude_snapshot(entry, clean):
    def consume(state, index, record):
        claude_consume(state['messages'], index, record, clean)
        if record.get('type') == 'custom-title' and isinstance(record.get('customTitle'), str):
            state['name'] = ' '.join(record['customTitle'].split()) or None  # set by /rename or claude --name
        if record.get('isSidechain') or record.get('isMeta'):
            return
        content = (record.get('message') or {}).get('content')
        if record.get('type') not in ('assistant', 'user') or not isinstance(content, list):
            return
        for part in content:
            if part.get('type') == 'tool_use' and part.get('name') == 'ExitPlanMode' and record['type'] == 'assistant':
                text = (part.get('input') or {}).get('plan')
                state['plan'] = {'id': part.get('id') or str(index), 'text': text} if isinstance(text, str) and text.strip() else None
            elif part.get('type') == 'tool_result' and part.get('tool_use_id') == (state.get('plan') or {}).get('id'):
                state['plan'] = None  # A finished request must not pair with a later menu.
            if part.get('type') == 'tool_use' and part.get('name') == 'AskUserQuestion' and record['type'] == 'assistant':
                state['ask'] = ask_questions(part)
            elif part.get('type') == 'tool_result' and part.get('tool_use_id') == (state.get('ask') or {}).get('id'):
                state['ask'] = None
    state = history.read(entry['transcript'], lambda: {'messages': [], 'dropped': 0, 'plan': None, 'ask': None}, consume)
    messages = activity.group_actions(activity.merge(list(state['messages'])))
    metrics = {}
    try:
        metrics = json.loads((METRICS / (entry['thread'] + '.json')).read_text())
    except (OSError, ValueError):
        pass
    context = metrics.get('context_window', {})
    rate_metrics = metrics
    inherited = False
    if not metrics.get('rate_limits'):
        def modified(path):
            try:
                return path.stat().st_mtime
            except OSError:  # replaced or removed since the glob
                return 0
        candidates = sorted(METRICS.glob('*.json'), key=modified, reverse=True)
        for path in candidates:
            try:
                candidate = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if any(isinstance(value, dict) and isinstance(value.get('resets_at'), (int, float))
                   and value['resets_at'] > time.time() and value.get('used_percentage') is not None
                   for value in (candidate.get('rate_limits') or {}).values()):
                rate_metrics = candidate
                inherited = True
                break
    rates = [{'label': name.replace('_', ' '), 'percent': value.get('used_percentage'),
              'resets_at': value.get('resets_at')}
             for name, value in (rate_metrics.get('rate_limits') or {}).items() if isinstance(value, dict)]
    report = usage({'percent': context.get('used_percentage'),
                    'tokens': context.get('total_input_tokens'),
                    'limit': context.get('context_window_size')}, rates, metrics.get('reported_at'))
    report.update(allowance_inherited=inherited, allowance_reported_at=rate_metrics.get('reported_at'),
                  model=(metrics.get('model') or {}).get('display_name') or (metrics.get('model') or {}).get('id') or '')
    return {'messages': merge_quota_notices(dict(entry, agent='claude'), messages), 'dropped': state['dropped'],
            'busy': entry.get('busy', False), 'usage': report, 'plan': state.get('plan'), 'ask': state.get('ask'), 'claude_name': state.get('name')}
