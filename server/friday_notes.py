"""Friday's saved topic notes, shared by the HTTP server and the voice worker."""
import contextlib
import fcntl
import json
from math import inf
import os
import re
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

try:
    from . import paths
except ImportError:
    import paths

PREFIX = '/mobile/api/friday-notes'
ACTIONS = ('hold', 'drop', 'draft', 'draft-edit', 'draft-send', 'draft-close', 'draft-asked')
SENT_AGE = 7 * 86400
TITLE_LIMIT = 60
NOTE_LIMIT = 1000
TOPIC_LIMIT = 20
NOTES_LIMIT = 50
BATCH_LIMIT = 5
KEYS = ('id', 'title', 'chat', 'notes', 'state', 'created', 'updated', 'sent_at', 'sent_to', 'draft')
TOPIC_ID = re.compile(r'T[1-9][0-9]*')
PANE = re.compile(r'%[0-9]+')
SKILL = re.compile(r'/[A-Za-z0-9][A-Za-z0-9_.:-]*')
DRAFT_STALE = 7 * 60
DRAFT_STOPPED = 'Drafting stopped. Ask Friday to make the prompt again.'
REVISION_LIMIT = 20
PROMPT_LIMIT = 20000
SUMMARY_LIMIT = 400
EDIT_LIMIT = 1000
DRAFT_ERROR_LIMIT = 2 * 4096 + 128  # The trust error includes a filesystem path twice.


class NotesError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextlib.contextmanager
def _locked(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(folder / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _title(text):
    return ' '.join(text.split()) if isinstance(text, str) else ''


def _valid_text(text):
    return isinstance(text, str) and 1 <= len(text) <= NOTE_LIMIT and bool(text.strip())


def _valid_time(value):
    return type(value) in (int, float) and -inf < value < inf


def _fields(value, *keys):
    return isinstance(value, dict) and value.keys() == set(keys)


def _limited(value, limit):
    return isinstance(value, str) and len(value) <= limit


def _valid_revision(revision, number):
    return (_fields(revision, 'rev', 'text', 'skill', 'summary', 'edit', 'at')
            and type(revision['rev']) is int and revision['rev'] == number
            and _limited(revision['text'], PROMPT_LIMIT) and bool(revision['text'])
            and (revision['skill'] is None or
                 isinstance(revision['skill'], str) and SKILL.fullmatch(revision['skill']))
            and _limited(revision['summary'], SUMMARY_LIMIT)
            and (revision['edit'] is None or _limited(revision['edit'], EDIT_LIMIT))
            and _valid_time(revision['at']))


def _valid_chat(chat):
    if not isinstance(chat, dict) or chat.keys() - {'stamp', 'gone'} != {'pane', 'thread', 'name'}:
        return False
    pane, stamp = chat['pane'], chat.get('stamp')
    return (isinstance(pane, str) and PANE.fullmatch(pane)
            and isinstance(chat['thread'], str) and isinstance(chat['name'], str)
            and ('gone' not in chat or chat['gone'] is True)
            and ('stamp' not in chat or (isinstance(stamp, list) and len(stamp) == 4
                 and all(isinstance(field, str) for field in stamp) and stamp[1] == pane and stamp[3].isdigit())))


def _valid_draft(draft):
    if draft is None:
        return True
    if not _fields(draft, 'state', 'error', 'started', 'updated', 'chat', 'asked_close', 'revisions'):
        return False
    chat, revisions = draft['chat'], draft['revisions']
    return (draft['state'] in ('drafting', 'ready', 'failed', 'closed')
            and (draft['error'] is None or _limited(draft['error'], DRAFT_ERROR_LIMIT))
            and _valid_time(draft['started']) and _valid_time(draft['updated'])
            and (chat is None or _valid_chat(chat))
            and type(draft['asked_close']) is bool
            and isinstance(revisions, list) and len(revisions) <= REVISION_LIMIT
            and all(_valid_revision(item, number) for number, item in enumerate(revisions, 1)))


def _valid(topic):
    return (isinstance(topic, dict) and all(key in topic for key in KEYS)
            and isinstance(topic['id'], str) and TOPIC_ID.fullmatch(topic['id'])
            and 1 <= len(_title(topic['title'])) <= TITLE_LIMIT and isinstance(topic['chat'], str)
            and isinstance(topic['notes'], list) and len(topic['notes']) <= NOTES_LIMIT
            and all(isinstance(note, dict) and _valid_text(note.get('text')) and _valid_time(note.get('at'))
                    for note in topic['notes'])
            and topic['state'] in ('open', 'sent') and _valid_time(topic['created']) and _valid_time(topic['updated'])
            and (_valid_time(topic['sent_at']) or (topic['state'] == 'open' and topic['sent_at'] is None))
            and (topic['sent_to'] is None or isinstance(topic['sent_to'], str)) and _valid_draft(topic['draft']))


def _open_chat(draft):
    return bool(draft and draft['state'] != 'closed' and draft['chat'] and not draft['chat'].get('gone'))


def _visible(topic, now):
    return topic['state'] == 'open' or topic['sent_at'] >= now - SENT_AGE


def _raw_topics(source):
    """The exact JSON of each topic, including whitespace and escapes within the record."""
    decoder = json.JSONDecoder()
    skip = re.compile(r'[ \t\r\n]*').match
    index = skip(source, skip(source).end() + 1).end()  # the file is a parsed object
    start = None
    while source[index] != '}':
        key, end = decoder.raw_decode(source, index)
        index = skip(source, skip(source, end).end() + 1).end()
        if key == 'topics':
            start = index
        _, end = decoder.raw_decode(source, index)
        index = skip(source, end).end()
        if source[index] == '}':
            break
        index = skip(source, index + 1).end()
    index = skip(source, start + 1).end()  # the last "topics" value is the array the decoder used
    while source[index] != ']':
        _, end = decoder.raw_decode(source, index)
        yield source[index:end]
        index = skip(source, end).end()
        if source[index] == ']':
            break
        index = skip(source, index + 1).end()


def _load(folder, now, *, strict=False):
    try:
        source = (folder / 'notes.json').read_bytes().decode('utf-8')
        data = json.loads(source)
    except FileNotFoundError:
        data = {}
    except (ValueError, RecursionError) as exc:
        if strict:
            raise NotesError('Saved topic notes could not be read.', 503) from exc
        data = {}
    if not isinstance(data, dict):
        data = {}
    saved_next = data.get('next')
    next_id = max(1, saved_next) if type(saved_next) is int else 1
    records = data.get('topics')
    topics, order = [], []
    if isinstance(records, list):
        raw_records = None
        for index, topic in enumerate(records):
            identifier = topic.get('id') if isinstance(topic, dict) else None
            if isinstance(identifier, str) and TOPIC_ID.fullmatch(identifier):
                try:
                    next_id = max(next_id, int(identifier[1:]) + 1)
                except ValueError:  # Python rejects integers with more digits than its safety limit.
                    pass
            if not _valid(topic):
                if raw_records is None:
                    raw_records = list(_raw_topics(source))
                order.append(raw_records[index])
                continue
            draft = topic['draft']
            if draft and draft['state'] == 'drafting' and now - draft['updated'] > DRAFT_STALE:
                draft.update(state='failed', error=DRAFT_STOPPED)
            # A sent topic can leave the list before its draft chat reaches its own close deadline.
            if _visible(topic, now) or _open_chat(draft):
                topics.append(topic)
                order.append(topic)
    return {'next': next_id, 'topics': topics, 'order': order}


def _save(folder, data):
    remaining = {id(topic): topic for topic in data['topics']}
    records = []
    for record in data['order']:
        if isinstance(record, str):
            records.append(record)
        elif id(record) in remaining:
            records.append(remaining.pop(id(record)))
    records.extend(remaining.values())
    fd, temp = tempfile.mkstemp(prefix='.notes-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(f'{{\n "version": 1,\n "next": {data["next"]},\n "topics": [')
            if records:
                stream.write('\n  ' + ',\n  '.join(record if isinstance(record, str) else
                             json.dumps(record, indent=1).replace('\n', '\n  ') for record in records) + '\n ')
            stream.write(']\n}\n')
        os.replace(temp, folder / 'notes.json')
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def public(topic):
    result = {key: topic[key] for key in KEYS}
    result['notes'] = [{'text': note['text'], 'at': note['at']} for note in topic['notes']]
    if (draft := result['draft']) and (chat := draft['chat']) and 'stamp' in chat:
        result['draft'] = dict(draft, chat={key: value for key, value in chat.items() if key != 'stamp'})
    return result


def _normalized(name):
    return ' '.join(re.findall(r'[a-z0-9]+', name.casefold()))


def find(topics, name, *, open_only=True):
    wanted = _normalized(name)
    matches = []
    for topic in topics:
        if open_only and topic['state'] != 'open':
            continue
        title = _normalized(topic['title'])
        if title == wanted:
            return topic
        if topic['state'] == 'open' and wanted and title and (
                f' {wanted} ' in f' {title} ' or f' {title} ' in f' {wanted} '):
            matches.append(topic)
    if len(matches) > 1:
        titles = ', '.join(topic['title'] for topic in matches)
        raise NotesError(f'Several topics match: {titles}. Which one?')
    return matches[0] if matches else None


def _order(topic):
    return topic['state'] != 'open', -topic['updated']


def missing(topics, name):
    titles = [topic['title'] for topic in sorted(topics, key=_order) if topic['state'] == 'open'][:5]
    available = f"Open topics: {', '.join(titles)}." if titles else 'There are no open topics.'
    return NotesError(f'No topic matches {name}. {available}', 404)


def listing(now=None, *, strict=False):
    now = time.time() if now is None else now
    topics = _load(paths.friday_notes(), now, strict=strict)['topics']
    return {'topics': [public(topic) for topic in sorted(topics, key=_order) if _visible(topic, now)]}


def hold(topic, notes, chat=None, now=None):
    title = _title(topic)
    if not 1 <= len(title) <= TITLE_LIMIT:
        raise NotesError(f'Use a topic name of 1 to {TITLE_LIMIT} characters.')
    if not isinstance(notes, list) or not 1 <= len(notes) <= BATCH_LIMIT:
        raise NotesError('Provide one to five notes.')
    if not all(_valid_text(text) for text in notes):
        raise NotesError(f'Each note needs 1 to {NOTE_LIMIT} characters of text.')
    if chat is not None and not isinstance(chat, str):
        raise NotesError('Use text for the chat name.')
    now = time.time() if now is None else float(now)
    folder = paths.friday_notes()
    with _locked(folder):
        data = _load(folder, now, strict=True)
        topics = data['topics']
        saved = find(topics, title)
        if saved is None:
            if sum(topic['state'] == 'open' for topic in topics) >= TOPIC_LIMIT:
                raise NotesError(f'Keep at most {TOPIC_LIMIT} open topics.')
            saved = dict(id=f"T{data['next']}", title=title, chat='', notes=[], state='open',
                         created=now, updated=now, sent_at=None, sent_to=None, draft=None)
            data['next'] += 1
            topics.append(saved)
        if len(saved['notes']) + len(notes) > NOTES_LIMIT:
            raise NotesError(f'Keep at most {NOTES_LIMIT} notes per topic.')
        if chat and chat.strip():
            saved['chat'] = chat.strip()
        saved['notes'].extend({'text': text, 'at': now} for text in notes)
        saved['updated'] = now
        _save(folder, data)
        return public(saved)


def _close_for_drop(draft, now, runtime):
    if not _open_chat(draft):
        return
    if runtime is None:
        raise NotesError('Close the draft chat first.', 409)
    import friday_drafts

    try:
        friday_drafts.close_chat(draft, now, runtime=runtime)
    except friday_drafts.CloseFailed as exc:
        raise NotesError('Close the draft chat first.', 409) from exc


def drop(topic, note=None, now=None, *, topic_id=None, text=None, count=None, texts=None, draft_runtime=None):
    """topic_id and text (from the screen) make the drop exact: a list that changed since it showed gets 409.

    A topic drop with texts deletes only those notes in that order: one note removed and one added keeps the
    count, but not the texts, so a note the user never saw or heard is never deleted."""
    title = _title(topic)
    if not title and topic_id is None:
        raise NotesError('Provide a topic name.')
    now = time.time() if now is None else float(now)
    folder = paths.friday_notes()
    with _locked(folder):
        data = _load(folder, now, strict=True)
        topics = data['topics']
        if topic_id is not None:
            saved = next((item for item in topics if item['id'] == topic_id and item['state'] == 'open'), None)
            if saved is None:
                raise NotesError('Those notes changed. Look at the list again.', 409)
        else:
            saved = find(topics, title)
            if saved is None:
                raise missing(topics, title)
        if note is None:
            if ((count is not None and count != len(saved['notes']))
                    or (texts is not None and list(texts) != [item['text'] for item in saved['notes']])):
                raise NotesError('Those notes changed. Look at the list again.', 409)
            _close_for_drop(saved['draft'], now, draft_runtime)
            topics.remove(saved)
            result = {'dropped': 'topic', 'topic': saved['title'], 'left': 0}
        else:
            if type(note) is not int or not 1 <= note <= len(saved['notes']):
                raise NotesError('Choose one of the note numbers in that topic.')
            if text is not None and saved['notes'][note - 1]['text'] != text:
                raise NotesError('Those notes changed. Look at the list again.', 409)
            if len(saved['notes']) == 1:
                _close_for_drop(saved['draft'], now, draft_runtime)
            saved['notes'].pop(note - 1)
            saved['updated'] = now
            if not saved['notes']:
                # An empty topic would hide from the chip yet still count toward the open-topic limit.
                topics.remove(saved)
            result = {'dropped': 'note', 'topic': saved['title'], 'left': len(saved['notes'])}
        _save(folder, data)
        return result


def pinned(now=None, budget=4000):
    now = time.time() if now is None else now
    topics = [topic for topic in _load(paths.friday_notes(), now)['topics'] if topic['state'] == 'open']
    topics.sort(key=_order)
    result = [{'topic': topic['title'], **({'chat': topic['chat']} if topic['chat'] else {}),
               'notes': [note['text'] for note in topic['notes']]} for topic in topics]
    for item, topic in zip(result, topics):
        if draft := topic['draft']:
            latest = draft['revisions'][-1] if draft['revisions'] else {}
            item['draft'] = {'state': draft['state'], 'rev': latest.get('rev'),
                             'summary': latest.get('summary', ''), 'skill': latest.get('skill')}
    size = len(json.dumps(result))
    if size <= budget:
        return result

    # Each topic keeps its latest note even if those notes alone exceed the budget.
    older = []
    for i, topic in enumerate(topics):
        # Cut a topic's notes from the front only, so "more": N always means notes 1..N are hidden
        # and the first shown note is number N+1 (drop_notes counts from the stored order).
        oldest = float('-inf')
        for j, note in enumerate(topic['notes'][:-1]):
            oldest = max(oldest, note['at'])
            older.append((oldest, -i, j))
    dropped = [set() for _ in topics]
    for _, reverse_i, j in sorted(older):
        i = -reverse_i
        item = result[i]
        previous = item.get('more', 0)
        item['more'] = previous + 1
        # The list keeps one entry, so each cut also removes a separator.
        size -= len(json.dumps(item['notes'][j])) + len(', ')
        size += (len(f', "more": {previous + 1}') if not previous else
                 len(str(previous + 1)) - len(str(previous)))
        dropped[i].add(j)
        if size <= budget:
            break
    for item, indices in zip(result, dropped):
        item['notes'] = [text for j, text in enumerate(item['notes']) if j not in indices]
    return result


def handles(path):
    path = path.split('?', 1)[0]
    return path in (PREFIX, f'{PREFIX}/target') or path in [f'{PREFIX}/{action}' for action in ACTIONS]


def get(handler, *, draft_runtime):
    import friday_drafts

    try:
        url = urlsplit(handler.path)
        query = parse_qs(url.query)
        if url.path == f'{PREFIX}/target':
            handler.respond(200, friday_drafts.resolve(query.get('chat', [''])[0], runtime=draft_runtime))
            return
        if url.path != PREFIX:
            raise NotesError('Not found', 404)
        friday_drafts.schedule_sweep(runtime=draft_runtime)
        result = listing()
        name = query.get('topic', [''])[0].strip()
        if name:
            topics = result['topics']
            topic = find(topics, name, open_only=False)
            if topic is None:
                raise missing(topics, name)
            result = {'topics': [topic]}
        handler.respond(200, result)
    except NotesError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except OSError:
        handler.respond(503, {'error': 'Saved topic notes could not be read.'})


def _draft_action(action, body, runtime):
    import friday_drafts

    identifier = body.get('id')
    if not isinstance(identifier, str) or not TOPIC_ID.fullmatch(identifier):
        raise NotesError('Use a topic id.')
    if action == 'draft':
        return friday_drafts.start(identifier, runtime=runtime)
    if action == 'draft-asked':
        return friday_drafts.mark_asked(identifier, now=runtime.clock())
    if action in ('draft-edit', 'draft-send'):
        rev = body.get('rev')
        if type(rev) is not int or rev < 1:
            raise NotesError('Use a draft revision number.')
        if action == 'draft-edit':
            return friday_drafts.edit(identifier, rev, body.get('edit'), runtime=runtime)
    pane, thread = body.get('pane'), body.get('thread')
    # Only draft-close accepts an empty thread. It must exactly match the stored thread and uses the creation stamp.
    if (not isinstance(pane, str) or not PANE.fullmatch(pane) or not isinstance(thread, str)
            or not (thread.strip() or (action == 'draft-close' and thread == ''))):
        raise NotesError('Use a pane and conversation id.')
    if action == 'draft-send':
        return friday_drafts.send(identifier, rev, pane, thread, runtime=runtime)
    return friday_drafts.close(identifier, pane, thread, runtime=runtime)


def serve(handler, body, *, draft_runtime):
    action = handler.path.split('?', 1)[0][len(PREFIX) + 1:]
    try:
        if not isinstance(body, dict):
            raise NotesError('Use a JSON object.')
        if action == 'hold':
            result = hold(body.get('topic'), body.get('notes'), body.get('chat'))
        elif action == 'drop':
            if any(key in body and not isinstance(body[key], str) for key in ('id', 'text')):
                raise NotesError('Use text for the topic id and note.')
            if 'count' in body and type(body['count']) is not int:
                raise NotesError('Use a whole number for the note count.')
            if 'texts' in body and not (isinstance(body['texts'], list) and all(isinstance(item, str) for item in body['texts'])):
                raise NotesError('Use a list of note texts.')
            result = drop(body.get('topic'), body.get('note'), topic_id=body.get('id'), text=body.get('text'),
                          count=body.get('count'), texts=body.get('texts'), draft_runtime=draft_runtime)
        elif action in ACTIONS:
            result = _draft_action(action, body, draft_runtime)
        else:
            raise NotesError('Not found', 404)
        handler.respond(200, result)
    except NotesError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except OSError:
        handler.respond(503, {'error': 'Saved topic notes could not be changed.'})
