"""One backlog for the whole app, in Markdown, that the person and every agent share.

paths.TETHER/backlog/
  BACKLOG.md   the list. Items at the top are the person's inbox; "## name" starts a project's items.
  .lock        held for every change, by the server and by the agent command (they are separate processes)
  versions/    the last copies, so a bad overwrite can be undone by hand

Codex's sandbox gets write access to this folder only, so it holds nothing else. Agents change the list through
agent_backlog_cli.py. A saved version check catches anything that edits the file another way."""
import contextlib
import fcntl
import hashlib
import os
import re
import time

import paths
from projects import ProjectError, _read, _write, invalid

# A request body is at most 262,144 bytes (projects_api.py). 60,000 characters fit in any language.
LIMIT = 60000
VERSIONS = 50
HEADING = re.compile(r'#{1,6}\s+(.*?)\s*$')
ITEM = re.compile(r'[-*] \[([ xX])\] (.*)$')
SOURCE = re.compile(r'\s*\((?:claude|codex)(?: · [^()·]+)*\)\s*$')


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


def _replace(current, text):
    if len(text) > LIMIT:
        raise invalid(f'The backlog is full ({LIMIT:,} characters). Delete some items first.')
    _keep(current)  # an agent may have edited the file directly since the last copy
    _write(_path(), text)
    _keep(text)
    return _state(text)


def save(text, version):
    """Save only over the version the person last saw."""
    if not isinstance(text, str):
        raise invalid('Invalid backlog text.')
    if not isinstance(version, str):
        raise invalid('Reload the backlog and try again.')
    with _locked():
        current = read()
        if current['version'] != version:
            raise ProjectError(409, 'The backlog changed while you were editing.')
        return _replace(current['text'], text)


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
        _replace(current, '\n'.join(lines) + '\n')
    return result


def add(item, source='', project=''):
    """Add an open item to a project's section, or to the inbox with no project."""
    item = _one_line(item)
    if not item:
        raise invalid('Write the item text.')
    line = f'- [ ] {item}{f" ({source})" if source else ""}'

    def change(lines):
        start, end = _section(lines, project)
        if start is None:
            lines.extend([''] * bool(lines) + [f'## {project}', line])
        else:
            _insert_at_end(lines, start, end, [line])
        return line
    return _change(change)


def _open_items(lines, project):
    spans = [_section(lines, '')] + ([_section(lines, project)] if project else [])
    for start, end in spans:
        for at in range(start or 0, end if start is not None else 0):
            found = ITEM.fullmatch(lines[at])
            if found and found[1] == ' ':
                yield at, SOURCE.sub('', found[2])


def _match(lines, query, project):
    """The one open item that the words name, in the inbox or this project. Never a guess between several."""
    query = _one_line(query).casefold()
    if not query:
        raise invalid('Write part of the item text.')
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
        at = _match(lines, query, project) + 1
        while at < len(lines) and lines[at].strip() and lines[at][0].isspace():
            at += 1
        lines.insert(at, f'  - {text}')
    _change(change)


def mark_done(query, project=''):
    def change(lines):
        at = _match(lines, query, project)
        lines[at] = lines[at].replace('[ ]', '[x]', 1)
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
    lines = _lines(read()['text'])
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
