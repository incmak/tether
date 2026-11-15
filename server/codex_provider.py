"""Which provider new Codex panes use: the ChatGPT plan (the default) or Azure Foundry.

Foundry runs as `codex -p foundry`: Codex layers ~/.codex/foundry.config.toml on top of ~/.codex/config.toml. Tether reads only
their structure (profile present, provider named, how the provider gets its key) and never a key value, and it never runs the
provider's key command. Running panes keep the provider they started with.

The switch also sets the top-level `model_provider` line in ~/.codex/config.toml, so every new `codex` run on the server follows it:
Tether panes, Claude Code lanes and scripts. Codex 0.166 refuses a default `profile` key, so this one line is the only lever.
Tether changes no other line, keeps a one-time backup (config.toml.bak-tether), and keeps the file mode.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import threading
import tomllib

TOP_PROVIDER = re.compile(r'^model_provider\s*=.*$', re.M)

import paths
import procinfo

FILE = paths.CACHE / 'codex_provider.json'
PROFILE = 'foundry'
CHOICES = ('plan', 'foundry')
NOT_SET_UP = 'Foundry is not set up on the server yet.'
PROFILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
_lock = threading.Lock()
LOCK_FILE = '.config.toml.tether-lock'
_config_lock = threading.Lock()


@contextlib.contextmanager
def config_lock(directory):
    """The one lock for every Tether write to config.toml in `directory` (a path or an open directory fd):
    this provider switch and codex_trust.write_trust. A thread lock in this process, an flock across processes."""
    with _config_lock:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(LOCK_FILE, flags, 0o600, dir_fd=directory) if isinstance(directory, int) \
            else os.open(Path(directory) / LOCK_FILE, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def chosen():
    try:
        value = json.loads(FILE.read_text()).get('provider')
    except (OSError, ValueError, AttributeError):
        return 'plan'
    return value if value in CHOICES else 'plan'


def _toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


def readiness(home=None):
    """(ready, hint, model). Checks the profile file, the provider it names, and that the provider's key source can work here."""
    home = Path(home or paths.CODEX)
    profile = _toml(home / f'{PROFILE}.config.toml')
    if profile is None:
        return False, f'{NOT_SET_UP} ~/.codex/{PROFILE}.config.toml is missing.', None
    model = profile.get('model') if isinstance(profile.get('model'), str) else None
    base = _toml(home / 'config.toml') or {}
    name = profile.get('model_provider') or base.get('model_provider')
    if not isinstance(name, str) or name in ('openai', ''):
        return False, f'{NOT_SET_UP} The {PROFILE} profile does not name a Foundry provider.', model
    # A provider defined in the profile replaces the one in config.toml.
    provider = ((profile.get('model_providers') or {}).get(name) or (base.get('model_providers') or {}).get(name))
    if not isinstance(provider, dict):
        return False, f'{NOT_SET_UP} Provider "{name}" is not in ~/.codex/config.toml.', model
    auth = provider.get('auth')
    if isinstance(auth, dict) and isinstance(auth.get('command'), str):
        command = auth['command']
        # Only whether the program exists. Running it would print the key.
        found = (os.path.isfile(command) and os.access(command, os.X_OK)) if os.sep in command else shutil.which(command)
        if not found:
            return False, f'{NOT_SET_UP} Its key command ({Path(command).name}) does not run on Linux.', model
        return True, None, model
    if isinstance(provider.get('env_key'), str) or isinstance(provider.get('experimental_bearer_token'), str):
        return True, None, model
    return False, f'{NOT_SET_UP} Provider "{name}" has no key source.', model


def _provider_name(provider, home):
    """The provider name for config.toml: the Foundry profile's provider, or "openai" for the plan."""
    if provider == 'plan':
        return 'openai'
    name = (_toml(home / f'{PROFILE}.config.toml') or {}).get('model_provider')
    return name if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) else None


def default_provider(home=None):
    """The top-level model_provider in config.toml: what a plain `codex` run uses. Codex's own default is "openai"."""
    value = (_toml(Path(home or paths.CODEX) / 'config.toml') or {}).get('model_provider')
    return value if isinstance(value, str) else 'openai'


def set_default(provider, home=None):
    """Make plain `codex` runs use `provider`: rewrite only the top-level model_provider line of config.toml."""
    home = Path(home or paths.CODEX)
    name = _provider_name(provider, home)
    config = home / 'config.toml'
    if not name or not config.exists():
        raise ValueError('Could not set the default Codex provider: ~/.codex/config.toml or the Foundry profile is missing.')
    with config_lock(home):
        text = config.read_text()
        table = re.search(r'^\s*\[', text, re.M)
        head, tail = (text[:table.start()], text[table.start():]) if table else (text, '')
        line = f'model_provider = "{name}"'
        head = TOP_PROVIDER.sub(line, head, count=1) if TOP_PROVIDER.search(head) else f'{line}\n{head}'
        updated = head + tail
        if updated == text:
            return
        try:
            tomllib.loads(updated)
        except ValueError as failure:
            raise ValueError('Could not set the default Codex provider: config.toml would not parse.') from failure
        backup = home / 'config.toml.bak-tether'
        if not backup.exists():
            shutil.copy2(config, backup)
        temporary = home / 'config.toml.tether-tmp'
        temporary.write_text(updated)
        os.chmod(temporary, config.stat().st_mode & 0o777)
        if config.read_text() != text:  # Codex has no lock: refuse when it wrote in the meantime
            temporary.unlink()
            raise ValueError('Codex config.toml changed while Tether wrote it. Try again.')
        os.replace(temporary, config)


def status(home=None):
    ready, hint, model = readiness(home)
    provider = chosen()
    everywhere = default_provider(home) == _provider_name(provider, Path(home or paths.CODEX))
    return {'provider': provider, 'profile': PROFILE, 'model': model, 'ready': ready, 'hint': hint, 'everywhere': everywhere}


def choose(provider, home=None):
    """Save the provider for new Codex panes. Foundry needs a working setup; the plan is always allowed."""
    if provider not in CHOICES:
        raise ValueError('Choose ChatGPT plan or Azure Foundry.')
    if provider == 'foundry' and not readiness(home)[0]:
        raise ValueError(readiness(home)[1])
    with _lock:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = FILE.with_suffix('.tmp')
        temporary.write_text(json.dumps({'provider': provider}))
        os.replace(temporary, FILE)
        set_default(provider, home)
    return status(home)


def launch_profile(home=None):
    """The profile for a new Codex pane: None for the plan. Refuses Foundry when its setup broke after it was chosen."""
    if chosen() != 'foundry':
        return None
    ready, hint, _ = readiness(home)
    if not ready:
        raise RuntimeError(hint + ' Switch Codex provider to ChatGPT plan in Plan usage.')
    return PROFILE


def profile_from_args(args):
    """The profile in a Codex command line (-p NAME, --profile NAME, --profile=NAME), or None."""
    for index, arg in enumerate(args):
        value = None
        if arg in ('-p', '--profile') and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith('--profile='):
            value = arg.split('=', 1)[1]
        elif arg.startswith('-p') and len(arg) > 2 and not arg.startswith('--'):
            value = arg[2:]
        if value is not None:
            return value if PROFILE_NAME.fullmatch(value) else None
    return None


def pane_profile(pid):
    """The profile a running Codex process started with, from /proc. None for the plan or when it cannot be read."""
    try:
        args = [arg.decode(errors='replace') for arg in procinfo.argv(pid)]
    except OSError:
        return None
    return profile_from_args(args[1:])
