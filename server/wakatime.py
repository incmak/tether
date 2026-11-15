"""Opt-in activity time for WakaTime.

Editor plugins send WakaTime a "heartbeat" while a person works; WakaTime adds the heartbeats up into time per project.
Work in Tether happens in a browser, so no editor sends them. With the 'wakatime' preference on, the page reports that
the person used a tab (at most once a minute), and this module sends one heartbeat for that tab's folder through the
official wakatime-cli. The CLI reads the API key from ~/.wakatime.cfg and keeps an offline queue; Tether never reads
or stores the key.

wakatime-cli parses every agent transcript (Claude, Codex, ...) on each heartbeat unless told not to. That can take
minutes of CPU, and it counts agent time, not the person's time, so heartbeats pass --sync-ai-disabled. The
'wakatimeAgents' preference turns that parse on as its own job, at most once every SYNC_EVERY seconds.
"""
import configparser
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import paths
import prefs_sync

PATH = '/mobile/api/activity'
PLUGIN = 'tether/1 tether-wakatime/1'
CATEGORY = 'coding'
SAME_PROJECT = 120  # seconds; WakaTime plugins resend the same entity at most every 2 minutes
SYNC_EVERY = 600  # seconds between agent transcript parses
TODAY_EVERY = 300  # seconds a "today" total stays fresh
TIMEOUT = 60  # one heartbeat
SYNC_TIMEOUT = 900  # the first transcript parse can be long
CODES = {102: 'WakaTime did not answer. The CLI saved the time offline and sends it later.',
         103: 'The WakaTime config file has an error. Check ~/.wakatime.cfg.',
         104: 'WakaTime rejected the API key. Check api_key in ~/.wakatime.cfg.',
         112: 'WakaTime asked the CLI to wait. The CLI saved the time offline.'}


def cli():
    """The wakatime-cli path, or None. TETHER_WAKATIME_CLI overrides; editor plugins install it under ~/.wakatime."""
    override = os.environ.get('TETHER_WAKATIME_CLI')
    if override:
        return override if os.access(override, os.X_OK) else None
    home = Path(os.environ.get('WAKATIME_HOME') or paths.HOME)
    for name in ('wakatime-cli', 'wakatime-cli-linux-amd64', 'wakatime-cli-linux-arm64', 'wakatime-cli-darwin-arm64', 'wakatime-cli-darwin-amd64'):
        found = home / '.wakatime' / name
        if os.access(found, os.X_OK):
            return str(found)
    return shutil.which('wakatime-cli') or shutil.which('wakatime')


def config_file():
    """The file wakatime-cli reads, in its own order (see wakatime-cli --help, --config)."""
    if os.environ.get('WAKATIME_HOME'):
        return Path(os.environ['WAKATIME_HOME']) / '.wakatime.cfg'
    home = paths.HOME / '.wakatime.cfg'
    if home.exists():
        return home
    return Path(os.environ.get('XDG_CONFIG_HOME') or paths.HOME / '.config') / 'wakatime' / 'wakatime.cfg'


def has_key():
    """True when the config names an API key. The key itself never leaves this function."""
    if os.environ.get('WAKATIME_API_KEY'):
        return True
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_file())
        return bool(parser.get('settings', 'api_key', fallback='').strip())
    except (configparser.Error, OSError, UnicodeError):
        return False


def enabled():
    prefs = prefs_sync.load()['prefs']
    return prefs.get('wakatime') is True, prefs.get('wakatimeAgents') is True


def project(folder, run=subprocess.run):
    """(name, detected): the repository's name for a git folder (the main checkout's name for a worktree), else the
    folder's own name. A worktree such as ~/apps/tether-wt/fix counts as the project 'tether'."""
    try:
        result = run(['git', '-C', folder, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                     capture_output=True, text=True, timeout=5)
        common = result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        common = ''
    if common:
        root = Path(common)
        root = root.parent if root.name == '.git' else root  # a bare repository has no .git folder
        return root.name.removesuffix('.git') or Path(folder).name, True
    return Path(folder).name or 'home', False


class Tracker:
    """Sends heartbeats from one background thread, so a page request never waits on the network."""

    def __init__(self, run=subprocess.run, clock=time.time, find=cli):
        self.run = run
        self.clock = clock
        self.find = find
        self.jobs = queue.Queue(maxsize=16)
        self.lock = threading.Lock()
        self.sent = {}  # folder -> time of the last heartbeat
        self.synced = 0.0
        self.last = None  # {'at', 'ok', 'error'} for the newest finished job
        self.today = None  # {'at', 'text'}
        self.thread = None

    def _start(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name='wakatime', daemon=True)
                self.thread.start()

    def _queue(self, job):
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            return False  # WakaTime joins heartbeats up to 15 minutes apart, so a dropped one costs nothing
        self._start()
        return True

    def _work(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception as exc:  # one failed send must not stop the next one
                self.last = {'at': self.clock(), 'ok': False, 'error': f'Tether could not run wakatime-cli: {type(exc).__name__}.'}
            finally:
                self.jobs.task_done()

    def _call(self, argv, timeout, record=True):
        """Run the CLI and, for a send, record the outcome. Never raises for the CLI's own failures."""
        error = ''
        try:
            result = self.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            result, error = None, 'wakatime-cli took too long. Tether stopped it.'
        except OSError:
            result, error = None, 'Tether could not start wakatime-cli.'
        if result is not None and result.returncode:
            error = CODES.get(result.returncode, f'wakatime-cli stopped with code {result.returncode}.')
        if record:
            self.last = {'at': self.clock(), 'ok': not error, 'error': error}
        return result

    def heartbeat(self, entry):
        """Queue one heartbeat for a tab. Returns why it did not, or ''. entry is a sessions() row."""
        on, _agents = enabled()
        if not on:
            return 'off'
        folder = entry.get('cwd') or ''
        if not folder.startswith('/') or not os.path.isdir(folder):
            return 'no-folder'
        binary = self.find()
        if not binary:
            return 'no-cli'
        now = self.clock()
        with self.lock:
            if now - self.sent.get(folder, 0) < SAME_PROJECT:
                return 'recent'
            self.sent[folder] = now
            if len(self.sent) > 500:
                self.sent = {key: value for key, value in self.sent.items() if now - value < SAME_PROJECT}

        def send():
            name, detected = project(folder, self.run)
            argv = [binary, '--entity', folder, '--entity-type', 'file', '--category', CATEGORY, '--time', f'{now:.3f}',
                    '--plugin', PLUGIN, '--sync-ai-disabled', '--project' if detected else '--alternate-project', name]
            self._call(argv, TIMEOUT)
        return '' if self._queue(send) else 'busy'

    def observe(self):
        """Called by the server monitor. Parses agent transcripts when that option is on and the last parse is old."""
        on, agents = enabled()
        if not (on and agents) or self.clock() - self.synced < SYNC_EVERY:
            return False
        binary = self.find()
        if not binary:
            return False
        self.synced = self.clock()
        argv = ['nice', '-n', '10', binary] if shutil.which('nice') else [binary]
        return self._queue(lambda: self._call(argv + ['--sync-ai-activity', '--plugin', PLUGIN], SYNC_TIMEOUT))

    def _refresh_today(self, binary):
        result = self._call([binary, '--today', '--plugin', PLUGIN, '--sync-ai-disabled'], TIMEOUT, record=False)
        text = ' '.join((result.stdout if result and result.returncode == 0 else '').split())[:200]
        self.today = {'at': self.clock(), 'text': text}

    def status(self):
        on, agents = enabled()
        binary = self.find()
        key = has_key()
        if on and binary and key and (self.today is None or self.clock() - self.today['at'] > TODAY_EVERY):
            self.today = {'at': self.clock(), 'text': (self.today or {}).get('text', '')}  # one refresh at a time
            self._queue(lambda: self._refresh_today(binary))
        return {'enabled': on, 'agents': agents, 'cli': bool(binary), 'key': key, 'config': str(config_file()).replace(str(paths.HOME), '~', 1),
                'today': (self.today or {}).get('text', ''), 'last': self.last}


TRACKER = Tracker()


def handles(path):
    return path == PATH


def get(handler):
    handler.respond(200, TRACKER.status())


def post(handler, sessions):
    """{pane}: the person used this tab now."""
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 1024:
            raise ValueError
        body = json.loads(handler.rfile.read(size))
        pane = body.get('pane') if isinstance(body, dict) else None
        if not isinstance(pane, str) or not 0 < len(pane) <= 64:
            raise ValueError
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
        return
    entry = next((entry for entry in sessions() if entry.get('id') == pane), None)
    if entry is None:
        handler.respond(200, {'sent': False, 'reason': 'gone'})
        return
    reason = TRACKER.heartbeat(entry)
    handler.respond(200, {'sent': not reason, 'reason': reason})
