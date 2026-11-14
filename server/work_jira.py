"""Done on the work board moves the item's Jira tickets: to Done, or to the project's QA status when it needs QA.

Owner rule (Mak, 12 Nov 2026): Tether moves only tickets assigned to the connected Jira account, which is Mak's.
A ticket assigned to anyone else, or to no one, is never moved; the card says "Owned by <name>: move it yourself or
ask them". A ticket already in the target status needs no move.

The tickets are the item refs that carry the project's Jira key (BS-1340, or its /browse/ link, for key BS). The Mover finds Done items in
the server loop, so a Done from the board and from `tether item stage` both count. Jira calls run on one worker
thread, never in a request or under the store lock. Each move ends in one `jira` event on the item: its history and
its card show the result, and the item stays Done whatever happens in Jira."""
import json
import queue
import re
import threading

import jira_client
import paths
import work_items
from jira_client import JiraError
from projects import ProjectError

NAME_LIMIT = 60
MOVE, OWNER, THERE = 'move', 'owner', 'there'
BROWSE = re.compile(r'https://[a-z0-9-]+\.atlassian\.net/browse/([A-Za-z][A-Za-z0-9]*-[0-9]+)/?', re.I)


def tickets(item, project):
    """The item's Jira keys for this project, in order, once each."""
    key = (project.get('jira') or '').upper()
    if not key:
        return []
    pattern = re.compile(re.escape(key) + r'-[1-9][0-9]{0,9}')
    found = []
    for ref in item.get('refs') or []:
        ticket = ref.strip()
        link = BROWSE.fullmatch(ticket)  # a pasted ticket link: https://<site>.atlassian.net/browse/BS-12
        ticket = (link[1] if link else ticket).upper()
        if pattern.fullmatch(ticket) and ticket not in found:
            found.append(ticket)
    return found


def target(item, project):
    """The status to move to, or None when the item needs QA and the project has no QA status."""
    if item.get('qa'):
        return (project.get('qaStatus') or '').strip() or None
    return 'Done'


def _name(value):
    text = ' '.join(str(value or '').split())
    return text if len(text) <= NAME_LIMIT else text[:NAME_LIMIT - 1] + '…'


def decide(issue, account_id, to):
    """The owner rule, on a fresh read of the ticket. Returns (MOVE | OWNER | THERE, owner name).
    THERE: already in the target status. OWNER: assigned to someone else, or to no one: never move it."""
    fields = issue.get('fields') if isinstance(issue, dict) else None
    fields = fields if isinstance(fields, dict) else {}
    status = fields.get('status') if isinstance(fields.get('status'), dict) else {}
    if isinstance(status.get('name'), str) and status['name'].casefold() == to.casefold():
        return THERE, ''
    assignee = fields.get('assignee')
    if not isinstance(assignee, dict) or not assignee.get('accountId'):
        return OWNER, ''
    if not account_id or assignee['accountId'] != account_id:
        return OWNER, _name(assignee.get('displayName') or 'someone else')
    return MOVE, ''


def owner_note(ticket, name):
    if not name:
        return f'{ticket} has no assignee: move it yourself.'
    return f'{ticket}: Owned by {name}: move it yourself or ask them.'


def _pick(transitions, to):
    """The one transition that ends in the target status, or None. Several with that end: the first one Jira lists."""
    for entry in transitions:
        end = entry.get('to') if isinstance(entry, dict) else None
        if isinstance(end, dict) and isinstance(end.get('name'), str) and end['name'].casefold() == to.casefold() \
                and entry.get('isAvailable', True) is not False and entry.get('id') is not None:
            return entry
    return None


def _move_one(client, ticket, account_id, to):
    """(outcome, note). outcome: 'moved', 'there', 'owner' or 'failed'. Reads the ticket just before the move."""
    kind, name = decide(client.issue(ticket), account_id, to)
    if kind == THERE:
        return 'there', ''
    if kind == OWNER:
        return 'owner', owner_note(ticket, name)
    choices = client.transitions(ticket)
    chosen = _pick(choices, to)
    if not chosen:
        ends = sorted({e['to']['name'] for e in choices if isinstance(e, dict) and isinstance(e.get('to'), dict)
                       and isinstance(e['to'].get('name'), str)})
        return 'failed', f'{ticket}: Jira offers no move to {to}' + (f' (it can go to {", ".join(ends)}).' if ends else '.')
    # Read the owner again just before the move: the list of moves can take seconds (retries). The Only Assignee
    # condition in Jira closes the last gap; without it the connection does not move tickets at all (move()).
    kind, name = decide(client.issue(ticket), account_id, to)
    if kind == THERE:
        return 'there', ''
    if kind == OWNER:
        return 'owner', owner_note(ticket, name)
    try:
        client.transition(ticket, chosen['id'])
    except JiraError as exc:
        if exc.status == 401:
            raise
        # The move may have happened (timeout, 409 for a move at the same time, 5xx): read it again, never post blind.
        try:
            again, _ = decide(client.issue(ticket), account_id, to)
        except JiraError:
            again = None
        if again == THERE:
            return 'moved', ''
        return 'failed', f'{ticket}: {exc}'
    return 'moved', ''


def move(item, project, config=None, client=None):
    """Move the item's tickets. Returns the jira event fields with 'text', or None when it has no tickets."""
    keys = tickets(item, project)
    if not keys:
        return None
    to = target(item, project)
    if to is None:
        return _result('failed', keys, None, [], [], 'This item needs QA, but the project has no QA status. '
                       'Set it in Settings, then use Move again.')
    if config is None:
        config, why = jira_client.load()
        if not config:
            return _result('off', keys, to, [], [], why)
    if config.get('guard') is not True:
        return _result('off', keys, to, [], [], jira_client.GUARD)
    wanted = (project.get('jiraUser') or '').strip().casefold()
    if wanted and wanted not in (config['accountId'].casefold(), (config.get('name') or '').casefold()):
        return _result('failed', keys, to, [], [], f'The project Jira user {_name(project["jiraUser"])} is not the '
                       f'connected account {_name(config.get("name") or config["accountId"])}. Nothing moved.')
    client = client or jira_client.Client(config)
    moved, owners, failed = [], [], []
    for ticket in keys:
        try:
            outcome, note = _move_one(client, ticket, config['accountId'], to)
        except JiraError as exc:
            if exc.status == 401:
                return _result('failed', keys, to, moved, owners, str(exc))
            outcome, note = 'failed', f'{ticket}: {exc}'
        if outcome in ('moved', 'there'):
            moved.append(ticket)
        elif outcome == 'owner':
            owners.append(note)
        else:
            failed.append(note)
    return _result('failed' if failed else 'owners' if owners else 'moved', keys, to, moved, owners, ' '.join(failed))


def _result(status, keys, to, moved, owners, error):
    if status == 'off':
        text = f'Jira not moved: {error}'
    elif status == 'failed' and not moved and not owners and to is None:
        text = f'Jira not moved: {error}'
    else:
        parts = [f'Jira: {", ".join(moved)} in {to}.'] if moved else []
        parts += owners
        if error:
            parts.append(f'Jira move failed: {error}')
        text = ' '.join(parts) or 'Jira: nothing to move.'
    return {'status': status, 'target': to or '', 'tickets': moved, 'owners': owners, 'error': error[:1000],
            'all': keys, 'text': text}


class Mover:
    """Finds items that became Done and have no Jira result for that Done yet, and moves their tickets on one worker
    thread. It keeps the event number of the first look per project (paths.work()/jira-start.json): Done items from
    before Jira on Done existed never move. After a restart it picks up a Done whose move did not finish."""

    def __init__(self, every=4.0, clock=None, start=True):
        import time
        self.every, self.clock = every, clock or time.monotonic
        self.next = 0.0
        self.jobs = queue.Queue()
        self.lock = threading.Lock()
        self.busy = set()     # item ids queued or moving
        self.empty = {}       # item id: Done event n that had no tickets (not written, so not looked at again)
        self.thread = None
        self.start = start

    def _path(self):
        return paths.work() / 'jira-start.json'

    def _starts(self):
        try:
            data = json.loads(self._path().read_text())
        except (OSError, ValueError):
            data = {}
        return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}

    def pending(self):
        """[(item id, Done event n)] for Done items with tickets to look at."""
        starts, changed, found = self._starts(), False, []
        for record in work_items.registry()['projects']:
            if not record.get('jira'):
                continue
            prefix = record['prefix']
            try:
                events = work_items._events(prefix)
            except ProjectError:
                continue
            if prefix not in starts:
                starts[prefix], changed = (events[-1]['n'] if events else 0), True
                continue
            done, answered = {}, {}  # item id: last Done event n; item id: the Done ns that have a result
            for event in events:
                if event['type'] == 'stage' and event.get('stage') == 'done':
                    done[event['item']] = event['n']
                elif event['type'] == 'jira' and type(event.get('done')) is int:
                    answered.setdefault(event['item'], set()).add(event['done'])
            items = None
            for item_id, n in done.items():
                if n <= starts[prefix] or n in answered.get(item_id, ()) or self.empty.get(item_id) == n:
                    continue
                items = items if items is not None else work_items.fold(record, events)
                item = items.get(item_id)
                if item and item['stage'] == 'done':
                    found.append((item_id, n))
        if changed:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(starts) + '\n')
        return found

    def observe(self):
        if self.clock() < self.next:
            return 0
        self.next = self.clock() + self.every
        added = 0
        for item_id, n in self.pending():
            added += self.queue(item_id, n)
        return added

    def queue(self, item_id, n=None):
        """Queue a move once. n: the Done event (None: Move again from the board; the result answers the last Done)."""
        with self.lock:
            if item_id in self.busy:
                return 0
            self.busy.add(item_id)
            self.jobs.put((item_id, n))
            if self.start and self.thread is None:
                self.thread = threading.Thread(target=self._work, name='work-jira', daemon=True)
                self.thread.start()
        return 1

    def _work(self):
        """The one worker. It ends after 30 s idle; it clears self.thread under the lock only when no job waits, so a
        job queued at that moment either finds this worker still on duty or starts a new one."""
        while True:
            try:
                job = self.jobs.get(timeout=30)
            except queue.Empty:
                with self.lock:
                    if self.jobs.empty():
                        self.thread = None
                        return
                continue
            try:
                self.run(*job)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
                print(f'work jira job failed for {job[0]}: {exc!r}')

    @staticmethod
    def snapshot(item_id):
        """(item, n of its last Done event or 0), both from one read of the item's events."""
        record, found = work_items.find(item_id)
        events = work_items._events(record['prefix'])
        item = work_items.fold(record, events).get(found['id'])
        done = [e['n'] for e in events if e['item'] == found['id'] and e['type'] == 'stage' and e.get('stage') == 'done']
        return item, (done[-1] if done else 0)

    def run(self, item_id, n=None, config=None, client=None):
        """One job: move the tickets of a Done item and write the result. Returns the event fields or None.
        The result answers the Done that the move read (n is only the queue's hint): a later Done stays waiting."""
        try:
            item, done = self.snapshot(item_id)
            if not item or item['stage'] != 'done':
                return None
            project = work_items.project(item['project'])
            result = move(item, project, config, client)
            if result is None:
                self.empty[item_id] = done
                return None
            text = result.pop('text')[:work_items.TEXT_LIMIT]
            result.pop('all')
            work_items.record(item_id, 'jira', text, done=done, **result)
            return result
        except (ProjectError, OSError, ValueError, JiraError) as exc:
            print(f'work jira move failed for {item_id}: {exc!r}')
            return None
        finally:
            with self.lock:
                self.busy.discard(item_id)


MOVER = Mover()
