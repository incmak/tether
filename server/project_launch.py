#!/usr/bin/env python3
"""Start Claude Code or Codex with the tmux session's Tether project (projects.py), or print the project text for
Claude Code's SessionStart hook.

  project_launch.py claude [args...]   run claude, with the project folder and the hook when the session has a project
  project_launch.py codex [args...]    run codex, with the backlog folder and the project text as developer_instructions
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


def claude_args(pid, args, group=None):
    if any(arg in CLAUDE_PLAIN or arg == '--settings' or arg.startswith('--settings=') for arg in args):
        return args
    import projects
    folder = projects.bundle(pid)
    (folder / 'notes').mkdir(mode=0o700, parents=True, exist_ok=True)
    hook = f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} hook {pid}'
    settings = {'hooks': {'SessionStart': [{'matcher': 'startup|resume|clear|compact',
                                            'hooks': [{'type': 'command', 'command': hook, 'timeout': 10}]}]}}
    record_revision(pid, group)
    folders = [str(folder), str(projects.bundle(group['pid']))] if group else [str(folder)]
    # --add-dir takes several folders: --settings after it ends the list, so a prompt in args stays a prompt.
    return ['--add-dir', *folders, '--settings', json.dumps(settings), *args]


def codex_has_own_instructions():
    import paths
    try:
        text = (Path(os.environ.get('CODEX_HOME') or paths.CODEX) / 'config.toml').read_text(errors='replace')
    except OSError:
        return False
    return re.search(r'^\s*developer_instructions\s*=', text, re.M) is not None


WRITE_FLAGS = {'--full-auto', '--dangerously-bypass-approvals-and-sandbox', '--yolo'}
UNSURE = object()


def _option(args, short, long):
    """Every value of an option, in order: `-s X`, `-sX`, `--sandbox X` and `--sandbox=X`."""
    values = []
    for i, arg in enumerate(args):
        if arg in (short, long) and i + 1 < len(args):
            values.append(args[i + 1])
        elif long and arg.startswith(long + '='):
            values.append(arg[len(long) + 1:])
        elif short and arg.startswith(short) and len(arg) > len(short) and not arg.startswith('--'):
            values.append(arg[len(short):])
    return values


def _mode(value):
    """True for a writable sandbox mode, False for read-only, UNSURE for anything else."""
    value = value.strip().strip('"\'') if isinstance(value, str) else value
    if value == 'read-only':
        return False
    return True if value in ('workspace-write', 'danger-full-access') else UNSURE


def _toml(path):
    try:
        import tomllib
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (ImportError, OSError, ValueError):
        return None


def _repo_root(cwd):
    """The trust root Codex uses: the main checkout of a repository, also for a linked worktree."""
    try:
        out = subprocess.run(['git', '-C', cwd, 'rev-parse', '--path-format=absolute', '--show-toplevel',
                              '--git-common-dir'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.splitlines() if out.returncode == 0 else []
    if len(lines) < 2:
        return None
    common = Path(lines[1])
    return str(common.parent) if common.name == '.git' else lines[0]


SYSTEM_FILES = ('/etc/codex/config.toml', '/etc/codex/requirements.toml', '/etc/codex/managed_config.toml')
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


def codex_writable(args, cwd=None):
    """True only when Codex will surely start with a writable workspace. Codex 0.166 exits with code 1 when it gets
    --add-dir and the start folder is not writable (tui/src/additional_dirs.rs). The order follows Codex:
    sandbox flags, then -c sandbox_mode, then config.toml, then the folder trust (core/src/config/permissions.rs:
    a [projects] entry with any trust_level gives the workspace profile; no entry gives read-only).
    Anything this does not model (profiles, permission profiles, system or managed config, project config layers)
    gives False: Codex then starts without --add-dir, as before."""
    if '--' in args:
        args = args[:args.index('--')]  # after --, everything is the prompt
    if any(os.path.exists(path) for path in SYSTEM_FILES):
        return False  # system requirements apply after any sandbox choice, even --yolo or --sandbox
    if any(arg in WRITE_FLAGS for arg in args):
        return True
    for value in reversed(_option(args, '-s', '--sandbox')):
        return _mode(value) is True
    if _option(args, '-p', '--profile'):
        return False
    overrides = [value.partition('=') for value in _option(args, '-c', '--config')]
    if any(key.strip() in ('default_permissions', 'profile') or key.strip().startswith('permissions.')
           for key, _, _ in overrides):
        return False  # Codex prefers permission profiles over sandbox_mode in the same layer, whatever the order
    for key, _, setting in reversed(overrides):
        if key.strip() == 'sandbox_mode':
            return _mode(setting) is True
    if any(key.strip().split('.')[0].strip('"\'') in ('projects', 'project_root_markers') for key, _, _ in overrides):
        return False  # a -c project entry can shadow the trusted root with an untrusted one
    import paths
    home = Path(os.environ.get('CODEX_HOME') or paths.CODEX)
    config = _toml(home / 'config.toml')
    if config is None or any(key in config for key in ('profile', 'default_permissions', 'permissions',
                                                       'project_root_markers')):
        return False
    folder = (_option(args, '-C', '--cd') or [None])[-1]
    start = cwd or os.getcwd()
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
    entries = own or ([known[root]] if root and root in known else [])
    return bool(entries) and all(isinstance(entry, dict) and entry.get('trust_level') in ('trusted', 'untrusted')
                                 for entry in entries)


def codex_folders(args=()):
    """--add-dir for the backlog and work item folders: the sandbox blocks `tether backlog` and `tether item` writes
    without them. Given even when the user has own instructions, but never to a read-only Codex: it exits on them."""
    import backlog
    import paths
    if not codex_writable(list(args)):
        return []
    out = []
    for place in (backlog.folder(), paths.items()):
        place.mkdir(mode=0o700, parents=True, exist_ok=True)
        out += ['--add-dir', str(place)]
    return out


def codex_args(pid, args, group=None):
    if any(arg in CODEX_PLAIN for arg in args):
        return args
    folder_args = codex_folders(args) if pid else []
    if any('developer_instructions' in arg for arg in args) or codex_has_own_instructions():
        return [*folder_args, *args]
    text = ''
    if pid:
        import backlog
        import projects
        text = projects.render(pid, 'codex', group=group, backlog_project=backlog.project_name())
        record_revision(pid, group)
    if os.environ.get('TETHER_CDP_URL'):
        from agent_browser_cli import note
        text = text + '\n\n' + note() if text else note()
        args = [*codex_browser_env_args(), *args]
    return ['-c', 'developer_instructions=' + toml_string(text), *folder_args, *args] if text else args


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
        except Exception as exc:  # noqa: BLE001 - any failure: start the plain agent
            print(f'tether-{agent}: project not loaded ({exc.__class__.__name__}). Starting plain {agent}.', file=sys.stderr)
            final = args
    os.environ[GUARD] = '1'
    os.execv(real, [agent, *final])


def hook(pid):
    try:
        sys.stdin.read()
    except OSError:
        pass
    try:
        sys.path.insert(0, str(HERE))
        import backlog
        import projects
        group = current_group()
        text = projects.render(pid, 'claude', group=group, backlog_project=backlog.project_name())
        record_revision(pid, group)
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
