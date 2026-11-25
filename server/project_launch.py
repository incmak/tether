#!/usr/bin/env python3
"""Start Claude Code or Codex with the tmux session's Tether project (projects.py), or print the project text for
Claude Code's SessionStart hook.

  project_launch.py claude [args...]   run claude, with the project folder and the hook when the session has a project
  project_launch.py codex [args...]    run codex, with standing rules, project text, and the backlog folder
  project_launch.py hook <id>          Claude Code SessionStart hook: print the project text as additionalContext

~/.local/bin/tether-claude and tether-codex call this. Any failure runs the plain agent with the same args, and the
hook prints nothing on a failure, so a broken project never stops an agent from starting."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from agent_browser_setup import has_browser_env, playwright_server

HERE = Path(__file__).resolve().parent
GUARD = 'TETHER_LAUNCH'
# Only an interactive session gets the project. Anything else (print mode, exec, login, mcp ...) runs as typed.
CLAUDE_PLAIN = {'-p', '--print', '-h', '--help', '-v', '--version', 'agents', 'attach', 'auth', 'auto-mode', 'doctor',
                'gateway', 'import', 'install', 'logs', 'mcp', 'plugin', 'purge', 'respawn', 'rm', 'setup-token', 'stop',
                'ultrareview', 'update'}
CODEX_PLAIN = {'-h', '--help', '-V', '--version', 'agents', 'exec', 'e', 'review', 'login', 'logout', 'mcp', 'mcp-server',
               'plugin', 'app-server', 'remote-control', 'completion', 'update', 'doctor', 'sandbox', 'debug', 'apply', 'a',
               'queue', 'archive', 'delete', 'migrate-rollouts', 'migrate-nfs-db', 'unarchive', 'cloud', 'exec-server',
               'features', 'help'}


def toml_string(text):
    """A TOML basic string. json.dumps is close but writes surrogate pairs and a raw DEL, which TOML refuses."""
    out = ['"']
    for char in text:
        if char in '"\\':
            out.append('\\' + char)
        elif char == '\n':
            out.append('\\n')
        elif char == '\t':
            out.append('\\t')
        elif ord(char) < 0x20 or ord(char) == 0x7f:
            out.append(f'\\u{ord(char):04X}')
        else:
            out.append(char)
    return ''.join(out) + '"'


def _tmux(*args):
    import paths
    return subprocess.run([paths.TMUX, *args], capture_output=True, text=True, timeout=3)


def current_project():
    """The project id of the tmux session this pane is in, or None outside tmux."""
    pane = os.environ.get('TMUX_PANE', '')
    if not os.environ.get('TMUX') or not re.fullmatch(r'%[0-9]+', pane):
        return None
    import projects
    shown = _tmux('display-message', '-p', '-t', pane, '#{session_id}')
    if shown.returncode:
        return None
    return projects.session_project(shown.stdout.strip(), lambda args, **kw: subprocess.run(args, **kw))[0]


def current_group():
    """{'pid', 'name'} of this pane's group (groups.py), with its bundle made, or None: no group, outside tmux, or any
    failure. Read at each launch and hook, so a tab moved into a group gets the group context at its next clear."""
    pane = os.environ.get('TMUX_PANE', '')
    if not os.environ.get('TMUX') or not re.fullmatch(r'%[0-9]+', pane):
        return None
    try:
        import groups
        run = lambda args, **kw: subprocess.run(args, **kw)
        session_id, group = groups.group_of_pane(pane, run)
        return {'pid': groups.context(session_id, group['id'], run), 'name': group['name']} if group else None
    except Exception:  # noqa: BLE001 - a broken group never stops the session project
        return None


def record_revision(pid, group=None):
    """The pane remembers which project revision its agent started with, so Tether can say "restart to apply".
    A tab in a group remembers both: "pid:rev+gpid:grev" (projects.marks)."""
    import projects
    pane = os.environ.get('TMUX_PANE', '')
    if os.environ.get('TMUX') and re.fullmatch(r'%[0-9]+', pane):
        mark = f'{pid}:{projects.meta(pid)["revision"]}'
        if group:
            mark += f'+{group["pid"]}:{projects.meta(group["pid"])["revision"]}'
        _tmux('set-option', '-p', '-t', pane, '@project_rev', mark)


def own_settings(args):
    """True for a run Tether leaves alone: a plain command (-p, mcp, update...) or one with its own --settings."""
    if any(arg in CLAUDE_PLAIN for arg in args):
        return True
    if any(arg == '--settings' or arg.startswith('--settings=') for arg in args):
        mark_pane('')  # an interactive tab with its own settings: Tether did not set its advisor
        return True
    return False


def claude_args(pid, args, group=None):
    if own_settings(args):
        return args
    import projects
    folder = projects.bundle(pid)
    (folder / 'notes').mkdir(mode=0o700, parents=True, exist_ok=True)
    hook = f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} hook {pid}'
    settings = {'hooks': {'SessionStart': [{'matcher': 'startup|resume|clear|compact',
                                            'hooks': [{'type': 'command', 'command': hook, 'timeout': 10}]}]}}
    advisor = advisor_settings(pid)
    record_revision(pid, group)
    # The project folders go in as settings, not --add-dir: from Claude Code 2.1.326, --add-dir stops a new tab on a
    # trust prompt for each folder not yet trusted (changelog 2.1.326; B188). additionalDirectories gives the same
    # file access without the prompt (code.claude.com/docs/en/permissions#working-directories). The bundle has no
    # .claude/ folder, so nothing that only --add-dir loads (skills, commands, agents) is lost.
    settings['permissions'] = {'additionalDirectories': [str(folder), str(projects.bundle(group['pid']))] if group else [str(folder)]}
    settings.update(advisor)
    return ['--settings', json.dumps(settings), *args]


def advisor_settings(pid):
    """advisorModel for the Tether advisor choice (claude_advisor.py), or {} for Off, and the pane option @advisor
    that tells the header and the prompt rules what the tab started with. Any failure gives {}: no advisor."""
    try:
        import claude_advisor
        entry = claude_advisor.launch_settings(pid)
        value = claude_advisor.mark(entry['advisorModel']) if entry else ''
    except Exception:  # noqa: BLE001 - a broken choice never stops a tab
        entry, value = {}, ''
    mark_pane(value)
    return entry


def mark_pane(value):
    """Set the pane option @advisor, or clear it for ''. Outside tmux, or on any tmux failure, do nothing."""
    pane = os.environ.get('TMUX_PANE', '')
    if not (os.environ.get('TMUX') and re.fullmatch(r'%[0-9]+', pane)):
        return
    try:
        if value:
            _tmux('set-option', '-p', '-t', pane, '@advisor', value)
        else:
            _tmux('set-option', '-p', '-u', '-t', pane, '@advisor')
    except (OSError, subprocess.SubprocessError):
        pass


def claude_plain_args(args):
    """A Claude tab outside any project gets only the global advisor choice."""
    if own_settings(args):
        return args
    entry = advisor_settings(None)
    return ['--settings', json.dumps(entry), *args] if entry else args


SUBCOMMANDS = {'resume', 'fork', 'exec', 'e', 'review'}
SANDBOX_PREFIXES = ('-s', '--sandbox', '-c', '--config', '-p', '--profile', '--approve-for-me', '-a', '--ask-for-approval')
WRITE_FLAGS = {'--dangerously-bypass-approvals-and-sandbox', '--yolo'}
UNKNOWN_FLAGS = {'--full-auto'}  # removed from Codex: 0.166 and 0.169 reject it
# --worktree moves Codex to a new checkout; --ignore-user-config drops config.toml. Neither is modelled.
UNMODELLED_FLAGS = {'--worktree', '--ignore-user-config'}
UNSURE = object()


def _option_locations(args, short, long):
    """Yield the argument index and prefix of each option value, in order."""
    for i, arg in enumerate(args):
        if arg in (short, long) and i + 1 < len(args):
            yield i + 1, ''
        elif long and arg.startswith(long + '='):
            yield i, long + '='
        elif short and arg.startswith(short) and len(arg) > len(short) and not arg.startswith('--'):
            yield i, short + '=' if arg.startswith(short + '=') else short


def _option(args, short, long):
    """Every value of an option, in order: `-s X`, `-sX`, `--sandbox X` and `--sandbox=X`."""
    return [args[i][len(prefix):] for i, prefix in _option_locations(args, short, long)]


def _mode(value):
    """True for a writable sandbox mode, False for read-only, UNSURE for anything else."""
    if value == 'read-only':
        return False
    return True if value in ('workspace-write', 'danger-full-access') else UNSURE


def _override(raw):
    """The value of a -c override as Codex reads it (utils/cli/src/config_override.rs): TOML, else the raw text
    without its outer quotes."""
    raw = raw.strip()
    try:
        import tomllib
        return tomllib.loads(f'_x_ = {raw}')['_x_']
    except Exception:
        return raw.strip().strip('"\'')


def _canon(path):
    """Strict realpath, as Codex canonicalize: None when any part is missing or broken."""
    if path is None:
        return None
    try:
        return os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        return None


def _toml(path):
    try:
        import tomllib
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (ImportError, OSError, ValueError):
        return None


ASCII_SPACE = ' \t\n\x0c\r'  # Rust trim_ascii (u8::is_ascii_whitespace), as Codex trims git metadata


def _read(path):
    """A git metadata file as Codex reads it (git-utils/src/trust.rs read_metadata_file): a regular file,
    not a symlink, at most 64 KiB. Anything else gives ''."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return ''
        data = path.read_bytes()
    except OSError:
        return ''
    if len(data) > 64 * 1024:
        return ''
    try:
        return data.decode().strip(ASCII_SPACE)
    except UnicodeDecodeError:
        return ''


def _join(base, target):
    """Codex PathUri::join_native_bytes: join, then drop "." and resolve ".." lexically, before any symlink.
    Codex rejects a NUL byte before it normalizes (utils/path-uri/src/lib.rs), so None."""
    if '\x00' in target:
        return None
    path = os.path.normpath(os.path.join(str(base), target))
    return '/' + path.lstrip('/') if path.startswith('//') else path  # PathUri keeps one leading slash


def _gitdir(dot_git):
    """The folder a .git file points to ("gitdir: PATH"), or None."""
    text = _read(dot_git)
    target = text[len('gitdir:'):].strip(ASCII_SPACE)
    if not text.startswith('gitdir:') or not target:
        return None
    return _join(dot_git.parent, target)


def _repo_root(cwd, *, logical=False):
    """The trust root Codex uses: the main checkout of a repository, also for a linked worktree.
    A port of git-utils/src/trust.rs resolve_root (same in Codex 0.166 through 0.170); git itself is not run.
    Find the nearest folder with a .git entry, skipping .git folders without HEAD. A .git folder is its own root.
    A .git file counts only for a registered linked worktree: its gitdir is <common>/worktrees/<name>,
    whose gitdir and commondir files point back to this checkout and to <common>, and the main checkout
    owns <common>. A submodule or a --separate-git-dir checkout gives no root. Where this port may differ
    from Codex (a symlinked .git), it gives no root, so Tether starts Codex without --add-dir."""
    # The process cwd is canonical, but Codex can preserve nested symlinks passed to --cd.
    base = str(cwd) if logical else _canon(cwd)
    if base is None:
        return None
    if not os.path.isdir(base):
        base = os.path.dirname(base)
    while True:
        candidate = base
        while not os.path.lexists(os.path.join(candidate, '.git')):
            parent = os.path.dirname(candidate)
            if parent == candidate:
                return None
            candidate = parent
        dot_git = Path(candidate) / '.git'
        if dot_git.is_symlink():
            return None
        if not dot_git.is_dir() or os.path.exists(dot_git / 'HEAD'):  # get_metadata follows symlinks
            break
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        base = parent
    root = candidate
    if dot_git.is_dir():
        return root
    if not dot_git.is_file():
        return None
    git_dir = _gitdir(dot_git)
    if git_dir is None or os.path.islink(git_dir) or not os.path.isdir(git_dir):
        return None
    real = _canon(git_dir)
    if real is None:
        return None
    real = Path(real)
    if real.parent.name != 'worktrees':
        return None
    common = real.parent.parent
    back, commondir = _read(real / 'gitdir'), _read(real / 'commondir')
    if not back or not commondir:
        return None
    back_dot_git, linked_common = _join(real, back), _join(real, commondir)
    if back_dot_git is None or linked_common is None or os.path.basename(back_dot_git) != '.git':
        return None
    checkout = _canon(os.path.dirname(back_dot_git))
    if checkout is None or checkout != _canon(root) or _canon(linked_common) != str(common):
        return None
    main_root = os.path.dirname(os.path.dirname(os.path.dirname(git_dir)))
    main_dot_git = Path(main_root) / '.git'
    if main_dot_git.is_dir():
        main_git_dir = str(main_dot_git)
    elif main_dot_git.is_file():
        main_git_dir = _gitdir(main_dot_git)
    else:
        return None
    if _canon(main_git_dir) != str(common):
        return None
    return main_root


SYSTEM_CONFIG = '/etc/codex/config.toml'
MANAGED_CONFIG = '/etc/codex/managed_config.toml'
SYSTEM_FILES = (SYSTEM_CONFIG, '/etc/codex/requirements.toml', MANAGED_CONFIG)
MANAGED_PREFERENCES = '/Library/Managed Preferences'


def _managed():
    """True when Codex can load system or administrator config: /etc/codex files, or on macOS forced (MDM)
    preferences for com.openai.codex (config/src/loader/macos.rs), stored for all users or for one user.
    Other systems (Windows) are not modelled, so they count as managed."""
    return any(os.path.exists(path) for path in SYSTEM_FILES) or _unmodelled_managed_config()


def _unmodelled_managed_config():
    """Detect macOS MDM preferences (config/src/loader/macos.rs) or an unsupported platform."""
    if sys.platform == 'darwin':
        name = 'com.openai.codex.plist'
        try:
            return any(os.path.exists(os.path.join(base, name)) for base in
                       [MANAGED_PREFERENCES, *(entry.path for entry in os.scandir(MANAGED_PREFERENCES) if entry.is_dir())])
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return not sys.platform.startswith('linux')
LAYER_KEYS = ('sandbox_mode', 'profile', 'default_permissions', 'permissions', 'projects', 'project_root_markers')


def _layers(folders, home):
    """Every .codex/config.toml Codex can load for these start folders: each folder and all its parents
    (config/src/loader/mod.rs: "parent directories up to root"). CODEX_HOME itself is the user layer, not a project one."""
    seen = set()
    for folder in folders:
        for place in [Path(folder), *Path(folder).parents]:
            layer = place / '.codex'
            if layer in seen or layer.resolve() == home:
                continue
            seen.add(layer)
            yield layer / 'config.toml'


def codex_writable(args, cwd=None, assume=None):
    """True only when Codex will surely start with a writable workspace. Codex 0.166 exits with code 1 when it gets
    --add-dir and the start folder is not writable (tui/src/additional_dirs.rs). The order follows Codex:
    sandbox flags, then -c sandbox_mode, then config.toml, then the folder trust (core/src/config/permissions.rs:
    a [projects] entry with any trust_level gives the workspace profile; no entry gives read-only).
    Anything this does not model (profiles, permission profiles, system or managed config, project config layers)
    gives False: Codex then starts without --add-dir, as before.
    assume: a [projects] key to treat as trusted, to ask "would trusting this key make Codex writable?" (codex_trust.py)."""
    if '--' in args:
        args = args[:args.index('--')]  # after --, everything is the prompt
    if any(arg.partition('=')[0] in UNKNOWN_FLAGS for arg in args):
        return False  # Codex stops with "unexpected argument"
    if any(arg.partition('=')[0] in UNMODELLED_FLAGS for arg in args):
        return False
    if _managed():
        return False  # system requirements apply after any sandbox choice, even --yolo or --sandbox
    # resume and fork: a True here adds --add-dir, and a non-empty --add-dir makes the resume send the current
    # permissions, so a saved session's own profile is not restored (tui/src/resume_permissions.rs:33,
    # app_server_session.rs). The exit-1 check uses the startup config (tui/src/startup_orchestration.rs:863).
    for i, arg in enumerate(args):
        if arg in SUBCOMMANDS:
            # A subcommand's own sandbox flags replace the ones before it, --yolo included
            # (utils/cli/src/shared_options.rs). Not modelled: any such flag after it gives False.
            if any(a in WRITE_FLAGS or a.startswith(SANDBOX_PREFIXES) for a in args[i + 1:]):
                return False
            break
    overrides = [value.partition('=') for value in _option(args, '-c', '--config')]
    if any(key.strip() in ('default_permissions', 'profile') or key.strip().startswith('permissions.')
           for key, _, _ in overrides):
        return False  # a permission profile wins over sandbox flags and sandbox_mode, whatever the order
    sandboxes = _option(args, '-s', '--sandbox')
    if any(_mode(value) is UNSURE for value in sandboxes):
        return False  # clap rejects a sandbox value that is not an exact mode, even with --yolo
    if any(arg in WRITE_FLAGS for arg in args):
        return True
    for value in reversed(sandboxes):
        return _mode(value) is True
    if _option(args, '-p', '--profile'):
        return False
    for key, _, setting in reversed(overrides):
        if key.strip() == 'sandbox_mode':
            return _mode(_override(setting)) is True
    if any(key.strip().split('.')[0].strip('"\'') in ('projects', 'project_root_markers') for key, _, _ in overrides):
        return False  # a -c project entry can shadow the trusted root with an untrusted one
    import paths
    home = Path(os.environ.get('CODEX_HOME') or paths.CODEX)
    config = _toml(home / 'config.toml')
    if config is None or any(key in config for key in ('profile', 'default_permissions', 'permissions',
                                                       'project_root_markers')):
        return False
    if assume is not None:
        known = config.get('projects') if isinstance(config.get('projects'), dict) else {}
        entry = known.get(assume) if isinstance(known.get(assume), dict) else {}
        config = {**config, 'projects': {**known, assume: {**entry, 'trust_level': 'trusted'}}}
    folder = (_option(args, '-C', '--cd') or [None])[-1]
    start = cwd or os.getcwd()
    if folder:
        raw = os.path.join(start, folder)
        if '..' in raw.split('/') or _canon(raw) != os.path.normpath(raw):
            return False  # Codex keeps a logical --cd path through symlinks and '..'; not modelled
    folder = os.path.normpath(os.path.join(start, folder) if folder else start)
    real = os.path.realpath(folder)
    root = _repo_root(folder)
    for path in _layers([folder, real], home.resolve()):
        layer = _toml(path)
        if layer is None or any(key in layer for key in LAYER_KEYS):
            return False
    if 'sandbox_mode' in config:
        return _mode(config['sandbox_mode']) is True
    known = config.get('projects') if isinstance(config.get('projects'), dict) else {}
    # Codex takes the first entry it finds; both spellings of the start folder come before the repository root.
    own = [known[key] for key in dict.fromkeys([folder, real]) if key in known]
    # For the root too, Codex tries the canonical spelling before the original (config/src/project_trust.rs).
    entries = own or [known[key] for key in dict.fromkeys([_canon(root), root]) if key and key in known]
    return bool(entries) and all(isinstance(entry, dict) and entry.get('trust_level') in ('trusted', 'untrusted')
                                 for entry in entries)


def codex_folders(args=()):
    """--add-dir for the backlog, work item and reminder folders: the sandbox blocks `tether backlog`, `tether item` and reminder writes
    without them. Given even when the user has own instructions, but never to a read-only Codex: it exits on them."""
    import backlog
    import paths
    if not codex_writable(list(args)):
        return []
    out = []
    for place in (backlog.folder(), paths.items(), paths.reminders()):
        place.mkdir(mode=0o700, parents=True, exist_ok=True)
        out += ['--add-dir', str(place)]
    return out


def _cli_discovery_layer(options):
    """Build the discovery and TUI fields as config/src/overrides.rs apply_toml_override does."""
    layer = {}
    for arg in _option(options, '-c', '--config'):
        key, sep, value = arg.partition('=')
        parts = key.strip().split('.')
        if not sep:
            continue
        if parts == ['profile']:
            return None
        if parts[0] not in ('projects', 'project_root_markers', 'tui'):
            continue
        table = layer
        for part in parts[:-1]:
            if not isinstance(table.get(part), dict):
                table[part] = {}
            table = table[part]
        table[parts[-1]] = _override(value)
    return layer


def _merge_tables(base, overlay):
    """Merge the discovery tables like config/src/merge.rs merge_toml_values."""
    merged = {**base, **overlay}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge_tables(base[key], value)
    return merged


def _has_project_marker(folder, marker):
    path = _join(folder, marker)
    return path is not None and os.path.exists(path) and (
        marker != '.git' or not os.path.isdir(path) or os.path.exists(os.path.join(path, 'HEAD')))


def _trust_keys(path):
    if path is not None:
        raw = str(path)
        real = _canon(raw)
        if real and real != raw:
            yield real
        yield raw


def _project_trust(config):
    """Read typed trust decisions as config/src/loader/mod.rs project_trust_context does."""
    entries = config.get('projects', {})
    if not isinstance(entries, dict) or any(
        not isinstance(entry, dict) or ('trust_level' in entry and entry['trust_level'] not in ('trusted', 'untrusted'))
        for entry in entries.values()
    ):
        return None
    return {key: entry['trust_level'] for key, entry in entries.items() if 'trust_level' in entry}


def _trusted_project_configs(cwd, home, markers, trust):
    """Port config/src/loader/mod.rs discover_project_root, decision_for_dir and discover_project_layers.
    Resolve trust for every layer before the caller reads any project config."""
    folders = [cwd, *cwd.parents]
    root_index = next((i for i, folder in enumerate(folders)
                       if any(_has_project_marker(folder, marker) for marker in markers)), 0)
    inherited = [*_trust_keys(folders[root_index]), *_trust_keys(_repo_root(cwd, logical=True))]
    home_real = _canon(home)
    for folder in reversed(folders[:root_index + 1]):
        level = next((trust[key] for key in [*_trust_keys(folder), *inherited] if key in trust), None)
        if level != 'trusted':
            continue
        layer = folder / '.codex'
        if os.path.isdir(layer) and layer != home and not (home_real and _canon(layer) == home_real):
            yield layer / 'config.toml'


def _instruction_cwd(options, cwd):
    """Resolve --cd like utils/absolute-path/src/lib.rs canonicalize_existing_preserving_symlinks."""
    base = (_canon(cwd) or cwd) if cwd is not None else os.getcwd()
    cd = (_option(options, '-C', '--cd') or [''])[-1]
    logical = _join(base, cd)
    real = _canon(os.path.join(base, cd))
    if not logical or not real:
        return None
    folder = Path(logical)
    if logical != real and any(parent.is_symlink() for parent in [folder, *folder.parents][:-2]):
        return folder  # Keep nested symlinks; canonicalize top-level aliases such as macOS /tmp.
    return Path(real)


CODEX_SESSION_VALUE_FLAGS = {'-a', '-c', '-i', '-m', '-p', '-s', '--add-dir', '--ask-for-approval', '--config',
                             '--disable', '--enable', '--image', '--local-provider', '--model', '--profile',
                             '--remote-auth-token-env', '--sandbox'}


def _explicit_session_id(args):
    """Find the first session positional in cli/src/main.rs ResumeCommand or ForkCommand."""
    command = session = None
    positional = images = False
    tokens = iter(args)
    for arg in tokens:
        if not positional and arg.startswith('-') and arg != '-':
            if arg == '--':
                if command is None:
                    return None
                positional = True
                images = False
                continue
            flag = arg.partition('=')[0] if arg.startswith('--') else arg[:2]
            if flag in ('-C', '--cd', '--last', '--remote', '--worktree'):
                return None
            if arg == flag and flag in CODEX_SESSION_VALUE_FLAGS:
                next(tokens, None)
            images = flag in ('-i', '--image')  # SharedCliOptions takes one or more image paths.
        elif not images:
            if command is None:
                if arg not in ('resume', 'fork'):
                    return None
                command = arg
            elif session is None:
                session = arg
    if session is not None:
        try:
            parsed = UUID(session)
        except ValueError:
            return None
        if session.lower() in (str(parsed), parsed.hex, '{' + str(parsed) + '}', parsed.urn):
            return str(parsed)
    return None


def _raise_walk_error(error):
    raise error


def _rollout_cwd(home, session_id):
    """Find one rollout and apply the cwd fields in state/src/extract.rs in record order."""
    prefix = 'rollout-'
    suffix = f'-{session_id}.jsonl'
    path = None
    for directory in (home / 'sessions', home / 'archived_sessions'):
        if not directory.exists():
            continue
        for folder, _, filenames in os.walk(directory, onerror=_raise_walk_error):
            for name in filenames:
                if name.startswith(prefix) and name[len(prefix):].lower().endswith(suffix):
                    if path is not None:
                        return None
                    path = Path(folder) / name
    if path is None or not path.is_file():
        return None
    cwd = None
    with path.open('rb') as stream:
        for line in stream:
            if not any(kind in line for kind in (b'session_meta', b'thread_settings_applied', b'turn_context')):
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            payload = item.get('payload') if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            kind = item.get('type')
            if kind == 'event_msg' and payload.get('type') == 'thread_settings_applied':
                settings = payload.get('thread_settings')
                if isinstance(settings, dict) and isinstance(settings.get('cwd'), str):
                    cwd = settings['cwd']
            elif kind == 'session_meta' or (kind == 'turn_context' and not cwd):
                value = payload.get('cwd')
                if isinstance(value, str) and value:
                    cwd = value
    return cwd or None


def _project_instruction_config(base, start, home, markers, trust):
    config = base
    for path in list(_trusted_project_configs(start, home, markers, trust)):
        layer = _toml(path)
        if layer is None:
            return None
        config = _merge_tables(config, {k: layer[k] for k in ('developer_instructions', 'tui') if k in layer})
    return config


def _instruction_config(options, cwd, args):
    """Select local instructions using config/src/loader/mod.rs load_config_layers_state.
    Enterprise cloud config bundles are not readable locally and are not modelled."""
    import paths
    home = Path(os.environ.get('CODEX_HOME') or paths.CODEX).absolute()
    system, managed = _toml(SYSTEM_CONFIG), _toml(MANAGED_CONFIG)
    if system is None or managed is None or 'developer_instructions' in managed:
        return None
    user, profile = {}, {}
    if '--ignore-user-config' not in options:
        user = _toml(home / 'config.toml')
        if user is None or isinstance(user.get('profile'), str):
            return None
        name = (_option(options, '-p', '--profile') or [''])[-1]
        if name:
            # ProfileV2Name (protocol/src/config_types.rs) accepts plain ASCII names only.
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                return None
            profile = _toml(home / f'{name}.config.toml')
            if profile is None:
                return None

    cli = _cli_discovery_layer(options)
    if cli is None:
        return None
    discovery = {}
    for layer in (system, user, profile, cli, managed):
        discovery = _merge_tables(discovery, {k: layer[k] for k in ('projects', 'project_root_markers') if k in layer})
    markers = discovery.get('project_root_markers', ['.git'])
    if not isinstance(markers, list) or not all(isinstance(marker, str) for marker in markers):
        return None

    base = {}
    for layer in (system, user, profile):
        base = _merge_tables(base, {k: layer[k] for k in ('developer_instructions', 'tui') if k in layer})
    if _unmodelled_managed_config() or '--worktree' in options:
        return base  # The final checkout of --worktree is chosen by Codex after startup.
    trust = _project_trust(discovery)
    if trust is None:
        return None  # project_trust_context cannot deserialize the projects table.
    start = _instruction_cwd(options, cwd)
    if start is None:
        return base
    config = _project_instruction_config(base, start, home, markers, trust)
    if config is None:
        return None
    try:
        startup = config
        for layer in (cli, managed):
            if 'tui' in layer:
                startup = _merge_tables(startup, {'tui': layer['tui']})
        tui = startup.get('tui')
        # With no mode, tui/src/session_resume.rs may prompt after exec. Keep the launch config.
        if isinstance(tui, dict) and tui.get('resume_cwd') == 'session':
            session_id = _explicit_session_id(args)
            saved_cwd = _rollout_cwd(home, session_id) if session_id else None
            # core/src/config/mod.rs uses an absolute fallback_cwd without canonicalizing it.
            folder = _join(start, saved_cwd) if saved_cwd else None
            if folder and Path(folder) != start:
                session = Path(folder)
                if not session.is_dir():
                    return base
                resolved = _project_instruction_config(base, session, home, markers, trust)
                if resolved is not None and isinstance(resolved.get('developer_instructions', ''), str):
                    return resolved
    except Exception:
        pass  # A failed session lookup must preserve the launch instructions and Tether's rules.
    return config


def codex_args(pid, args, group=None, cwd=None):
    options = args[:args.index('--')] if '--' in args else args
    if any(arg in CODEX_PLAIN for arg in options):
        return args
    import autonomy
    import codex_level
    # Children inherit the root developer_instructions, and Codex rebuilds them after compaction. Do not set
    # features.multi_agent_v2.subagent_developer_instructions: it would replace what children inherit.
    block = autonomy.codex_standing_rules(autonomy.resolve(pid), codex_level.resolve(pid))
    folder_args = codex_folders(args) if pid else []

    cli_instruction = None
    for i, prefix in reversed(list(_option_locations(options, '-c', '--config'))):
        key, sep, value = options[i][len(prefix):].partition('=')
        if sep and key.strip() == 'developer_instructions':
            user = _override(value)
            if not isinstance(user, str):
                return [*folder_args, *args]
            cli_instruction = i, prefix
            break
    else:
        config = _instruction_config(options, cwd, args)
        if config is None:
            return [*folder_args, *args]
        user = config.get('developer_instructions')
        if user is not None and not isinstance(user, str):
            return [*folder_args, *args]

    parts = [] if user is None else [user]
    if pid:
        import backlog
        import projects
        parts.append(projects.render(pid, 'codex', group=group, backlog_project=backlog.project_name()))
        record_revision(pid, group)
    browser_args = []
    if os.environ.get('TETHER_CDP_URL'):
        from agent_browser_cli import note
        parts.append(note())
        browser_args = codex_browser_env_args()
    parts.append(block)
    override = 'developer_instructions=' + toml_string('\n\n'.join(parts))
    if cli_instruction is not None:
        # Replace the last CLI instruction in place so its scope and precedence are preserved on resume/fork.
        i, prefix = cli_instruction
        args = list(args)
        args[i] = prefix + override
        return [*folder_args, *browser_args, *args]
    return ['-c', override, *folder_args, *browser_args, *args]


def codex_browser_env_args():
    """Pass Tether's connection to tool shells and configured Playwright MCP servers.

    Codex's core inheritance drops these variables. Set only Tether's handoff
    values, leaving the user's inheritance policy and other explicit values intact.
    """
    overrides = []
    for name in ('TETHER_CDP_URL', 'PLAYWRIGHT_MCP_CDP_ENDPOINT', 'TMUX', 'TMUX_PANE'):
        value = os.environ.get(name)
        if value:
            overrides.extend(['-c', 'shell_environment_policy.set.' + name + '=' + toml_string(value)])
    try:
        import tomllib
    except ImportError:
        return overrides  # Python 3.10 can still pass the shell handoff.
    import paths
    try:
        config = tomllib.loads((Path(os.environ.get('CODEX_HOME') or paths.CODEX) / 'config.toml').read_text())
    except FileNotFoundError:
        return overrides
    for name, server in config.get('mcp_servers', {}).items():
        if not playwright_server(server):
            continue
        env_vars = list(server.get('env_vars', []))
        variable = 'PLAYWRIGHT_MCP_CDP_ENDPOINT'
        if has_browser_env(env_vars):
            continue
        env_vars.append(variable)
        # Preserve both plain names and Codex's {name, source} entries.
        values = [toml_string(value) if isinstance(value, str) else
                  '{' + ', '.join(toml_string(key) + ' = ' + toml_string(val) for key, val in value.items()) + '}'
                  for value in env_vars]
        overrides.extend(['-c', 'mcp_servers.' + toml_string(name) + '.env_vars=[' + ', '.join(values) + ']'])
    return overrides


def launch(agent, args):
    """Replace this process with the agent. The project args are added only when every step works."""
    real = shutil.which(agent)
    if not real:
        sys.exit(f'tether-{agent}: {agent} is not on PATH.')
    final = args
    if not os.environ.get(GUARD):
        try:
            sys.path.insert(0, str(HERE))
            pid = current_project()
            if pid or agent == 'codex':
                group = current_group() if pid else None
                final = (claude_args if agent == 'claude' else codex_args)(pid, list(args), group)
            else:
                final = claude_plain_args(list(args))
        except Exception as exc:  # noqa: BLE001 - any failure: start the plain agent
            print(f'tether-{agent}: project not loaded ({exc.__class__.__name__}). Starting plain {agent}.', file=sys.stderr)
            final = args
    os.environ[GUARD] = '1'
    os.execv(real, [agent, *final])


MOVED_NOTE = ('This tab moved here from another project. Until it restarts, it may not have file access to this '
              'project folder.')


def live_project(pid):
    """The project of the session the pane is in now, else pid (outside tmux, or any failure). The hook command holds
    the project the tab started in; a pane moved to another session (tmux_merge.py) must get the new project's notes
    at its next start, clear or compact (B326)."""
    try:
        return current_project() or pid
    except Exception:  # noqa: BLE001 - a tmux failure keeps the project the tab started with
        return pid


def hook(pid):
    try:
        sys.stdin.read()
    except OSError:
        pass
    try:
        sys.path.insert(0, str(HERE))
        import backlog
        import projects
        live = live_project(pid)
        group = current_group()
        text = projects.render(live, 'claude', group=group, backlog_project=backlog.project_name())
        if live == pid:
            record_revision(pid, group)
        elif text:
            # The stale mark stays (projects.marks): file access still holds the old project folder until a restart.
            text += '\n\n' + MOVED_NOTE
    except Exception:  # noqa: BLE001 - a hook must never stop a session
        return
    if text:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}))


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'hook':
        hook(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] in ('claude', 'codex'):
        launch(sys.argv[1], sys.argv[2:])
    else:
        sys.exit(__doc__)
