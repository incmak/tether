"""Work items: pieces of work that move by stage, across projects (the work board, B59).

paths.work()/projects.json      the work projects: name, item prefix, folder, sessions, feed, Jira key, QA status.
                              Only the server and Mak's own commands write it; Codex's sandbox cannot.
paths.items()/<PREFIX>/items.jsonl  a project's items, as append-only events, one JSON object per line. The folder is
                              named by the prefix, which never changes. Codex tabs get write access to paths.items().
paths.items()/.lock             held for every item write, by the server and by the agent command (separate processes)

A work project is not a session bundle (projects.py): one work project spans a triage session and a work session.
The server folds the events into each item's current state. Every write names the item version the writer last saw
(its count of events); a write over an older version is refused, so two writers never overwrite each other.

Stages: triage, needs (Needs you), ready, building, review, done, hold (On hold). An agent's ask moves the item to
needs; Mak's answer clears the ask and moves it on: Build to ready, Hold to hold, anything else back to where it was."""
import contextlib
import fcntl
import json
import os
import re
import stat
import time

import paths
from projects import ProjectError, _read, _write, invalid

STAGES = ('triage', 'needs', 'ready', 'building', 'review', 'done', 'hold')
STAGE_LABELS = {'triage': 'Triage', 'needs': 'Needs you', 'ready': 'Ready', 'building': 'Building',
                'review': 'Review', 'done': 'Done', 'hold': 'On hold'}
ROLES = ('triage', 'fix', 'review', 'other')
FEEDS = ('manual', 'teams', 'github', 'email')
RESERVED = ('B',)  # backlog items (#B12) fold in later
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')
PREFIX = re.compile(r'[A-Z]{1,3}')
ITEM_ID = re.compile(r'([A-Z]{1,3})-([0-9]{1,6})')
PANE = re.compile(r'%[0-9]{1,6}')
CHAT = re.compile(r'[A-Za-z0-9_-]{1,80}')
ASK_ID = re.compile(r'[0-9a-f]{8,32}')
TITLE_LIMIT = 200
TEXT_LIMIT = 4000
OPTION_LIMIT, OPTIONS = 40, 6
REF_LIMIT, REFS = 300, 50
FIELD_LIMIT = 300
FILE_LIMIT = 8 * 1024 * 1024  # a project's events; far above years of items
_CACHE = {}


def folder():
    """The items folder: the one Codex tabs may write."""
    return paths.items()


def _registry_path():
    return paths.work() / 'projects.json'


def _items_path(prefix):
    return folder() / prefix / 'items.jsonl'


@contextlib.contextmanager
def _lock_in(place):
    place.mkdir(mode=0o700, parents=True, exist_ok=True)
    if place.is_symlink():
        raise invalid(f'{place} is a link. Tether does not open it.')
    handle = os.open(place / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)  # also releases the lock


def locked():
    return _lock_in(folder())


# ---- text checks ----

def _line(value, limit, what, required=True):
    """One line of text, spaces folded. Too long is refused, not cut: a cut question can change its meaning."""
    if value is None and not required:
        return ''
    if not isinstance(value, str):
        raise invalid(f'Invalid {what}.')
    value = ' '.join(value.split())
    if required and not value:
        raise invalid(f'Write the {what}.')
    if len(value) > limit:
        raise invalid(f'The {what} is over {limit:,} characters.')
    return value


def _text(value, limit, what):
    """Several lines allowed; control characters other than new lines go."""
    if not isinstance(value, str):
        raise invalid(f'Invalid {what}.')
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value.replace('\r\n', '\n')).strip()
    if not value:
        raise invalid(f'Write the {what}.')
    if len(value) > limit:
        raise invalid(f'The {what} is over {limit:,} characters.')
    return value


def _refs(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > REFS:
        raise invalid(f'Give at most {REFS} links.')
    out = []
    for value in values:
        ref = _line(value, REF_LIMIT, 'link')
        if ref not in out:
            out.append(ref)
    return out


def _options(values):
    if not isinstance(values, list) or not 1 <= len(values) <= OPTIONS:
        raise invalid(f'Give 1 to {OPTIONS} options.')
    out = []
    for value in values:
        option = _line(value, OPTION_LIMIT, 'option')
        if option.casefold() in (o.casefold() for o in out):
            raise invalid(f'The option {option} is there twice.')
        out.append(option)
    return out


# ---- projects ----

def _clean_project(data, existing=None):
    """A project record from the form or the command. Name and prefix cannot change after the project exists:
    item ids and folders use them."""
    if not isinstance(data, dict):
        raise invalid('Invalid project.')
    name = existing['name'] if existing else data.get('name')
    prefix = existing['prefix'] if existing else data.get('prefix')
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise invalid('A project name has lower-case letters, digits and dashes, up to 32.')
    if not isinstance(prefix, str) or not PREFIX.fullmatch(prefix):
        raise invalid('An item prefix is 1 to 3 capital letters.')
    feed = data.get('feed') or 'manual'
    if feed not in FEEDS:
        raise invalid('Choose a feed.')
    jira = _line(data.get('jira'), 20, 'Jira key', required=False)
    if jira and not re.fullmatch(r'[A-Z][A-Z0-9]{0,19}', jira):
        raise invalid('A Jira key is capital letters and digits, like BS.')
    record = {
        'name': name, 'prefix': prefix,
        'folder': _line(data.get('folder'), 1024, 'folder'),
        'triage': _line(data.get('triage'), 64, 'triage session', required=False) or f'{name}-triage',
        'work': _line(data.get('work'), 64, 'work session', required=False) or name,
        'feed': feed,
        'feedTarget': _line(data.get('feedTarget'), FIELD_LIMIT, 'feed target', required=False) if feed != 'manual' else '',
        'jira': jira,
        'qaStatus': _line(data.get('qaStatus'), 64, 'QA status', required=False) or 'QA Testing',
        'jiraUser': _line(data.get('jiraUser'), 128, 'Jira user', required=False),
    }
    if feed != 'manual' and not record['feedTarget']:
        raise invalid('Name where the feed reads from.')
    return record


def registry():
    """{'revision': n, 'projects': [records, by name]}. A broken file reads as empty rather than stopping the board."""
    try:
        data = json.loads(_read(_registry_path()) or '{}')
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    found = data.get('projects') if isinstance(data.get('projects'), dict) else {}
    projects = []
    for name, record in sorted(found.items()):
        if isinstance(record, dict) and NAME.fullmatch(name) and PREFIX.fullmatch(str(record.get('prefix', ''))):
            if all(p['prefix'] != record['prefix'] for p in projects):  # a hand edit that repeats a prefix: first wins
                projects.append(dict(record, name=name))
    revision = data.get('revision') if isinstance(data.get('revision'), int) else 0
    return {'revision': revision, 'projects': projects}


def project(name):
    found = next((p for p in registry()['projects'] if p['name'] == name), None)
    if not found:
        raise ProjectError(404, f'No project {name}. Add it first.')
    return found


def save_project(data, revision, create):
    """Add a project (create) or change one. revision: the registry revision the writer last saw."""
    if not isinstance(revision, int):
        raise invalid('Reload the board and try again.')
    with _lock_in(paths.work()):
        current = registry()
        if current['revision'] != revision:
            raise ProjectError(409, 'The projects changed in another tab. Reload and try again.')
        by_name = {p['name']: p for p in current['projects']}
        if create:
            record = _clean_project(data)
            if record['name'] in by_name:
                raise ProjectError(409, f'A project named {record["name"]} already exists.')
            if record['prefix'] in RESERVED:
                raise invalid(f'The prefix {record["prefix"]} is kept for the backlog.')
            if any(p['prefix'] == record['prefix'] for p in current['projects']):
                raise ProjectError(409, f'Another project already uses the prefix {record["prefix"]}.')
        else:
            existing = by_name.get(data.get('name') if isinstance(data, dict) else None)
            if not existing:
                raise ProjectError(404, 'That project is gone. Reload and try again.')
            record = _clean_project(data, existing)
        by_name[record['name']] = record
        stored = {name: {k: v for k, v in p.items() if k != 'name'} for name, p in sorted(by_name.items())}
        _write(_registry_path(), json.dumps({'revision': revision + 1, 'projects': stored}, indent=2) + '\n')
        return registry()


# ---- events ----

def _open_project(prefix, create=False):
    """A descriptor for the project's events folder, opened step by step without following links: an agent that
    swaps a folder for a link between a check and an open cannot steer a write outside the items folder. None when
    the folder is missing and create is False."""
    if not PREFIX.fullmatch(prefix):
        raise invalid('Invalid prefix.')
    root = folder()
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        top = os.open(root, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise invalid(f'{root} is a link or not a folder. Tether does not open it.') from exc
    try:
        if create:
            try:
                os.mkdir(prefix, 0o700, dir_fd=top)
            except FileExistsError:
                pass
        try:
            return os.open(prefix, flags, dir_fd=top)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise invalid(f'{root / prefix} is a link or not a folder. Tether does not open it.') from exc
    finally:
        os.close(top)


def _open_events(place, flags):
    """items.jsonl in an open folder: no links, no pipes or devices (a pipe would hang the reader)."""
    try:
        handle = os.open('items.jsonl', flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=place)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProjectError(413, 'The items file is a link. Tether does not open it.') from exc
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_LIMIT:
        os.close(handle)
        raise ProjectError(413, 'The items file is too large or not a plain file. Tether does not read it.')
    return handle


def _strings(value, limit):
    return isinstance(value, list) and len(value) <= limit and all(isinstance(v, str) for v in value)


def _valid(event):
    """An event the fold can trust: the envelope and the fields its type uses. Agents write this file by hand too."""
    if not (isinstance(event, dict) and isinstance(event.get('item'), str) and ITEM_ID.fullmatch(event['item'])
            and isinstance(event.get('type'), str) and type(event.get('n')) is int):
        return False
    kind = event['type']
    text_fields = ('title', 'source', 'q', 'text', 'pane', 'role', 'chat', 'name', 'session', 'stack', 'ask',
                   'choice', 'by', 'who', 'ref', 'status', 'error', 'target')
    if any(k in event and not isinstance(event[k], str) for k in text_fields):
        return False
    if 'at' in event and (isinstance(event['at'], bool) or not isinstance(event['at'], (int, float))):
        return False
    if 'stage' in event and event['stage'] not in STAGES:
        return False
    if 'refs' in event and not _strings(event['refs'], REFS):
        return False
    if 'options' in event and not _strings(event['options'], OPTIONS):
        return False
    if 'qa' in event and not isinstance(event['qa'], bool):
        return False
    if kind in ('link', 'unlink') and not PANE.fullmatch(event.get('pane', '')):
        return False
    if kind == 'link' and event.get('role') not in ROLES:
        return False
    if kind == 'jira' and any(k in event and not _strings(event[k], 50) for k in ('tickets', 'owners')):
        return False
    return True


def _events(prefix):
    """The project's events, read once per change of the file. A torn, hand-broken or ill-typed line is skipped.
    Each event carries n, its place in the file, strictly increasing: the server's watcher remembers the last n."""
    place = _open_project(prefix)
    if place is None:
        return []
    try:
        handle = _open_events(place, os.O_RDONLY)
    finally:
        os.close(place)
    if handle is None:
        return []
    with os.fdopen(handle, 'rb') as source:
        info = os.fstat(source.fileno())
        key = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        cached = _CACHE.get(prefix)
        if cached and cached[0] == key:
            return cached[1]
        raw = source.read(FILE_LIMIT + 1)
    events = []
    for line in raw.decode('utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if _valid(event) and (not events or event['n'] > events[-1]['n']):
            events.append(event)
    _CACHE[prefix] = (key, events)
    return events


def _append(prefix, event):
    """Add one event as the next n. Call under the lock. A torn last line (a crash mid-write) gets its own line end
    first, so the new event is never glued to it."""
    events = _events(prefix)
    event = dict(event, n=(events[-1]['n'] if events else 0) + 1)
    data = (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    place = _open_project(prefix, create=True)
    try:
        handle = _open_events(place, os.O_CREAT | os.O_RDWR | os.O_APPEND)
    finally:
        os.close(place)
    try:
        size = os.fstat(handle).st_size
        if size and os.pread(handle, 1, size - 1) != b'\n':
            data = b'\n' + data
        os.write(handle, data)
        os.fsync(handle)
    finally:
        os.close(handle)
    return event


def _history_text(event):
    kind = event['type']
    if kind == 'new':
        return f'Added: {event.get("title", "")}' if event.get('by') == 'mak' else 'Created'
    if kind == 'ask':
        return f'Asked: {event.get("q", "")}'
    if kind == 'answer':
        return f'You chose: {event["choice"]}' if event.get('choice') else f'You replied: {event.get("text", "")}'
    if kind == 'stage':
        moved = f'Moved to {STAGE_LABELS.get(event.get("stage"), event.get("stage"))}'
        return f'{moved}: {event["text"]}' if event.get('text') else moved
    if kind == 'link':
        return f'Tab linked: {event.get("name") or event.get("pane")} ({event.get("role")})'
    if kind == 'unlink':
        return f'Tab unlinked: {event.get("pane")}'
    if kind == 'note':
        return event.get('text', '')
    if kind == 'qa':
        return 'Marked: needs QA testing' if event.get('qa') else 'Marked: no QA needed'
    if kind == 'refs':
        return 'Links added: ' + ', '.join(event.get('refs') or [])
    if kind == 'title':
        return f'Renamed: {event.get("title", "")}'
    if kind == 'jira':
        return event.get('text', '')
    if kind in ('sent', 'fix', 'tab'):
        return event.get('text', '')
    return ''


def _apply(item, event):
    kind = event['type']
    item['version'] += 1
    item['updated'] = event.get('at', item['updated'])
    if kind == 'ask':
        if item['stage'] != 'needs':
            item['askFrom'] = item['stage']
        item['stage'] = 'needs'
        item['ask'] = {'id': event.get('ask'), 'q': event.get('q', ''), 'options': event.get('options') or [],
                       'pane': event.get('pane', ''), 'chat': event.get('chat', ''), 'at': event.get('at')}
    elif kind == 'answer':
        item['ask'] = None
        item['stage'] = event.get('stage') or item.get('askFrom') or 'triage'
        item['answer'] = {'choice': event.get('choice', ''), 'text': event.get('text', ''), 'at': event.get('at')}
    elif kind == 'stage':
        if event.get('stage') != 'needs':
            item['ask'] = None
        item['stage'] = event.get('stage', item['stage'])
    elif kind == 'link':
        item['tabs'] = [t for t in item['tabs'] if t['pane'] != event.get('pane')]
        item['tabs'].append({k: event.get(k, '') for k in ('pane', 'role', 'chat', 'name', 'session', 'stack')})
    elif kind == 'unlink':
        item['tabs'] = [t for t in item['tabs'] if t['pane'] != event.get('pane')]
    elif kind == 'qa':
        item['qa'] = bool(event.get('qa'))
    elif kind == 'refs':
        item['refs'] += [r for r in event.get('refs') or [] if r not in item['refs']]
    elif kind == 'title':
        item['title'] = event.get('title') or item['title']
    elif kind == 'jira':
        item['jira'] = {k: event.get(k) for k in ('status', 'error', 'tickets', 'owners', 'target') if k in event}
    if kind == 'stage' and event.get('stage') == 'done':
        item['doneAt'] = event.get('at')
    elif kind in ('stage', 'ask', 'answer') and item['stage'] != 'done':
        item['doneAt'] = None
    text = _history_text(event)
    if text:
        entry = {'at': event.get('at'), 'by': event.get('by', 'agent'), 'text': text}
        if event.get('ref'):
            entry['ref'] = event['ref']
        item['history'].append(entry)


def fold(record, events=None):
    """{item id: current state}, in creation order. Events for an item that was never created, or with another
    project's prefix, are ignored."""
    project_name, prefix = record['name'], record['prefix']
    items = {}
    for event in _events(prefix) if events is None else events:
        if not event['item'].startswith(prefix + '-'):
            continue
        if event['type'] == 'new':
            if event['item'] in items:
                continue
            items[event['item']] = {
                'id': event['item'], 'project': project_name, 'title': event.get('title', ''), 'stage': 'triage',
                'qa': bool(event.get('qa')), 'ask': None, 'askFrom': None, 'answer': None,
                'refs': list(event.get('refs') or []), 'tabs': [], 'history': [], 'source': event.get('source', ''),
                'created': event.get('at'), 'updated': event.get('at'), 'doneAt': None, 'jira': None, 'version': 0}
            item = items[event['item']]  # version: its count of events, so the new event makes it 1
            if event.get('stage') in STAGES and event['stage'] != 'needs':
                item['stage'] = event['stage']
            _apply(item, event)
        elif event['item'] in items:
            _apply(items[event['item']], event)
    return items


def board():
    """Everything the board needs: projects and every item, newest change first per project."""
    reg = registry()
    items = []
    for record in reg['projects']:
        try:
            items.extend(fold(record).values())
        except ProjectError:
            continue
    return {'revision': reg['revision'], 'projects': reg['projects'], 'items': items, 'stages': list(STAGES)}


def find(item_id):
    """(project name, item) for an id like T-022. The prefix names the project."""
    found = ITEM_ID.fullmatch(item_id.strip().upper()) if isinstance(item_id, str) else None
    if not found:
        raise invalid('Write an item id like T-022.')
    record = next((p for p in registry()['projects'] if p['prefix'] == found[1]), None)
    if not record:
        raise ProjectError(404, f'No project uses the prefix {found[1]}.')
    item = fold(record).get(found[0])
    if not item:
        raise ProjectError(404, f'No item {found[0]}.')
    return record, item


def _write_event(item_id, version, event, check=None):
    """Append one event to an existing item, under the lock, over the version the writer saw (None: the current
    one, for agent commands that read and write in one step). check(item) may refuse the write."""
    with locked():
        record, item = find(item_id)
        if version is not None and version != item['version']:
            raise ProjectError(409, f'{item["id"]} changed since you loaded it. Reload and try again.')
        if check:
            check(item)
        _append(record['prefix'], dict(event, item=item['id'], at=time.time()))
        return fold(record)[item['id']]


def _next_id(prefix, items):
    taken = [int(ITEM_ID.fullmatch(i)[2]) for i in items if ITEM_ID.fullmatch(i)]
    return f'{prefix}-{max(taken, default=0) + 1:03d}'


def new(project_name, title, by, who='', refs=None, source='', item_id=None, stage='triage', qa=False):
    """Create an item. item_id: keep an id from an import; it must use the project's prefix and be free."""
    title = _line(title, TITLE_LIMIT, 'title')
    refs = _refs(refs)
    source = _line(source, FIELD_LIMIT, 'source', required=False)
    if stage not in STAGES or stage == 'needs':
        raise invalid('A new item starts in Triage, Ready, Building, Review, Done or On hold. Use ask for Needs you.')
    with locked():
        record = project(project_name)
        items = fold(record)
        if item_id is not None:
            found = ITEM_ID.fullmatch(item_id.strip().upper()) if isinstance(item_id, str) else None
            if not found or found[1] != record['prefix']:
                raise invalid(f'An id in {record["name"]} looks like {record["prefix"]}-001.')
            item_id = found[0]
            if item_id in items:
                raise ProjectError(409, f'{item_id} already exists.')
        else:
            item_id = _next_id(record['prefix'], items)
        event = {'item': item_id, 'type': 'new', 'at': time.time(), 'by': by, 'who': who, 'title': title,
                 'refs': refs, 'source': source, 'stage': stage, 'qa': bool(qa)}
        _append(record['prefix'], event)
        return fold(record)[item_id]


def ask(item_id, question, options, pane='', chat='', by='agent', who='', version=None):
    """An agent asks Mak. The item moves to Needs you. pane and chat name the asking tab: the answer goes there."""
    question = _text(question, TEXT_LIMIT, 'question')
    options = _options(options)
    if pane and not PANE.fullmatch(pane):
        raise invalid('Invalid pane.')
    if chat and not CHAT.fullmatch(chat):
        raise invalid('Invalid chat.')

    def check(item):
        if item['stage'] in ('done',):
            raise invalid(f'{item["id"]} is Done. Move it back before you ask.')
    return _write_event(item_id, version, {'type': 'ask', 'by': by, 'who': who, 'ask': os.urandom(8).hex(),
                                           'q': question, 'options': options, 'pane': pane, 'chat': chat}, check)


def answer_stage(item, choice):
    """Where an answer moves the item: Build approves a build, Hold parks it, anything else returns it."""
    key = (choice or '').casefold()
    if key == 'build':
        return 'ready'
    if key == 'hold':
        return 'hold'
    back = item.get('askFrom') or 'triage'
    return back if back != 'needs' else 'triage'


def answer(item_id, ask_id, version, choice=None, text=None):
    """Mak answers the open question, by one of its options or in words. A stale ask id (the agent asked again, or
    someone answered already) is refused."""
    if not isinstance(ask_id, str) or not ASK_ID.fullmatch(ask_id):
        raise invalid('That question is gone. Reload and try again.')
    if (choice is None) == (text is None):
        raise invalid('Choose an option or write a reply.')
    event = {'type': 'answer', 'by': 'mak', 'ask': ask_id}
    if choice is not None:
        event['choice'] = _line(choice, OPTION_LIMIT, 'option')
    else:
        event['text'] = _text(text, TEXT_LIMIT, 'reply')

    def check(item):
        current = item.get('ask') or {}
        if item['stage'] != 'needs' or current.get('id') != ask_id:
            raise ProjectError(409, f'{item["id"]} has no open question like that now. Reload and try again.')
        if choice is not None and event['choice'] not in current.get('options', []):
            raise invalid('Choose one of the options.')
        event['stage'] = answer_stage(item, event.get('choice'))
        event['pane'], event['chat'] = current.get('pane', ''), current.get('chat', '')
    return _write_event(item_id, version, event, check)


def stage(item_id, to, by, who='', text='', version=None, ref=''):
    if to not in STAGES:
        raise invalid('Stage is one of: ' + ', '.join(STAGES) + '.')
    if to == 'needs':
        raise invalid('Use ask to move an item to Needs you: it needs a question.')
    text = _line(text, TEXT_LIMIT, 'text', required=False)
    ref = _line(ref, REF_LIMIT, 'link', required=False)
    event = {'type': 'stage', 'by': by, 'who': who, 'stage': to}
    if text:
        event['text'] = text
    if ref:
        event['ref'] = ref
    return _write_event(item_id, version, event)


def link(item_id, pane, role, by='agent', who='', chat='', name='', session='', stack='', version=None):
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise invalid('Give the pane, like %12.')
    if role not in ROLES:
        raise invalid('Role is one of: ' + ', '.join(ROLES) + '.')
    if chat and not CHAT.fullmatch(chat):
        raise invalid('Invalid chat.')
    event = {'type': 'link', 'by': by, 'who': who, 'pane': pane, 'role': role, 'chat': chat,
             'name': _line(name, 120, 'tab name', required=False),
             'session': _line(session, 64, 'session', required=False),
             'stack': _line(stack, 64, 'stack', required=False)}
    return _write_event(item_id, version, event)


def unlink(item_id, pane, by='agent', who='', version=None):
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise invalid('Give the pane, like %12.')
    return _write_event(item_id, version, {'type': 'unlink', 'by': by, 'who': who, 'pane': pane})


def note(item_id, text, by, who='', ref='', version=None, refs=None):
    event = {'type': 'note', 'by': by, 'who': who, 'text': _text(text, TEXT_LIMIT, 'note')}
    ref = _line(ref, REF_LIMIT, 'link', required=False)
    if ref:
        event['ref'] = ref
    return _write_event(item_id, version, event)


def add_refs(item_id, refs, by, who='', version=None):
    refs = _refs(refs)
    if not refs:
        raise invalid('Give a link.')
    return _write_event(item_id, version, {'type': 'refs', 'by': by, 'who': who, 'refs': refs})


def set_qa(item_id, value, by, who='', version=None):
    if not isinstance(value, bool):
        raise invalid('Invalid QA flag.')
    return _write_event(item_id, version, {'type': 'qa', 'by': by, 'who': who, 'qa': value})


def record(item_id, kind, text, by='tether', **fields):
    """A Tether-made history line (sent, jira): no version check, it follows a write that had one."""
    if kind not in ('sent', 'jira', 'fix', 'tab'):
        raise invalid('Invalid record.')
    return _write_event(item_id, None, dict(fields, type=kind, by=by, text=_line(text, TEXT_LIMIT, 'text')))


def needs(items):
    """Items that wait on Mak, oldest question first."""
    waiting = [i for i in items if i['stage'] == 'needs' and i.get('ask')]
    return sorted(waiting, key=lambda i: i['ask'].get('at') or 0)


def tab_marks(items):
    """{pane: {'item', 'stage', 'title'}} for the sidebar. A pane linked to several items shows the newest change."""
    out = {}
    for item in sorted(items, key=lambda i: i.get('updated') or 0):
        if item['stage'] == 'done' and item.get('doneAt') and time.time() - item['doneAt'] > 7 * 86400:
            continue
        for tab in item['tabs']:
            out[tab['pane']] = {'item': item['id'], 'stage': item['stage'], 'title': item['title']}
    return out
