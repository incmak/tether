"""Opt-in activity time, kept by Tether itself.

With the 'activity' preference on, Tether marks each minute in which the person used a tab (the page reports input, at
most once a minute per tab) for that tab's project. With 'activityAgents' on as well, it also marks each minute in
which an agent tab was Working. Minutes are stored per UTC day under TETHER_ACTIVITY (default ~/.tether/activity), one
small JSON file per day, and kept until the person deletes them.

Time is counted the way WakaTime counts heartbeats, so old and new numbers compare: each marked minute is one minute,
and a gap of up to GAP minutes between two marked minutes counts as work too. A project's time uses its own minutes; a
day's total uses the union of all projects' minutes, so two tabs in one minute count once.

Optionally Tether publishes the full history as one JSON file into a git checkout (for example a side branch of a
stats repository) and pushes it: at most every PUBLISH_EVERY seconds when the data changed, or on request.
"""
import datetime
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import paths
import prefs_sync
import tether_zone

PATH = '/mobile/api/activity'
PUBLISH_PATH = '/mobile/api/activity/publish'
EXPORT_PATH = '/mobile/api/activity/export'
FILE_NAME = 'tether-activity.json'
GAP = 15  # minutes; WakaTime's default keystroke timeout
AGENTS = ('claude', 'codex', 'antigravity')
PUBLISH_EVERY = 6 * 3600
GIT_TIMEOUT = 60
PROJECT_TTL = 600
DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
ZONE = re.compile(r'[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}')


def folder():
    return Path(os.environ.get('TETHER_ACTIVITY') or paths.TETHER / 'activity')


def config_file():
    return paths.CONFIG / 'activity.json'


def enabled():
    prefs = prefs_sync.load()['prefs']
    on = prefs.get('activity') is True
    return on, on and prefs.get('activityAgents') is True


def project(path, run=subprocess.run):
    """The repository's name for a git folder (the main checkout's name for a worktree), else the folder's own name.
    A worktree such as ~/apps/tether-wt/fix counts as the project 'tether'."""
    try:
        result = run(['git', '-C', path, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                     capture_output=True, text=True, timeout=5, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        common = result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        common = ''
    if common:
        root = Path(common)
        root = root.parent if root.name == '.git' else root  # a bare repository has no .git folder
        return root.name.removesuffix('.git') or Path(path).name or 'home'
    return Path(path).name or 'home'


def covered(minutes):
    """The minutes of work in a sorted list of marked epoch minutes: each marked minute, plus the minutes of a gap
    of up to GAP minutes before it. Split by day after this step, so a gap across midnight still counts."""
    out, previous = [], None
    for minute in minutes:
        if previous is not None and minute - previous <= GAP:
            out.extend(range(previous + 1, minute + 1))
        elif minute != previous:
            out.append(minute)
        previous = minute
    return out


def seconds(minutes):
    """Seconds of work in a sorted list of epoch minutes."""
    return 60 * len(covered(minutes))


def _write_json(path, data):
    """Write through a new, exclusive temporary file, then rename it over the path. The rename replaces a link at
    the path itself; it never writes through one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(data, separators=(',', ':'), sort_keys=True))
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class Store:
    """Marked minutes per UTC day: {'you': {project: [minute, ...]}, 'agents': {...}}. Thread-safe."""

    def __init__(self, clock=time.time, run=subprocess.run):
        self.clock = clock
        self.run = run
        self.lock = threading.RLock()
        self.days = {}  # UTC day -> loaded data
        self.projects = {}  # folder -> (name, checked at)
        self.publishing = False
        self.last_publish = None  # {'at', 'ok', 'error'}

    # Configuration: the first marked minute and the publish folder. Owner-only, outside the repo.
    def config(self):
        data = _read_json(config_file()) or {}
        return {'since': _number(data.get('since')),
                'publish': data.get('publish') if isinstance(data.get('publish'), str) else '',
                'published': _number(data.get('published')) or 0,
                'revision': int(_number(data.get('revision')) or 0),
                'published_revision': int(_number(data.get('published_revision')) or 0)}

    def _save_config(self, **changes):
        with self.lock:
            data = {**self.config(), **changes}
            _write_json(config_file(), data)
            try:
                os.chmod(config_file(), 0o600)
            except OSError:
                pass
            return data

    def zone(self):
        return tether_zone.zone()

    def project_of(self, path):
        now = self.clock()
        with self.lock:
            cached = self.projects.get(path)
        if cached and now - cached[1] < PROJECT_TTL:
            return cached[0]
        name = project(path, self.run)
        with self.lock:
            if len(self.projects) > 500:
                self.projects.clear()
            self.projects[path] = (name, now)
        return name

    def _day_file(self, day):
        return folder() / f'{day}.json'

    def _load_day(self, day):
        if day not in self.days:
            data = _read_json(self._day_file(day)) or {}
            clean = {}
            for who in ('you', 'agents'):
                part = data.get(who) if isinstance(data.get(who), dict) else {}
                clean[who] = {name: sorted({m for m in minutes if isinstance(m, int) and not isinstance(m, bool)})
                              for name, minutes in part.items() if isinstance(name, str) and isinstance(minutes, list)}
            self.days[day] = clean
            for old in sorted(self.days)[:-8]:  # keep only recent days in memory
                self.days.pop(old, None)
        return self.days[day]

    def mark(self, who, name, at=None):
        """Mark the minute `at` (epoch seconds) for a project. Returns True when the minute is new. Checks consent
        here, at the write, so a switch turned off during a project lookup still stops the mark."""
        minute = int((self.clock() if at is None else at) // 60)
        day = datetime.datetime.fromtimestamp(minute * 60, datetime.timezone.utc).strftime('%Y-%m-%d')
        name = (name or 'home')[:100]
        with self.lock:
            if not enabled()[0 if who == 'you' else 1]:
                return False
            data = self._load_day(day)
            minutes = data[who].setdefault(name, [])
            if minute in minutes:
                return False
            minutes.append(minute)
            minutes.sort()
            _write_json(self._day_file(day), data)
            config = self.config()
            self._save_config(revision=config['revision'] + 1,
                              **({'since': minute * 60} if config['since'] is None or minute * 60 < config['since'] else {}))
        return True

    def human(self, path, zone=''):
        """Mark the use of a tab in `path`. The browser zone is accepted for old clients."""
        if not enabled()[0]:
            return 'off'
        if not isinstance(path, str) or not path.startswith('/') or not os.path.isdir(path):
            return 'no-folder'
        return '' if self.mark('you', self.project_of(path)) or enabled()[0] else 'off'

    def observe(self, entries, status, tab_folder):
        """Monitor loop: mark this minute for each project with a Working agent tab. Returns the count of new marks."""
        if not enabled()[1]:
            return 0
        marked = 0
        for entry in entries:
            if entry.get('agent') not in AGENTS:
                continue
            try:
                if status(entry).get('state') != 'Working':
                    continue
                path = tab_folder(entry)
            except Exception:  # a tab that just closed must not stop the others
                continue
            if isinstance(path, str) and path.startswith('/'):
                marked += self.mark('agents', self.project_of(path))
        return marked

    # Reading
    def _all_minutes(self, first_day=None):
        """{who: {project: [minutes]}} over the stored UTC days from first_day on."""
        out = {'you': {}, 'agents': {}}
        try:
            names = sorted(path.stem for path in folder().glob('*.json') if DAY.fullmatch(path.stem))
        except OSError:
            names = []
        with self.lock:
            for day in names:
                if first_day and day < first_day:
                    continue
                data = self.days.get(day) or _read_json(self._day_file(day)) or {}
                for who in out:
                    part = data.get(who) if isinstance(data.get(who), dict) else {}
                    for name, minutes in part.items():
                        if isinstance(name, str) and isinstance(minutes, list):
                            out[who].setdefault(name, []).extend(m for m in minutes if isinstance(m, int) and not isinstance(m, bool))
        return out

    def summary(self, recent=None, *, zone=None):
        """{day: {seconds, agent_seconds, projects, agent_projects}} by day in the Tether zone, from the
        first marked day to today. Days with no work are zero; days before the first mark are absent."""
        config = self.config()
        if config['since'] is None:
            return {}
        zone = zone or self.zone()
        today = datetime.datetime.fromtimestamp(self.clock(), zone).date()
        start = datetime.datetime.fromtimestamp(config['since'], zone).date()
        if recent:
            start = max(start, today - datetime.timedelta(days=recent - 1))
        first_utc = (start - datetime.timedelta(days=2)).isoformat()  # marks just before start bridge gaps into it
        out = {}
        day = start
        while day <= today:
            out[day.isoformat()] = {'seconds': 0, 'projects': {}, 'agent_seconds': 0, 'agent_projects': {}}
            day += datetime.timedelta(days=1)
        day_of = {}  # zone offsets change on quarter hours at most, so one lookup per quarter hour

        def add(minutes, row_key, name=None):
            for minute in covered(minutes):
                quarter, rest = divmod(minute, 15)
                base = day_of.get(quarter)
                if base is None:
                    base = day_of[quarter] = datetime.datetime.fromtimestamp(quarter * 900, zone)
                row = out.get((base + datetime.timedelta(minutes=rest)).date().isoformat())
                if row is None:
                    continue
                if name is None:
                    row[row_key] += 60
                else:
                    row[row_key][name] = row[row_key].get(name, 0) + 60

        everything = self._all_minutes(first_utc)
        for who, total_key, project_key in (('you', 'seconds', 'projects'), ('agents', 'agent_seconds', 'agent_projects')):
            projects = everything[who]
            for name, minutes in sorted(projects.items()):
                add(sorted(set(minutes)), project_key, name)
            add(sorted(set().union(*projects.values())) if projects else [], total_key)
        return out

    def export(self):
        with self.lock:  # the revision is read first, so a later mark always stays pending
            revision = self.config()['revision']
        zone = self.zone()
        return {'version': 1, 'source': 'tether', 'zone': zone.key, 'gap_minutes': GAP,
                'generated': int(self.clock()), 'revision': revision, 'days': self.summary(zone=zone)}

    def status(self):
        on, agents = enabled()
        config = self.config()
        zone = self.zone()
        days = self.summary(recent=14, zone=zone)
        projects = {}
        for row in days.values():
            for key, who in (('projects', 'you'), ('agent_projects', 'agents')):
                for name, value in row[key].items():
                    projects.setdefault(name, {'name': name, 'you': 0, 'agents': 0})[who] += value
        top = sorted(projects.values(), key=lambda item: (-(item['you'] + item['agents']), item['name']))[:8]
        home = str(paths.HOME)
        return {'enabled': on, 'agents': agents, 'zone': zone.key, 'since': config['since'],
                'store': str(folder()).replace(home, '~', 1),
                'days': [{'day': day, 'you': row['seconds'], 'agents': row['agent_seconds']} for day, row in days.items()],
                'projects': top,
                'publish': {'folder': config['publish'].replace(home, '~', 1) if config['publish'] else '',
                            'file': FILE_NAME, 'busy': self.publishing, 'last': self.last_publish,
                            'published': config['published'] or None,
                            'pending': bool(config['publish']) and config['revision'] > config['published_revision']}}

    # Publishing
    def set_publish(self, value):
        """Validate and save the publish folder: '' turns publishing off; else the top folder of a git checkout whose
        branch has an upstream."""
        if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Use an absolute folder path or a path that starts with ~/.')
        value = value.strip()
        if not value:
            self._save_config(publish='')
            return ''
        path = paths.HOME / value[2:] if value.startswith('~/') else Path(value)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('The folder does not exist. Use an absolute path or a path that starts with ~/.')
        path = path.resolve()
        top = self._git(path, 'rev-parse', '--show-toplevel')
        if top.returncode or not top.stdout.strip() or Path(top.stdout.strip()).resolve() != path:
            raise ValueError('The folder must be the top folder of a git checkout.')
        if self._git(path, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}').returncode:
            raise ValueError('The checkout’s branch has no upstream. Run “git push -u origin BRANCH” there once.')
        self._save_config(publish=str(path))
        return str(path)

    def _git(self, path, *args):
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
        try:
            return self.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                            env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, '', 'git took too long.')
        except OSError as exc:
            return subprocess.CompletedProcess(args, 127, '', f'git did not start: {type(exc).__name__}.')

    def publish(self):
        """Write the export into the publish folder, commit it and push. Returns '' or an error message."""
        target = self.config()['publish']
        if not target:
            error = 'Choose a publish folder first.'
        else:
            path = Path(target)
            started = self.clock()
            file = path / FILE_NAME
            snapshot = None
            error = ''
            if file.is_symlink():
                error = f'{FILE_NAME} in the publish folder is a link. Tether does not write through links.'
            elif not path.is_dir():
                error = 'The publish folder is gone.'
            else:
                snapshot = self.export()
                for step in (None, ('add', '--', FILE_NAME), ('commit', '-q', '-m', 'Tether activity', '--', FILE_NAME), ('push', '-q')):
                    # Consent and the destination are checked again before each step: either can change mid-job.
                    if not enabled()[0]:
                        error = 'Activity time is off, so Tether stopped the publish.'
                        break
                    if self.config()['publish'] != target:
                        error = 'The publish folder changed, so Tether stopped the publish.'
                        break
                    if step is None:
                        _write_json(file, snapshot)
                        continue
                    result = self._git(path, *step)
                    if step[0] == 'commit' and result.returncode and 'nothing' in (result.stdout or '') + (result.stderr or ''):
                        continue  # no change since the last commit; still push an earlier unpushed one
                    if result.returncode:
                        detail = ' '.join((result.stderr or result.stdout or '').split())[:300]
                        error = f'git {step[0]} failed: {detail or "code " + str(result.returncode)}'
                        break
            if not error:
                with self.lock:
                    if self.config()['publish'] == target:
                        self._save_config(published=started, published_revision=snapshot['revision'])
        self.last_publish = {'at': self.clock(), 'ok': not error, 'error': error}
        return error

    def maybe_publish(self, force=False):
        """Start a background publish when due. Returns True when one started."""
        config = self.config()
        with self.lock:
            # Consent always applies; force only skips the schedule.
            if not config['publish'] or self.publishing or not enabled()[0]:
                return False
            if not force:
                if config['revision'] <= config['published_revision']:
                    return False
                last = (self.last_publish or {}).get('at') or config['published']
                if self.clock() - last < PUBLISH_EVERY:
                    return False
            self.publishing = True

        def job():
            try:
                self.publish()
            except Exception as exc:  # a publish failure must never reach the monitor
                self.last_publish = {'at': self.clock(), 'ok': False, 'error': f'Publish failed: {type(exc).__name__}.'}
            finally:
                self.publishing = False
        threading.Thread(target=job, name='activity-publish', daemon=True).start()
        return True


STORE = Store()


def handles(path):
    return path in (PATH, PUBLISH_PATH, EXPORT_PATH)


def get(handler, path):
    handler.respond(200, STORE.export() if path == EXPORT_PATH else STORE.status())


def _body(handler, limit=2048):
    size = int(handler.headers.get('Content-Length', '0') or 0)
    if not 0 < size <= limit:
        raise ValueError
    body = json.loads(handler.rfile.read(size))
    if not isinstance(body, dict):
        raise ValueError
    return body


def post(handler, path, sessions, tab_folder):
    try:
        body = _body(handler)
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
        return
    if path == EXPORT_PATH:
        handler.respond(405, {'error': 'Use GET.'})
        return
    if path == PUBLISH_PATH:
        try:
            if 'folder' in body:
                STORE.set_publish(body['folder'])
        except ValueError as exc:
            handler.respond(400, {'error': str(exc)})
            return
        if body.get('now') is True and not enabled()[0]:
            handler.respond(409, {'error': 'Turn on Keep activity time first. Tether publishes only while it is on.'})
            return
        started = STORE.maybe_publish(force=True) if body.get('now') is True else False
        handler.respond(200, {**STORE.status(), 'started': started})
        return
    pane, zone = body.get('pane'), body.get('zone', '')
    if not isinstance(pane, str) or not 0 < len(pane) <= 64 or not isinstance(zone, str) or (zone and not ZONE.fullmatch(zone)):
        handler.respond(400, {'error': 'Invalid request.'})
        return
    entry = next((entry for entry in sessions() if entry.get('id') == pane), None)
    if entry is None:
        handler.respond(200, {'sent': False, 'reason': 'gone'})
        return
    reason = STORE.human(tab_folder(entry), zone)
    handler.respond(200, {'sent': not reason, 'reason': reason})
