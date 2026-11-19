"""Read-only Microsoft To Do and Planner tasks for Reminders. Graph is only read through graph_client.Client.

paths.task_sources() holds settings.json and mirror.json. The server and CLI share the locks and replace the files
atomically. A source publishes only after all of its pages were read. Only an explicit copy writes to the backlog.

To Do uses the web app home: Graph exposes no task web URL and the app's task routes do not consistently use Graph
IDs (https://techcommunity.microsoft.com/discussions/to-do/how-to-link-to-todo-tasks/502415).
Planner uses the current basic-plan task route reproduced in this example:
https://community.powerplatform.com/forums/thread/details/?threadid=56c509e3-b3bb-f011-bbd2-000d3a151ca1
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import backlog
import graph_client
import paths
import reminders

PREFIX = '/mobile/api/tasks'
POSTS = {f'{PREFIX}/{action}' for action in ('sync', 'settings', 'backlog')}
LABELS = {'todo': 'Microsoft To Do', 'planner': 'Microsoft Planner'}
DEFAULTS = {'enabled': True, 'alerts': False}
EVERY = 300
TODO_HOME = 'https://to-do.office.com/tasks/'
PLANNER_HOME = 'https://planner.cloud.microsoft/'
UNNAMED_PLAN = 'Plan without access'
TODO_FIELDS = 'id,title,status,importance,dueDateTime,reminderDateTime,isReminderOn,lastModifiedDateTime'
TODO_FILTER = "status ne 'completed'"
WINDOWS_ZONES = {'India Standard Time': 'Asia/Kolkata', 'GMT Standard Time': 'Europe/London',
                 'W. Europe Standard Time': 'Europe/Berlin', 'Eastern Standard Time': 'America/New_York',
                 'Central Standard Time': 'America/Chicago', 'Mountain Standard Time': 'America/Denver',
                 'Pacific Standard Time': 'America/Los_Angeles'}


class TaskError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextlib.contextmanager
def _lock(name, *, blocking=True):
    directory = paths.task_sources()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def _read(name):
    try:
        data = json.loads((paths.task_sources() / name).read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except ValueError:
        pass
    raise TaskError(f'Cannot read task sources {name}.', 500)


def _save(name, data):
    directory = paths.task_sources()
    fd, temp = tempfile.mkstemp(prefix=f'.{name}-', dir=directory)
    try:
        # The default ASCII escaping also handles unusual Unicode in external data.
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, directory / name)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _account_snapshots(mirror, account):
    return mirror['accounts'].setdefault(json.dumps(account), {}) if account is not None else {}


def _mirror():
    mirror = _read('mirror.json')
    mirror.setdefault('accounts', {})
    for source in LABELS:
        legacy = mirror.pop(source, {})
        account = legacy.pop('account', None)
        if account is not None:
            _account_snapshots(mirror, account).setdefault(source, legacy)
    return mirror


def _snapshot(snapshots, source):
    return snapshots.get(source, {'tasks': [], 'syncedAt': None, 'complete': False, 'error': '',
                                  'backlog': {}, 'reminder': {}})


def _command(connection):
    kinds = [kind for kind in graph_client.SCOPES if kind == 'tasks' or connection.get(kind)]
    return 'tether graph connect --tenant <tenant id> --client <app id> --feeds ' + ','.join(kinds)


def _one_line(text):
    if not isinstance(text, str):
        return ''
    return ' '.join(''.join(char if char.isprintable() else ' ' for char in text).split())[:200].rstrip()


def _graph_id(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or len(value) > 1000
            or not value.isprintable() or any(char.isspace() for char in value)):
        raise TaskError('Microsoft returned a task, list, or plan without a valid ID.', 502)
    return value


def _time(value):
    """(date, epoch, uncertain), with timed values shown in the reminder display zone."""
    zone = 'UTC'
    if isinstance(value, dict):
        zone, value = value.get('timeZone'), value.get('dateTime')
    if not isinstance(value, str):
        return None, None, False
    try:
        if re.fullmatch(r'\d{4}-\d\d-\d\d', value):
            return date.fromisoformat(value).isoformat(), None, False
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        uncertain = False
        if parsed.tzinfo is None:
            try:
                local = ZoneInfo(WINDOWS_ZONES.get(zone, zone))
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                local, uncertain = timezone.utc, True
            parsed = parsed.replace(tzinfo=local)
        return parsed.astimezone(reminders.ZONE).date().isoformat(), parsed.timestamp(), uncertain
    except (OverflowError, OSError, ValueError):
        raise TaskError('Microsoft returned an invalid task date.', 502) from None


def _task(source, item, name, link):
    key = source + ':' + _graph_id(item.get('id'))
    day, due_at, due_uncertain = _time(item.get('dueDateTime'))
    reminder_at, reminder_uncertain = None, False
    if source == 'todo' and item.get('isReminderOn') is True:
        _, reminder_at, reminder_uncertain = _time(item.get('reminderDateTime'))
    importance = item.get('importance')
    _, modified, _ = _time(item.get('lastModifiedDateTime'))
    row = {'key': key, 'source': source, 'list': name,
           'title': _one_line(item.get('title')) or 'Untitled task',
           'due': {'date': day, 'at': due_at} if day else None, 'reminderAt': reminder_at,
           'importance': importance if importance in ('low', 'normal', 'high') else 'normal',
           'link': link, 'modified': modified}
    if due_uncertain or reminder_uncertain:
        row['timeUncertain'] = True
    return row


def _query(path, **fields):
    return path + '?' + urlencode({'$' + key: value for key, value in fields.items()}, safe='$,', quote_via=quote)


def _pages(client, path, *, alternatives=()):
    """Read all pages within the Graph cap. Retry an unsupported query only on the first request."""
    for attempt, first in enumerate((path, *alternatives)):
        try:
            page = client.get(first)
            break
        except graph_client.GraphError as exc:
            if attempt == len(alternatives) or exc.status not in (400, 501):
                raise
    for index in range(graph_client.PAGE_LIMIT):
        rows = page.get('value') if isinstance(page, dict) else None
        if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
            raise TaskError('Microsoft returned an invalid task page.', 502)
        yield from rows
        link = page.get('@odata.nextLink')
        if link is None:
            return
        if not isinstance(link, str) or not link.startswith(graph_client.GRAPH + '/'):
            raise TaskError('Microsoft returned an invalid task page link.', 502)
        if index + 1 == graph_client.PAGE_LIMIT:
            raise TaskError(f'Microsoft returned more than {graph_client.PAGE_LIMIT} pages. The last full sync is kept.', 502)
        page = client.get(link)


def _todo(client):
    lists = '/me/todo/lists'
    for task_list in _pages(client, _query(lists, select='id,displayName')):
        list_id = _graph_id(task_list.get('id'))
        name = _one_line(task_list.get('displayName')) or 'Tasks'
        path = f"{lists}/{quote(list_id, safe='')}/tasks"
        for item in _pages(client, _query(path, filter=TODO_FILTER, select=TODO_FIELDS),
                           alternatives=(_query(path, select=TODO_FIELDS),)):
            if item.get('status') != 'completed':
                yield dict(_task('todo', item, name, TODO_HOME), listId=list_id)


_PLAN_NAMES = {}  # (tenant, app, account, plan) -> (expires, title), only used under the sync lock


def _planner(client, config, now):
    for key, (expires, _) in list(_PLAN_NAMES.items()):
        if expires <= now:
            del _PLAN_NAMES[key]
    for item in _pages(client, '/me/planner/tasks'):
        if item.get('percentComplete') == 100:
            continue
        plan = quote(_graph_id(item.get('planId')), safe='')
        task = quote(_graph_id(item.get('id')), safe='')
        cache_key = (*_account(config), plan)
        if cache_key not in _PLAN_NAMES:
            path = f'/planner/plans/{plan}'
            try:
                try:
                    found = client.get(_query(path, select='title'))
                except graph_client.GraphError as exc:
                    if exc.status not in (400, 501):
                        raise
                    found = client.get(path)
            except graph_client.GraphError as exc:
                # A plan Mak can no longer open (left the group, plan deleted) must not hide his other tasks.
                if exc.status not in (403, 404):
                    raise
                found = {'title': UNNAMED_PLAN}
            name = _one_line(found.get('title') if isinstance(found, dict) else None)
            if not name:
                raise TaskError('Microsoft returned a Planner plan without a title.', 502)
            _PLAN_NAMES[cache_key] = (now + 3600, name)
        name = _PLAN_NAMES[cache_key][1]
        link = f'{PLANNER_HOME}webui/v1/plan/{plan}/view/board/task/{task}'
        yield _task('planner', item, name, link)


def _settings():
    data = _read('settings.json')
    settings = {}
    for key in LABELS:
        value = data.get(key, {})
        if not isinstance(value, dict) or any(type(value.get(name, default)) is not bool
                                              for name, default in DEFAULTS.items()):
            raise TaskError('Cannot read task source settings.', 500)
        settings[key] = {name: value.get(name, default) for name, default in DEFAULTS.items()}
    return settings


def _account(config):
    return tuple(config.get(field) for field in ('tenant', 'client', 'account'))


def _overlapping_moves(rows, previous):
    """Match a known and a new key in different lists with the same title and reminder time.

    A move plus a rename inside one poll cannot be detected. Leave ambiguous matches as separate tasks.
    """
    old_keys = {row['key'] for row in previous}
    groups = defaultdict(list)
    for row in rows.values():
        if row['reminderAt'] is not None:
            groups[row['title'], row['reminderAt']].append(row)
    moves = {}
    for group in groups.values():
        old = [row for row in group if row['key'] in old_keys]
        new = [row for row in group if row['key'] not in old_keys]
        for before in old:
            candidates = [row for row in new if row['listId'] != before['listId']]
            if len(candidates) == 1:
                after = candidates[0]
                if sum(row['listId'] != after['listId'] for row in old) == 1:
                    moves[before['key']] = after['key']
    return moves


def sync():
    """Run one poll here. Another server or CLI with a poll in progress already covers this request."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_lock('.sync', blocking=False))
        except BlockingIOError:
            return
        connection = graph_client.load()
        config, _ = connection
        if not graph_client.status(connection)['tasks']:
            return
        identity = _account(config)
        account = list(identity)
        with _lock('.lock'):
            settings = _settings()
        for source in LABELS:
            if not settings[source]['enabled']:
                continue
            now = time.time()
            try:
                client = graph_client.Client(config)
                found = _todo(client) if source == 'todo' else _planner(client, config, now)
                rows = {row['key']: row for row in found}
            except graph_client.GraphError as exc:
                # Do not persist Graph response text, which can contain request URLs or task data.
                error = f'Microsoft could not sync this source (HTTP {exc.status}).' if exc.status else (
                    'Microsoft did not finish reading this source.')
            except TaskError as exc:
                error = str(exc)
            else:
                error = ''
            with graph_client.locked(), _lock('.lock'):
                connection = graph_client.load()
                if not graph_client.status(connection)['tasks'] or _account(connection[0]) != identity:
                    return  # another sign-in or a disconnect superseded this read
                current = _settings()[source]
                if not current['enabled']:
                    continue
                mirror = _mirror()
                snapshots = _account_snapshots(mirror, account)
                saved = _snapshot(snapshots, source)
                complete = not error
                if complete and source == 'todo' and current['alerts']:
                    moves = _overlapping_moves(rows, saved['tasks'])
                    resolved = {old: new for old, new in saved.get('moves', {}).items()
                                if old not in rows and new in rows}
                    try:
                        saved['reminder'] = reminders.sync_source(
                            LABELS[source], account, saved['reminder'], rows, time.time(),
                            suppressed=set(moves.values()), moved=resolved)
                    except (reminders.ReminderError, OSError):
                        error = 'Tether could not update the linked reminders.'
                    else:
                        saved['moves'] = moves
                elif complete:
                    saved.pop('moves', None)
                if not error:
                    saved.update(tasks=list(rows.values()), syncedAt=time.time())
                saved.update(complete=complete, error=error)
                snapshots[source] = saved
                _save('mirror.json', mirror)


_WAKE = threading.Event()
_START = threading.Lock()
_THREAD = None


def _tick():
    """One poll that never ends the thread. Log only the error type: a message can hold task data."""
    try:
        sync()
    except (TaskError, OSError):
        print('Task sources could not read or save their local files.', file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 - a daemon poller must survive an unexpected Graph body or bug
        print(f'Task sources sync failed ({type(exc).__name__}). The next poll tries again.', file=sys.stderr, flush=True)


def _run():
    while True:
        _WAKE.clear()
        _tick()
        _WAKE.wait(EVERY)


def start():
    global _THREAD
    with _START:
        if _THREAD is None:
            _THREAD = threading.Thread(target=_run, name='task-sources', daemon=True)
            _THREAD.start()


def sync_now():
    """Queue an immediate poll on the server's one sync thread."""
    _WAKE.set()
    start()


def _syncing():
    if _WAKE.is_set():
        return True
    try:
        with _lock('.sync', blocking=False):
            return False
    except BlockingIOError:
        return True


def _sort_key(row, now, today):
    due = row['due']
    title = row['title'].casefold(), row['key']
    if due is None:
        return (2, '', 0, *title)
    at = due['at']
    overdue = at < now if at is not None else due['date'] < today
    return (0 if overdue else 1, due['date'], at if at is not None else float('-inf'), *title)


def listing():
    with _lock('.lock'):
        settings = _settings()
        mirror = _mirror()
        loaded = graph_client.load()
    connection = graph_client.status(loaded)
    account = list(_account(loaded[0])) if connection['tasks'] else None
    snapshots = _account_snapshots(mirror, account)
    syncing = _syncing()
    sources, tasks = [], []
    for key, label in LABELS.items():
        saved, setting = _snapshot(snapshots, key), settings[key]
        why = command = ''
        if not setting['enabled']:
            state, why = 'off', 'This source is disabled.'
        elif not connection['tasks']:
            state = 'off'
            why = connection.get('why') or 'The Microsoft connection has no Tasks.Read permission.'
            if why == graph_client.NOT_CONNECTED:
                why = 'Microsoft 365 is not connected.'
            command = _command(connection)
        elif syncing or (not saved['syncedAt'] and not saved['error']):
            state = 'syncing'
        elif saved['error']:
            state, why = 'error', saved['error']
        else:
            state = 'ok'
        rows = saved['tasks'] if setting['enabled'] else []
        sources.append({'id': key, 'label': label, **setting, 'state': state, 'why': why, 'command': command,
                        'syncedAt': saved['syncedAt'], 'complete': saved['complete'], 'count': len(rows)})
        for row in rows:
            task = dict(row, backlog=saved['backlog'].get(row['key']),
                        reminder=saved['reminder'].get(row['key'], {}).get('id'))
            task.pop('listId', None)
            tasks.append(task)
    now = time.time()
    today = datetime.fromtimestamp(now, reminders.ZONE).date().isoformat()
    return {'sources': sources, 'tasks': sorted(tasks, key=lambda row: _sort_key(row, now, today))}


def settings_for(source, **changes):
    if not isinstance(source, str) or source not in LABELS:
        raise TaskError('Choose Microsoft To Do or Planner.')
    if any(key not in DEFAULTS or type(value) is not bool for key, value in changes.items()):
        raise TaskError('Task settings must be true or false.')
    with _lock('.lock'):
        settings = _settings()
        settings[source].update(changes)
        _save('settings.json', settings)
    if changes.get('enabled') or changes.get('alerts'):
        sync_now()


def to_backlog(key, project=''):
    if not isinstance(key, str) or ':' not in key or key.partition(':')[0] not in LABELS:
        raise TaskError('Choose a Microsoft task.')
    if (not isinstance(project, str) or not re.fullmatch(r'[\w .-]{0,80}', project)
            or project != project.strip()):
        raise TaskError('Choose a valid backlog project.')
    source = key.partition(':')[0]
    with graph_client.locked(), _lock('.lock'):
        connection = graph_client.load()
        if not graph_client.status(connection)['tasks']:
            raise TaskError('That task is no longer in the list.', 404)
        mirror = _mirror()
        saved = _snapshot(_account_snapshots(mirror, _account(connection[0])), source)
        if key in saved['backlog']:
            return {'id': saved['backlog'][key], 'created': False}
        copying = saved.setdefault('copying', {})
        try:
            if key in copying:
                rid, created = copying[key], False
                if not backlog.has_id(rid):
                    raise TaskError(f'Tether cannot confirm that the previous copy {rid} was saved. Check the backlog.', 409)
            else:
                row = next((row for row in saved['tasks'] if row['key'] == key), None)
                if row is None or not _settings()[source]['enabled']:
                    raise TaskError('That task is no longer in the list.', 404)

                def reserve(line):
                    copying[key] = 'B' + backlog.ID.search(line)[1]
                    _save('mirror.json', mirror)

                backlog.add(row['title'], project=project, literal=True, before_write=reserve,
                            details=[row['link'], f"From {LABELS[source]}, list {row['list']}"])
                rid, created = copying[key], True
        except backlog.ProjectError as exc:
            raise TaskError(str(exc), exc.status if exc.status in (400, 404, 409) else 500) from None
        saved['backlog'][key] = rid
        del copying[key]
        _save('mirror.json', mirror)
        return {'id': rid, 'created': created}


def handles(path):
    return path.split('?', 1)[0] in POSTS


def get(handler):
    try:
        result = listing()
    except TaskError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Cannot read task sources.'})
    else:
        handler.respond(200, result)


def serve(handler, body):
    action = handler.path.split('?', 1)[0].rsplit('/', 1)[-1]
    try:
        if action == 'backlog':
            result = to_backlog(body.get('key'), body.get('project', ''))
        elif action == 'settings':
            settings_for(body.get('source'), **{key: value for key, value in body.items() if key != 'source'})
            result = listing()
        elif action == 'sync':
            sync_now()
            result = listing()
        else:
            raise TaskError('Not found', 404)
    except TaskError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Cannot save task sources.'})
    else:
        handler.respond(200, result)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tether tasks', description='Read Microsoft task sources.')
    parser.add_argument('command', choices=('status', 'sync'), help='show source status or run one sync')
    args = parser.parse_args(argv)
    try:
        if args.command == 'sync':
            sync()
        sources = listing()['sources']
    except (TaskError, OSError):
        print('Task sources could not read or save their local files.', file=sys.stderr)
        return 1
    for source in sources:
        noun = 'task' if source['count'] == 1 else 'tasks'
        print(f"{source['label']}: {source['state']} ({source['count']} {noun})")
        for field in ('why', 'command'):
            if source[field]:
                print('  ' + source[field])
    return int(args.command == 'sync' and any(source['state'] == 'error' for source in sources))
