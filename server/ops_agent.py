"""The daily ops report (B212): once a day, Tether writes yesterday's facts to a postmortem note and starts one
cheap agent tab per project to say what to learn from them.

Off by default: the tab spends tokens every day, so each person turns it on (Settings › Agents › Daily ops report).

What one run does, for each open session that has a project folder and work from yesterday:
1. Tether itself reads the facts. No model is involved:
   - the commits of that day in the repositories the session's tabs work in, and their "Verified:" stamps;
   - the DEPLOYED.md lines of that day in those repositories, and their stamps;
   - the reviews that Tether paused at the round limit that day (the server records them in events_path());
   - the commits that revert another commit.
2. Tether writes them to <project folder>/notes/postmortems/<day>.md.
3. When the facts show a problem (work with no verification, a paused review or a revert), it starts a Claude tab
   with MODEL at EFFORT effort. The tab reads the evidence, fills the note's "What to learn" section and proposes
   lesson candidates with `agent_lessons_cli.py propose`. A candidate is not a lesson: the person adds or dismisses
   it (lesson_candidates.py). A day with no problem gets the note and no tab, so it costs nothing.

A repository belongs to one project a day: the session with the most agent tabs in it. At most TABS tabs start a day.
State: paths.CACHE/ops-agent.json. Setting: paths.CONFIG/ops-agent.json.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import delegate
import factory
import lesson_candidates
import paths
import projects

HOUR = 7             # local time: the run starts at the first monitor tick after this hour
MODEL = 'haiku'
EFFORT = 'low'
MODEL_LABEL = 'Claude Haiku, low effort'
TABS = 3
RUNS = 30            # run records kept
LAUNCH_DAYS = 60     # days of launch records kept (state 'launched'), apart from the run records
LIST = 30            # rows of one kind in a note
EVENTS_SIZE = 512 * 1024
GIT_TIMEOUT = 20
PLACEHOLDER = '_The ops tab has not written this section yet._'
QUIET = '_Nothing went wrong in the facts above, so no ops tab ran._'
DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
PAUSE_TITLES = ('Review paused', 'Review needs you')
ITEM = r'[a-z][a-z-]*\([^()\n]+\)'
STAMP = re.compile(rf'none(?: \([^()\n]+\))?|{ITEM}(?: +{ITEM})*')  # the same rule as scripts/verification.py
FOUND = re.compile(r'\bVerified:[ \t]*([^\n]*)')
REVERTS = re.compile(r'This reverts commit ([0-9a-f]{7,40})')
_LOCK = threading.RLock()


def settings_path():
    return paths.CONFIG / 'ops-agent.json'


def state_path():
    return paths.CACHE / 'ops-agent.json'


def events_path():
    return paths.CACHE / 'ops-events.jsonl'


def _write(file, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(f'.{file.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(json.dumps(data) + '\n', encoding='utf-8')
        os.replace(temp, file)
    finally:
        temp.unlink(missing_ok=True)


def enabled():
    try:
        data = json.loads(settings_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get('enabled') is True


def set_enabled(value):
    if type(value) is not bool:
        raise ValueError('Choose whether the daily ops report is on.')
    with _LOCK:
        _write(settings_path(), {'enabled': value})


def load(strict=False):
    """The state. A missing file is an empty state. strict: a file that is there but cannot be read or is not valid
    raises (OSError, ValueError), so nothing that depends on the launch records runs on a guess."""
    empty = {'version': 1, 'last_day': '', 'request': False, 'runs': [], 'launched': {}}
    try:
        data = json.loads(state_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        if strict:
            raise
        return empty
    if (not isinstance(data, dict) or not isinstance(data.get('last_day'), str)
            or not isinstance(data.get('runs'), list) or not all(isinstance(row, dict) for row in data['runs'])):
        if strict:
            raise ValueError(f'The ops report state file is invalid: {state_path()}. Fix or remove it.')
        return empty
    launched = data.get('launched') if isinstance(data.get('launched'), dict) else {}
    launched = {day: [pid for pid in pids if isinstance(pid, str)] for day, pids in launched.items()
                if isinstance(day, str) and DAY.fullmatch(day) and isinstance(pids, list)}
    return {'version': 1, 'last_day': data['last_day'], 'request': data.get('request') is True, 'runs': data['runs'],
            'launched': launched}


def _change(work):
    with _LOCK:
        data = load(strict=True)
        work(data)
        data['runs'] = data['runs'][-RUNS:]
        _write(state_path(), data)
        return data


def reserve(day, pid):
    """Record that a project gets its ops tab for a day, before the tab starts. '' when it may start; else the
    reason. One tab per project and day, TABS per day, across every run of that day: the record is in the state
    file, apart from the run list, so "Run now" and a restart cannot start more."""
    result = []

    def work(data):
        taken = data['launched'].setdefault(day, [])
        if pid in taken:
            result.append('This project already got its ops tab for that day.')
        elif len(taken) >= TABS:
            result.append(f'Tether starts at most {TABS} ops tabs for a day.')
        else:
            taken.append(pid)
        for old in sorted(data['launched'])[:-LAUNCH_DAYS]:
            del data['launched'][old]
    _change(work)
    return result[0] if result else ''


def release(day, pid):
    """Give a reservation back. Only for a launch that failed before Tether asked tmux for the tab."""
    _change(lambda data: data['launched'].get(day, []).remove(pid) if pid in data['launched'].get(day, []) else None)


def request():
    """Ask for one run now (Settings › Run now). The monitor loop starts it within a few seconds."""
    _change(lambda data: data.update(request=True))


# ---- the review log ----

def record_review(title, body, pane, entries=(), now=None):
    """Called when Tether pauses a review at the round limit. One JSON line; the file is cut when it grows large."""
    if title not in PAUSE_TITLES:
        return
    entry = next((item for item in entries if item.get('id') == pane), {})
    line = json.dumps({'at': time.time() if now is None else now, 'type': 'review-paused', 'pane': str(pane)[:12],
                       'session': str(entry.get('tmux_session') or '')[:12],
                       'limit': title == 'Review needs you', 'text': ' '.join(str(body).split())[:600]})
    file = events_path()
    with _LOCK:
        file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if file.stat().st_size > EVENTS_SIZE:
                kept = file.read_text(encoding='utf-8', errors='replace').splitlines()[-500:]
                file.write_text('\n'.join(kept) + '\n', encoding='utf-8')
        except OSError:
            pass
        with open(file, 'a', encoding='utf-8') as out:
            out.write(line + '\n')


def reviews(start, end):
    """The paused-review records with start <= at < end."""
    try:
        lines = events_path().read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get('type') == 'review-paused' and type(row.get('at')) in (int, float)
                and start <= row['at'] < end and isinstance(row.get('text'), str)):
            found.append(row)
    return found


# ---- the facts ----

def stamp(text):
    """('verified' | 'none' | 'missing', the stamp text) of a commit message or a DEPLOYED.md line."""
    found = FOUND.findall(text or '')
    if not found:
        return 'missing', ''
    value = re.sub(r'\s+', ' ', found[-1]).strip()
    if not STAMP.fullmatch(value):
        return 'missing', ''
    return ('none' if value.startswith('none') else 'verified'), value


def day_bounds(day):
    """(start, end) in epoch seconds of a local calendar day 'YYYY-MM-DD'."""
    start = datetime.strptime(day, '%Y-%m-%d')
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def commits(root, start, end, run=subprocess.run):
    """The non-merge commits of every branch with start <= commit time < end, oldest first."""
    since = datetime.fromtimestamp(start - 1).astimezone().isoformat()
    until = datetime.fromtimestamp(end).astimezone().isoformat()
    try:
        done = run(['git', '-C', root, 'log', '--all', '--no-merges', f'--since={since}', f'--until={until}',
                    '--format=%h%x1f%ct%x1f%s%x1f%B%x1e'], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode:
        return []
    found = []
    for record in done.stdout.split('\x1e'):
        parts = record.strip('\n').split('\x1f')
        if len(parts) != 4 or not parts[1].isdigit() or not start <= int(parts[1]) < end:
            continue
        kind, text = stamp(parts[3])
        reverted = REVERTS.search(parts[3])
        found.append({'hash': parts[0], 'at': int(parts[1]), 'subject': parts[2][:200], 'stamp': kind, 'stamp_text': text[:200],
                      'reverts': reverted[1][:12] if reverted else '', 'revert': bool(reverted) or parts[2].startswith('Revert '),
                      'repo': Path(root).name})
    return sorted(found, key=lambda item: item['at'])


def deploys(root, start, end):
    """The DEPLOYED.md lines of a repository's main checkout with start <= time < end."""
    try:
        lines = (Path(root) / 'DEPLOYED.md').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parsed = factory.parse_deployed_line(line)
        if parsed and start <= parsed['at'] < end:
            kind, text = stamp(line)
            found.append({'at': parsed['at'], 'commit': parsed['commit'] or '', 'stamp': kind, 'stamp_text': text[:200],
                          'text': FOUND.split(parsed['text'])[0].rstrip(' ·')[:200], 'repo': Path(root).name})
    return found


def gather(day, roots, paused, run=subprocess.run):
    """The facts of one day for one project: {'day', 'commits', 'deploys', 'paused', 'reverts', 'problems'}."""
    start, end = day_bounds(day)
    found_commits = [item for root in roots for item in commits(root, start, end, run)]
    found_deploys = [item for root in roots for item in deploys(root, start, end)]
    facts = {'day': day, 'commits': found_commits, 'deploys': found_deploys, 'paused': paused,
             'reverts': [item for item in found_commits if item['revert']]}
    facts['problems'] = (sum(item['stamp'] != 'verified' for item in found_commits + found_deploys)
                         + len(paused) + len(facts['reverts']))
    return facts


def _count(rows):
    parts = [f'{sum(row["stamp"] == kind for row in rows)} {label}'
             for kind, label in (('verified', 'verified'), ('none', '"Verified: none"'), ('missing', 'no stamp'))]
    return f'{len(rows)} ({", ".join(parts)})' if rows else '0'


def _rows(rows, line):
    shown = [line(row) for row in rows[:LIST]]
    if len(rows) > LIST:
        shown.append(f'- And {len(rows) - LIST} more.')
    return shown or ['- None.']


def _clock(at):
    return datetime.fromtimestamp(at).strftime('%H:%M')


def _why(row):
    return f'Verified: {row["stamp_text"]}' if row['stamp'] == 'none' else 'no Verified stamp'


def note_text(facts, name, learn=PLACEHOLDER):
    """The postmortem note. Tether writes every section but "What to learn"."""
    unverified_commits = [row for row in facts['commits'] if row['stamp'] != 'verified']
    unverified_deploys = [row for row in facts['deploys'] if row['stamp'] != 'verified']
    lines = [f'# Postmortem {facts["day"]}: {name}', '',
             'Tether wrote the facts from git, DEPLOYED.md and its review log. '
             f'An ops tab ({MODEL_LABEL}) writes "What to learn".', '',
             '## Numbers',
             f'- Commits: {_count(facts["commits"])}',
             f'- Deploys: {_count(facts["deploys"])}',
             f'- Reviews paused at the round limit: {len(facts["paused"])}',
             f'- Reverts: {len(facts["reverts"])}', '',
             '## Commits without verification',
             *_rows(unverified_commits, lambda row: f'- `{row["hash"]}` {_clock(row["at"])} {row["subject"]} ({_why(row)}; {row["repo"]})'), '',
             '## Deploys without verification',
             *_rows(unverified_deploys, lambda row: f'- {_clock(row["at"])} {row["text"]} ({_why(row)}; {row["repo"]})'), '',
             '## Paused reviews',
             *_rows(facts['paused'], lambda row: f'- {_clock(row["at"])} {row["text"]}'), '',
             '## Reverts',
             *_rows(facts['reverts'], lambda row: f'- `{row["hash"]}` {_clock(row["at"])} {row["subject"]}'
                    + (f' (reverts `{row["reverts"]}`)' if row['reverts'] else '') + f' ({row["repo"]})'), '',
             '## What to learn', learn, '']
    return '\n'.join(lines)


def note_path(pid, day):
    """The postmortem note of one day. Refuses a link at the notes folder, the postmortems folder or the file: a
    read or a write must never leave the project folder."""
    if not isinstance(day, str) or not DAY.fullmatch(day):
        raise ValueError('Choose a day.')
    file = projects.bundle(pid) / 'notes' / 'postmortems' / f'{day}.md'
    # The folder must be where its name says, inside the projects folder: no link at the project folder, notes/ or
    # postmortems/ may send it elsewhere. (The projects folder itself may be a link the person made.)
    inside = projects.root().resolve() / pid / 'notes' / 'postmortems'
    if file.is_symlink() or file.parent.resolve() != inside:
        raise ValueError('The postmortem folder or note is a link. Tether does not read or change it.')
    return file


def read_note(pid, day):
    file = note_path(pid, day)
    if not file.is_file():
        raise projects.ProjectError(404, 'That postmortem is gone.')
    return {'project': pid, 'day': day, 'path': str(file), 'text': projects._read(file, projects.NOTE_LIMIT)}


def brief(day, note, folder):
    here = Path(__file__).resolve().parent
    return '\n'.join([
        f'Daily ops report for {day}. Tether starts this task once a day; the person turned it on in Settings.',
        'Do only this task. Do not change code, do not commit, do not start other tabs and do not take backlog items.',
        f'1. Read {note}. Tether wrote yesterday\'s facts there.',
        '2. For each row under "Commits without verification", "Deploys without verification", "Paused reviews" and '
        f'"Reverts", look at the evidence with read-only commands in {folder} (for example `git show --stat HASH`). '
        'Find the cause in one line. Do not guess: when the evidence does not show a cause, say so.',
        f'3. In that note, replace the line "{PLACEHOLDER}" under "## What to learn" with at most 8 bullets: what went '
        'wrong, the likely cause, and what prevents it next time. Change no other section.',
        '4. For each cause that will recur (at most 3), propose a lesson: '
        f'`python3 {here / "agent_lessons_cli.py"} propose --avoid "When <situation>, <what to do>." --quote "<the evidence>"` '
        '(use --keep for a thing that worked). A proposal goes to a list and the person decides. Do not run `add`.',
        f'5. Run `python3 {here / "agent_tab_cli.py"} close --self`, then reply with a summary of 3 lines or fewer.',
    ])


# ---- one run ----

def session_project(session_id, run=subprocess.run):
    """(project id, session name) of a tmux session, or None when it has no project. It never gives it one."""
    try:
        shown = run([paths.TMUX, 'display-message', '-p', '-t', session_id + ':', '#{@project}\t#{session_name}'],
                    capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = shown.stdout.rstrip('\n').split('\t')
    if shown.returncode or len(fields) != 2 or not projects.ID.fullmatch(fields[0]):
        return None
    return fields[0], fields[1]


def plan(entries, folder_of, run=subprocess.run):
    """[{'session', 'project', 'name', 'anchor', 'roots'}]: the sessions to report on, the one with the most agent
    tabs first. A repository goes to the first session that has a tab in it."""
    by_session = {}
    for entry in entries:
        if entry.get('agent') in ('claude', 'codex') and entry.get('tmux_session'):
            by_session.setdefault(entry['tmux_session'], []).append(entry)
    found, taken = [], set()
    for session, tabs in sorted(by_session.items(), key=lambda pair: -len(pair[1])):
        project = session_project(session, run)
        if project is None:
            continue
        roots = []
        for entry in tabs:
            try:
                folder = folder_of(entry)
            except (OSError, KeyError, ValueError, TypeError):
                continue
            root = factory.repository(folder, run) if folder else None
            if root and root not in taken and root not in roots:
                roots.append(root)
        taken.update(roots)
        anchor = next((entry for entry in tabs if entry.get('writable')), tabs[0])
        found.append({'session': session, 'project': project[0], 'name': project[1], 'anchor': anchor, 'roots': roots})
    return found


def run_day(day, entries, folder_of, run=subprocess.run, lock=None, start=delegate.start, sessions=None):
    """Write the notes of one day and start the ops tabs. Returns the run records.
    sessions: the live tab list, read again under the send lock just before a tab starts.
    A day's note is written once and never replaced. A project gets one tab for a day and a day gets TABS tabs
    (reserve): a second run ("Run now") only starts a tab that an earlier run could not ask tmux for."""
    begin, end = day_bounds(day)
    paused = reviews(begin, end)
    records = []
    for item in plan(entries, folder_of, run):
        record = {'day': day, 'project': item['project'], 'project_name': item['name'], 'at': time.time(),
                  'status': 'quiet', 'why': '', 'pane': ''}
        try:
            mine = [row for row in paused if row.get('session') == item['session']]
            facts = gather(day, item['roots'], mine, run)
            record.update(commits=len(facts['commits']), deploys=len(facts['deploys']), problems=facts['problems'])
            if not facts['commits'] and not facts['deploys'] and not mine:
                record['why'] = 'No commits, deploys or paused reviews that day.'
                records.append(record)
                continue
            note = note_path(item['project'], day)
            record['note'] = str(note)
            if note.is_file():
                if PLACEHOLDER not in projects._read(note, projects.NOTE_LIMIT):
                    record['why'] = 'The postmortem for that day exists already. Tether does not write it again.'
                    records.append(record)
                    continue
            else:
                projects._write(note, note_text(facts, item['name'], PLACEHOLDER if facts['problems'] else QUIET))
            if not facts['problems']:
                record['why'] = 'Every commit and deploy was verified. No tab was needed.'
            else:
                refused = reserve(day, item['project'])
                if refused:
                    record.update(status='failed' if 'at most' in refused else 'quiet', why=refused)
                    records.append(record)
                    continue
                folder = item['roots'][0] if item['roots'] else str(note.parent)
                asked = False
                try:
                    with lock or threading.Lock():
                        live = sessions() if sessions else entries
                        anchor = next((entry for entry in live if entry['id'] == item['anchor']['id']), None)
                        if anchor is None:
                            raise ValueError('The session has no open tab now.')
                        body = {'agent': 'claude', 'model': MODEL, 'effort': EFFORT, 'name': f'Ops report {day}',
                                'message': brief(day, note, folder), 'session': anchor['id'], 'thread': anchor.get('thread')}
                        delegate.validate(body, live)
                        asked = True  # from here a tab may exist even when start() fails: the reservation stays
                        pane, _ = start(anchor, body, folder, run=run, group='')
                except BaseException:
                    if not asked:
                        release(day, item['project'])
                    raise
                record.update(status='started', pane=pane)
        except Exception as exc:  # noqa: BLE001 - one project must not stop the others
            record.update(status='failed', why=(str(exc) or type(exc).__name__)[:300])
        records.append(record)
    return records


class Ops:
    """Starts the daily run from the monitor loop, in its own thread."""

    def __init__(self, clock=datetime.now):
        self.clock = clock
        self.busy = False     # the worker looks or runs
        self.running = False  # a run is in progress (what Settings shows)
        self.next = 0.0

    def due(self, state, now):
        if state['request']:
            return True
        return enabled() and now.hour >= HOUR and state['last_day'] != now.strftime('%Y-%m-%d')

    def observe(self, entries, folder_of, run=subprocess.run, lock=None, sessions=None, wait=False):
        """Look every 5 seconds whether a run is due, in a worker thread: the monitor thread reads no file.
        Returns True when it started the worker. wait=True waits for it."""
        with _LOCK:
            if self.busy or time.monotonic() < self.next:
                return False
            self.next = time.monotonic() + 5
            self.busy = True
        thread = threading.Thread(target=self._work, args=(list(entries), folder_of, run, lock, sessions), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self.busy = False
            raise
        if wait:
            thread.join()
        return True

    def _work(self, entries, folder_of, run, lock, sessions):
        try:
            now = self.clock()
            try:
                if not self.due(load(strict=True), now):
                    return
                # Marked before the work: a crash must not start the tabs again on every tick. "Run now" counts as
                # the day's run too. When the state cannot be read or the mark cannot be saved, nothing starts.
                _change(lambda data: data.update(request=False, last_day=now.strftime('%Y-%m-%d')))
            except (OSError, ValueError):
                self.next = time.monotonic() + 300  # a state file in trouble is not asked every tick
                raise
            self.running = True
            day = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            records = run_day(day, entries, folder_of, run, lock, sessions=sessions)
            if not records:
                records = [{'day': day, 'project': '', 'project_name': '', 'at': time.time(), 'status': 'quiet',
                            'why': 'No open session has a project folder and agent tabs.', 'pane': ''}]
            _change(lambda data: data['runs'].extend(records))
        except Exception as exc:  # noqa: BLE001 - the report must never take the monitor down
            print(f'ops report failed: {exc!r}', flush=True)
        finally:
            self.busy = self.running = False


OPS = Ops()


def state():
    """What Settings shows: the switch, the last runs (newest first) and the lesson candidates of every project."""
    data = load()
    candidates, skipped = [], []
    root = projects.root()
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        pid = folder.name
        if not projects.ID.fullmatch(pid) or not folder.is_dir():
            continue
        try:
            rows = lesson_candidates.entries(pid)
            name = projects.meta(pid)['name'] or pid
        except (OSError, ValueError):
            skipped.append(pid)
            continue
        candidates.extend(dict(row, project=pid, project_name=name) for row in rows)
    runs = []
    for row in reversed(data['runs'][-10:]):
        note = bool(row.get('project')) and bool(row.get('note')) and Path(row['note']).is_file()
        runs.append({'day': row.get('day', ''), 'project': row.get('project', ''), 'project_name': row.get('project_name', ''),
                     'status': row.get('status', ''), 'why': row.get('why', ''), 'pane': row.get('pane', ''),
                     'at': row.get('at', 0), 'note': note, 'problems': row.get('problems', 0)})
    return {'enabled': enabled(), 'hour': HOUR, 'model': MODEL_LABEL, 'tabs': TABS, 'busy': OPS.running or data['request'],
            'runs': runs, 'candidates': sorted(candidates, key=lambda row: -row['at']), 'skipped': skipped}
