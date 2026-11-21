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
ACTIONS = ('hold', 'drop')
SENT_AGE = 7 * 86400
TITLE_LIMIT = 60
NOTE_LIMIT = 1000
TOPIC_LIMIT = 20
NOTES_LIMIT = 50
BATCH_LIMIT = 5
KEYS = ('id', 'title', 'chat', 'notes', 'state', 'created', 'updated', 'sent_at', 'sent_to', 'draft')


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


def _valid(topic):
    return (isinstance(topic, dict) and all(key in topic for key in KEYS)
            and isinstance(topic['id'], str) and re.fullmatch(r'T[1-9][0-9]*', topic['id'])
            and 1 <= len(_title(topic['title'])) <= TITLE_LIMIT and isinstance(topic['chat'], str)
            and isinstance(topic['notes'], list) and len(topic['notes']) <= NOTES_LIMIT
            and all(isinstance(note, dict) and _valid_text(note.get('text')) and _valid_time(note.get('at'))
                    for note in topic['notes'])
            and topic['state'] in ('open', 'sent') and _valid_time(topic['created']) and _valid_time(topic['updated'])
            and (_valid_time(topic['sent_at']) or (topic['state'] == 'open' and topic['sent_at'] is None))
            and (topic['sent_to'] is None or isinstance(topic['sent_to'], str)) and topic['draft'] is None)


def _load(folder, now, *, strict=False):
    try:
        data = json.loads((folder / 'notes.json').read_text(encoding='utf-8'))
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
    topics = []
    for topic in records if isinstance(records, list) else ():
        if not _valid(topic):
            continue
        try:
            number = int(topic['id'][1:])
        except ValueError:
            continue
        next_id = max(next_id, number + 1)
        if topic['state'] == 'open' or topic['sent_at'] >= now - SENT_AGE:
            topics.append(topic)
    return {'version': 1, 'next': next_id, 'topics': topics}


def _save(folder, data):
    fd, temp = tempfile.mkstemp(prefix='.notes-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=1)
            stream.write('\n')
        os.replace(temp, folder / 'notes.json')
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def public(topic):
    result = {key: topic[key] for key in KEYS}
    result['notes'] = [{'text': note['text'], 'at': note['at']} for note in topic['notes']]
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
    return {'topics': [public(topic) for topic in sorted(topics, key=_order)]}


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


def drop(topic, note=None, now=None, *, topic_id=None, text=None, count=None, texts=None):
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
            topics.remove(saved)
            result = {'dropped': 'topic', 'topic': saved['title'], 'left': 0}
        else:
            if type(note) is not int or not 1 <= note <= len(saved['notes']):
                raise NotesError('Choose one of the note numbers in that topic.')
            if text is not None and saved['notes'][note - 1]['text'] != text:
                raise NotesError('Those notes changed. Look at the list again.', 409)
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
    return path == PREFIX or path in [f'{PREFIX}/{action}' for action in ACTIONS]


def get(handler):
    try:
        result = listing()
        name = parse_qs(urlsplit(handler.path).query).get('topic', [''])[0].strip()
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


def serve(handler, body):
    action = handler.path.split('?', 1)[0][len(PREFIX) + 1:]
    try:
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
                          count=body.get('count'), texts=body.get('texts'))
        else:
            raise NotesError('Not found', 404)
        handler.respond(200, result)
    except NotesError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except OSError:
        handler.respond(503, {'error': 'Saved topic notes could not be changed.'})
