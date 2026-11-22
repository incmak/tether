"""Suite-wide home and store roots, including for test subprocesses."""
import os
from pathlib import Path
import tempfile


_store = None


def isolate():
    global _store
    if _store is not None:
        return
    _store = tempfile.TemporaryDirectory(prefix='tether-unittest-', dir='/tmp')
    root = Path(_store.name)
    home = root / 'home'
    tmp = root / 'tmp'
    tmp.mkdir()
    os.environ.update(HOME=str(home), USERPROFILE=str(home),
                      TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp),
                      XDG_CONFIG_HOME=str(home / '.config'), XDG_CACHE_HOME=str(home / '.cache'),
                      XDG_DATA_HOME=str(home / '.local/share'), XDG_STATE_HOME=str(home / '.local/state'),
                      GIT_CONFIG_GLOBAL=os.devnull)
    tempfile.tempdir = None
    for key, folder in (
        ('MOBILE_TMUX_HOME', 'home'),
        ('MOBILE_TMUX_CACHE', 'cache'),
        ('MOBILE_TMUX_CONFIG', 'config'),
        ('TETHER_HOME', 'tether'),
    ):
        location = root / folder
        location.mkdir()
        os.environ[key] = str(location)
    # Use defaults under the isolated roots. Individual tests can still override them.
    for key in ('TETHER_SHARE', 'TETHER_ATTENTION', 'TETHER_URGENT_CALLS', 'TETHER_LAYOUTS', 'TETHER_COMMIT_GATE_LOG',
                'TETHER_BACKGROUNDS', 'TETHER_REMINDERS', 'TETHER_TASK_SOURCES', 'MOBILE_TMUX_CODEX_HOOKS', 'CODEX_HOME',
                'TETHER_ACTIVITY', 'TETHER_INSTALL_STATE', 'TETHER_OFFICE_CONFIG', 'TETHER_DEPLOYED',
                'TETHER_FRIDAY_MODELS', 'TETHER_FRIDAY_NOTES', 'TETHER_FRIDAY_DRAFTS',
                'MOBILE_TMUX_FILES_ROOT', 'TMUX_CHAT_DIST'):
        os.environ.pop(key, None)
