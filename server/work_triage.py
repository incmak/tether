"""The triage ledger on the work board: `tether triage` keeps a project's triage log as work item events.

A project that triages bug reports in tabs (one tab per report) used to keep a ledger file, one line per event:
`- <time> | T-NNN | <status> | <text> [| ref: <where>]`. Now the items are the record. Each ledger line is a
'triage' event with its status, text and ref; some statuses move the item (STAGE_OF). The ledger file becomes a
view: Tether writes it again after every command, from the items, the lines before the import (kept unchanged in
the archive file) and the seen marks.

Ids: an import keeps the old ids of open items and reserves the highest old id, so a new item never takes an old
number. A closed item from before the import comes in from the archive when a command names it again.

needs-mak asks Mak in Needs you (default answers: Go ahead, Hold). A question on an item that already waits on Mak
replaces the old one without a second push. The import makes no push."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import paths
import work_items
from projects import _read, _write

STATUSES = ('handed', 'triaging', 'needs-mak', 'ticket', 'fixing', 'fixed', 'closed', 'duplicate', 'wontfix')
DONE = ('closed', 'duplicate', 'wontfix')
STAGE_OF = {'fixing': 'building', 'fixed': 'review', 'closed': 'done', 'duplicate': 'done', 'wontfix': 'done'}
LABEL = {'handed': 'new', 'triaging': 'triaging', 'needs-mak': 'needs decision', 'ticket': 'ticket',
         'fixing': 'fixing', 'fixed': 'fixed, review', 'closed': 'done', 'duplicate': 'done', 'wontfix': 'done'}
OPTIONS = ['Go ahead', 'Hold']
LINE = re.compile(r'^- (\d{4}-\d\d-\d\d \d\d:\d\d) \| ([A-Z]{1,3}-\d+|seen) \| (\S+) \| (.*)$')
NAME = re.compile(r'[a-z0-9][a-z0-9-]{1,29}')
SEEN_KEEP = 500
ARCHIVE = 'TRIAGE-before-board.md'
TAB_EVENTS = ('link', 'unlink')
ROW_TYPES = ('new', 'triage', 'ask')  # the events an imported ledger line becomes; links and reserves are not lines
HEADER = """# Triage ledger

Tether writes this file from the work board items after each `triage-log` command. Do not edit it: your lines
would be lost. One line per event: `- <time> | <id> | <status> | <text> [| ref: <where>]`. Status `tether` marks
a change made on the board, like Mak's answer. Lines from before the board are also kept in {archive}.

"""


class TriageError(Exception):
    pass


def _sha(text):
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def stamp(at):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(at))


def parse_time(text):
    return time.mktime(time.strptime(text, '%Y-%m-%d %H:%M'))


def one_line(text):
    return ' '.join(str(text or '').split())


def split_ref(text):
    """'what | ref: where' -> ('what', 'where')."""
    head, sep, ref = text.rpartition(' | ref: ')
    return (head, ref.strip()) if sep and ref.strip() else (text, '')


def parse(text):
    """Ledger rows: (time, id, status, text, ref, key), in file order. Lines that are not rows are skipped. key
    sorts the rows: the minute, plus a millisecond per line before it, so rows of one minute keep the file order
    (and an import gives each event that time)."""
    out = []
    for line in text.splitlines():
        found = LINE.match(line.rstrip('\n'))
        if found:
            when, item, status, rest = found.groups()
            body, ref = split_ref(rest) if item != 'seen' else (rest, '')
            out.append((when, item, status, body, ref, parse_time(when) + min(len(out), 59999) / 1000))
    return out


def row_line(row):
    when, item, status, text, ref = row[:5]
    return f'- {when} | {item} | {status} | {text}' + (f' | ref: {ref}' if ref else '')


class Ledger:
    """One project's ledger: its items, the archive next to the view file, and the seen marks."""

    def __init__(self, project, view):
        self.record = work_items.project(project)
        self.prefix = self.record['prefix']
        self.view = Path(view)
        self.archive = self.view.with_name(ARCHIVE)

    def _marker(self):
        return work_items.folder() / self.prefix / 'triage-import.json'

    def _mark(self):
        try:
            data = json.loads(_read(self._marker()) or 'null')
        except ValueError:
            data = None
        return data if isinstance(data, dict) and isinstance(data.get('sha'), str) else None

    def _set_mark(self, sha):
        _write(self._marker(), json.dumps({'view': str(self.view), 'sha': sha}) + '\n')

    def state(self):
        """'ready' once an import finished (or there was never a ledger), 'old' while the view file is still the old
        ledger, 'partial' when an import stopped after it moved the ledger to the archive."""
        if self._mark() is not None:
            return 'ready'
        if self.archive.exists():
            return 'partial'
        return 'old' if self.view.exists() else 'ready'

    def imported(self):
        return self.state() == 'ready'

    # ---- reading ----

    def items(self):
        return work_items.fold(self.record)

    def archived(self):
        return parse(_read(self.archive)) if self.archive.exists() else []

    def _seen_path(self):
        return work_items.folder() / self.prefix / 'triage-seen.json'

    def seen_rows(self):
        try:
            data = json.loads(_read(self._seen_path()) or '[]')
        except ValueError:
            data = []
        return [(r['at'], 'seen', 'chat', r['text'], '', r['ts']) for r in data
                if isinstance(r, dict) and isinstance(r.get('at'), str) and isinstance(r.get('text'), str)
                and isinstance(r.get('ts'), (int, float)) and not isinstance(r.get('ts'), bool)]

    def item_rows(self, item_id, events=None, items=None):
        """The ledger rows of one item, from its events: triage lines as they were written, other changes as
        `tether` lines (the history text). Tab links are bookkeeping, not steps: they make no line, so `open` still
        shows each item's last step."""
        rows = []
        events = work_items._events(self.prefix) if events is None else events
        items = self.items() if items is None else items
        if item_id not in items:
            return rows
        made = False
        for event in events:
            if event['item'] != item_id:
                continue
            if event['type'] == 'new':
                if made:
                    continue
                made = True
            elif not made:
                continue
            at = event.get('at') or 0
            when = stamp(at)
            if event.get('status') and event['type'] in ('new', 'triage', 'ask'):
                text = event.get('q') if event['type'] == 'ask' else event.get('text', '')
                rows.append((when, item_id, event['status'], text or '', event.get('ref', ''), at))
                continue
            if event['type'] in TAB_EVENTS:
                continue
            text = work_items._history_text(event)
            if text:
                rows.append((when, item_id, 'tether', one_line(text), event.get('ref', ''), at))
        return rows

    def rows(self):
        """Every row: archive rows of items not on the board, seen marks, and each item's rows, by time."""
        items = self.items()
        events = work_items._events(self.prefix)
        out = [r for r in self.archived() if r[1] == 'seen' or r[1] not in items]
        out += self.seen_rows()
        for item_id in items:
            out += self.item_rows(item_id, events, items)
        return sorted(out, key=lambda r: r[5])

    def seen_mark(self, rows=None):
        marks = [r[3] for r in (self.rows() if rows is None else rows) if r[1] == 'seen']
        return marks[-1] if marks else 'never'

    def open_rows(self, rows=None):
        """The newest row of each open item (not Done), in id order."""
        items = self.items()
        last = {}
        for row in self.rows() if rows is None else rows:
            if row[1] in items and items[row[1]]['stage'] != 'done':
                last[row[1]] = row
        return [last[i] for i in sorted(last, key=lambda i: int(i.split('-')[1]))]

    # ---- the view ----

    def write_view(self):
        """Write the view file in one step. Returns an error text, or ''. Before the import, the file is the old
        ledger itself: it is never written over."""
        if not self.imported():
            return ''
        with work_items.locked():
            mark = self._mark()
            if mark is not None and self._foreign(mark):
                return (f'{self.view.name} has lines that Tether did not write (an old triage-log still running?). '
                        f'Tether left the file as it is. Bring the lines in with: tether triage --project '
                        f'{self.record["name"]} --view {self.view} import')
            rows = self.rows()
            body = HEADER.format(archive=ARCHIVE) + ''.join(row_line(r) + '\n' for r in rows)
            try:
                _write(self.view, body)
                self._set_mark(_sha(body))
            except OSError as exc:
                return f'Tether could not write {self.view.name} ({exc.strerror or exc}). The items are saved.'
        return ''

    def _foreign(self, mark):
        """True when the view file is not the one Tether wrote last."""
        if not self.view.exists():
            return False
        return not mark['sha'] or _sha(_read(self.view)) != mark['sha']

    # ---- writing ----

    def _ensure(self, item_id):
        """The item, from the board, or from the archive for an old closed item. TriageError when unknown."""
        item_id = item_id.strip().upper()
        if not re.fullmatch(rf'{self.prefix}-\d+', item_id):
            raise TriageError(f'An id looks like {self.prefix}-001.')
        items = self.items()
        if item_id in items:
            return items[item_id]
        rows = [r for r in self.archived() if r[1] == item_id]
        if not rows:
            raise TriageError(f'No item {item_id}.')
        with work_items.locked():
            self._import_item(item_id, rows)
        return self.items()[item_id]

    def new(self, name, msgs, window='', note=''):
        if not NAME.fullmatch(name or ''):
            raise TriageError('The name is 2 to 30 characters: a-z, 0-9 and -.')
        msgs = one_line(msgs)
        if not msgs:
            raise TriageError('Give --msgs: the Chat message ids.')
        text = f'{name}; msgs {msgs}' + (f'; window {one_line(window)}' if window else '') + \
            (f'; {one_line(note)}' if note else '')
        with work_items.locked():
            items = self.items()
            item_id = work_items._next_id(self.prefix, items)
            work_items._append(self.prefix, {'item': item_id, 'type': 'new', 'at': time.time(), 'by': 'agent',
                                             'who': '', 'title': name, 'refs': [], 'source': f'Chat msgs {msgs}'[:300],
                                             'stage': 'triage', 'qa': False, 'status': 'handed',
                                             'text': work_items._text(text, work_items.TEXT_LIMIT, 'text')})
        return item_id

    def add(self, item_id, status, text, ref='', options=None, pane='', chat='', who=''):
        """Record one step. Returns the item. needs-mak asks Mak; other statuses may move the item (stage_for)."""
        if status not in STATUSES:
            raise TriageError('Status is one of: ' + ' '.join(STATUSES) + '.')
        text = one_line(text)
        if not text:
            raise TriageError('Give the text: one short line.')
        ref = one_line(ref)
        item = self._ensure(item_id)
        if status == 'needs-mak':
            if item['stage'] == 'done':
                work_items.stage(item['id'], 'triage', 'agent', who, 'Asked again after Done')
            quiet = item['stage'] == 'needs'
            return work_items.ask(item['id'], text, options or OPTIONS, pane, chat, who=who, quiet=quiet,
                                  status=status, ref=ref)
        event = {'type': 'triage', 'by': 'agent', 'who': who, 'status': status,
                 'text': work_items._text(text, work_items.TEXT_LIMIT, 'text')}
        if ref:
            event['ref'] = work_items._line(ref, work_items.REF_LIMIT, 'link')

        def check(current):
            to = stage_for(current, status)
            if to:
                event['stage'] = to
        return work_items._write_event(item['id'], None, event, check)

    def seen(self, mark):
        mark = one_line(mark)
        if not mark:
            raise TriageError('Give the time the Chat feed was checked up to.')
        now = time.time()
        row = {'at': stamp(now), 'text': mark[:200], 'ts': now}
        with work_items.locked():
            data = [{'at': r[0], 'text': r[3], 'ts': r[5]} for r in self.seen_rows()]
            data = (data + [row])[-SEEN_KEEP:]
            _write(self._seen_path(), json.dumps(data) + '\n')
        return (row['at'], 'seen', 'chat', row['text'], '', now)

    # ---- import ----

    def _import_item(self, item_id, rows, start=0):
        """Write archive rows[start:] of one item as events, at their own times. Call under the lock. A needs-mak
        last row becomes a quiet question."""
        for index, (_, _, status, text, ref, at) in enumerate(rows[start:], start):
            text = work_items._text(text or '(no text)', work_items.TEXT_LIMIT, 'text')
            ref = ref[:work_items.REF_LIMIT]
            if index == 0:
                name = text.split(';', 1)[0].strip()[:work_items.TITLE_LIMIT] or item_id
                event = {'item': item_id, 'type': 'new', 'at': at, 'by': 'import', 'who': '', 'title': name,
                         'refs': [], 'source': 'Triage ledger', 'stage': 'triage', 'qa': False,
                         'status': status, 'text': text}
            else:
                event = {'item': item_id, 'type': 'triage', 'at': at, 'by': 'import', 'who': '', 'status': status,
                         'text': text}
                current = work_items.fold(self.record)[item_id]
                to = stage_for(current, status if status != 'needs-mak' else 'ticket')
                if to:
                    event['stage'] = to
            if ref:
                event['ref'] = ref
            last = index == len(rows) - 1
            if last and status == 'needs-mak' and index > 0:
                event = {'item': item_id, 'type': 'ask', 'at': at, 'by': 'import', 'who': '',
                         'ask': os.urandom(8).hex(), 'q': text, 'options': list(OPTIONS), 'pane': '', 'chat': '',
                         'quiet': True, 'status': status, **({'ref': ref} if ref else {})}
            work_items._append(self.prefix, event)

    def import_ledger(self):
        """Move the ledger file to the archive (once) and bring its open items onto the board, with their ids,
        times and lines. Safe to run again: it adds only rows that came after the last run, and lines that an old
        triage-log wrote into the view after the import. Returns a report.

        The item lock is held from the first read to the end, and the marker that lets other commands write comes
        last: no command can take an id between the move and the reservation. An id on the board that did not come
        from the ledger stops the import before it changes anything."""
        with work_items.locked():
            state = self.state()
            source = self.view if state == 'old' else self.archive
            if not source.exists():
                raise TriageError(f'No ledger at {self.view}.')
            with open(source, 'a+', encoding='utf-8') as handle:  # the old script's lock: no line is half written
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0)
                rows = parse(handle.read())
                late = self._late_rows(rows) if state == 'ready' else []
                rows += late
                by_item = {}
                for row in rows:
                    if row[1] != 'seen':
                        by_item.setdefault(row[1], []).append(row)
                items = self.items()
                events = work_items._events(self.prefix)
                ours = {e['item'] for e in events if e.get('by') == 'import' and e['type'] == 'new'}
                taken = sorted(i for i in by_item if i in items and i not in ours)
                if taken:
                    raise TriageError(f'{", ".join(taken)} on the board did not come from the ledger. Tether stopped '
                                      'the import and changed nothing. Move those items to new ids first.')
                top = max((int(i.split('-')[1]) for i in by_item if i.startswith(self.prefix + '-')), default=0)
                if top and int(work_items._next_id(self.prefix, items).split('-')[1]) <= top:
                    # first, before any other change: a board write after a stopped import still skips old ids
                    work_items._append(self.prefix, {'item': f'{self.prefix}-{top:03d}', 'type': 'reserve',
                                                     'at': time.time(), 'by': 'import'})
                late_items = [r for r in late if r[1] != 'seen']  # seen marks go to the seen list, not the archive
                if late_items:
                    handle.seek(0, os.SEEK_END)
                    handle.write(''.join(row_line(r) + '\n' for r in late_items))
                    handle.flush()
                if state == 'old':
                    os.rename(self.view, self.archive)  # a writer still waiting on the lock appends to the archive
            made, added = [], 0
            for item_id, item_rows in by_item.items():
                if not item_id.startswith(self.prefix + '-'):
                    continue
                if item_id in items:
                    done = sum(1 for e in events if e['item'] == item_id and e.get('by') == 'import'
                               and e['type'] in ROW_TYPES)
                    if done < len(item_rows):
                        self._import_item(item_id, item_rows, done)
                        added += len(item_rows) - done
                    continue
                if item_rows[-1][2] in DONE:
                    continue
                self._import_item(item_id, item_rows)
                made.append(item_id)
            if any(r[1] == 'seen' for r in late):
                self._seen_late([r for r in late if r[1] == 'seen'])
            if state != 'ready' or not self.view.exists():
                self._set_mark('')  # the view is gone: the next command writes it
            else:
                # every line of the view is now in the items, the archive or the seen list (also after a view
                # write whose marker write failed): the view may be written again
                self._set_mark(_sha(_read(self.view)))
        return {'made': made, 'added': added, 'rows': len(rows), 'late': len(late)}

    def _late_rows(self, archived):
        """Lines in the view that Tether did not write and that are not in the archive yet: an old triage-log that
        was still running during the switch appended them."""
        mark = self._mark()
        if mark is None or not self._foreign(mark):
            return []
        known = {row_line(r) for r in self.rows()} | {row_line(r) for r in archived}
        return [r for r in parse(_read(self.view)) if row_line(r) not in known]

    def _seen_late(self, rows):
        data = [{'at': r[0], 'text': r[3], 'ts': r[5]} for r in self.seen_rows()]
        last = max((r['ts'] for r in data), default=0)
        data += [{'at': r[0], 'text': r[3], 'ts': r[5]} for r in rows if r[5] > last]
        _write(self._seen_path(), json.dumps(sorted(data, key=lambda r: r['ts'])[-SEEN_KEEP:]) + '\n')


def stage_for(item, status):
    """Where a triage line moves the item, or None. fixing, fixed and the done statuses move it (STAGE_OF);
    handed, triaging and ticket move nothing, except that they reopen a Done or On hold item, and return an item
    that waits on Mak to where it was (the step after needs-mak means Mak decided)."""
    stage = item['stage']
    to = STAGE_OF.get(status)
    if to:
        return to if to != stage else None
    if stage in ('done', 'hold'):
        return 'triage'
    if stage == 'needs':
        back = item.get('askFrom') or 'triage'
        return back if back != 'needs' else 'triage'
    return None


# ---- tabs ----

def _tmux(*args):
    try:
        shown = subprocess.run([paths.TMUX, *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return shown.stdout.rstrip('\n') if shown.returncode == 0 else None


def triage_pane(item):
    tabs = [t for t in item.get('tabs') or [] if t.get('role') == 'triage']
    return tabs[-1]['pane'] if tabs else ''


def show_status(item, status, here=''):
    """Name the item's triage tab "<name> · <label>"; close its window when the item is done, unless the command
    runs in that window. Only the linked triage pane counts, and only while its window still has the item's name:
    a pane id that tmux gave to another window later is left alone, and so is a window the pane left. Returns a line for the user, or ''."""
    pane = triage_pane(item)
    if not pane:
        return ''
    shown = _tmux('display-message', '-p', '-t', pane, '#{window_id}\t#{window_name}\t#{session_name}')
    if not shown or shown.count('\t') != 2:
        return ''
    window, name, session = shown.split('\t')
    base = item['title']
    if name != base and not name.startswith(base + ' · '):
        return ''
    label = f'{base} · {LABEL[status]}'
    panes = (_tmux('list-panes', '-t', window, '-F', '#{pane_id}') or '').split()
    if pane not in panes:  # the pane moved between the two looks: this window is not the item's any more
        return ''
    now = str(int(time.time() * 1000))
    for each in panes:
        _tmux('set-option', '-p', '-t', each, '@chat_name', label)
        _tmux('set-option', '-p', '-t', each, '@chat_name_at', now)
    _tmux('rename-window', '-t', window, label)
    if status in DONE and here not in panes:
        again = _tmux('display-message', '-p', '-t', pane, '#{window_id}\t#{window_name}')
        if again != f'{window}\t{label}':  # check once more right before the kill: the pane, its window, the name
            return f'window {session}:{base} -> {label} (left open: it changed while Tether named it)'
        _tmux('kill-window', '-t', window)
        return f'closed window {session}:{base}'
    return f'window {session}:{base} -> {label}'


def link_window(item, session):
    """At import: link the one window named after the item in the triage session as its triage tab."""
    if triage_pane(item):
        return ''
    shown = _tmux('list-windows', '-t', session, '-F', '#{window_name}\t#{pane_id}')
    if not shown:
        return ''
    base = item['title']
    found = [line.split('\t')[1] for line in shown.splitlines() if line.count('\t') == 1
             and (line.split('\t')[0] == base or line.split('\t')[0].startswith(base + ' · '))]
    return found[0] if len(found) == 1 else ''
