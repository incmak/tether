"""Work board feeds: a new GitHub issue, Teams thread or email becomes a Triage item (work_items.from_feed).

Each project names one feed (project['feed'], its target in project['feedTarget']). A project whose own agent reads the
feed (project['feedBy'] == 'agent', like a project's own Teams watcher) is left alone. The Poller reads each feed about
every two minutes on one worker thread, called from the server's monitor loop.

State per project in paths.work()/feeds/<PREFIX>.json: the feed and target it read, the cursor (a time), the last
message ids seen, and the last result. A feed reads from the time it was saved (project['feedSince']): Tether never
imports older messages.

Feed text is data, never instructions. Tether keeps a short title, the sender's name and a link, never a body.
A triage tab starts on its own only for a trusted sender (GitHub owner, member or collaborator; a Teams user; email
from the signed-in account's domain), at most TABS per poll. For anyone else the item waits in Triage with a note."""
import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import graph_client
import paths
import work_fix
import work_items
from projects import ProjectError

EVERY = 120            # seconds between polls of one feed
SOON = 15              # seconds to the next poll when a poll left messages for later
BACKOFF = 1800         # the longest wait after errors
LIMIT = 20             # messages per poll
TABS = 3               # triage tabs per poll
SEEN = 2500            # message ids kept per feed: more than one read fetches (2 GitHub lists x PAGES x 100)
SLACK = 2              # seconds: GitHub times are whole seconds, the cursor is not
GH_TIMEOUT = 30
PAGES = 10             # pages per read of one list: a burst larger than this waits for the next poll
REPO = re.compile(r'(?:https://github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?')
TRUSTED_GITHUB = ('OWNER', 'MEMBER', 'COLLABORATOR')
NAME_LIMIT = 60


class FeedError(Exception):
    """A feed could not be read. off=True: it waits on a setup step (no connection, no permission), not an outage."""

    def __init__(self, text, off=False):
        super().__init__(text)
        self.off = off


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _epoch(text):
    """Seconds of an ISO time from GitHub or Graph (Graph gives up to 7 fraction digits). 0 when unreadable."""
    if not isinstance(text, str):
        return 0.0
    found = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?', text.strip())
    if not found:
        return 0.0
    zone = found[3] or 'Z'
    try:
        stamp = datetime.fromisoformat(found[1] + ('+00:00' if zone == 'Z' else zone))
    except ValueError:
        return 0.0
    return stamp.timestamp() + (float('0.' + found[2][:6]) if found[2] else 0.0)


def _name(value):
    return ' '.join(str(value or '').split())[:NAME_LIMIT] or 'someone'


def _plain(markup):
    """The text of a Teams message body (HTML), for a title only."""
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', str(markup or ''))).split())


def _message(key, msg, at, title, link, who, trusted, root, place):
    """One feed message. key: the thread (an item); msg: this message; root: it starts the thread."""
    return {'key': key, 'msg': msg, 'at': at, 'title': ' '.join(str(title or '').split())[:200], 'link': link if isinstance(link, str) else '',
            'who': _name(who), 'trusted': bool(trusted), 'root': root, 'place': place}


# GitHub: issues and issue comments of one repository, with the gh command line and its sign-in.

def repo(target):
    found = REPO.fullmatch((target or '').strip())
    if not found:
        raise FeedError('The GitHub target must be owner/repo, like octo-org/app.', off=True)
    return f'{found[1]}/{found[2]}'


def gh(args, run=subprocess.run):
    """The JSON answer of `gh api`. GET only, no prompts, a time limit."""
    env = {**os.environ, 'GH_PROMPT_DISABLED': '1', 'GH_NO_UPDATE_NOTIFIER': '1', 'NO_COLOR': '1'}
    try:
        done = run(['gh', 'api', '--method', 'GET', *args], capture_output=True, text=True, timeout=GH_TIMEOUT, env=env)
    except FileNotFoundError:
        raise FeedError('The GitHub command line (gh) is not installed. Install it and run: gh auth login', off=True) from None
    except subprocess.TimeoutExpired:
        raise FeedError('GitHub took too long. The feed tries again later.') from None
    except OSError as exc:
        raise FeedError(f'gh did not run ({exc.strerror or exc}).') from None
    if done.returncode:
        line = next((l.strip() for l in (done.stderr or '').splitlines() if l.strip()), f'gh failed ({done.returncode})')
        off = 'auth login' in line or 'HTTP 401' in line or 'HTTP 404' in line
        raise FeedError(f'GitHub: {line[:200]}', off=off)
    try:
        return json.loads(done.stdout or 'null')
    except ValueError:
        raise FeedError('GitHub gave an answer that was not JSON.') from None


def github(target, since, state, run=subprocess.run):
    """New issues and new comments since the cursor. A comment counts only on an issue the feed made an item of."""
    name = repo(target)
    # Both lists: changed since the cursor, oldest first. A poll reads at most PAGES pages; the next poll goes on from
    # the next page while the cursor stays, or starts again at page 1 once the cursor moved past what it read.
    after = ['-f', f'since={_iso(since - SLACK)}', '-f', 'sort=created', '-f', 'direction=asc']
    issues = _gh_pages(f'repos/{name}/issues', ['-f', 'state=all', *after], 'issues', since, state, run)
    comments = _gh_pages(f'repos/{name}/issues/comments', after, 'comments', since, state, run)
    out = []
    for issue in issues:
        if 'pull_request' in issue or not isinstance(issue.get('number'), int):
            continue
        user = issue.get('user') or {}
        if user.get('type') == 'Bot':
            continue
        out.append(_message(f'github:{name}#{issue["number"]}', f'i{issue.get("id")}', _epoch(issue.get('created_at')),
                            f'#{issue["number"]} {issue.get("title") or ""}', issue.get('html_url'), user.get('login'),
                            issue.get('author_association') in TRUSTED_GITHUB, True, f'GitHub {name}'))
    for comment in comments:
        number = str(comment.get('issue_url') or '').rsplit('/', 1)[-1]
        user = comment.get('user') or {}
        if not number.isdigit() or user.get('type') == 'Bot':
            continue
        out.append(_message(f'github:{name}#{number}', f'c{comment.get("id")}', _epoch(comment.get('created_at')),
                            '', comment.get('html_url'), user.get('login'),
                            comment.get('author_association') in TRUSTED_GITHUB, False, f'GitHub {name}'))
    # The lists share one cursor. A list cut at the page cap has unread pages after its last fetched time: the cursor
    # holds there (poll keeps it at or below 'hold'), so the next read fetches those pages. Messages it fetches again
    # are filtered by seen, and a repeated reply adds no second note.
    marks = state.get('resume') or {}
    ends = [max((_epoch(entry.get('created_at')) for entry in found), default=since)
            for key, found in (('issues', issues), ('comments', comments)) if key in marks]
    if ends:
        state['hold'] = min(ends)
    return out


def _resume(state, key, since):
    """Where the last poll stopped in a list it did not finish, when the cursor has not moved since."""
    mark = (state.get('resume') or {}).get(key) or {}
    return mark.get('at') if mark.get('since') == since else None


def _park(state, key, since, at):
    """Remember where to go on in a list (at), or forget it (at None)."""
    marks = dict(state.get('resume') or {})
    if at is None:
        marks.pop(key, None)
    else:
        marks[key] = {'since': since, 'at': at}
    state['resume'] = marks


def _gh_pages(endpoint, args, key, since, state, run):
    first = _resume(state, key, since)
    first = first if isinstance(first, int) and first > 1 else 1
    out = []
    for page in range(first, first + PAGES):
        found = gh([endpoint, *args, '-f', 'per_page=100', '-f', f'page={page}'], run)
        found = [entry for entry in found if isinstance(entry, dict)] if isinstance(found, list) else []
        out += found
        if len(found) < 100:
            _park(state, key, since, None)
            return out
    _park(state, key, since, first + PAGES)
    return out


# Microsoft 365, through graph_client with Mak's own Entra app (delegated, read only).

def _client(kind):
    config, why = graph_client.load()
    if not config:
        raise FeedError(why, off=True)
    if not graph_client.status().get(kind):
        what = 'Teams channels' if kind == 'teams' else 'mail'
        raise FeedError(f'The Microsoft connection has no access to {what}. Run: tether graph connect '
                        f'--tenant <tenant id> --client <app id> --feeds mail,teams', off=True)
    return graph_client.Client(config)


def _graph(work):
    try:
        return work()
    except graph_client.GraphError as exc:
        raise FeedError(str(exc), off=exc.status in (401, 403)) from None


def _id(value):
    """A Graph id as one path segment. Channel ids look like 19:abc@thread.tacv2."""
    if not isinstance(value, str) or not value:
        raise FeedError('Microsoft gave an item with no id.')
    return urllib.parse.quote(value, safe=':@=')


def _query(**fields):
    """An OData query string: $filter=... with spaces and quotes encoded."""
    return urllib.parse.urlencode({'$' + k: v for k, v in fields.items()}, quote_via=urllib.parse.quote, safe="$,")


def _tenant_id(client, state):
    """The tenant UUID of the signed-in account: the config's tenant when it is a UUID, else /organization (User.Read
    is enough for its id). Kept in the state."""
    tenant = (client.config.get('tenant') or '').casefold()
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', tenant):
        return tenant
    if state.get('tenantFor') == tenant and state.get('tenantId'):
        return state['tenantId']
    found = client.pages('/organization?' + _query(select='id'), limit=1)
    uuid = str((found[0] if found else {}).get('id') or '').casefold()
    if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', uuid):
        return ''  # nobody is trusted
    state.update(tenantFor=tenant, tenantId=uuid)
    return uuid


def _activity(root):
    """The last change in a thread: Graph sorts the roots by it."""
    return max(_epoch(m.get(field)) for m in [root, *[r for r in root.get('replies') or [] if isinstance(r, dict)]]
               for field in ('createdDateTime', 'lastModifiedDateTime'))


def _stamp(epoch):
    return time.strftime('%d %b %H:%M', time.localtime(epoch))


def teams(target, since, state, client=None):
    """New root messages and replies in one channel, named "Team/Channel". The ids are kept in the state."""
    team_name, _, channel_name = (target or '').partition('/')
    team_name, channel_name = team_name.strip(), channel_name.strip()
    if not team_name or not channel_name:
        raise FeedError('The Teams target must be Team/Channel, like Brisk/QA.', off=True)
    client = client or _client('teams')

    def read():
        place = state.get('place') or {}
        if place.get('target') != target:
            found = [t for t in client.pages('/me/joinedTeams?' + _query(select='id,displayName'))
                     if str(t.get('displayName', '')).casefold() == team_name.casefold()]
            if not found:
                raise FeedError(f'You are not in a team named {team_name}.', off=True)
            channels = [c for c in client.pages(f'/teams/{_id(found[0].get("id"))}/channels?'
                                                + _query(select='id,displayName'))
                        if str(c.get('displayName', '')).casefold() == channel_name.casefold()]
            if not channels:
                raise FeedError(f'The team {team_name} has no channel named {channel_name}.', off=True)
            place = state['place'] = {'target': target, 'team': found[0]['id'], 'channel': channels[0]['id']}
        # Graph sorts the roots by the last change in each thread, newest first: read back to the cursor
        link, roots = f'/teams/{_id(place["team"])}/channels/{_id(place["channel"])}/messages?' + _query(
            top=50, expand='replies'), []
        for _ in range(PAGES):
            data = client.get(link)
            page = [m for m in data.get('value') or [] if isinstance(m, dict)]
            for message in page:
                more = message.get('replies@odata.nextLink')
                if isinstance(more, str) and more:  # a thread with more replies than one answer holds
                    message['replies'] = list(message.get('replies') or []) + client.pages(more, limit=PAGES)
            roots += page
            link = data.get('@odata.nextLink')
            if not isinstance(link, str) or not link or not page or min(map(_activity, page)) < since - SLACK:
                break
        else:  # Graph has no filter for channel messages: threads older than the last page are not read
            state['gap'] = {'from': since, 'to': min(map(_activity, roots))}  # threads in this range were not read
        return roots, place, _tenant_id(client, state)
    messages, place, tenant = _graph(read)
    out = []

    def add(message, root_id):
        sender = message.get('from') or {}
        user = sender.get('user') or {}
        if message.get('messageType') != 'message' or message.get('deletedDateTime') or not user or sender.get('application'):
            return
        home = str(user.get('tenantId') or '').casefold()
        # only a member of the signed-in tenant: a guest, an outside user or a sender with no tenant waits for Mak
        trusted = bool(tenant) and home == tenant and user.get('userIdentityType') == 'aadUser'
        title = message.get('subject') or _plain((message.get('body') or {}).get('content'))[:200]
        root = message.get('id') == root_id
        out.append(_message(f'teams:{place["channel"]}/{root_id}', f'm{message.get("id")}',
                            _epoch(message.get('createdDateTime')), title if root else '', message.get('webUrl'),
                            user.get('displayName'), trusted, root, f'Teams {target}'))
    for message in messages:
        if not message.get('id'):
            continue
        add(message, message['id'])
        for reply in message.get('replies') or []:
            if isinstance(reply, dict):
                add(reply, message['id'])
    return out


def email(target, since, state, client=None):
    """New mail in one folder (Inbox when no target). One conversation is one item; later mail in it is a note."""
    folder = (target or '').strip() or 'Inbox'
    client = client or _client('mail')

    def read():
        place = state.get('place') or {}
        if place.get('target') != folder:
            if folder.casefold() == 'inbox':
                found = 'inbox'
            else:
                name = folder.replace("'", "''")
                hits = client.pages('/me/mailFolders?' + _query(filter=f"displayName eq '{name}'", select='id'), limit=1)
                if not hits:
                    raise FeedError(f'Your mailbox has no top-level folder named {folder}.', off=True)
                found = hits[0].get('id')
            place = state['place'] = {'target': folder, 'folder': found}
        # oldest first from the cursor; a poll reads at most PAGES pages and the next one goes on from there
        link = _resume(state, 'mail', since) or (f'/me/mailFolders/{_id(place["folder"])}/messages?' + _query(
            filter=f'receivedDateTime ge {_iso(since - SLACK)}', orderby='receivedDateTime asc', top=50,
            select='id,subject,from,receivedDateTime,conversationId,webLink'))
        out = []
        for _ in range(PAGES):
            try:
                data = client.get(link)
            except graph_client.GraphError:
                _park(state, 'mail', since, None)  # a stale link must not stop the feed: start again next time
                raise
            out += [m for m in data.get('value') or [] if isinstance(m, dict)]
            link = data.get('@odata.nextLink')
            if not isinstance(link, str) or not link:
                _park(state, 'mail', since, None)
                return out
        _park(state, 'mail', since, link)
        return out
    messages = _graph(read)
    account = str(client.config.get('account') or '')
    domain = account.rpartition('@')[2].casefold() if '@' in account else ''
    out = []
    for message in messages:
        address = ((message.get('from') or {}).get('emailAddress') or {})
        sender = str(address.get('address') or '')
        conversation = str(message.get('conversationId') or '')
        if not conversation or not re.fullmatch(r'[!-~]{1,270}', conversation):
            continue
        trusted = bool(domain) and sender.rpartition('@')[2].casefold() == domain
        out.append(_message(f'email:{conversation}', f'e{message.get("id")}', _epoch(message.get('receivedDateTime')),
                            message.get('subject') or '', message.get('webLink'), address.get('name') or sender,
                            trusted, True, f'Email {folder}'))
    return out


SOURCES = {'github': github, 'teams': teams, 'email': email}


# State and polling.

def _path(prefix):
    return paths.work() / 'feeds' / f'{prefix}.json'


def load(prefix):
    try:
        data = json.loads(_path(prefix).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(prefix, state):
    path = _path(prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    temp.write_text(json.dumps(state) + '\n')
    os.replace(temp, path)


def _find(record, key):
    return next((item for item in work_items.fold(record).values() if item.get('feed') == key), None)


def poll(record, sessions=None, run=subprocess.run, lock=None, now=None, sources=None):
    """Read one project's feed once. Returns the state it saved. sessions: the live tabs (None: no triage tabs)."""
    now = time.time() if now is None else now
    prefix, feed, target = record['prefix'], record.get('feed') or 'manual', record.get('feedTarget') or ''
    state = load(prefix)
    since = record.get('feedSince')
    if not (isinstance(since, (int, float)) and 0 < since <= now):  # a project saved before feedSince existed
        kept = state.get('since')
        same = state.get('feed') == feed and state.get('target') == target
        since = kept if same and isinstance(kept, (int, float)) and 0 < kept <= now else now
    if (state.get('feed') != feed or state.get('target') != target or state.get('since') != since
            or not isinstance(state.get('cursor'), (int, float))):
        state = {'feed': feed, 'target': target, 'since': since, 'cursor': since, 'seen': [], 'status': 'ok',
                 'at': now, 'error': '', 'errors': 0, 'items': 0, 'more': False}
        save(prefix, state)  # a feed starts when it was saved: older messages never become items
    source = (sources or SOURCES).get(feed)
    if source is None:
        return state
    state.pop('gap', None)
    queue = [m for m in state.get('queue') or [] if _queued(m)]
    state['later'] = [m for m in state.get('later') or [] if _queued(m)]
    waiting = {m['msg'] for m in state['later']}  # they passed the time check when first read
    if queue:  # messages a read fetched past LIMIT: the read went on past them, so they come before a new read
        found = queue
    else:
        state.pop('hold', None)  # a source sets it again when a list it read was cut at the page cap
        try:
            found = source(target, state['cursor'], state)
        except FeedError as exc:
            state.update(status='off' if exc.off else 'error', at=now, error=str(exc)[:300],
                         errors=state.get('errors', 0) + (0 if exc.off else 1))
            save(prefix, state)
            return state
        found = list(found) + state['later']
        state['later'] = []  # merged into this read
    seen = list(state.get('seen') or [])
    fresh = sorted({m['msg']: m for m in found if (m['at'] >= state['cursor'] - SLACK or m['msg'] in waiting)
                    and m['msg'] not in seen}.values(),
                   key=lambda m: (m['at'], not m['root']))  # a thread's first message before its replies at one time
    hold = state.get('hold')
    hold = hold if isinstance(hold, (int, float)) else None
    state['queue'] = fresh
    save(prefix, state)  # the read position moves only with the messages it fetched kept
    tabs = made = 0
    for index, message in enumerate(fresh[:LIMIT]):
        if hold is not None and not message['root'] and _find(record, message['key']) is None:
            # a list was cut at the page cap: this reply's thread can start on a page not read yet. It waits, unseen.
            state.update(later=state['later'] + [message], queue=fresh[index + 1:])
            save(prefix, state)
            continue
        try:
            item, tab = _take(record, message, sessions, run, lock, tabs < TABS)
            made, tabs = made + item, tabs + tab
        except (ProjectError, OSError, ValueError) as exc:  # one bad message must not stop the feed
            print(f'work feed {prefix}: {message["msg"]} skipped: {exc!r}')
        seen = (seen + [message['msg']])[-SEEN:]
        at = min(message['at'], state['hold']) if isinstance(state.get('hold'), (int, float)) else message['at']
        state.update(cursor=max(state['cursor'], at), seen=seen, queue=fresh[index + 1:])
        save(prefix, state)  # the item is written before the cursor moves past its message
    _gap(record, state, state.pop('gap', None))
    state.update(status='ok', at=now, error='', errors=0, items=state.get('items', 0) + made,
                 more=bool(state['queue'] or state.get('later') or state.get('resume')))
    save(prefix, state)
    return state


def _queued(message):
    """A message from the saved queue has the shape _message gives."""
    return (isinstance(message, dict) and isinstance(message.get('at'), (int, float))
            and all(isinstance(message.get(k), str) for k in ('key', 'msg', 'title', 'link', 'who', 'place'))
            and isinstance(message.get('trusted'), bool) and isinstance(message.get('root'), bool))


def _gap(record, state, gap):
    """One item per unread range. A range that overlaps the last one is the same gap: no second item."""
    if not isinstance(gap, dict):
        return
    last = state.get('gapRange') or {}
    if isinstance(last.get('to'), (int, float)) and gap['from'] <= last['to'] + SLACK:
        state['gapRange'] = {'from': last['from'], 'to': max(last['to'], gap['to'])}
        return
    target = record.get('feedTarget') or ''
    try:  # an item, so Mak sees it and closes it when checked
        work_items.new(record['name'], f'Feed gap: check {record.get("feed")} {target}'.strip(), 'feed',
                       source=(f'More than {PAGES * 50} threads changed at once. Some threads with activity from '
                               f'{_stamp(gap["from"])} were not read. Check {target} from then.')[:200])
    except (ProjectError, OSError, ValueError) as exc:
        print(f'work feed {record["prefix"]}: gap item failed: {exc!r}')
        return
    state['gapRange'] = dict(gap)


def _take(record, message, sessions, run, lock, tab_ok):
    """Write one message. Returns (items made, tabs started), each 0 or 1."""
    ref = message['link'] if len(message['link']) <= work_items.REF_LIMIT else ''
    if not message['root']:
        item = _find(record, message['key'])
        if item is not None:
            if ref and any(entry.get('ref') == ref for entry in item.get('history') or []):
                return 0, 0  # a reply read again: one note only
            work_items.note(item['id'], f'New reply from {message["who"]}. Read it at the link.', 'feed',
                            who=message['who'], ref=ref)
            return 0, 0
        if not message['key'].startswith('email:'):
            return 0, 0  # a reply in a thread from before the feed
        # a mail in a conversation from before the feed: the conversation becomes an item
    item, created = work_items.from_feed(record['name'], message['key'], message['title'], message['link'],
                                         f'{message["place"]}, from {message["who"]}')
    if not created:  # a later mail in a known conversation; a GitHub or Teams root seen again adds nothing
        if message['key'].startswith('email:') and not (
                ref and any(entry.get('ref') == ref for entry in item.get('history') or [])):
            work_items.note(item['id'], f'New message from {message["who"]}. Read it at the link.', 'feed',
                            who=message['who'], ref=ref)
        return 0, 0
    if not message['trusted']:
        why = f'{message["who"]} is outside your team. Read the report first, then use Start triage tab.'
    elif not sessions:
        why = 'triage tabs are off here. Use Start triage tab.'
    elif not tab_ok:
        why = f'the feed starts at most {TABS} per poll. Use Start triage tab.'
    else:
        with work_fix.START_LOCK:  # the drawer's Start triage tab takes it too: read the item and tabs inside it
            _, item = work_items.find(item['id'])
            entries = sessions()
            if item['stage'] != 'triage' or work_fix.triage_running(item, entries, run):
                return 1, 0
            try:
                _, started = work_fix.start_triage(item, entries, run, lock)
            except ProjectError:  # the item changed while the launch waited for the send lock
                return 1, 0
        return 1, int(started['started'])
    work_items.record(item['id'], 'tab', f'No triage tab: {why}')
    return 1, 0


def due(record):
    """A feed that Tether reads: not by hand, and not read by the project's own agent."""
    return (record.get('feed') or 'manual') in SOURCES and record.get('feedBy', 'tether') == 'tether'


def statuses():
    """{project name: feed state} for the board. No cursor detail, no message ids."""
    out = {}
    for record in work_items.registry()['projects']:
        if (record.get('feed') or 'manual') == 'manual':
            continue
        state = load(record['prefix'])
        same = state.get('feed') == record.get('feed') and state.get('target') == (record.get('feedTarget') or '')
        out[record['name']] = {
            'by': record.get('feedBy', 'tether'),
            'status': (state.get('status') or 'ok') if same else 'waiting',
            'at': state.get('at') if same else None,
            'error': state.get('error', '') if same else '',
            'errors': state.get('errors', 0) if same else 0,
            'items': state.get('items', 0) if same else 0}
    return out


class Poller:
    """Reads due feeds on one worker thread. observe() runs in the monitor loop; it only queues."""

    def __init__(self, clock=None, start=True):
        self.clock = clock or time.monotonic
        self.next = {}        # prefix: monotonic time of the next poll
        self.busy = set()
        self.lock = threading.Lock()
        self.jobs = []
        self.thread = None
        self.start = start
        self.tabs = (None, subprocess.run, None)

    def observe(self, sessions=None, run=subprocess.run, lock=None):
        self.tabs = (sessions, run, lock)
        now, added = self.clock(), 0
        for record in work_items.registry()['projects']:
            prefix = record['prefix']
            if not due(record) or now < self.next.get(prefix, 0):
                continue
            with self.lock:
                if prefix in self.busy:
                    continue
                self.busy.add(prefix)
                self.jobs.append((record['name'], prefix))
                added += 1
                if self.start and self.thread is None:
                    self.thread = threading.Thread(target=self._work, name='work-feeds', daemon=True)
                    self.thread.start()
        return added

    def _work(self):
        while True:
            with self.lock:
                if not self.jobs:
                    self.thread = None
                    return
                name, prefix = self.jobs.pop(0)
            self.run(name, prefix)

    def run(self, name, prefix):
        """One poll of one project. It reads the project again: a change since observe() counts."""
        try:
            record = work_items.project(name)
            if record['prefix'] != prefix or not due(record):
                return None
            sessions, run, lock = self.tabs
            state = poll(record, sessions, run, lock)
            wait = SOON if state.get('more') else EVERY
            if state.get('status') == 'error':
                wait = min(BACKOFF, EVERY * 2 ** min(state.get('errors', 1), 4))
            elif state.get('status') == 'off':
                wait = 600  # a setup step is missing: look again now and then
            self.next[prefix] = self.clock() + wait
            return state
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the others
            print(f'work feed {name} failed: {exc!r}')
            self.next[prefix] = self.clock() + EVERY
            return None
        finally:
            with self.lock:
                self.busy.discard(prefix)


POLLER = Poller()
