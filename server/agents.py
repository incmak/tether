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


# A process start token is exact to 1 s (macOS ps, Linux btime); a thread a pane begins is created after it starts.
START_SLACK_MS = 5_000


def started_ms(ticks):
    """Wall-clock start of a process, in milliseconds, from its platform start token."""
    return procinfo.start_ms(ticks)


BOUND = ('open', 'title', 'resume')
STRENGTH = {'open': 0, 'title': 1, 'resume': 2}  # a startup argument goes stale; a title is the pane's own word
# Codex 0.169 terminal title (tui/src/chatwidget/status_surfaces.rs, bottom_pane/title_setup.rs, terminal_title.rs):
# default items ["activity", "thread-name", "project-name"], joined by " | ", with a space after the activity item.
TITLE_SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
TITLE_ACTIVITY = re.compile('^(?:●(?: [%s])?|[%s]) ' % (TITLE_SPINNER, TITLE_SPINNER))
TITLE_ACTION = re.compile(r'^(?:● )?\[ [!.] \] Action Required(?: \| |$)')
TITLE_MAX = 240  # MAX_TERMINAL_TITLE_CHARS in codex-rs/tui/src/terminal_title.rs
TITLE_ID = re.compile(r'(?<![0-9a-f-])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{5})\.\.\.')
TITLE_HIDDEN = re.compile('[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f­͏؜᠎​-‏‪-‮'
                          '⁠-⁯︀-️﻿￹-￻\U0001bca0-\U0001bca3\U000e0100-\U000e01ef]')
TITLE_ALIASES = {'spinner': 'activity', 'project': 'project-name', 'session-id': 'thread-id'}
NAME_LAYOUTS = (('activity', 'thread-name', 'project-name'), ('thread-name', 'project-name'))
_title_config = {}


def codex_title_items():
    """The items of Codex's terminal title (tui.terminal_title in config.toml), or None for the default."""
    path = paths.CODEX / 'config.toml'
    try:
        stamp = path.stat().st_mtime_ns
        if _title_config.get('stamp') != stamp:
            import tomllib
            tui = tomllib.loads(path.read_text()).get('tui')
            items = tui.get('terminal_title') if isinstance(tui, dict) else None
            _title_config.update(stamp=stamp, items=items if isinstance(items, list) else None)
        return _title_config['items']
    except (OSError, ValueError):
        return None


def title_layout():
    """('name', activity) when the title is "[activity ]<thread name> | <project>", ('id', activity) when it shows the
    thread id, else (None, False): a title Tether cannot read names no thread."""
    items = codex_title_items()
    if items is None:
        return 'name', True
    items = tuple(TITLE_ALIASES.get(item, item) for item in items if isinstance(item, str))
    if 'thread-id' in items:
        return 'id', 'activity' in items
    return ('name', items[0] == 'activity') if items in NAME_LAYOUTS else (None, False)


def title_cut(text, limit):
    """Codex's truncate_terminal_title_part (by code point here, by grapheme in Codex)."""
    return text if len(text) <= limit else text[:limit - 3] + '...'


def title_text(text):
    """Codex's sanitize_terminal_title: hidden and control characters dropped, each whitespace run one space."""
    return ' '.join(TITLE_HIDDEN.sub('', text).split())


def shown_name(name):
    name = (name or '').strip()
    return title_text(title_cut(name, 48)) if name else ''


def same_name(shown, name):
    """Whether a thread name shows as `shown` in the title. A long name is cut to 45 characters and "..."."""
    if shown == shown_name(name):
        return True
    return shown.endswith('...') and len(shown) >= 40 and title_text((name or '').strip()).startswith(shown[:-3])


def read_title(title, cwd):
    """What a Codex title says about its thread: {('id', prefix or None)}, or the set of thread names it can show
    ({('name', name)}, with None for an unnamed thread), or None when the title does not have the expected form.
    A name may itself begin with a spinner frame or "[ ! ] Action Required", so a title can have two readings."""
    layout, activity = title_layout() if title is not None and cwd else (None, False)
    if title is not None and len(title) >= TITLE_MAX:  # Codex cuts the whole title there with no mark: any item may be cut
        return None
    if layout == 'id':
        found = set(TITLE_ID.findall(title))
        return {('id', found.pop() if found else None)} if len(found) <= 1 else None
    if layout != 'name':
        return None
    texts = {title}
    # Codex shows "Action Required" only when the layout has the activity item (terminal_title_shows_action_required).
    for prefix in (TITLE_ACTION, TITLE_ACTIVITY) if activity else ():
        match = prefix.match(title)
        if match:
            texts.add(title[match.end():])
    projects = [title_text(title_cut(folder.name, 24)) for folder in (Path(cwd), *Path(cwd).parents) if folder.name]
    readings = set()
    for text in texts:
        # project-name is the git root or project folder, or else the current folder: always cwd or one of its parents.
        for project in filter(None, projects):
            if text == project:
                readings.add(('name', None))
            elif text.endswith(' | ' + project) and len(text) > len(project) + 3:
                readings.add(('name', text[:-len(project) - 3]))
    return readings or None


def pane_titles(run):
    """Each pane's terminal title (#{pane_title}): Codex sets it, Tether never does."""
    try:
        result = run([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}\t#{pane_title}'], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if getattr(result, 'returncode', 0):
        return None
    return dict(line.split('\t', 1) for line in (result.stdout or '').splitlines() if '\t' in line)


def pane_title(run, pane):
    try:
        result = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_title}'], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return None if getattr(result, 'returncode', 0) else (result.stdout or '').rstrip('\n')


def has_names(connection):
    return any(column[1] == 'name' for column in connection.execute('PRAGMA table_info(threads)'))


def daemon_threads(connection, processes, strict=False):
    """The threads that a Codex app-server holds loaded (their rollouts open), or None when no app-server runs.
    Plain `codex` 0.169 is a client of the shared daemon (`codex app-server --listen`): the daemon, not the client,
    holds the rollout, and it unloads a thread within about a minute after its last client leaves (/clear, /new,
    exit). So a client's conversation is always one of these."""
    servers = []
    for pid, (_, name) in processes.items():
        if name == 'codex':
            try:
                arguments = procinfo.argv(pid)
            except OSError:
                if strict:
                    raise
                continue
            if b'app-server' in arguments and b'--listen' in arguments:
                servers.append(pid)
    if not servers:
        return None
    loaded = set()
    for pid in servers:
        try:
            targets = [target for _, target in procinfo.fd_targets(pid)]
        except OSError:
            if strict:  # a send check must not widen the set it checked last time
                raise
            return None  # an unreadable daemon says nothing: no filter rather than a wrong one
        rollouts = [target for target in targets if Path(target).name.startswith('rollout-') and target.endswith('.jsonl')]
        for index in range(0, len(rollouts), 500):
            batch = rollouts[index:index + 500]
            loaded.update(row[0] for row in connection.execute(
                'SELECT id FROM threads WHERE rollout_path IN (%s)' % ','.join('?' * len(batch)), batch))
    return loaded


def open_rollout(connection, pid):
    """The interactive thread whose rollout this process holds open. In-process Codex holds it; a client of the shared
    app-server daemon holds none, and several open rollouts say nothing."""
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
    return None


def resume_argument(connection, pid):
    try:
        arguments = [arg.decode('utf-8', 'ignore') for arg in procinfo.argv(pid)]
    except OSError:
        arguments = []
    for index, argument in enumerate(arguments[:-1]):
        if argument == 'resume' and UUID.match(arguments[index + 1]):
            row = connection.execute('SELECT id,rollout_path FROM threads WHERE id=?', (arguments[index + 1],)).fetchone()
            if row:
                return tuple(row)
    return None


def loaded_only(loaded):
    """SQL that keeps only the threads a daemon holds loaded; nothing when no daemon runs."""
    if loaded is None:
        return '', []
    return ' AND id IN (%s)' % ','.join('?' * len(loaded)) if loaded else ' AND 0', sorted(loaded)


def title_threads(connection, title, cwd, loaded):
    """The threads a name-layout title may show, as {id: (rollout, name or None)} with each thread's own readings:
    Codex shows the Git project or folder of the thread, so a title is read against each thread's folder. With a daemon:
    every thread it holds, in any folder (the TUI can open a thread of another folder). Without one: this folder's."""
    where, params = loaded_only(loaded)
    folder, folder_params = (' AND cwd=?', (cwd,)) if loaded is None else ('', ())
    rows = connection.execute("SELECT id,rollout_path,name,cwd FROM threads WHERE archived=0 AND source IN ('cli','vscode')"
                              + folder + where + ' ORDER BY updated_at_ms DESC', (*folder_params, *params))
    readings = {}
    found = []
    for thread, rollout, name, folder_path in rows:
        if folder_path not in readings:
            readings[folder_path] = read_title(title, folder_path) or set()
        shown = readings[folder_path]
        if name and any(value and same_name(value, name) for _, value in shown):
            found.append((thread, rollout, True))
        elif not name and ('name', None) in shown:
            found.append((thread, rollout, False))
    return found


def codex_claim(connection, pid, ticks, cwd, title=None, loaded=None):
    """What says which conversation this Codex process shows, strongest first. Returns {kind, rows, unnamed, named}:
    open    - it holds the rollout open (Codex in-process);
    title   - its terminal title names exactly one thread. A daemon client holds no rollout; its title is the only
              word that comes from the client itself;
    resume  - `codex resume ID` while the title shows no name (the thread is unnamed);
    tie     - the title names several threads;
    guess   - the newest thread of the folder since the process started. Never verified;
    unknown - the title names a thread Tether cannot find (yet);
    fresh   - the title shows no thread id: no conversation yet;
    none    - nothing found.
    `loaded` (daemon_threads) limits every claim but an open rollout to the threads a daemon holds."""
    row = open_rollout(connection, pid)
    if row:
        return {'kind': 'open', 'rows': [row], 'unnamed': False, 'named': False, 'readable': True}
    claim = codex_title_claim(connection, pid, ticks, cwd, title, loaded)
    claim['readable'] = title is not None and has_names(connection) and (
        read_title(title, cwd) is not None or claim['kind'] in ('title', 'tie', 'older') and bool(claim['rows'])
        and title_layout()[0] == 'name' and bool(title_threads(connection, title, cwd, loaded)))
    return claim


def codex_title_claim(connection, pid, ticks, cwd, title, loaded):
    where, params = loaded_only(loaded)
    readings = read_title(title, cwd) if title is not None and has_names(connection) else None
    ids = [value for kind, value in readings or () if kind == 'id']
    if ids:
        if not ids[0]:
            return {'kind': 'fresh', 'rows': [], 'unnamed': False, 'named': True}
        rows = [tuple(row) for row in connection.execute(
            "SELECT id,rollout_path FROM threads WHERE substr(id,1,?)=? AND archived=0 AND source IN ('cli','vscode')" + where,
            (len(ids[0]), ids[0], *params))]
        return {'kind': 'title' if len(rows) == 1 else 'tie' if rows else 'unknown', 'rows': rows, 'unnamed': False, 'named': True}
    shown = title_threads(connection, title, cwd, loaded) if readings is not None or title is not None and loaded is not None \
        and has_names(connection) and title_layout()[0] == 'name' else []
    named = [(thread, rollout) for thread, rollout, has_name in shown if has_name]
    unnamed = readings is not None and ('name', None) in readings or any(not has_name for *_, has_name in shown)
    if named:  # two threads of one name, or a title that may also show no name: Tether cannot tell
        kind = 'title' if len(named) == 1 and not unnamed else 'tie'
        also = [(thread, rollout) for thread, rollout, has_name in shown if not has_name]  # Resume must see these too
        return {'kind': kind, 'rows': named, 'also': also, 'unnamed': False, 'named': True, 'maybe_unnamed': unnamed}
    if (readings or shown) and not unnamed:
        return {'kind': 'unknown', 'rows': [], 'unnamed': False, 'named': True}
    # `codex resume ID` names the thread the process started with. It counts only while a readable title shows no name
    # (a named thread would show it; another layout could show anything) and the daemon still holds it: /clear, /new and
    # /resume keep the process and its arguments.
    # With no title at all (or no names to show), the argument is the only word; sends still need a read title.
    resumed = resume_argument(connection, pid) if unnamed or title is None or not has_names(connection) else None
    if resumed and (loaded is None or resumed[0] in loaded) and not (unnamed and thread_name(connection, resumed[0])):
        return {'kind': 'resume', 'rows': [resumed], 'unnamed': unnamed, 'named': False}
    since = started_ms(ticks) - START_SLACK_MS
    rows = [tuple(row) for row in connection.execute(
        "SELECT id,rollout_path FROM threads WHERE cwd=? AND archived=0 AND source IN ('cli','vscode') "
        "AND created_at_ms>=?" + (" AND (name IS NULL OR name='')" if unnamed else '') + where
        + ' ORDER BY updated_at_ms DESC', (cwd, since, *params))]
    # Another thread the daemon holds may be open in this pane too (`/resume` in the TUI, any folder), or in a pane that
    # closed a moment ago. It is never shown here, but it means "no conversation yet" cannot be proven. With a read
    # title: the unnamed threads that title fits; without one: every thread the daemon holds.
    if loaded is None:
        older = []
    elif readings is not None or shown:
        older = [(thread, rollout) for thread, rollout, has_name in shown if not has_name]
    else:
        older = [tuple(row) for row in connection.execute(
            "SELECT id,rollout_path FROM threads WHERE archived=0 AND source IN ('cli','vscode')" + where
            + ' ORDER BY updated_at_ms DESC', params)]
    older = [row for row in older if row not in rows]
    if rows:
        return {'kind': 'guess', 'rows': rows, 'also': older, 'unnamed': unnamed, 'named': False}
    return {'kind': 'older' if older else 'none', 'rows': older, 'unnamed': unnamed, 'named': False}


def thread_name(connection, thread):
    row = connection.execute('SELECT name FROM threads WHERE id=?', (thread,)).fetchone()
    return (row[0] or '') if row else ''


def codex_thread(connection, pid, ticks, cwd, title=None, loaded=None):
    """The conversation this Codex process shows on its own evidence, or its best guess; None for a tie or a new one."""
    claim = codex_claim(connection, pid, ticks, cwd, title, loaded)
    return claim['rows'][0] if claim['rows'] and claim['kind'] not in ('tie', 'unknown') else None


def start_key(entry):
    return (float if procinfo.DARWIN else int)(entry.get('process_start') or 0)


def match_unresolved_codex(connection, entries, taken=frozenset(), loaded=None):
    """A Codex pane whose folder cannot be read (a narrow pane cuts its footer off) is matched by time instead: it owns the
    conversations begun after it started and before the next Codex process did. It never takes a thread in `taken`
    (bound to another pane), one the daemon has not loaded, nor a named thread when its title shows no name."""
    codex = sorted((e for e in entries if e['agent'] == 'codex'), key=start_key)
    where, params = loaded_only(loaded)
    for index, entry in enumerate(codex):
        if not entry.get('pending_conversation'):
            continue
        since = started_ms(entry['process_start']) - 60_000
        later = codex[index + 1]['process_start'] if index + 1 < len(codex) else None
        until = started_ms(later) - 60_000 if later else None
        names = entry.get('codex_unnamed') and has_names(connection)
        rows = [tuple(row) for row in connection.execute(
                "SELECT id,rollout_path,cwd FROM threads WHERE archived=0 AND source IN ('cli','vscode') AND created_at_ms>=? "
                "AND (? IS NULL OR created_at_ms<?)" + (" AND (name IS NULL OR name='')" if names else '') + where
                + ' ORDER BY updated_at_ms DESC', (since, until, until, *params)) if row[0] not in taken]
        if rows:
            entry.pop('pending_conversation', None)
            entry.update(thread=rows[0][0], transcript=rows[0][1], thread_cwd=rows[0][2], candidates=[row[:2] for row in rows])


def pending_codex(entry):
    pending = 'pending-' + str(entry['owner_pid']) + '-' + str(entry['process_start'])
    entry.update(thread=pending, transcript=str(HOME / '.codex/sessions/pending' / (pending + '.jsonl')), pending_conversation=True)
    entry.pop('thread_cwd', None)


def bind_codex(connection, entries, run, processes=None, strict=False):
    """Give each live Codex pane its own conversation, or an empty one. Two plain `codex` panes in one folder share the
    app-server daemon (0.169), so neither holds a rollout and the folder and time cannot tell them apart. A guess or a
    tie never takes a thread that another pane is bound to; panes that share a guess or a tie all wait (pending) and
    keep the threads as candidates, so Resume does not open one a second time."""
    codex = sorted((e for e in entries if e['agent'] == 'codex' and 'owner_pid' in e), key=start_key)
    if not codex:
        return
    titles = loaded = None
    claims = {}
    for entry in codex:
        claim = codex_claim(connection, entry['owner_pid'], entry['process_start'], entry['cwd'])
        if claim['kind'] != 'open':
            if titles is None:
                titles = pane_titles(run) or {}
                loaded = daemon_threads(connection, processes if processes is not None else procinfo.table(run), strict)
            claim = codex_claim(connection, entry['owner_pid'], entry['process_start'], entry['cwd'], titles.get(entry['id']), loaded)
        claim['cwd'] = entry['cwd']
        if claim['kind'] in ('none', 'unknown', 'older') or claim['kind'] == 'guess' and not claim['unnamed']:
            shown = footer_cwd(run, entry['id'])  # after /cd, Codex records its conversation under the new folder
            if shown and shown != entry['cwd']:
                moved = codex_claim(connection, entry['owner_pid'], entry['process_start'], shown, (titles or {}).get(entry['id']), loaded)
                if moved['kind'] in BOUND or moved['kind'] in ('tie', 'guess', 'older') and claim['kind'] in ('none', 'unknown', 'older'):
                    claim, claim['cwd'] = moved, shown
        claims[entry['id']] = claim
    bound = {}
    for entry in sorted(codex, key=lambda e: (STRENGTH.get(claims[e['id']]['kind'], 9), start_key(e))):
        claim = claims[entry['id']]
        if claim['kind'] in BOUND and claim['rows'][0][0] not in bound.values():
            bound[entry['id']] = claim['rows'][0][0]

    def others(pane):
        # Only a pane's own word (an open rollout or its title) rules a thread out for another pane: a startup argument
        # goes stale after /clear while another pane may `/resume` that thread.
        return {thread for owner, thread in bound.items() if owner != pane and claims[owner]['kind'] in ('open', 'title')}
    changed = True
    while changed:  # a promoted tie binds a thread that other ties and guesses must then give up
        changed = False
        for pane, claim in claims.items():
            if claim['kind'] in ('tie', 'guess', 'older'):
                claim['rows'] = [row for row in claim['rows'] if row[0] not in others(pane)]
                claim['also'] = [row for row in claim.get('also', []) if row[0] not in others(pane)]
                if not claim['rows'] and claim['also']:
                    # its main threads are open in other panes, but it may still show one of the others
                    claim.update(kind='older', rows=claim['also'], also=[])
                elif not claim['rows']:
                    # every thread it could show is open in another pane; with only older ones, the pane is new
                    claim['kind'] = 'none' if claim['kind'] == 'older' else 'held'
        for pane, claim in claims.items():
            if claim['kind'] == 'tie' and len(claim['rows']) == 1 and not claim.get('maybe_unnamed'):
                thread = claim['rows'][0][0]
                # Other panes hold the other threads of that name, and no other title names this one. A folder guess
                # is weaker than a name and gives the thread up in the next round.
                if not any(thread in (row[0] for row in other['rows']) for key, other in claims.items()
                           if key != pane and other['kind'] == 'tie'):
                    claim['kind'] = 'title'
                    bound[pane] = thread
                    changed = True
    unresolved = [{'agent': 'codex', 'id': entry['id'], 'process_start': entry['process_start'],
                   'pending_conversation': claims[entry['id']]['kind'] == 'none', 'codex_unnamed': claims[entry['id']]['unnamed']}
                  for entry in codex]
    strong = {thread for owner, thread in bound.items() if claims[owner]['kind'] in ('open', 'title')}
    match_unresolved_codex(connection, unresolved, taken=strong, loaded=loaded)
    for item in unresolved:
        if claims[item['id']]['kind'] == 'none' and 'thread' in item:
            claims[item['id']].update(kind='guess', rows=item['candidates'], cwd=item['thread_cwd'])
    guesses = {}
    for claim in claims.values():
        if claim['kind'] == 'guess':
            guesses[claim['rows'][0][0]] = guesses.get(claim['rows'][0][0], 0) + 1
    for entry in codex:
        claim = claims[entry['id']]
        kind, rows = claim['kind'], claim['rows']
        if bound.get(entry['id']) or kind == 'guess' and guesses[rows[0][0]] == 1:
            entry.update(thread=rows[0][0], transcript=rows[0][1], thread_cwd=claim['cwd'], codex_binding=kind,
                         codex_title_read=claim.get('readable', False))
            entry.pop('pending_conversation', None)
            if kind == 'guess' and len(rows) + len(claim.get('also', [])) > 1:
                # The newest of several threads the pane could show: fine to read, never enough to send or resume.
                entry['codex_candidates'] = [row[0] for row in rows + claim.get('also', [])]
                entry['codex_unresolved'] = True
        else:
            # A tie, a shared guess, or a second client of a bound thread: an empty chat, never another pane's history.
            pending_codex(entry)
            entry['codex_title_read'] = claim.get('readable', False)
            if kind in ('tie', 'guess', 'older') and rows:
                entry['codex_candidates'] = [row[0] for row in rows + claim.get('also', [])]
            if kind not in ('none', 'fresh'):
                # The pane shows a conversation, but Tether cannot tell which one: its chat is empty and sends wait.
                entry['codex_unresolved'] = True


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


_transcripts = {}


def claude_transcript(cwd, session):
    """The file Claude writes for this session.

    Claude names a new session's folder after its working folder, but a resumed session keeps writing
    to the folder where it began (another working folder, or the Mac path of a session moved to Linux).
    So the folder named after the current working folder is only the first guess: else find the file by its id.
    """
    guess = HOME / '.claude/projects' / re.sub(r'[^a-zA-Z0-9]', '-', cwd) / (session + '.jsonl')
    if guess.is_file():
        return guess
    known = _transcripts.get(session)
    if known and known.is_file():
        return known
    found = next(history.transcript_files(session), None)
    if not found:
        return guess  # a new session writes its first line here
    _transcripts[session] = found[0]
    return found[0]


def claude_registries(processes):
    """The live interactive Claude sessions (~/.claude/sessions/*.json)."""
    claude = []
    for path in (HOME / '.claude/sessions').glob('*.json'):
        try:
            registry = json.loads(path.read_text())
            if registry.get('kind') == 'interactive' and registry.get('pid') in processes:
                registry['registry'] = str(path)
                claude.append(registry)
        except (OSError, ValueError):
            continue
    return claude


def codex_recheck(entry, run):
    """The conversation the pane of `entry` shows now, by the same binding as discovery over every live Codex pane:
    one pane's evidence can rule out another pane's thread. None when the pane no longer runs that Codex process."""
    processes = procinfo.table(run)
    result = run([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id}\t#{pane_pid}\t#{session_name}\t#{@zoom_for}\t#{pane_current_path}'],
                 capture_output=True, text=True, timeout=5, check=True)
    claude = claude_registries(processes)
    entries = []
    for line in result.stdout.splitlines():
        pane, pid, session, zoom, cwd = (line.split('\t', 4) + [''] * 4)[:5]
        if not pid.isdigit() or zoom or session == SIDE_SHELLS:  # the panes that discover() skips
            continue
        tree = descendants(int(pid), processes)
        if any(r['pid'] in tree and r.get('tmux', '').endswith('.' + pane) for r in claude) \
                or any(processes[p][1] == 'claude' for p in tree if p in processes):
            continue  # a Claude pane, by the rules of discover(), even when Claude runs `codex exec`
        owner = next((p for p in tree if p in processes and processes[p][1] == 'codex'), None)
        if owner:
            entries.append({'id': pane, 'agent': 'codex', 'cwd': cwd, 'owner_pid': owner, 'process_start': process_start(owner)})
    connection = codex_database()
    try:
        bind_codex(connection, entries, run, processes, strict=True)
    finally:
        connection.close()
    fresh = next((e for e in entries if e['id'] == entry['id']), None)
    return fresh if fresh and (fresh['owner_pid'], fresh['process_start']) == (entry['owner_pid'], entry['process_start']) else None


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
    claude = claude_registries(processes)
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
                     'pane_pid': int(pid), 'pane_start': process_start(int(pid)), 'cwd': cwd, 'pane_name': pane_name.strip()[:40], 'tmux_pane_name': pane_name.strip()[:40], 'full_pane_name': pane_name.strip(), 'pane_icon': icon if ICON.fullmatch(icon) else '',
                     'pane_named_at': int(named_at) if named_at.isdigit() else 0}
            if role in ('advisor', 'second-advisor'):
                entry['role'] = 'advisor'
            elif role == 'reviewer':
                entry['helper'] = 'reviewer'  # not a role: reviewers keep their AI tab names (tab_brief.py)
            elif role in ('coordinator', 'coordinator-needs', 'coordinator-off'):
                entry['coordinator'] = role.partition('-')[2] or 'all'  # the session's coordinator tab and what it gets (coordinator.py)
            live = next((r for r in claude if r['pid'] in tree and r.get('tmux', '').endswith('.' + pane)), None)
            if live:
                transcript = claude_transcript(live['cwd'], live['sessionId'])
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
                # bind_codex (below) chooses the conversation once every pane is known: one pane's evidence can rule out another's guess.
                entry.update(agent='codex', writable=True, cwd=cwd, owner_pid=owner, process_start=started)
            elif 'agy' in names or any(processes[p][1] in ('agy', 'exe') for p in tree if p in processes):
                entry['agent'] = 'antigravity'
                entry.update(antigravity_entry([p for p in tree if p in processes and processes[p][1] in ('agy', 'exe')], cwd=cwd))
            entries.append(entry)
        bind_codex(connection, entries, run, processes)
    finally:
        connection.close()
    counts = {e['label']: sum(other['label'] == e['label'] for other in entries) for e in entries}
    for entry in entries:
        if counts[entry['label']] > 1:
            entry['label'] += ':' + entry['position']
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


def scheduled_fire(record):
    """{'id', 'prompt'} for the hidden record that starts a scheduled (CronCreate) turn; None otherwise."""
    if record.get('type') != 'user' or record.get('isSidechain') or not isinstance(record.get('scheduledTaskId'), str):
        return None
    content = (record.get('message') or {}).get('content')
    if isinstance(content, list):
        content = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text')
    return {'id': record['scheduledTaskId'][:64], 'prompt': content if isinstance(content, str) else ''}


def person_prompt(record):
    """True for a prompt the person sent: typed, or queued while Claude was busy. A compact summary, a tool result,
    a scheduled fire and the prompts Claude Code writes itself (task notices, peer messages) are not."""
    if record.get('isSidechain') or record.get('isMeta') or record.get('isCompactSummary') or record.get('turnOrigin') == 'scheduled':
        return False
    if record.get('type') == 'attachment':
        attachment = record.get('attachment') or {}
        text = attachment.get('prompt') if attachment.get('type') == 'queued_command' else None
    elif record.get('type') == 'user':
        content = (record.get('message') or {}).get('content')
        text = content if isinstance(content, str) else '\n'.join(
            part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text') if isinstance(content, list) else None
    else:
        return False
    return isinstance(text, str) and bool(text.strip()) and task_notice(text, 0, None) is None


def scheduled_runs(state, index, record):
    """Keep each job's last run: its start time and Claude's last words in that turn (the result)."""
    runs = state.setdefault('scheduled', {})
    fire = scheduled_fire(record)
    if fire:
        runs.pop(fire['id'], None)
        runs[fire['id']] = {'at': record.get('timestamp'), 'message': str(index), 'result': ''}
        while len(runs) > 20:
            runs.pop(next(iter(runs)))
        state['scheduled_turn'] = fire['id']
        return
    current = state.get('scheduled_turn')
    if not current or record.get('isSidechain') or record.get('isMeta'):
        return
    if person_prompt(record):
        state['scheduled_turn'] = None  # the person's own prompt starts a new turn
        return
    message = record.get('message') or {}
    content = message.get('content')
    if record.get('type') == 'assistant' and isinstance(content, list) and current in runs:
        text = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text').strip()
        if text:
            runs[current]['result'] = text[:600]


def claude_advisor_model(record, message):
    usage = message.get('usage')
    iterations = usage.get('iterations') if isinstance(usage, dict) else None
    if isinstance(iterations, list):
        for item in iterations:
            if isinstance(item, dict) and item.get('type') == 'advisor_message':
                model = item.get('model')
                if isinstance(model, str) and model:
                    return model
    model = record.get('advisorModel')
    return model if isinstance(model, str) else ''


def claude_advisor_result(messages, part, ts):
    identifier = part.get('tool_use_id')
    if not isinstance(identifier, str) or not identifier:
        return
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        advisor = message.get('advisor')
        if message.get('kind') != 'advisor' or not advisor or advisor['id'] != identifier:
            continue
        content = part.get('content')
        result_type = content.get('type') if isinstance(content, dict) else None
        updated = {key: advisor[key] for key in ('id', 'model', 'started')}
        updated['ended'] = ts
        if result_type == 'advisor_redacted_result':
            updated['status'] = 'answered'
            text = 'answered'
        elif result_type == 'advisor_tool_result_error':
            updated['status'] = 'error'
            error = content.get('error_code')
            updated['error'] = error if isinstance(error, str) and re.fullmatch(r'[a-z0-9_]{1,80}', error) else 'unknown'
            text = 'no answer'
        else:
            updated['status'] = 'unknown'
            updated['result_type'] = (
                result_type if isinstance(result_type, str) and re.fullmatch(r'[a-z0-9_]{1,80}', result_type) else 'unknown'
            )
            text = 'reply in an unknown form'
        messages[index] = dict(message, advisor=updated, text=f'Asked the advisor · {text}')
        break


def claude_consume(messages, index, record, clean):
    """Fold one transcript record into the list of chat messages."""
    fire = scheduled_fire(record)
    if fire:
        # A session cron job (CronCreate) started this turn. Claude Code writes its prompt as a hidden record.
        import session_crons
        messages.append({'id': str(index), 'role': 'assistant', 'kind': 'notice', 'title': 'Scheduled run',
                         'text': session_crons.label(fire['prompt']) or fire['id'], 'time': record.get('timestamp'),
                         'quiet': 'schedule', 'watch': session_crons.label(fire['prompt']) or fire['id'], 'cron': fire['id']})
        return
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
        item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text')
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
            if isinstance(part, dict) and part.get('type') == 'tool_result':
                tool_details.mark_result(messages, part.get('tool_use_id'), part, time=record.get('timestamp'),
                                          details=record.get('toolUseResult'))
    if role == 'assistant' and isinstance(content, list):
        ts = record.get('timestamp')
        for position, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'tool_use':
                item = activity.action(f'{index}.{position}', ts,
                                       activity.claude_tool_line(part.get('name', 'Tool'), part.get('input')))
                item['tool'] = tool_details.summary(part)
                if ts:
                    item['tool']['started'] = ts
                messages.append(item)
            elif part.get('type') == 'server_tool_use' and part.get('name') == 'advisor':
                identifier = part.get('id')
                if isinstance(identifier, str) and identifier:
                    messages.append({
                        'id': f'{index}.{position}', 'role': 'assistant', 'kind': 'advisor',
                        'text': 'Asked the advisor', 'time': ts,
                        'advisor': {'id': identifier, 'model': claude_advisor_model(record, message),
                                    'status': 'pending', 'started': ts},
                    })
            elif part.get('type') == 'advisor_tool_result':
                claude_advisor_result(messages, part, ts)


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
        scheduled_runs(state, index, record)
        if record.get('type') == 'custom-title' and isinstance(record.get('customTitle'), str):
            state['name'] = ' '.join(record['customTitle'].split()) or None  # set by /rename or claude --name
        if record.get('isSidechain') or record.get('isMeta'):
            return
        content = (record.get('message') or {}).get('content')
        if record.get('type') not in ('assistant', 'user') or not isinstance(content, list):
            return
        for part in content:
            if not isinstance(part, dict):
                continue
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
            'busy': entry.get('busy', False), 'usage': report, 'plan': state.get('plan'), 'ask': state.get('ask'), 'claude_name': state.get('name'),
            'scheduled': {key: dict(value) for key, value in state.get('scheduled', {}).items()}}
