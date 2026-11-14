"""Where Mobile Tmux finds its own files and each agent's files. Every location can be overridden with an environment variable."""
import os
from pathlib import Path
import shutil


def _find_tmux():
    found = os.environ.get('MOBILE_TMUX_TMUX') or shutil.which('tmux')
    if found:
        return found
    import procinfo  # Only the fallback needs platform helpers; minimal CLI bundles omit them.
    if procinfo.DARWIN:
        for path in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux'):
            if os.access(path, os.X_OK):
                return path
    return '/usr/bin/tmux'


HOME = Path(os.environ.get('MOBILE_TMUX_HOME') or Path.home())
CACHE = Path(os.environ.get('MOBILE_TMUX_CACHE') or HOME / '.cache/mobile-tmux')
TETHER = Path(os.environ.get('TETHER_HOME') or HOME / '.tether')  # session projects (projects.py)
SHARE = Path(os.environ.get('TETHER_SHARE') or TETHER / 'share')
CONFIG = Path(os.environ.get('MOBILE_TMUX_CONFIG') or HOME / '.config/mobile-tmux')
ATTENTION = Path(os.environ.get('TETHER_ATTENTION') or CACHE / 'attention.json')  # Attention mode asks (attention.py)
LAYOUTS = Path(os.environ.get('TETHER_LAYOUTS') or CONFIG / 'layouts')  # custom layout JSON files (custom_layouts.py)
BACKGROUNDS = Path(os.environ.get('TETHER_BACKGROUNDS') or CONFIG / 'backgrounds')
TMUX = _find_tmux()
SYSTEMD_RUN = shutil.which('systemd-run')
# server.py sets this at start when it runs as a systemd service. Not read from the environment here: tmux panes
# started by the service inherit INVOCATION_ID too.
SCOPE_TMUX = False


def tmux_new_session():
    """The start of a `tmux new-session` command. When this runs as a systemd service (INVOCATION_ID is set) and no
    tmux server is up yet, new-session starts the server. It must not start inside this service's cgroup: a service
    restart kills every process there, and with it every user session (this happened on 2 Nov 2026). So the command
    runs in its own transient scope."""
    if SYSTEMD_RUN and SCOPE_TMUX:
        return [SYSTEMD_RUN, '--user', '--scope', '--quiet', '--collect', TMUX]
    return [TMUX]

CLAUDE = HOME / '.claude'
CODEX = HOME / '.codex'
CODEX_HOOKS = Path(os.environ.get('MOBILE_TMUX_CODEX_HOOKS') or
                   Path(os.environ.get('CODEX_HOME') or CODEX) / 'hooks.json')
ANTIGRAVITY = HOME / '.gemini/antigravity-cli'


def work():
    """The work board's project list (work_items.py). Read at call time: tests move TETHER."""
    return TETHER / 'work'


def items():
    """Work items, by project prefix. Codex tabs get write access to this folder only (project_launch.py)."""
    return TETHER / 'items'


def private_file(name):
    """A file under the config folder (owner-only data such as the access token and push keys)."""
    return CONFIG / name
