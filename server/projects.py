"""Session projects: context that agents in a tmux session get when they start.

Each tmux session has the session option @project (a random id). The id names a bundle folder,
paths.TETHER/projects/<id>/:
  project.json      name, revision (+1 on each change the person makes), budget, seen, archived_from
  INSTRUCTIONS.md   what the person wrote
  files/            pinned copies; links.json holds pins that point at files that exist elsewhere
  notes/            agent notes that the person reviews; a deleted note goes to notes/.trash/
render() builds the text an agent gets at start, resume, clear and compact. It stays inside the budget. An
empty project gives only the short Notes part, so agents learn where notes go. Rename, cd, split and close do not change the id. A bundle folder exists only after the
first write."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import files
import paths


ID = re.compile(r'[0-9a-f]{12}')
NOTE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,79}\.md')
BUDGET = 4000
BUDGET_MIN, BUDGET_MAX = 1000, 10000  # 10,000 is the limit of a Claude Code hook's output
INSTRUCTIONS_LIMIT = 20000
NOTE_LIMIT = 100000
COPY_LIMIT = 1024 * 1024
PINS_LIMIT = 50
NEWEST = 5
EXCERPT = 600


class ProjectError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def invalid(message):
    return ProjectError(400, message)


def root():
    return paths.TETHER / 'projects'


def bundle(pid):
    if not isinstance(pid, str) or not ID.fullmatch(pid):
        raise invalid('Choose a project.')
    return root() / pid


# ---- the session option ----

def session_project(session_id, run=subprocess.run):
    """The @project id of a tmux session ($N). A session without one gets a new id."""
    if not isinstance(session_id, str) or not re.fullmatch(r'\$[0-9]+', session_id):
        raise invalid('Choose a session.')
    shown = run([paths.TMUX, 'display-message', '-p', '-t', session_id + ':', '#{session_id}\t#{session_name}\t#{@project}'],
                capture_output=True, text=True, timeout=5)
    fields = shown.stdout.rstrip('\n').split('\t')
    if shown.returncode or len(fields) != 3 or fields[0] != session_id:
        raise ProjectError(404, 'That session is gone. Refresh the list and try again.')
    pid = fields[2]
    if not ID.fullmatch(pid):
        pid = secrets.token_hex(6)
        set_ = run([paths.TMUX, 'set-option', '-t', session_id, '@project', pid], capture_output=True, text=True, timeout=5)
        if set_.returncode:
            raise ProjectError(500, set_.stderr.strip() or 'Could not give the session a project.')
    return pid, fields[1]


# ---- files on disk ----

def _write(path, text):
    """Write a file in one step: a reader sees the old file or the new one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    try:
        with open(temp, 'x', encoding='utf-8') as out:
            out.write(text)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path, limit=None):
    try:
        with open(path, encoding='utf-8', errors='replace') as source:
            return source.read(limit) if limit else source.read()
    except FileNotFoundError:
        return ''


def meta(pid):
    data = {}
    try:
        data = json.loads(_read(bundle(pid) / 'project.json') or '{}')
    except ValueError:
        pass
    data = data if isinstance(data, dict) else {}
    budget = data.get('budget')
    return {'name': data.get('name') if isinstance(data.get('name'), str) else '',
            'revision': data.get('revision') if isinstance(data.get('revision'), int) else 0,
            'budget': budget if isinstance(budget, int) and BUDGET_MIN <= budget <= BUDGET_MAX else BUDGET,
            'seen': data.get('seen') if isinstance(data.get('seen'), (int, float)) else 0,
            'archived_from': data.get('archived_from') if isinstance(data.get('archived_from'), list) else []}


def _save_meta(pid, data):
    _write(bundle(pid) / 'project.json', json.dumps(data, indent=2) + '\n')


def _changed(pid, revision=None, **fields):
    """Save fields and count one more revision. A stale revision (another tab saved first) is refused."""
    data = meta(pid)
    if revision is not None and revision != data['revision']:
        raise ProjectError(409, 'The project changed in another tab. Reload it and try again.')
    data.update(fields, revision=data['revision'] + 1)
    _save_meta(pid, data)
    return data


def set_name(pid, name):
    """Keep the name the bundle shows in an archive. Not a revision: agents do not see it."""
    if isinstance(name, str) and name and meta(pid)['name'] != name and bundle(pid).exists():
        data = meta(pid)
        data['name'] = name
        _save_meta(pid, data)


def instructions(pid):
    return _read(bundle(pid) / 'INSTRUCTIONS.md')


def set_instructions(pid, text, revision):
    if not isinstance(text, str) or len(text) > INSTRUCTIONS_LIMIT:
        raise invalid(f'Instructions can have at most {INSTRUCTIONS_LIMIT:,} characters.')
    if not isinstance(revision, int):
        raise invalid('Reload the project and try again.')
    data = _changed(pid, revision)
    _write(bundle(pid) / 'INSTRUCTIONS.md', text.strip() + '\n' if text.strip() else '')
    return data


def set_budget(pid, chars, revision):
    if not isinstance(chars, int) or not BUDGET_MIN <= chars <= BUDGET_MAX:
        raise invalid(f'Choose a size from {BUDGET_MIN:,} to {BUDGET_MAX:,} characters.')
    return _changed(pid, revision, budget=chars)


# ---- pinned files ----

def _links(pid):
    try:
        data = json.loads(_read(bundle(pid) / 'links.json') or '[]')
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get('name'), str) and isinstance(item.get('path'), str)] if isinstance(data, list) else []


def pins(pid, home=None):
    """Every pin: copies in files/ first, then links. A link to a file that is gone says so."""
    home = files.files_root(home)
    folder = bundle(pid) / 'files'
    found = []
    if folder.is_dir():
        for entry in sorted(folder.iterdir()):
            if entry.is_file() and not entry.is_symlink() and not entry.name.startswith('.'):
                found.append({'name': entry.name, 'kind': 'copy', 'path': str(entry), 'size': entry.stat().st_size, 'missing': False})
    for link in _links(pid):
        real = Path(link['path'])
        found.append({'name': link['name'], 'kind': 'link', 'path': files.shown(real, home) if files.inside(real, home) else str(real),
                      'size': real.stat().st_size if real.is_file() else None, 'missing': not real.is_file()})
    return found


def pin(pid, path_text, mode, revision, home=None):
    """Pin a file from the Files explorer: a link to it, or a copy (at most 1 MB). Secret files are refused."""
    if mode not in ('link', 'copy'):
        raise invalid('Choose link or copy.')
    home = files.files_root(home)
    try:
        _, real, is_dir, _ = files.locate(path_text, home)
    except files.FilesError as exc:
        raise ProjectError(exc.status, str(exc)) from exc
    if is_dir:
        raise invalid('Pin a file, not a folder.')
    current = pins(pid, home)
    if len(current) >= PINS_LIMIT:
        raise invalid(f'A project can pin at most {PINS_LIMIT} files.')
    names = {item['name'] for item in current}
    name = real.name
    stem, dot, suffix = name.partition('.')
    count = 2
    while name in names:
        name = f'{stem} {count}{dot}{suffix}'
        count += 1
    if mode == 'link':
        if any(item['kind'] == 'link' and item['path'] == files.shown(real, home) for item in current):
            raise invalid('That file is pinned already.')
        data = _changed(pid, revision)
        _write(bundle(pid) / 'links.json', json.dumps(_links(pid) + [{'name': name, 'path': str(real)}], indent=2) + '\n')
        return data
    fd = files.open_read(home, real)
    with os.fdopen(fd, 'rb') as source:
        body = source.read(COPY_LIMIT + 1)
    if len(body) > COPY_LIMIT:
        raise invalid('A copy can be at most 1 MB. Pin a link instead.')
    data = _changed(pid, revision)
    target = bundle(pid) / 'files' / name
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(target, 'xb') as out:
        out.write(body)
    return data


def unpin(pid, name, revision):
    if not isinstance(name, str) or not name or '/' in name or name.startswith('.'):
        raise invalid('Choose a pinned file.')
    links = _links(pid)
    kept = [item for item in links if item['name'] != name]
    copy = bundle(pid) / 'files' / name
    if len(kept) == len(links) and not copy.is_file():
        raise ProjectError(404, 'That pin is gone.')
    data = _changed(pid, revision)
    if len(kept) != len(links):
        _write(bundle(pid) / 'links.json', json.dumps(kept, indent=2) + '\n')
    else:
        copy.unlink()
    return data


# ---- notes ----

def _title(text, name):
    """A note's title: the frontmatter name or description, else the first heading, else the file name."""
    lines = text.splitlines()
    if lines[:1] == ['---']:
        for line in lines[1:20]:
            if line == '---':
                break
            key, _, value = line.partition(':')
            if key.strip() in ('description', 'name') and value.strip():
                return value.strip()[:120]
    for line in lines[:30]:
        if line.startswith('#'):
            return line.lstrip('#').strip()[:120] or name
    return name


def _note(pid, name):
    if not isinstance(name, str) or not NOTE.fullmatch(name):
        raise invalid('Use a note name that ends in .md, with letters, digits, space, dot, dash or underscore.')
    return bundle(pid) / 'notes' / name


def notes(pid):
    """Every note, newest first. A note is new when it changed after the person last looked."""
    folder = bundle(pid) / 'notes'
    seen = meta(pid)['seen']
    found = []
    if folder.is_dir():
        for entry in folder.iterdir():
            if NOTE.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink():
                st = entry.stat()
                found.append({'name': entry.name, 'title': _title(_read(entry, 4096), entry.name), 'size': st.st_size,
                              'mtime': int(st.st_mtime * 1000), 'new': st.st_mtime > seen})
    return sorted(found, key=lambda item: -item['mtime'])


def read_note(pid, name):
    path = _note(pid, name)
    if not path.is_file() or path.is_symlink():
        raise ProjectError(404, 'That note is gone.')
    return {'name': name, 'text': _read(path, NOTE_LIMIT)}


def write_note(pid, name, text):
    if not isinstance(text, str) or len(text) > NOTE_LIMIT:
        raise invalid(f'A note can have at most {NOTE_LIMIT:,} characters.')
    path = _note(pid, name)
    if path.is_symlink():
        raise invalid('That note is a link. Tether does not change it.')
    _write(path, text)
    return {'name': name}


def delete_note(pid, name):
    """Move the note to notes/.trash/. Nothing is deleted for good."""
    path = _note(pid, name)
    if not path.is_file() or path.is_symlink():
        raise ProjectError(404, 'That note is gone.')
    trash = path.parent / '.trash'
    trash.mkdir(mode=0o700, exist_ok=True)
    os.replace(path, trash / f'{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(2)}-{name}')
    return {'name': name}


def mark_seen(pid):
    if bundle(pid).exists():
        data = meta(pid)
        data['seen'] = time.time()
        _save_meta(pid, data)


# ---- what an agent gets ----

def _fit(parts, budget):
    """Join the parts in order. The first part that does not fit is cut, and the rest are left out."""
    out, used = [], 0
    for part in parts:
        if used + len(part) <= budget:
            out.append(part)
            used += len(part)
            continue
        note = '\n[Cut to fit the project size limit. The full text is in the project folder.]\n'
        room = budget - used - len(note)
        if room > 200:
            out.append(part[:room].rsplit('\n', 1)[0] + note)
        elif budget - used >= len(note):
            out.append(note)
        break
    return ''.join(out)


def _group_parts(group, home):
    """A group's context in three tiers, most important first: (main part, notes list, note excerpts)."""
    gpid, name = group['pid'], group['name']
    text = instructions(gpid).strip()
    pinned = pins(gpid, home)
    found = notes(gpid)
    folder = bundle(gpid)
    main = (f'\n## Stack: {name}\nThis tab is in the stack "{name}" of this tmux session. '
            + ('The user wrote this guidance for agents in the stack. Follow it.\n' if text or pinned else '\n')
            + f'Stack folder: {folder}\n'
            + f'Save a fact that only this stack needs as a Markdown note in {folder}/notes/ (same rules as project notes). '
            'Facts for the whole session go in the project notes folder.\n')
    if text:
        main += f'\n### Stack instructions\n{text}\n'
    if pinned:
        lines = '\n'.join(f'- {item["name"]}: {item["path"]}{" (gone)" if item["missing"] else ""}' for item in pinned)
        main += f'\n### Stack pinned files\nRead a file when the task needs it.\n{lines}\n'
    listing = ('\n### Stack notes\n' + ''.join(f'- {item["name"]}: {item["title"]}\n' for item in found)) if found else ''
    excerpts = []
    for item in found[:NEWEST]:
        body = _read(folder / 'notes' / item['name'], EXCERPT + 1).strip()
        excerpts.append(f'\n#### {item["name"]}\n{body[:EXCERPT]}{" …" if len(body) > EXCERPT else ""}\n')
    return main, listing, excerpts


def render(pid, agent='claude', home=None, group=None, backlog_project=None):
    """The text an agent gets. An empty project still gets the Notes part: without it no agent knows the notes folder,
    so no note is ever written. Claude keeps its own auto memory where it is: Tether does not move it, so Claude's
    notes from before stay in reach. Both agents get the same text. Reading never makes the bundle folder.
    group: {'pid', 'name'} for a tab in a group (groups.py). Its part comes after the session parts of the same
    tier, so _fit cuts note excerpts first, then note lists, and the group's own guidance last.
    backlog_project: the agent's repository name (backlog.project_name). With it, the open backlog items for the inbox
    and that project are listed, cut before the note lists. The project preview has no tab, so it passes none."""
    data = meta(pid)
    text = instructions(pid).strip()
    pinned = pins(pid, home)
    found = notes(pid)
    folder = bundle(pid)
    parts = [f'# Tether project{": " + data["name"] if data["name"] else ""}\n'
             + ('Project guidance from Tether. The user wrote it for every agent in this tmux session. Follow it.\n'
                if text or pinned else 'Agents in this tmux session share this project folder.\n')
             + f'Project folder: {folder}\n']
    if text:
        parts.append(f'\n## Instructions\n{text}\n')
    if pinned:
        lines = '\n'.join(f'- {item["name"]}: {item["path"]}{" (gone)" if item["missing"] else ""}' for item in pinned)
        parts.append(f'\n## Pinned files\nRead a file when the task needs it.\n{lines}\n')
    parts.append('\n## Tab icon\nOnce the task is clear, run `tmux set-option -p -t "$TMUX_PANE" @chat_icon NAME`. NAME: a lucide.dev '
                 f'icon name, or svg-X after you write a 24x24 stroke SVG to {paths.TETHER}/icons/X.svg. Skip it if @chat_icon_by is "user" or "tether".\n')
    parts.append('\n## Naming tabs\nIn replies, write the visible name of a tab in backticks, like `B69 review`: Tether links it. Never write a pane id like %71 unless the user must type it.\n')
    parts.append('\n## Claims of absence\nBefore you say that something does not exist, cannot work, or has no fix, read the primary source for the exact version in use: '
                 'source code, official docs, or the real config. A failed search on guessed names, or a page that did not load, is not evidence of absence. '
                 'If you did not read the primary source, write "not checked", name what you did not read, and keep looking when the answer can change what the user does.\n')
    cli = Path(__file__).resolve().with_name('agent_tab_cli.py')
    parts.append(f'\n## New tabs and reviewers\nUse your own subagents for searches, research and side work. '
                 'A separate task the user asks for here (not a step of yours) goes to a worker tab: a full spec, `--worktree` for code, then `wait`. '
                 'Start other tabs only for a reviewer or advisor that watches the work, or a task the user wants to steer.\n'
                 f'To start a new tab or session, or a reviewer, run `{sys.executable} {cli} start ...`, not raw tmux. '
                 'It opens a tab Tether can track. Options: `--review-it` (this tab reviews each turn of the new tab), `--reviews-me` (the new tab reviews this tab), '
                 '`--agent claude|codex`, `--name NAME`, `--cwd DIR`, `--worktree BRANCH` (new branch and folder; refused with `--reviews-me`), `--context`, `--stack NAME` (made if missing). '
                 '`stop --worker %N [--reviewer %N]` ends a review; `list` shows this tab\'s links. '
                 '`link --worker %N --reviewer %N` makes one open tab review another. '
                 'Start your review reply with APPROVED when nothing is wrong. '
                 '`send --pane %N "text"` (`-` = stdin) steers a tab you started. '
                 '`wait --pane %N [--timeout SECONDS]` (max 600) prints its new reply as JSON. '
                 'Exit: 0 finished, 3 timeout (still working), 4 gone, 5 needs_input (with prompt), 6 no_turn (none in 60 s; message may be lost). '
                 'When a tab you started or review is done, save a note in notes/ (what it did, outcome, findings), '
                 'then run `close --pane %N`. Busy tabs stay open.\n')
    backlog = f'{sys.executable} {Path(__file__).resolve().with_name("agent_backlog_cli.py")}'
    parts.append(f'\n## Backlog\nOne backlog serves all sessions. Change it only with `{backlog}`, never edit its file. '
                 'Commands: `list`, `add "title" --detail "why"`, `detail B12 "text"`, `start B12`, `done B12 "result"`, `move B12 PROJECT`. '
                 '`--project NAME` files elsewhere. `list` shows the inbox and this project\'s open items.\n'
                 '- Add an item, without asking, for real work outside your task over about 5 minutes. No style nits or steps of your task.\n'
                 '- Work on an item only when the user asks. Run `start` first. When finished, run `done` with the commit, PR, or outcome.\n')
    parts.append(f'\n## Notes\nSave a project fact that other agents in this session need as a Markdown file in {folder}/notes/ '
                 '(one topic per file, a "# Title" first line). Keep your own memory as before. The user reviews notes in Tether.\n')
    listing, excerpts, waiting = [], [], []
    if backlog_project is not None:
        import backlog as backlog_store  # backlog imports this module
        items, count = backlog_store.summary(backlog_project)
        if items:
            shown = ''.join(f'- {item[:100]}\n' for item in items)
            more = f'{count - len(items)} more: run `list`.\n' if count > len(items) else ''
            waiting.append(f'\n### Open backlog items ({count})\n{shown}{more}')
    if found:
        listing.append(''.join(f'- {item["name"]}: {item["title"]}\n' for item in found))
        for item in found[:NEWEST]:
            body = _read(folder / 'notes' / item['name'], EXCERPT + 1).strip()
            excerpts.append(f'\n### {item["name"]}\n{body[:EXCERPT]}{" …" if len(body) > EXCERPT else ""}\n')
    if group:
        main, group_listing, group_excerpts = _group_parts(group, home)
        parts.append(main)
        listing = ['\n### Project notes\n' + listing[0]] if listing else []  # not under the group heading
        listing += [group_listing] if group_listing else []
        excerpts += group_excerpts
    return _fit(parts + listing + waiting + excerpts, data['budget'])


def state(pid, home=None):
    """What the Project sheet shows."""
    data = meta(pid)
    preview = render(pid, 'claude', home)
    return {'id': pid, 'name': data['name'], 'revision': data['revision'], 'budget': data['budget'],
            'budget_min': BUDGET_MIN, 'budget_max': BUDGET_MAX, 'instructions': instructions(pid),
            'pins': pins(pid, home), 'notes': notes(pid), 'preview': preview, 'chars': len(preview),
            'folder': str(bundle(pid))}


def has_context(pid, home=None):
    """True when the person gave agents in the session something: instructions, a pin or a note.
    The Notes part that every project sends does not count."""
    return bundle(pid).is_dir() and bool(instructions(pid).strip() or pins(pid, home) or notes(pid))


def marks(run=subprocess.run, home=None):
    """For the sidebar: {session id: {project, context, revision, stale: [pane ids]}}. Read only: a session with no
    @project yet is left out. A pane is stale when its agent started with an older revision or another project
    (@project_rev, set by project_launch.py). A pane with no @project_rev did not start through Tether.
    A pane in a group (groups.py) started with "pid:rev+gpid:grev"; it is stale when either part changed."""
    import groups  # groups imports this module
    shown = run([paths.TMUX, 'list-panes', '-a', '-f', '#{==:#{@zoom_for},}', '-F',
                 '#{session_id}\t#{@project}\t#{pane_id}\t#{@project_rev}\t#{@group}\t#{@groups}'],
                capture_output=True, text=True, timeout=5)
    if shown.returncode:
        return {}
    out, revisions = {}, {}
    for line in shown.stdout.splitlines():
        fields = line.split('\t', 5)
        if len(fields) not in (4, 6) or not ID.fullmatch(fields[1]):
            continue
        session_id, pid, pane, started = fields[:4]
        label, raw = fields[4:] if len(fields) == 6 else ('', '')
        if session_id not in out:
            data = meta(pid)
            out[session_id] = {'project': pid, 'revision': data['revision'], 'stale': [], 'context': has_context(pid, home)}
        mark = out[session_id]
        expected = f'{pid}:{mark["revision"]}'
        gpid = next((g['project'] for g in groups.parse(raw) if g['id'] == label), '') if label else ''
        if gpid:
            if gpid not in revisions:
                revisions[gpid] = meta(gpid)['revision']
            expected += f'+{gpid}:{revisions[gpid]}'
        if started and started != expected:
            mark['stale'].append(pane)
    return out


def archive(pid):
    """Move a bundle to projects/.archive/ (merge keeps the other one). It can be moved back."""
    folder = bundle(pid)
    if folder.exists():
        target = root() / '.archive' / f'{pid}-{time.strftime("%Y%m%d-%H%M%S")}'
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(folder), str(target))
