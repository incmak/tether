"""Clean-up of stale lessons and notes (B211). A weekly check flags them; the person removes each one with a tap.

- A lesson is stale when it never repeated (hits == 1) and it is older than lessons.DAYS_STALE days.
- A note is stale when its file did not change for NOTE_DAYS days. Only the notes of a project folder are read
  (notes/*.md); stack notes and subfolders such as notes/postmortems/ are not.
- "Keep" records the time: the item is not flagged again until it would be stale counted from then.
- Nothing is removed without the person's tap on that item. check() only writes the list of flagged keys.
- A removed lesson stays in this module's file for UNDO_DAYS days, and "Undo" puts it back as it was. After that the
  weekly check drops it for good. A removed note moves to notes/.trash/ (projects.delete_note) and stays there: Tether
  never deletes a note file. "Undo" moves it back for UNDO_DAYS days; later the person can still copy it from .trash.

State: paths.CONFIG/prune.json
  {"version": 1, "last_run": epoch, "flagged": [key], "kept": {key: epoch}, "trash": [record]}
"""
import fcntl
import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager

import decisions
import lessons
import paths
import projects

WEEK = 7 * 86400
DAY = 86400
NOTE_DAYS = 30
UNDO_DAYS = 7
TRASH = 200          # undo records at one time; a removal past that is refused, never an older record dropped
_LOCK = threading.RLock()


class PruneError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoteInTrash(PruneError):
    """The note is in notes/.trash and could not go back to its place. Its undo record must stay."""


def path():
    return paths.CONFIG / 'prune.json'


@contextmanager
def _locked():
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _LOCK:
        fd = os.open(str(file) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield


def _load():
    try:
        data = json.loads(path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = None
    except (OSError, ValueError):
        raise PruneError(500, 'The clean-up file is invalid. Tether does not change it.') from None
    if data is None:
        return {'version': 1, 'last_run': 0, 'flagged': [], 'kept': {}, 'trash': []}
    if (not isinstance(data, dict) or data.get('version') != 1 or type(data.get('last_run')) not in (int, float)
            or not isinstance(data.get('flagged'), list) or not isinstance(data.get('kept'), dict)
            or not isinstance(data.get('trash'), list)
            or any(not isinstance(item, dict) or item.get('kind') not in ('lesson', 'note')
                   or not isinstance(item.get('token'), str) or type(item.get('at')) not in (int, float)
                   for item in data['trash'])):
        raise PruneError(500, 'The clean-up file is invalid. Tether does not change it.')
    return data


def _save(data):
    lessons._write(path(), data)


def _key(kind, pid, name):
    return f'{kind}:{pid or "global"}:{name}'


def _project_ids():
    root = projects.root()
    return [folder.name for folder in sorted(root.iterdir())
            if decisions.PROJECT_ID.fullmatch(folder.name) and folder.is_dir()] if root.is_dir() else []


def _project_name(pid):
    try:
        return projects.meta(pid)['name'] or pid
    except (OSError, ValueError):
        return pid


def _mark(*parts):
    """A short version stamp of an item as the list shows it. A Remove tap carries it, and the server removes the
    item only when it still has this stamp: a lesson that repeated or a note that changed since stays."""
    return hashlib.sha256('\x1f'.join(str(part) for part in parts).encode()).hexdigest()[:16]


def stale(now=None, kept=None):
    """Every stale item now: {'lessons': [...], 'notes': [...], 'skipped': [project ids that did not read]}."""
    now = time.time() if now is None else now
    kept = kept or {}
    found = lessons.all_entries()
    rows = []
    for row in found['lessons']:
        key = _key('lesson', row['project'], row['id'])
        if _lesson_stale(row, key, kept, now):
            rows.append(dict(row, key=key, mark=_lesson_mark(row)))
    notes, skipped = [], list(found['skipped'])
    for pid in _project_ids():
        try:
            listed = projects.notes(pid)
        except (OSError, ValueError, projects.ProjectError):
            if pid not in skipped:
                skipped.append(pid)
            continue
        name = _project_name(pid)
        for note in listed:
            key = _key('note', pid, note['name'])
            if now - max(note['mtime'] / 1000, kept.get(key, 0)) > NOTE_DAYS * DAY:
                try:
                    mark = _note_mark(projects.bundle(pid) / 'notes' / note['name'])
                except OSError:
                    continue  # gone since the folder was listed
                notes.append({'key': key, 'project': pid, 'project_name': name, 'name': note['name'], 'mark': mark,
                              'title': note['title'], 'size': note['size'], 'mtime': note['mtime']})
    notes.sort(key=lambda note: note['mtime'])
    return {'lessons': rows, 'notes': notes, 'skipped': skipped}


def _fresh(data, now):
    """Drop undo records past UNDO_DAYS (a lesson record that goes here is gone for good; its removal was confirmed).
    Nothing younger is ever dropped. Returns True when the data changed."""
    kept = [item for item in data['trash'] if now - item['at'] <= UNDO_DAYS * DAY]
    changed = len(kept) != len(data['trash'])
    data['trash'] = kept
    return changed


def check(now=None):
    """The weekly check: flag what is stale now. Returns {'lessons': n, 'notes': n}. It removes nothing but undo
    records that are past UNDO_DAYS. The project files are read outside the lock; the lock covers only the state."""
    now = time.time() if now is None else now
    every = stale(now)  # without the kept times: a kept item that still exists keeps its time
    live = {item['key'] for item in every['lessons'] + every['notes']}
    with _locked():
        data = _load()
        kept = data['kept']
        lessons_now = [row for row in every['lessons'] if now - kept.get(row['key'], 0) > lessons.DAYS_STALE * DAY]
        notes_now = [note for note in every['notes'] if now - kept.get(note['key'], 0) > NOTE_DAYS * DAY]
        data.update(last_run=now, flagged=[item['key'] for item in lessons_now + notes_now],
                    kept={key: at for key, at in kept.items() if key in live})
        _fresh(data, now)
        _save(data)
    return {'lessons': len(lessons_now), 'notes': len(notes_now)}


def state(now=None):
    """What the clean-up list shows: the items the last check flagged that are still stale, and what can be undone."""
    now = time.time() if now is None else now
    with _locked():
        data = _load()
        if _fresh(data, now):
            _save(data)
    flagged = set(data['flagged'])
    found = stale(now, data['kept'])
    removed = [{'token': item['token'], 'kind': item['kind'], 'at': item['at'], 'until': item['at'] + UNDO_DAYS * DAY,
                'project_name': item.get('project_name') or 'All projects',
                'text': item['lesson']['text'] if item['kind'] == 'lesson' else item.get('title') or item['name'],
                'name': item.get('name', '')}
               for item in reversed(data['trash'])]
    return {'last_run': data['last_run'], 'next_run': data['last_run'] + WEEK if data['last_run'] else 0,
            'stale_days': lessons.DAYS_STALE, 'note_days': NOTE_DAYS, 'undo_days': UNDO_DAYS,
            'lessons': [row for row in found['lessons'] if row['key'] in flagged],
            'notes': [note for note in found['notes'] if note['key'] in flagged],
            'removed': removed, 'skipped': found['skipped']}


def _target(kind, pid, name):
    if kind not in ('lesson', 'note') or not isinstance(name, str) or not name:
        raise PruneError(400, 'Choose a lesson or a note.')
    if pid is None:
        if kind == 'note':
            raise PruneError(400, 'Choose a project.')
    elif not isinstance(pid, str) or not decisions.PROJECT_ID.fullmatch(pid):
        raise PruneError(400, 'Choose a project.')
    return _key(kind, pid, name)


def keep(kind, pid, name, now=None):
    """The person keeps a flagged item: it leaves the list until it is stale again, counted from now."""
    now = time.time() if now is None else now
    key = _target(kind, pid, name)
    with _locked():
        data = _load()
        if key not in data['flagged']:
            raise PruneError(404, 'That item is no longer in the clean-up list.')
        data['kept'][key] = now
        _save(data)


def _lesson_mark(row):
    return _mark(row['text'], row['kind'], row['hits'], row['last_hit'], row['status'], row['reviewed_at'])


def _note_mark(file):
    """The stamp of a note file: its content. A note with other text never has the stamp of the one the list showed."""
    return _mark(hashlib.sha256(file.read_bytes()).hexdigest())


def _lesson_stale(row, key, kept, now):
    basis = max(row['created'], row['last_hit'], row['reviewed_at'] or 0, kept.get(key, 0))
    return row['hits'] == 1 and now - basis > lessons.DAYS_STALE * DAY


def _move_note(pid, name, kept_as, mark):
    """Move one note to notes/.trash/<kept_as>, but only the note the person saw. The rename comes first and the
    check reads the moved file, so the text that was checked is the text that was removed: an edit cannot land
    between the two. A file with another stamp goes back to its place, and nothing is removed."""
    if not projects.NOTE.fullmatch(name):
        raise PruneError(400, 'Choose a note.')
    folder = projects.bundle(pid) / 'notes'
    source, trash = folder / name, folder / '.trash'
    if source.is_symlink() or not source.is_file():
        raise PruneError(404, 'That note is gone.')
    trash.mkdir(mode=0o700, exist_ok=True)
    target = trash / kept_as
    if target.exists():
        raise PruneError(409, 'Tether could not move the note. Try again.')
    os.replace(source, target)
    try:
        if _note_mark(target) == mark:
            return
        problem = 'That note changed after the list loaded. Read it again, then choose.'
    except OSError:
        problem = 'Tether could not read the note, so it did not remove it.'
    try:
        os.link(target, source)  # back in place, unless a newer note took the name in the meantime
    except OSError:
        # Not back in place (a newer note has the name, or the disk refused). The text is in .trash and the undo
        # record stays, so the list shows it under Removed with Undo.
        raise NoteInTrash(409, 'That note changed or could not be read, and Tether could not put it back. '
                               'It is under Removed: use Undo.') from None
    try:
        target.unlink()
    except OSError:
        pass  # the note is back in place; a second copy stays in .trash, where nothing is ever deleted
    raise PruneError(409, problem)


def remove(kind, pid, name, mark, now=None):
    """The person's tap on one flagged item. Returns the undo token. mark: the item's stamp in the list the person
    saw. An item that changed, repeated or was kept since is not removed: for a lesson the check and the removal
    hold the lesson file's lock (lessons.take); for a note the check reads the file after its move (_move_note).
    The undo record is saved before the item goes, so a failure between the two steps loses nothing."""
    now = time.time() if now is None else now
    key = _target(kind, pid, name)
    changed = PruneError(409, 'That item changed after the list loaded. Read it again, then choose.')
    with _locked():
        data = _load()
        if key not in data['flagged']:
            raise PruneError(404, 'That item is no longer in the clean-up list.')
        if not isinstance(mark, str) or not mark:
            raise changed
        _fresh(data, now)
        if len(data['trash']) >= TRASH:
            raise PruneError(409, f'{TRASH} removals wait in Undo. Each one leaves it after {UNDO_DAYS} days. Remove more then.')
        token = secrets.token_hex(8)
        record = {'token': token, 'kind': kind, 'project': pid, 'at': now, 'key': key,
                  'project_name': _project_name(pid) if pid else 'All projects'}

        def forget():
            data['trash'] = [item for item in data['trash'] if item['token'] != token]
            _save(data)

        if kind == 'lesson':
            def before(row):  # under the lesson file's lock
                if not _lesson_stale(row, key, data['kept'], now):
                    raise PruneError(404, 'That item is no longer in the clean-up list.')
                if _lesson_mark(row) != mark:
                    raise changed
                record['lesson'] = row
                data['trash'].append(record)
                _save(data)
            try:
                gone = lessons.take(pid, name, before)
            except BaseException:
                forget()  # the lesson stays, so no undo record may stay either
                raise
            if gone is None:
                raise PruneError(404, 'That item is no longer in the clean-up list.')
        else:
            shown = next((note for note in projects.notes(pid) if note['name'] == name), None)
            if shown is None or now - max(shown['mtime'] / 1000, data['kept'].get(key, 0)) <= NOTE_DAYS * DAY:
                raise PruneError(404, 'That item is no longer in the clean-up list.')
            record.update(name=name, title=shown['title'], file=projects.trash_name(name))
            data['trash'].append(record)
            _save(data)
            try:
                _move_note(pid, name, record['file'], mark)
            except NoteInTrash:
                data['flagged'] = [item for item in data['flagged'] if item != key]
                _save(data)  # the record stays: it is the way back for the note
                raise
            except BaseException:
                forget()  # the note is in its place (or the move never happened)
                raise
        data['flagged'] = [item for item in data['flagged'] if item != key]
        _save(data)
    return token


def restore(token, now=None):
    """Undo one removal within UNDO_DAYS. The item comes back as it was and is kept (not flagged again at once)."""
    now = time.time() if now is None else now
    with _locked():
        data = _load()
        record = next((item for item in data['trash'] if item['token'] == token), None)
        if record is None or now - record['at'] > UNDO_DAYS * DAY:
            raise PruneError(404, f'That removal can no longer be undone. Undo works for {UNDO_DAYS} days.')
        pid = record.get('project')
        if record['kind'] == 'lesson':
            try:
                lessons.restore(pid, record['lesson'])  # False: it is there already, so only the record goes
            except ValueError as exc:
                raise PruneError(409, str(exc)) from None
        else:
            folder = projects.bundle(pid) / 'notes'
            source, target = folder / '.trash' / record['file'], folder / record['name']
            if not projects.NOTE.fullmatch(record['name']) or source.is_symlink() or not source.is_file():
                raise PruneError(404, 'The removed note is no longer in the notes trash folder.')
            try:
                os.link(source, target)  # never replaces a note that was written since
            except FileExistsError:
                raise PruneError(409, f'A note named {record["name"]} exists now. Rename it, then undo again.') from None
            source.unlink()
        data['trash'] = [item for item in data['trash'] if item['token'] != token]
        data['kept'][record.get('key') or _key(record['kind'], pid, record.get('name') or record['lesson']['id'])] = now
        _save(data)


def notice(counts, silent=False):
    parts = [f'{counts[key]} {word if counts[key] == 1 else word + "s"}' for key, word in (('lessons', 'lesson'), ('notes', 'note'))
             if counts[key]]
    return {'title': 'Clean-up: stale lessons and notes', 'tag': 'tether-cleanup', 'go': 'cleanup', 'priority': 'low',
            'body': f'{" and ".join(parts)} look stale. Review the list. Nothing is removed until you choose it.',
            'silent': silent, 'url': '/chat/?go=cleanup'}


class Pruner:
    """Runs check() from the monitor loop once a week (and once when no check ran yet)."""

    def __init__(self, notify=None, delivery=None, clock=time.time):
        self.notify, self.delivery, self.clock = notify, delivery, clock
        self.next = 0  # the file is read at most once a minute
        self.busy = False
        self.counts = None  # of the last check this object ran

    def observe(self, wait=False):
        """Start the check in its own thread when it is due (it reads every project's files). Returns True when it
        started one. wait=True runs it before it returns."""
        now = self.clock()
        if self.busy or now < self.next:
            return False
        self.next = now + 60
        self.busy = True
        thread = threading.Thread(target=self._work, args=(now,), daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True

    def _work(self, now):
        try:
            with _locked():
                last = _load()['last_run']
            if last and now - last < WEEK:
                return
            self.counts = counts = check(now)
            mode = (self.delivery() if self.delivery else {}).get('low', 'off')
            if (counts['lessons'] or counts['notes']) and self.notify and mode != 'off':
                self.notify(notice(counts, silent=mode == 'silent'), False)
        except Exception as exc:  # noqa: BLE001 - the check must never take the monitor down
            print(f'clean-up check failed: {exc!r}', flush=True)
        finally:
            self.busy = False
