"""One backlog for the whole app, in Markdown, that the person and every agent share.

paths.TETHER/backlog/
  BACKLOG.md   the list. Items at the top are the person's inbox; "## name" starts a project's items.
  .lock        held for every change, by the server and by the agent command (they are separate processes)
  versions/    the last copies, so a bad overwrite can be undone by hand
  last-id      the highest ID ever saved, so no ID is given twice
  DONE.md      finished items moved out of BACKLOG.md when it grows near its limit, in file order, under their section

Codex's sandbox gets write access to this folder only, so it holds nothing else. Agents change the list through
agent_backlog_cli.py. A saved version check catches anything that edits the file another way.

The sandbox cannot write the audit log (B303). audit.folder()/backlog-seen.json holds the last audited list and the
IDs of checked items in DONE.md; the server records the difference when it sees the files change. The sandbox can
write anything in the backlog folder, so the server cannot prove who made a change. The server sees only net changes,
and a change undone before observation leaves no trace."""
import contextlib
from collections import Counter, defaultdict
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

import audit
import paths
from projects import ProjectError, _read, _write, invalid

# A backlog save is at most 1 MiB (projects_api.py BACKLOG_BODY_LIMIT). 200,000 characters fit in any language,
# 4 bytes each at most in UTF-8, with room for the JSON around them.
LIMIT = 200000
# Past ARCHIVE_AT characters, finished items move to DONE.md, in file order, until the list is at ARCHIVE_TO or less.
# Agents read the whole list, so it stays short; LIMIT is room for a list with many open items.
ARCHIVE_AT, ARCHIVE_TO = 48000, 36000
ARCHIVE_READ = 32 * 1024 * 1024  # the server compares DONE.md up to this size (B303); it is about 220 KB in 2026
VERSIONS = 50
# The patterns below run on every line of a text the browser sends. Each runs in linear time: no match can start
# inside a run of spaces, and nothing lazy meets a trailing \s*. Long runs of spaces took seconds before (15 Nov 2026).
HEADING = re.compile(r'#{1,6}\s+(\S(?:.*\S)?|)\s*$')
ITEM = re.compile(r'[-*] \[([ xX])\] (.*)$')
SOURCE = re.compile(r'(?<!\s)\s*\((?:claude|codex)(?: · [^()·]+)*\)\s*$')
ID = re.compile(r'(?<!\s)\s+#B([0-9]+)\s*$')  # every item ends with its ID, like #B12. The UI gives IDs the same way (backlogItems.ts).
ID_QUERY = re.compile(r'#?[Bb]([0-9]+)')
STATUS = re.compile(r'\s+- (?:Doing|Done):')
PANE_HINT = re.compile(r'pane (%[0-9]+)')
ARCHIVE_STAMP = re.compile(r' \(moved [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}\)$')
WORKTREE = re.compile(r'gitdir:\s*(.+?)/\.git/worktrees/')
_SEEN = 'backlog-seen.json'
_observed_files = None
_READ = object()  # _catch_up reads DONE.md itself
_busy_since = None
BUSY_LIMIT = 30.0


def folder():
    return paths.TETHER / 'backlog'


def _path():
    path = folder() / 'BACKLOG.md'
    if folder().is_symlink() or path.is_symlink():
        raise invalid('The backlog is a link. Tether does not open it.')
    return path


def _ids(text):
    return [int(found[1]) for line in text.split('\n') if ITEM.fullmatch(line) and (found := ID.search(line))]


def _last_id():
    """The highest ID ever saved. Items can leave the list (moved to DONE.md, or deleted), so the list alone cannot
    say which IDs were given."""
    try:
        return int(_read(folder() / 'last-id', 32).strip() or 0)
    except ValueError:
        return 0


def _state(text):
    return {'text': text, 'version': hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest(), 'limit': LIMIT,
            'next_id': max(_ids(text) + [_last_id()]) + 1}


def read():
    """The text and a version made from it. A file that grew too long is refused: showing part of it would make the
    next save drop the rest."""
    text = _read(_path(), LIMIT + 1)
    if len(text) > LIMIT:
        raise ProjectError(413, f'The backlog is over {LIMIT:,} characters. Shorten {_path()}.')
    return _state(text)


def _open_lock():
    folder().mkdir(mode=0o700, parents=True, exist_ok=True)
    _path()
    return os.open(folder() / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


@contextlib.contextmanager
def _locked():
    handle = _open_lock()
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)  # also releases the lock


@contextlib.contextmanager
def _try_locked():
    try:
        handle = _open_lock()
    except OSError:  # a worker can put a folder or a link at the lock path; treat it as a busy lock
        yield False
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(handle)


def _keep(text):
    """Keep a copy unless the newest one matches. A failed copy never stops a save."""
    if not text.strip():
        return
    base = folder() / 'versions'
    try:
        base.mkdir(mode=0o700, exist_ok=True)
        names = sorted(base.glob('*.md'))
        if names and _read(names[-1]) == text:
            return
        _write(base / f'{time.time_ns()}.md', text)
        for old in sorted(base.glob('*.md'))[:-VERSIONS]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _item_end(lines, at):
    """The end of an item and all its detail lines. Like the UI (backlogItems.ts parseBacklog), an indented line after
    a blank line still belongs to the item; trailing blank lines do not."""
    end = at + 1
    for index in range(at + 1, len(lines)):
        if lines[index].strip():
            if not lines[index][0].isspace():
                break
            end = index + 1
    return end


def _finished(text, everything, keep_since=None):
    """The finished top-level items to move, in file order: (moved [(section, block)], lines, the line numbers to drop).
    everything: all of them; else only until the text is at ARCHIVE_TO or less. keep_since: items done at or after
    this time stay, so the work board still shows them (board_items)."""
    lines = text.split('\n')
    size, moved, section, drop = len(text), [], 'Inbox', set()
    for at, line in enumerate(lines):
        heading = HEADING.fullmatch(line)
        if heading:
            section = heading[1]
            continue
        found = ITEM.fullmatch(line)
        if not found or found[1] == ' ':
            continue
        end = _item_end(lines, at)
        if keep_since is not None and (_done_time(lines[at:end]) or 0) >= keep_since:
            continue
        moved.append((section, lines[at:end]))
        gap = end
        while (at == 0 or not lines[at - 1].strip()) and gap < len(lines) and not lines[gap].strip():
            gap += 1  # no blank line is left where a blank-separated item was
        drop.update(range(at, gap))
        size -= sum(len(part) + 1 for part in lines[at:end])
        if not everything and size <= ARCHIVE_TO:
            break
    return moved, lines, drop


def _kept(text):
    """The length of the text after every finished item moves to DONE.md: the most the list must hold."""
    _, lines, drop = _finished(text, True)
    return len('\n'.join(line for at, line in enumerate(lines) if at not in drop))


def _archive(text, everything=False):
    """Move finished items in file order, with their detail lines, to DONE.md: past ARCHIVE_AT until the text is at
    ARCHIVE_TO or less, or all of them with everything. last-id keeps the moved IDs from being given again. DONE.md is
    written first: a failed write keeps the items in the list, never loses them."""
    if not everything and len(text) <= ARCHIVE_AT:
        return text
    moved, lines, drop = _finished(text, everything, None if everything else time.time() - WEEK)
    if not everything and len(text) - sum(len(lines[at]) + 1 for at in drop) > LIMIT:
        moved, lines, drop = _finished(text, everything)  # the list must fit: this week's finished items move too
    if not moved:
        return text
    stamp = time.strftime('%Y-%m-%d %H:%M')
    out, last = [], None
    for section, block in moved:
        if section != last:
            out += ['', f'## {section} (moved {stamp})']
            last = section
        out += block
    path = folder() / 'DONE.md'
    if path.is_symlink():
        raise invalid('The backlog archive is a link. Tether does not open it.')
    head = '' if path.exists() else '# Finished backlog items\n\nMoved here from BACKLOG.md, by hand (agent_backlog_cli.py archive) or when the list grew long.\n'
    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, 'a', encoding='utf-8') as archive:
        archive.write(head + '\n'.join(out) + '\n')
    return '\n'.join(line for at, line in enumerate(lines) if at not in drop)


def _replace(current, text, everything=False, audit_before=None, before_write=None):
    with contextlib.ExitStack() as stack:
        audited = _audit_writable()
        if audited:
            try:
                stack.enter_context(_audit_lock())
            except OSError:
                audited = False
        return _replace_audited(current, text, everything, audit_before, before_write, audited)


def _replace_audited(current, text, everything, audit_before, before_write, audited):
    # Catch up before _archive adds this write's items to DONE.md. Use the file on disk: the caller read `current`
    # before it held the audit lock, and a worker can replace the backlog lock.
    on_disk = _disk_text() if audited else None
    seen = _catch_up(on_disk) if on_disk is not None else None
    highest = max(_ids(current) + _ids(text) + [0])
    if highest > _last_id():
        _write(folder() / 'last-id', f'{highest}\n')
    edited = text
    text = _archive(text, everything)
    if len(text) > LIMIT:
        raise invalid(f'The backlog is full ({LIMIT:,} characters). Delete some items first.')
    _keep(current)  # an agent may have edited the file directly since the last copy
    if before_write:
        before_write()
    _write(_path(), text)
    _keep(text)
    if audited:
        recorded = audit_before is None or _audit_changes(audit_before, edited)
        # When the file changed after the caller read it, this write may drop those changes. Keep the snapshot at
        # the file before the write, so observe() records the whole difference (a repeat is better than a loss).
        if seen is not None and recorded and on_disk == current:
            # Only this write's own archived items join the seen set. Anything else new in DONE.md stays for observe().
            moved = _audit_items(edited)[0].keys() - _audit_items(text)[0].keys()
            _save_seen(text, seen | moved)
    return _state(text)


def _audit_writable():
    try:
        place = audit.folder()
        while not place.exists() and place != place.parent:
            place = place.parent  # the audit folder is made on the first record
        return os.access(place, os.W_OK | os.X_OK)
    except OSError:
        return False


@contextlib.contextmanager
def _audit_lock(block=True):
    """Serialise catch-ups. The lock lives in the audit folder, which a sandboxed worker cannot write. The file is
    write-only (0200), so the worker cannot open it read-only to hold the lock either."""
    place = audit.folder()
    place.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(place / '.backlog-seen.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o200)
    try:
        os.fchmod(handle, 0o200)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        with contextlib.suppress(OSError):
            os.close(handle)  # also releases the lock; an error here must not fail a saved action


def _save_seen(text, archived):
    try:
        _write(audit.folder() / _SEEN, json.dumps({'text': text, 'archived': sorted(archived)}))
    except Exception as exc:
        audit._warn(f'could not write the backlog snapshot ({type(exc).__name__}).')
        return False
    return True


def _catch_up(text, *, archive=_READ):
    """Record changes since the last audited list, and return the checked DONE.md IDs that the snapshot now holds, or
    None when it did not advance. The caller holds the audit lock. An archive write can be half done or fail half way
    (DONE.md has an item that BACKLOG.md still holds). Such an item is not counted as archived yet, so the next pass
    sees it leave the list for DONE.md."""
    try:
        path = audit.folder() / _SEEN
        old_archived = None
        if path.exists():
            seen = json.loads(_read(path))
            before, old_archived = seen['text'], set(seen['archived'])
        archived = _checked_archive(_read_archive() if archive is _READ else archive)
        if archived and old_archived is not None:
            # Defer only IDs that are new to the snapshot; the snapshot keeps the IDs it already holds.
            listed = _audit_items(text)[0].keys()
            archived = {ref: row for ref, row in archived.items() if ref not in listed or ref in (old_archived or ())}
        if archived is None:
            audit._warn('could not read the backlog archive; items that left the list count as removed.')
        if old_archived is not None:
            if before == text and (archived is None or old_archived == archived.keys()):
                return old_archived
            if not _audit_changes(before, text, observed=True, archived=archived or {}, old_archived=old_archived):
                # Keep the old snapshot. A later pass can repeat successful records, which is better than losing one.
                return None
    except Exception as exc:
        audit._warn(f'could not compare the backlog snapshot ({type(exc).__name__}).')
        return None
    # An unreadable archive keeps the old set, so its new entries still count once it can be read again. Items that
    # left the list meanwhile were recorded as removed; their archive entries must not count again later.
    refs = set(archived) if archived is not None else old_archived or set()
    if archived is None and old_archived is not None:
        refs |= _audit_items(before)[0].keys() - _audit_items(text)[0].keys()
    return refs if _save_seen(text, refs) else None


def _disk_text():
    try:
        text = _file_text(_read_file(_path(), 4 * LIMIT + 1))
    except OSError as exc:
        audit._warn(f'could not read the backlog for the audit ({type(exc).__name__}).')
        return None
    return text if len(text) <= LIMIT else None


def _read_file(path, limit=-1):
    # The sandbox can write in this folder. Refuse links and do not wait on pipes.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b''
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f'{path.name} is not a regular file.')
        return source.read(limit)


def _file_text(contents):
    return contents.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')


def _read_archive():
    """DONE.md, or None when it cannot be read (not a regular file, or too large). Unrelated changes still record."""
    try:
        contents = _read_file(folder() / 'DONE.md', ARCHIVE_READ + 1)
    except OSError:
        return None
    return None if len(contents) > ARCHIVE_READ else contents


def _backlog_files():
    path = _path()
    # DONE.md can change while BACKLOG.md returns to the same bytes.
    return path, _read_file(path, 4 * LIMIT + 1), _read_archive()


def _seen_revision():
    # A CLI write can advance the snapshot between observations and invalidate the cached comparison.
    path = audit.folder() / _SEEN
    try:
        info = path.stat()
    except FileNotFoundError:
        return path, None
    return path, (info.st_dev, info.st_ino, info.st_mtime_ns)


def _backlog_lock_free(locked):
    """A sandboxed worker can hold the backlog lock without end. Wait BUSY_LIMIT seconds, then observe without it."""
    global _busy_since
    if locked:
        _busy_since = None
        return True
    now = time.monotonic()
    if _busy_since is None:
        _busy_since = now
    return now - _busy_since >= BUSY_LIMIT


def observe():
    """Catch up after a file change. A failed observation never fails an audit request, and a busy lock skips it."""
    global _observed_files
    try:
        if (_seen_revision(), _backlog_files()) == _observed_files:
            return
        with _audit_lock(block=False) as audit_locked, _try_locked() as locked:
            if not audit_locked or not _backlog_lock_free(locked):
                return
            revision, files = _seen_revision(), _backlog_files()
            if (revision, files) == _observed_files:
                return
            _, contents, archive = files
            text = _file_text(contents)
            if len(text) > LIMIT:
                raise OSError('The backlog is too long to observe.')
            if _catch_up(text, archive=archive) is not None:
                _observed_files = _seen_revision(), files
    except Exception as exc:
        audit._warn(f'could not observe the backlog ({type(exc).__name__}).')


def watch(stop=None, interval=2.0):
    if stop is None:
        stop = threading.Event()
    while True:
        observe()
        if stop.wait(interval):
            return


def with_ids(text, before=''):
    """Give each item without an ID the next free one, in file order. Text that already has IDs stays the same.
    before: the saved text this replaces. Its IDs count too, so an item deleted in the same save never gives its ID
    again, even before last-id exists."""
    lines = text.split('\n')
    next_id = max(_ids(text) + _ids(before) + [_last_id()]) + 1
    for at, line in enumerate(lines):
        if ITEM.fullmatch(line) and not ID.search(line):
            lines[at] = f'{line.rstrip()} #B{next_id}'
            next_id += 1
    return '\n'.join(lines)


def numbered():
    """read(), after giving IDs to items that have none: a list from before IDs, or an item typed into the file."""
    state = read()
    if with_ids(state['text']) == state['text']:
        return state
    try:
        with _locked():
            current = read()['text']
            fixed = with_ids(current)
            return _replace(current, fixed) if fixed != current else _state(current)
    except ProjectError:  # too full for the IDs: show it as it is
        return read()


def save(text, version):
    """Save only over the version the person last saw. Items written as text get their IDs here."""
    if not isinstance(text, str):
        raise invalid('Invalid backlog text.')
    if not isinstance(version, str):
        raise invalid('Reload the backlog and try again.')
    if len(text) > LIMIT and _kept(text) > LIMIT:  # before the lock and the ID pass; finished items can move to DONE.md
        raise invalid(f'The backlog is full ({LIMIT:,} characters). Delete some items first.')
    with _locked():
        current = read()
        if current['version'] != version:
            raise ProjectError(409, 'The backlog changed while you were editing.')
        return _replace(current['text'], with_ids(text, current['text']), audit_before=current['text'])


def archive_done(dry_run=False):
    """Move every finished item, with its detail lines, to DONE.md now. Returns (items moved, characters before, after)."""
    with _locked():
        current = read()['text']
        moved, lines, drop = _finished(current, True)
        after = _kept(current)
        if moved and not dry_run:
            after = len(_replace(current, current, everything=True)['text'])
        return len(moved), len(current), after


def _one_line(text):
    return ' '.join(text.split()) if isinstance(text, str) else ''


def _lines(text):
    lines = text.split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _section(lines, project):
    """The (start, end) lines of the inbox, or of a project's items. start is None when the project has no heading yet."""
    headings = [at for at, line in enumerate(lines) if HEADING.fullmatch(line)]
    if not project:
        return 0, headings[0] if headings else len(lines)
    for place, at in enumerate(headings):
        if HEADING.fullmatch(lines[at])[1].casefold() == project.casefold():
            return at + 1, headings[place + 1] if place + 1 < len(headings) else len(lines)
    return None, len(lines)


def _insert_at_end(lines, start, end, new):
    """Put new lines after the section's last line that is not blank, with a blank line before a following heading."""
    at = end
    while at > start and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = new
    after = at + len(new)
    if after < len(lines) and lines[after].strip():
        lines.insert(after, '')


def _change(change, audit_action=True, before_write=None):
    with _locked():
        current = read()['text']
        lines = _lines(current)
        result = change(lines)
        _replace(current, with_ids('\n'.join(lines) + '\n', current), audit_before=current if audit_action else None,
                 before_write=(lambda: before_write(result)) if before_write else None)
    return result


def add(item, source='', project='', details=(), *, literal=False, before_write=None):
    """Add to a project or the inbox. before_write records the reserved item ID before the atomic save."""
    item = _one_line(item)
    if not item:
        raise invalid('Write the item text.')
    below = [f'  - {text}' for text in map(_one_line, details) if text]
    def change(lines):
        title = f'- [ ] {item}{f" ({source})" if source else ""}'
        # External titles are data. A trailing #B12 in one cannot pick the item's ID.
        if literal and ID.search(title):
            suffix = with_ids('\n'.join([*lines, '- [ ] New item'])).split('\n')[-1]
            line = title + ID.search(suffix)[0]
        else:
            line = with_ids('\n'.join([*lines, title])).split('\n')[-1]
        start, end = _section(lines, project)
        if start is None:
            lines.extend([''] * bool(lines) + [f'## {project}', line, *below])
        else:
            _insert_at_end(lines, start, end, [line, *below])
        return line
    return _change(change, before_write=before_write)


def has_id(ref):
    """Read a reserved ID without changing the backlog, including items that moved to the archive."""
    number = int(ref[1:])
    with _locked():
        return number in _ids(read()['text']) or number in _ids(_read(folder() / 'DONE.md'))


def _open_items(lines, project):
    spans = [_section(lines, '')] + ([_section(lines, project)] if project else [])
    for start, end in spans:
        for at in range(start or 0, end if start is not None else 0):
            found = ITEM.fullmatch(lines[at])
            if found and found[1] == ' ':
                yield at, _bare(found[2])


def _bare(text):
    """An item's words, without its ID and the record of who added it."""
    return SOURCE.sub('', ID.sub('', text))


def _last_status(lines, label):
    return next((line for line in reversed(lines) if line.lstrip().startswith(label)), None)


def _audit_items(text, *, blocks=None, unnumbered_blocks=None):
    lines = text.split('\n')
    items, unnumbered = {}, Counter()
    project = None
    for at, line in enumerate(lines):
        heading = HEADING.fullmatch(line)
        if heading:
            project = heading[1] or None
        elif item := ITEM.fullmatch(line):
            end = _item_end(lines, at)
            doing_line = _last_status(lines[at + 1:end], '- Doing:')
            doing = doing_line.split(':', 1)[1].strip() if doing_line else None
            stage = 'done' if item[1] != ' ' else 'building' if doing is not None else 'ready'
            snapshot = (project, _bare(item[2]).strip(), stage, doing)
            if number := ID.search(line):
                ref = f'B{number[1]}'
                items[ref] = snapshot
                if blocks is not None:
                    blocks[ref] = lines[at:end]
            else:
                unnumbered[snapshot] += 1
                if unnumbered_blocks is not None:
                    unnumbered_blocks[snapshot].append(lines[at:end])
    return items, unnumbered


def done_ids():
    """Completed items from both files. The current item wins during an archive write."""
    archive = folder() / 'DONE.md'
    if archive.is_symlink():
        raise invalid('The backlog archive is a link. Tether does not open it.')
    # Read the current list first: archive writes the destination before removing source items.
    current = _audit_items(read()['text'])[0]
    archived = _audit_items(_read(archive))[0]
    return {ident for ident, item in (archived | current).items() if item[2] == 'done'}


def _pane_hint(kind, block):
    if not block:
        return None
    if kind == 'backlog.add':
        source = SOURCE.search(ID.sub('', block[0]))
        if source and (pane := PANE_HINT.search(source[0])):
            return pane[1]
    if kind == 'backlog.done':
        done = _last_status(block[1:], '- Done:')
        dated = DONE_AT.fullmatch(done.rstrip()) if done else None
        source = SOURCE.search(dated[2]) if dated and dated[2] else None
        if source and source[0][0].isspace() and (pane := PANE_HINT.search(source[0])):
            return pane[1]
    if kind in ('backlog.start', 'backlog.done'):
        doing = _last_status(block[1:], '- Doing:')
        if doing:
            who = doing.split(':', 1)[1].strip()
            if SOURCE.fullmatch(f'({who})') and (pane := PANE_HINT.search(who)):
                return pane[1]
    return None


def _checked_archive(contents):
    if contents is None:
        return None
    blocks = {}
    items, _ = _audit_items(_file_text(contents), blocks=blocks)
    return {ref: (row, blocks[ref]) for ref, row in items.items() if row[2] == 'done'}


def _audit_changes(before, after, *, observed=False, archived=None, old_archived=frozenset()):
    # The sheet saves a whole list, while the CLI and board change one item. Compare the intended items before
    # automatic archiving so the same action records once and archiving does not look like a deletion.
    archived = archived or {}
    new_archived = archived.keys() - old_archived
    if before == after and not new_archived:
        return True
    previous, unnumbered = _audit_items(before)
    blocks = {}
    unnumbered_blocks = defaultdict(list) if observed else None
    current, current_unnumbered = _audit_items(after, blocks=blocks if observed else None,
                                               unnumbered_blocks=unnumbered_blocks)
    added_unnumbered = current_unnumbered - unnumbered
    unnumbered -= current_unnumbered  # only items that left the unnumbered list can get an ID or be removed
    succeeded = True

    def record(kind, summary, *, block=(), **detail):
        nonlocal succeeded
        if observed:
            pane = _pane_hint(kind, block)
            actor = {'source': 'agent-cli', 'pane': pane} if pane else {'source': 'unknown'}
            detail.update(observed=True, actor=actor)
            summary += ' (seen in the file)'
        try:
            if audit.record(kind, summary, **detail) is None:
                succeeded = False
        except Exception as exc:
            succeeded = False
            audit._warn(f'could not record ({type(exc).__name__}).')

    by_title = defaultdict(list)
    if unnumbered:
        # An existing item can get its first ID during a write. Match unchanged items before matching a legacy
        # item whose stage or section changed, so a save neither invents an add nor loses that actual action.
        added = {ref: row for ref, row in current.items() if ref not in previous}
        for ref, row in added.items():
            if unnumbered[row]:
                previous[ref] = row
                unnumbered[row] -= 1
        for row, count in unnumbered.items():
            by_title[row[1]].extend([row] * count)
        # Same title in two projects: pair every item with one from its own project first, and only then pair the
        # rest across projects, so a done in one project and a move in another do not look like two moves.
        for same_project in (True, False):
            for ref, (project, title, _, _) in added.items():
                rows = by_title[title]
                at = next((at for at, row in enumerate(rows) if not same_project or row[0] == project), None)
                if ref not in previous and at is not None:
                    previous[ref] = rows.pop(at)
    for ref, (project, title, stage, doing) in current.items():
        short = title[:80]
        block = blocks.get(ref, ())
        if ref not in previous:
            record('backlog.add', f'{ref} added: {short}', project=project, block=block)
            if observed and stage != 'ready':  # an add and a start or done that the server saw in one change
                record(f'backlog.{"start" if stage == "building" else "done"}',
                       f'{ref} {"started" if stage == "building" else "done"}: {short}', project=project, block=block)
            continue
        old_project, _, old_stage, old_doing = previous[ref]
        if project != old_project:
            record('backlog.move', f'{ref} moved: {short}', project=project,
                   from_project=old_project or 'Inbox', to_project=project or 'Inbox')
        if stage == 'building' and (stage != old_stage or doing != old_doing):
            record('backlog.start', f'{ref} started: {short}', project=project, block=block)
        elif stage == 'done' and stage != old_stage:
            record('backlog.done', f'{ref} done: {short}', project=project, block=block)
        elif stage == 'ready' and stage != old_stage:
            record('backlog.move', f'{ref} moved to Ready: {short}', project=project, from_stage=old_stage,
                   to_stage=stage)
    for ref, (project, title, stage, _) in previous.items():
        if ref in current:
            continue
        if ref not in new_archived:  # an entry the snapshot already held does not vouch for this removal
            record('backlog.remove', f'{ref} removed: {title[:80]}', project=project)
        elif stage != 'done':
            record('backlog.done', f'{ref} done: {title[:80]}', project=project, block=archived[ref][1])
    for ref in sorted(new_archived - previous.keys() - current.keys()):
        (project, title, _, _), block = archived[ref]
        project = ARCHIVE_STAMP.sub('', project or '')
        project = None if project == 'Inbox' else project
        record('backlog.add', f'{ref} added: {title[:80]}', project=project, block=block)
        record('backlog.done', f'{ref} done: {title[:80]}', project=project, block=block)
    if observed:
        for row, count in added_unnumbered.items():
            project, title, _, _ = row
            for block in unnumbered_blocks[row][-count:]:
                record('backlog.add', f'Added backlog item: {title[:80]}', project=project, block=block)
    for rows in by_title.values():
        for project, title, _, _ in rows:
            record('backlog.remove', f'Removed backlog item: {title[:80]}', project=project)
    return succeeded


def _by_id(lines, number, need_open):
    for at, line in enumerate(lines):
        found = ITEM.fullmatch(line)
        id_found = found and ID.search(line)
        if id_found and int(id_found[1]) == number:
            if need_open and found[1] != ' ':
                raise invalid(f'B{number} is already done.')
            return at
    raise invalid(f'No item B{number}. Run list to see the IDs.')


def _match(lines, query, project, need_open=True):
    """The one item that an ID names, anywhere in the list, or the one open item that the words name, in the inbox
    or this project. Never a guess between several."""
    query = _one_line(query).casefold()
    if not query:
        raise invalid('Write the item ID, like B12, or part of the item text.')
    by_id = ID_QUERY.fullmatch(query)
    if by_id:
        return _by_id(lines, int(by_id[1]), need_open)
    items = list(_open_items(lines, project))
    found = [at for at, text in items if text.casefold() == query] or [at for at, text in items if query in text.casefold()]
    if not found:
        raise invalid('No open item matches that. Run list to see them.')
    if len(found) > 1:
        raise invalid(f'{len(found)} open items match that. Use more of the item text.')
    return found[0]


def add_detail(query, text, project=''):
    """Add an indented line under an item."""
    text = _one_line(text)
    if not text:
        raise invalid('Write the detail text.')

    def change(lines):
        at = _block_end(lines, _match(lines, query, project, need_open=False))
        lines.insert(at, f'  - {text}')
    _change(change, audit_action=False)


def reword(query, text, project=''):
    """Give an item new words. Its ID and the record of who added it stay."""
    text = _one_line(text)
    if not text:
        raise invalid('Write the item text.')

    def change(lines):
        at = _match(lines, query, project, need_open=False)
        line = lines[at]
        body = ITEM.fullmatch(line)[2]
        number = ID.search(body)
        source = SOURCE.search(ID.sub('', body))
        lines[at] = f'{line[:line.index("] ") + 2]}{text}{source[0].rstrip() if source else ""}{number[0] if number else ""}'
        return lines[at]
    return _change(change, audit_action=False)


def move(query, target, project=''):
    """Move an item and its details to the end of another project's items, or to the inbox when target is empty.
    A project heading left with no lines goes."""
    target = _clean(_one_line(target))

    def change(lines):
        at = _match(lines, query, project, need_open=False)
        block = lines[at:_block_end(lines, at)]
        del lines[at:at + len(block)]
        for heading in [at for at, line in enumerate(lines) if HEADING.fullmatch(line)][::-1]:
            start, end = _section(lines, HEADING.fullmatch(lines[heading])[1])
            if start == heading + 1 and not any(line.strip() for line in lines[start:end]):
                del lines[heading:end]
        while lines and not lines[-1].strip():
            lines.pop()
        start, end = _section(lines, target)
        if start is None:
            lines.extend([''] * bool(lines) + [f'## {target}', *block])
        else:
            _insert_at_end(lines, start, end, block)
        return block[0]
    return _change(change)


def _block_end(lines, at):
    at += 1
    while at < len(lines) and lines[at].strip() and lines[at][0].isspace():
        at += 1
    return at


def _drop_status(lines, at):
    """Remove the item's Doing and Done lines, after a blank line too (_item_end). Other details stay."""
    end = _item_end(lines, at)
    lines[at + 1:end] = [line for line in lines[at + 1:end] if not STATUS.match(line)]


def start(query, who, project='', notes=None):
    """Mark an open item as in progress, by whom. A second start replaces the first. The item's detail lines go
    into notes, a list: read here, so an item that has no ID yet gives them too."""
    def change(lines):
        at = _match(lines, query, project)
        _drop_status(lines, at)
        if notes is not None:
            notes.extend(line for line in lines[at + 1:_item_end(lines, at)] if line.strip())
        lines.insert(at + 1, f'  - Doing: {_one_line(who) or "someone"}')
        return lines[at]
    return _change(change)


def mark_done(query, project='', result='', who=''):
    """Tick an open item. A result, and who did it, go in a Done line under it."""
    result, who = _one_line(result), _one_line(who)

    def change(lines):
        at = _match(lines, query, project)
        lines[at] = lines[at].replace('[ ]', '[x]', 1)
        _drop_status(lines, at)
        lines.insert(_item_end(lines, at), _done_line(result, who))
        return lines[at]
    return _change(change)


def _done_line(result, who):
    """The Done line, dated, so the work board can show the item for a week (board_items)."""
    if not who and SOURCE.search(result or ''):
        who = 'unknown'  # else the result's own (codex · pane %N) would read as the who part (B303)
    return f'  - Done: {time.strftime("%Y-%m-%d %H:%M")} · {result or "finished"}{f" ({who})" if who else ""}'


def _open_block(lines, start, end):
    """The open items in these lines, with their details."""
    out, keep = [], False
    for line in lines[start:end]:
        found = ITEM.fullmatch(line)
        if found:
            keep = found[1] == ' '
        elif not line[:1].isspace():
            keep = False
        if keep and line.strip():
            out.append(line)
    return out


def list_open(project=''):
    """What an agent sees: the inbox and its own project. Other projects stay out."""
    lines = _lines(numbered()['text'])
    parts = []
    inbox = _open_block(lines, *_section(lines, ''))
    if inbox:
        parts.append('Inbox:\n' + '\n'.join(inbox))
    if project:
        start, end = _section(lines, project)
        items = _open_block(lines, start, end) if start is not None else []
        if items:
            parts.append(f'{project}:\n' + '\n'.join(items))
    return '\n\n'.join(parts) or 'No open items.'


def brief(line):
    """An item's line for an agent: "B12 Fix login". Who added it and the checkbox stay in the file (show prints them)."""
    found = ITEM.fullmatch(line)
    if not found:
        return line.strip()
    number = ID.search(found[2])
    return f'{"B" + number[1] + " " if number else ""}{_bare(found[2]).strip()}'


def _worker(who):
    """Who works on an item, short: "claude · pane %5". The tmux session and the chat stay in the file."""
    parts = [part for part in who.split(' · ') if part and not part.startswith(('tmux ', 'chat '))]
    return ' · '.join(parts) or who.strip()


def _brief_block(lines, start, end):
    """One line for each open item in these lines: its ID and title, who works on it, and how many detail lines it has."""
    out = []
    for at in range(start, end):
        found = ITEM.fullmatch(lines[at])
        if not found or found[1] != ' ':
            continue
        doing, more = '', 0
        for row in lines[at + 1:min(_item_end(lines, at), end)]:
            if not row.strip():
                continue
            if STATUS.match(row):
                doing = _worker(row.split(':', 1)[1].strip()) if row.lstrip().startswith('- Doing:') else doing
            else:
                more += 1
        out.append(f'- {brief(lines[at])}' + (f' [doing: {doing}]' if doing else '')
                   + (f' [{more} detail line{"s" if more > 1 else ""}]' if more else ''))
    return out


def list_brief(project=''):
    """What an agent's `list` prints: the inbox and its own project, one line for each open item. The detail lines
    were two thirds of the full list (B74), and an agent needs them for the one item it works on: show prints them."""
    lines = _lines(numbered()['text'])
    parts = []
    inbox = _brief_block(lines, *_section(lines, ''))
    if inbox:
        parts.append('Inbox:\n' + '\n'.join(inbox))
    if project:
        start, end = _section(lines, project)
        items = _brief_block(lines, start, end) if start is not None else []
        if items:
            parts.append(f'{project}:\n' + '\n'.join(items))
    if not parts:
        return 'No open items.'
    if any('detail line' in part for part in parts):
        parts.append('The detail lines of an item: show B12.')
    return '\n\n'.join(parts)


def show(query, project=''):
    """One item with all its lines, as the file has them. An ID finds a finished item too."""
    lines = _lines(numbered()['text'])
    at = _match(lines, query, project, need_open=False)
    return '\n'.join(line for line in lines[at:_item_end(lines, at)] if line.strip())


def summary(project, limit=5):
    """Open items for an agent's start text: the inbox, then its own project, in list order. Returns (first items, count)."""
    try:
        lines = _lines(numbered()['text'])
    except (ProjectError, OSError):
        return [], 0
    items = []
    for at, text in _open_items(lines, project):
        found = ID.search(lines[at])
        items.append(f'{"B" + found[1] + " " if found else ""}{text.strip()}'[:140])
    return items[:limit], len(items)


def _clean(name):
    return re.sub(r'[^\w.\- ]+', '-', name).strip(' -')[:60]


def project_name(start=None):
    """The repository's folder name, the same from a subfolder or a worktree. No group for the home folder."""
    try:
        here = Path(start or os.getcwd()).resolve()
    except OSError:
        return ''
    home = Path(paths.HOME).resolve()
    if here == home:
        return ''
    for place in (here, *here.parents):
        if place == home:
            break
        marker = place / '.git'
        if marker.is_dir():
            return _clean(place.name)
        if marker.is_file():
            try:
                found = WORKTREE.match(marker.read_text(errors='replace'))
            except OSError:
                found = None
            return _clean(Path(found[1]).name if found else place.name)
    return _clean(here.name)


PANE_REF = re.compile(r'\bpane (%[0-9]+)\b')
CHAT_REF = re.compile(r'\bchat ([0-9A-Za-z-]{4,})\b')


def _names(text, pane, chat):
    """True when a record of who did it ("claude · tmux $3 · pane %12 · chat 019a…") names this tab. A record with a
    chat id must name this chat; a pane id alone counts only when the record has no chat id (tmux reuses pane ids)."""
    chats = CHAT_REF.findall(text)
    if chats:
        return bool(chat) and chat in chats
    return bool(pane) and pane in PANE_REF.findall(text)


def linked(pane, chat, limit=6):
    """The backlog items that one tab works on, finished, or added: [{id, title, state}], state 'doing', 'added'
    or 'done', in that order. It only reads: items without an ID yet are left out."""
    try:
        lines = _lines(read()['text'])
    except (ProjectError, OSError):
        return []
    found = []
    for at, line in enumerate(lines):
        item = ITEM.fullmatch(line)
        number = item and ID.search(line)
        if not number:
            continue
        details = lines[at + 1:_block_end(lines, at)]
        done = item[1] != ' '
        source = SOURCE.search(ID.sub('', line))
        doing = not done and any(re.match(r'\s+- Doing:', row) and _names(row, pane, chat) for row in details)
        finished = done and any(re.match(r'\s+- Done:', row) and _names(row, pane, chat) for row in details)
        added = bool(source) and _names(source[0], pane, chat)
        state = 'doing' if doing else 'done' if finished else 'added' if added else ''
        if state:
            found.append({'id': f'B{number[1]}', 'title': _bare(item[2]).strip()[:140], 'state': state,
                          'open': not done})
    order = {'doing': 0, 'added': 1, 'done': 2}
    found.sort(key=lambda row: (order[row['state']], not row['open']))
    return found[:limit]


# The work board shows backlog items as cards (work_backlog.py). Only Ready, Building and Done fit an item:
# open, open with a Doing line, and ticked.
BOARD_STAGES = ('ready', 'building', 'done')
DONE_AT = re.compile(r'\s+- Done: ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})(?: · (.*))?$')
DETAILS = 20  # detail lines a card carries, at most
WEEK = 7 * 86400


def _done_time(block):
    """When the item was done, from its last Done line (as _card reads it) if _done_line dated it, or None."""
    found = None
    for line in block[1:]:
        if STATUS.match(line) and line.strip().startswith('- Done:'):
            found = DONE_AT.fullmatch(line)
    if not found:
        return None
    try:
        return time.mktime(time.strptime(found[1], '%Y-%m-%d %H:%M'))
    except ValueError:
        return None


def _block_version(block):
    """A number for one item's lines (48 bits, so a browser keeps it exact). A change to the item changes it."""
    return int(hashlib.sha256('\n'.join(block).encode('utf-8', 'surrogatepass')).hexdigest()[:12], 16)


def _card(lines, at, area, rank, now):
    """One item as a card, or None: no ID, or done with no dated Done line or more than a week ago."""
    found = ITEM.fullmatch(lines[at])
    number = ID.search(lines[at])
    if not number:
        return None
    block = lines[at:_item_end(lines, at)]
    doing = done = None
    details = []
    for line in block[1:]:
        if not line.strip():
            continue
        if STATUS.match(line):
            kind, _, text = line.strip()[2:].partition(': ')
            if kind == 'Doing':
                doing = text.strip()
            else:
                dated = DONE_AT.fullmatch(line)
                done = (dated[2] or '') if dated else text.strip()
            continue
        if len(details) < DETAILS:
            details.append(re.sub(r'^[-*] (\[[ xX]\] )?', '', line.strip())[:300])
    finished, done_at = found[1] != ' ', _done_time(block)
    if finished and (done_at is None or now - done_at > WEEK):
        return None
    source = SOURCE.search(ID.sub('', found[2]))
    return {'id': f'B{number[1]}', 'area': area, 'title': _bare(found[2]).strip(), 'rank': rank,
            'stage': 'done' if finished else 'building' if doing is not None else 'ready',
            'source': source[0].strip()[1:-1] if source else '', 'doing': doing, 'done': done, 'doneAt': done_at,
            'details': details, 'version': _block_version(block)}


def board_items(now=None):
    """The backlog's items as work board cards, in file order. Reads only: a read never writes the file."""
    lines = read()['text'].split('\n')
    now = time.time() if now is None else now
    area, cards = 'Inbox', []
    for at, line in enumerate(lines):
        heading = HEADING.fullmatch(line)
        if heading:
            area = heading[1] or 'Inbox'
        elif ITEM.fullmatch(line):
            card = _card(lines, at, area, len(cards), now)
            if card:
                cards.append(card)
    return cards


def set_stage(ref, stage, version, who='Mak'):
    """Move one item from the work board: ready (reopen, or stop Doing), building (a Doing line) or done (ticked, with
    a dated Done line). version: the card's version; the item must not have changed since. Returns the new card."""
    found = ID_QUERY.fullmatch(_one_line(ref).replace('-', '')) if isinstance(ref, str) else None
    if not found:
        raise invalid('Write a backlog ID like B12.')
    if stage not in BOARD_STAGES:
        raise invalid('A backlog item moves only to Ready, Building or Done.')
    if not isinstance(version, int) or isinstance(version, bool):
        raise invalid('Reload the board and try again.')
    number, who = int(found[1]), _one_line(who) or 'Mak'
    with _locked():
        current = read()['text']
        lines = _lines(current)
        at = _by_id(lines, number, need_open=False)
        if _block_version(lines[at:_item_end(lines, at)]) != version:
            raise ProjectError(409, f'B{number} changed since you loaded it. Reload and try again.')
        doing = any(line.lstrip().startswith('- Doing:') for line in lines[at + 1:_item_end(lines, at)])
        finished = ITEM.fullmatch(lines[at])[1] != ' '
        if stage == 'ready':
            lines[at] = re.sub(r'^([-*] \[)[xX](\] )', r'\1 \2', lines[at])
            _drop_status(lines, at)
        elif stage == 'building' and (finished or not doing):
            lines[at] = re.sub(r'^([-*] \[)[xX](\] )', r'\1 \2', lines[at])
            _drop_status(lines, at)
            lines.insert(at + 1, f'  - Doing: {who}')
        elif stage == 'done' and not finished:
            lines[at] = lines[at].replace('[ ]', '[x]', 1)
            _drop_status(lines, at)
            lines.insert(_item_end(lines, at), _done_line('', who))
        text = '\n'.join(lines) + '\n'
        if text != current:
            _replace(current, with_ids(text, current), audit_before=current)
    card = next((c for c in board_items() if c['id'] == f'B{number}'), None)
    return card or {'id': f'B{number}', 'stage': stage, 'gone': True}
