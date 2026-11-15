"""One backlog for the whole app, in Markdown, that the person and every agent share.

paths.TETHER/backlog/
  BACKLOG.md   the list. Items at the top are the person's inbox; "## name" starts a project's items.
  .lock        held for every change, by the server and by the agent command (they are separate processes)
  versions/    the last copies, so a bad overwrite can be undone by hand
  DONE.md      finished items moved out of BACKLOG.md when it grows near its limit, in file order, under their section

Codex's sandbox gets write access to this folder only, so it holds nothing else. Agents change the list through
agent_backlog_cli.py. A saved version check catches anything that edits the file another way."""
import contextlib
import fcntl
import hashlib
import os
import re
import time
from pathlib import Path

import paths
from projects import ProjectError, _read, _write, invalid

# A backlog save is at most 1 MiB (projects_api.py BACKLOG_BODY_LIMIT). 200,000 characters fit in any language,
# 4 bytes each at most in UTF-8, with room for the JSON around them.
LIMIT = 200000
# Past ARCHIVE_AT characters, finished items move to DONE.md, in file order, until the list is at ARCHIVE_TO or less.
# Agents read the whole list, so it stays short; LIMIT is room for a list with many open items.
ARCHIVE_AT, ARCHIVE_TO = 48000, 36000
VERSIONS = 50
# The patterns below run on every line of a text the browser sends. Each runs in linear time: no match can start
# inside a run of spaces, and nothing lazy meets a trailing \s*. Long runs of spaces took seconds before (15 Nov 2026).
HEADING = re.compile(r'#{1,6}\s+(\S(?:.*\S)?|)\s*$')
ITEM = re.compile(r'[-*] \[([ xX])\] (.*)$')
SOURCE = re.compile(r'(?<!\s)\s*\((?:claude|codex)(?: · [^()·]+)*\)\s*$')
ID = re.compile(r'(?<!\s)\s+#B([0-9]+)\s*$')  # every item ends with its ID, like #B12. The UI gives IDs the same way (backlogItems.ts).
ID_QUERY = re.compile(r'#?[Bb]([0-9]+)')
STATUS = re.compile(r'\s+- (?:Doing|Done):')
WORKTREE = re.compile(r'gitdir:\s*(.+?)/\.git/worktrees/')


def folder():
    return paths.TETHER / 'backlog'


def _path():
    path = folder() / 'BACKLOG.md'
    if folder().is_symlink() or path.is_symlink():
        raise invalid('The backlog is a link. Tether does not open it.')
    return path


def _state(text):
    return {'text': text, 'version': hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest(), 'limit': LIMIT}


def read():
    """The text and a version made from it. A file that grew too long is refused: showing part of it would make the
    next save drop the rest."""
    text = _read(_path(), LIMIT + 1)
    if len(text) > LIMIT:
        raise ProjectError(413, f'The backlog is over {LIMIT:,} characters. Shorten {_path()}.')
    return _state(text)


@contextlib.contextmanager
def _locked():
    folder().mkdir(mode=0o700, parents=True, exist_ok=True)
    _path()
    handle = os.open(folder() / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)  # also releases the lock


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


def _archive(text):
    """Move finished items in file order, with their detail lines, to DONE.md until the text is at ARCHIVE_TO or less.
    The finished item with the highest ID stays, so new IDs (here and in the UI, backlogItems.ts) never repeat an
    archived one. DONE.md is written first: a failed write keeps the items in the list, never loses them."""
    if len(text) <= ARCHIVE_AT:
        return text
    lines = text.split('\n')
    ids = [int(found[1]) for line in lines if ITEM.fullmatch(line) and (found := ID.search(line))]
    keep = max(ids, default=None)
    size, moved, section, drop = len(text), [], 'Inbox', set()
    for at, line in enumerate(lines):
        heading = HEADING.fullmatch(line)
        if heading:
            section = heading[1]
            continue
        found = ITEM.fullmatch(line)
        if not found or found[1] == ' ' or ((number := ID.search(line)) and int(number[1]) == keep):
            continue
        end = _block_end(lines, at)
        moved.append((section, lines[at:end]))
        drop.update(range(at, end))
        size -= sum(len(part) + 1 for part in lines[at:end])
        if size <= ARCHIVE_TO:
            break
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
    head = '' if path.exists() else '# Finished backlog items\n\nMoved here from BACKLOG.md when the list grew near its limit.\n'
    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, 'a', encoding='utf-8') as archive:
        archive.write(head + '\n'.join(out) + '\n')
    return '\n'.join(line for at, line in enumerate(lines) if at not in drop)


def _replace(current, text):
    text = _archive(text)
    if len(text) > LIMIT:
        raise invalid(f'The backlog is full ({LIMIT:,} characters). Delete some items first.')
    _keep(current)  # an agent may have edited the file directly since the last copy
    _write(_path(), text)
    _keep(text)
    return _state(text)


def with_ids(text):
    """Give each item without an ID the next free one, in file order. Text that already has IDs stays the same."""
    lines = text.split('\n')
    taken = [int(found[1]) for line in lines if ITEM.fullmatch(line) and (found := ID.search(line))]
    next_id = max(taken, default=0) + 1
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
    if len(text) > LIMIT:  # before any parsing: IDs only make the text longer
        raise invalid(f'The backlog is full ({LIMIT:,} characters). Delete some items first.')
    with _locked():
        current = read()
        if current['version'] != version:
            raise ProjectError(409, 'The backlog changed while you were editing.')
        return _replace(current['text'], with_ids(text))


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


def _change(change):
    with _locked():
        current = read()['text']
        lines = _lines(current)
        result = change(lines)
        _replace(current, with_ids('\n'.join(lines) + '\n'))
    return result


def add(item, source='', project='', details=()):
    """Add an open item to a project's section, or to the inbox with no project. details: lines to put under it."""
    item = _one_line(item)
    if not item:
        raise invalid('Write the item text.')
    below = [f'  - {text}' for text in map(_one_line, details) if text]
    def change(lines):
        line = with_ids('\n'.join(lines + [f'- [ ] {item}{f" ({source})" if source else ""}'])).split('\n')[-1]
        start, end = _section(lines, project)
        if start is None:
            lines.extend([''] * bool(lines) + [f'## {project}', line, *below])
        else:
            _insert_at_end(lines, start, end, [line, *below])
        return line
    return _change(change)


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
    _change(change)


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
    return _change(change)


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
    """Remove the item's Doing and Done lines. Other details stay."""
    end = _block_end(lines, at)
    lines[at + 1:end] = [line for line in lines[at + 1:end] if not STATUS.match(line)]


def start(query, who, project=''):
    """Mark an open item as in progress, by whom. A second start replaces the first."""
    def change(lines):
        at = _match(lines, query, project)
        _drop_status(lines, at)
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
        if result or who:
            lines.insert(_block_end(lines, at), f'  - Done: {result or "finished"}{f" ({who})" if who else ""}')
        return lines[at]
    return _change(change)


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
