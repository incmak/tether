#!/usr/bin/env python3
"""Plan or install Tether's user services, without root or interactive prompts."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DARWIN = sys.platform == 'darwin'
TERMUX = not DARWIN and (bool(os.environ.get('TERMUX_VERSION')) or os.environ.get('PREFIX', '').startswith('/data/data/com.termux/'))
UNIT_NAMES = ('tether.service', 'tether-terminal.service', 'tether-voice.service')
UPDATE_UNITS = ('tether-update.service', 'tether-update.timer')
LEGACY_UNITS = ('mobile-tmux.service', 'mobile-tmux-terminal.service', 'mobile-tmux-whisper.service')
VERSION = 1


def run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=600, **kwargs)


def probe(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def occupied_ports():
    """Linux exposes sockets across users, including wildcard and IPv6 binds."""
    if DARWIN:
        result = probe('lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-F', 'n')
        if not result or result.returncode:
            return set()
        return {int(line.rsplit(':', 1)[1]) for line in result.stdout.splitlines()
                if line.startswith('n') and ':' in line and line.rsplit(':', 1)[1].isdigit()}
    ports = set()
    for name in ('tcp', 'tcp6'):
        for row in Path('/proc/net', name).read_text().splitlines()[1:]:
            fields = row.split()
            # Only listeners hold a port; TIME_WAIT rows from closed client
            # connections must not block a restart on the same port.
            if fields[3] == '0A':
                ports.add(int(fields[1].split(':')[1], 16))
    return ports


def owned_port(unit, port):
    """Accept an occupied port only when this user's installed service owns it."""
    if DARWIN:
        result = probe('launchctl', 'print', f'gui/{os.getuid()}/{launchd_label(unit)}')
        if not result or result.returncode:
            return False
        pid = next((line.partition('=')[2].strip() for line in result.stdout.splitlines()
                    if line.strip().startswith('pid = ')), '')
        if not pid.isdigit() or int(pid) == 0:
            return False
        result = probe('lsof', '-nP', '-a', '-p', pid, f'-iTCP:{port}', '-sTCP:LISTEN', '-F', 'p')
        return bool(result and result.returncode == 0 and
                    any(line.startswith('p') for line in result.stdout.splitlines()))
    if TERMUX:
        try:
            pid = int((service_units_dir(Path.home()) / runit_name(unit) / 'supervise/pid').read_text().strip())
        except (OSError, ValueError):
            return False
    else:
        result = probe('systemctl', '--user', 'show', unit, '--property=MainPID', '--value')
        if not result or result.returncode or not result.stdout.strip().isdigit():
            return False
        pid = int(result.stdout.strip())
    if not pid:
        return False
    try:
        directory = Path('/proc', str(pid))
        if directory.stat().st_uid != os.getuid():
            return False
        sockets = set()
        for entry in (directory / 'fd').iterdir():
            try:
                sockets.add(entry.readlink().name)
            except FileNotFoundError:
                # Descriptors can close while the service continues to run.
                continue
        for name in ('tcp', 'tcp6'):
            try:
                rows = Path('/proc/net', name).read_text().splitlines()[1:]
            except OSError:
                if TERMUX and name == 'tcp':
                    return True
                raise
            for row in rows:
                fields = row.split()
                if int(fields[1].split(':')[1], 16) == port and f'socket:[{fields[9]}]' in sockets:
                    return True
    except OSError:
        pass
    except (ValueError, IndexError):
        if not TERMUX:
            raise
    return False


def free_port(port, taken, reserved, unit=None, legacy_unit=None):
    if not 1 <= port <= 65535 or port in reserved:
        return False
    if port in taken:
        return bool((unit and owned_port(unit, port)) or
                    (legacy_unit and owned_port(legacy_unit, port)))
    # Detect reservations that have not reached listen(), and wildcard conflicts.
    try:
        with socket.socket() as sock:
            # Match the server, which also reuses addresses held in TIME_WAIT.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', port))
        if DARWIN:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        return True
    except OSError:
        if TERMUX:
            return bool((unit and owned_port(unit, port)) or
                        (legacy_unit and owned_port(legacy_unit, port)))
        return False


def pick_port(start, taken, reserved):
    for port in range(start, 65536):
        if free_port(port, taken, reserved):
            return port
    raise ValueError(f'No free port from {start} through 65535. Choose --port below {start}.')


def quote(value, executable=False):
    """Quote systemd words, including specifier and environment expansion."""
    validate_unit_value(value)
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if executable:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def validate_unit_value(value):
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in str(value)):
        raise ValueError('Systemd unit value contains a control character.')


def legacy_settings(unit, units_dir):
    """Read effective legacy settings, with a unit-file fallback."""
    environment = {}
    command = ''
    path = units_dir / unit
    sources = [path.read_text() if path.is_file() else '']
    result = probe('systemctl', '--user', 'show', unit, '--property=Environment,ExecStart')
    if result and result.returncode == 0:
        sources.append(result.stdout)
    for source in sources:
        for line in source.splitlines():
            key, sep, value = line.strip().partition('=')
            if not sep:
                continue
            if key == 'Environment':
                for word in shlex.split(value):
                    name, sep, setting = word.partition('=')
                    if sep:
                        environment[name] = setting
            elif key == 'ExecStart':
                command = value
    port_key = {LEGACY_UNITS[0]: 'MOBILE_TMUX_PORT',
                LEGACY_UNITS[2]: 'MOBILE_TMUX_TRANSCRIBE_PORT'}.get(unit)
    port = environment.get(port_key)
    if unit == LEGACY_UNITS[1]:
        # systemctl wraps ExecStart in metadata; argv[] holds the ttyd command.
        if 'argv[]=' in command:
            command = command.split('argv[]=', 1)[1].split(' ; ', 1)[0]
        words = shlex.split(command)
        if '-p' in words and words.index('-p') + 1 < len(words):
            port = words[words.index('-p') + 1]
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = None
    return port, environment


def unit_text(description, command, environment, *, oneshot=False):
    validate_unit_value(description)
    env = ''.join(f'Environment={quote(key + "=" + str(value))}\n' for key, value in sorted(environment.items()))
    service = 'Type=oneshot\n' if oneshot else ''
    restart = '' if oneshot else 'Restart=always\nRestartSec=3\n'
    return (f'[Unit]\nDescription={description}\nAfter=network.target\n\n[Service]\n{service}'
            f'ExecStart={" ".join(quote(word, executable=True) for word in command)}\n{env}'
            f'{restart}\n[Install]\nWantedBy=default.target\n')


def launchd_label(name):
    suffix = 'chat' if name == 'tether.service' else name.removeprefix('tether-').removesuffix('.service')
    return 'local.tether.' + suffix


def runit_name(name):
    return name.removesuffix('.service')


def service_units_dir(home):
    if TERMUX:
        return Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr')) / 'var/service'
    return home / ('Library/LaunchAgents' if DARWIN else '.config/systemd/user')


def unit_path(units_dir, name):
    if TERMUX:
        return units_dir / runit_name(name) / 'run'
    return units_dir / (launchd_label(name) + '.plist' if DARWIN else name)


def service_file(name, description, command, environment, oneshot=False):
    if TERMUX:
        prefix = Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr'))
        words = (['env', *(key + '=' + str(value) for key, value in sorted(environment.items()))]
                 if environment else []) + [str(word) for word in command]
        return (f'#!{prefix}/bin/sh\n# {description} (written by Tether install.py)\n'
                f'exec 2>&1\nexec {shlex.join(words)}\n')
    if not DARWIN:
        return unit_text(description, command, environment, oneshot=oneshot)
    label = launchd_label(name)
    log = str(Path.home() / 'Library/Logs/tether' / (label + '.log'))
    data = dict(Label=label, ProgramArguments=[str(word) for word in command],
                StandardOutPath=log, StandardErrorPath=log)
    if environment:
        data['EnvironmentVariables'] = {key: str(value) for key, value in environment.items()}
    if oneshot:
        data.update(RunAtLoad=False, StartCalendarInterval={'Hour': 4, 'Minute': 17})
    else:
        data.update(RunAtLoad=True, KeepAlive=True, ThrottleInterval=3)
    return plistlib.dumps(data).decode()


def needs_build(repo):
    dist = repo / 'ui/dist'
    if not (dist / 'index.html').is_file():
        return True
    stamp = max(path.stat().st_mtime_ns for path in dist.rglob('*') if path.is_file())
    return any(path.stat().st_mtime_ns > stamp for path in (repo / 'ui/src').rglob('*') if path.is_file())


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--apply', action='store_true')
    result.add_argument('--json', action='store_true', help='Print a deterministic JSON plan')
    result.add_argument('--uninstall', action='store_true')
    result.add_argument('--replace-legacy', action='store_true')
    result.add_argument('--port', type=int)
    result.add_argument('--host')
    result.add_argument('--auth', choices=('token', 'proxy'))
    result.add_argument('--updates', choices=('notify', 'auto', 'off'))
    result.add_argument('--release-current', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--defer-restart', action='store_true', help=argparse.SUPPRESS)
    result.add_argument('--ask-bridge-wait', type=int, help='Time for the phone to start answering, in seconds (5..120; default 30)')
    for name in ('terminal', 'voice', 'push', 'images', 'hooks', 'ask-bridge', 'resources', 'build-ui'):
        result.add_argument('--' + name, action=argparse.BooleanOptionalAction, default=None)
    return result


def claude_hook_entries(repo):
    # These events mutate one lifecycle state, so they must finish in event order.
    # SubagentStop only removes a unique id; it cannot revive a turn or an agent.
    events = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
              'PostToolUseFailure', 'PermissionRequest', 'PermissionDenied',
              'Notification', 'Stop', 'StopFailure', 'SubagentStart', 'SubagentStop',
              'SessionEnd', 'PostModelSwitch')
    command = shlex.join([sys.executable, str(repo / 'server/claude_hooks.py')])
    return {event: dict({'matcher': 'permission_prompt|idle_prompt'} if event == 'Notification' else {},
                       hooks=[dict(type='command', command=command,
                                   **({'async': True} if event == 'SubagentStop' else {'timeout': 1}))])
            for event in events}


def ask_bridge_entries(repo, notice=30):
    return {'PreToolUse': {'matcher': 'AskUserQuestion', 'hooks': [{
        'type': 'command', 'command': shlex.join([sys.executable, str(repo / 'server/ask_bridge.py')]),
        'timeout': 600, 'statusMessage': f'Question sent to Tether · back here in {notice} s unless answered there'}]}}


def merge_claude_hooks(data, entries, remove=False):
    """Append our groups without changing existing order; remove only our command."""
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        return None
    hooks = dict(data.get('hooks', {}))
    for event, entry in entries.items():
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or any(not isinstance(group, dict) or
                not isinstance(group.get('hooks'), list) or
                any(not isinstance(handler, dict) for handler in group['hooks']) for group in groups):
            return None
        command = entry['hooks'][0]['command']
        def owned(handler):
            return (handler.get('type') == 'command' and handler.get('command') == command or
                    remove and owned_command(handler, Path(shlex.split(command)[1])))
        if remove:
            if not any(owned(handler) for group in groups for handler in group['hooks']):
                continue
            remaining = []
            for group in groups:
                handlers = [handler for handler in group['hooks'] if not owned(handler)]
                if len(handlers) == len(group['hooks']):
                    remaining.append(group)
                elif handlers or set(group) - {'hooks', 'matcher'}:
                    remaining.append(dict(group, hooks=handlers))
            if remaining:
                hooks[event] = remaining
            else:
                hooks.pop(event, None)
        elif not any(owned(handler) for group in groups for handler in group['hooks']):
            hooks[event] = [*groups, entry]
    result = dict(data)
    if hooks or 'hooks' in data:
        result['hooks'] = hooks
    return result


def cli_link_plan(home, service_repo, uninstall=False):
    if uninstall or service_repo != home / '.local/share/tether/current':
        return None
    link, target = home / '.local/bin/tether', service_repo / 'tether'
    action = ('keep' if link.is_symlink() and link.readlink() == target else
              'preserve foreign path' if link.exists() or link.is_symlink() else 'create')
    return dict(path=str(link), target=str(target), action=action)


def owned_command(value, script):
    if (not isinstance(value, dict) or value.get('type') != 'command' or
            not isinstance(value.get('command'), str)):
        return False
    try:
        words = shlex.split(value.get('command', ''))
    except ValueError:
        return False
    return (len(words) == 2 or (len(words) == 4 and words[2] == '--then')) and words[1] == str(script)


def wrapped_original(value, script):
    if owned_command(value, script):
        words = shlex.split(value['command'])
        if len(words) == 4:
            return words[3]
    return None


def make_plan(args, repo=ROOT, home=None):
    home = home or Path.home()
    state_file = Path(os.environ.get('TETHER_INSTALL_STATE') or home / '.config/tether/install.json')
    units_dir = service_units_dir(home)
    unit_names = UNIT_NAMES if TERMUX else (*UNIT_NAMES, UPDATE_UNITS[0]) if DARWIN else (*UNIT_NAMES, *UPDATE_UNITS)
    saved = {}
    if state_file.exists():
        saved = json.loads(state_file.read_text())
        if saved.get('version') != VERSION or not isinstance(saved.get('options'), dict):
            raise ValueError(f'Unsupported or invalid install state: {state_file}')
    previous = saved.get('options', {})
    current = home / '.local/share/tether/current'
    managed = repo.parent == home / '.local/share/tether/releases' and current.is_symlink() and current.resolve() == repo.resolve()
    service_repo = args.release_current or (current if managed else repo)
    if args.release_current and args.release_current != home / '.local/share/tether/current':
        raise ValueError('Release unit paths must use the managed current symlink.')
    options = {}
    defaults = dict(host='127.0.0.1', auth='token', terminal=bool(shutil.which('ttyd')),
                    voice=False, push=False, images=False, hooks=False, ask_bridge=False, ask_bridge_wait=30, resources=False, build_ui=None, updates=saved.get('updates', 'notify'))
    reused = []
    for key, default in defaults.items():
        value = getattr(args, key)
        options[key] = value if value is not None else previous.get(key, default)
        if value is None and key in previous:
            reused.append(key)
    value = options['ask_bridge_wait']
    options['ask_bridge_wait'] = max(5, min(120, value)) if type(value) is int else 30
    options['port'] = args.port if args.port is not None else previous.get('port')
    if args.port is None and 'port' in previous:
        reused.append('port')
    checks = []

    def check(name, ok, fix):
        checks.append(dict(name=name, ok=bool(ok), fix=fix))

    if TERMUX:
        check('Termux', True, '')
    elif DARWIN:
        check('macOS', True, '')
    else:
        check('Linux', platform.system() == 'Linux', 'Run the installer on Linux.')
    check('Python >= 3.11', sys.version_info >= (3, 11),
          'Use Python 3.11 or newer, for example: brew install python, then run /opt/homebrew/bin/python3 install.py.'
          if DARWIN else 'Run: pkg install python' if TERMUX else 'Use Python 3.11 or newer.')
    legacy_files = set()
    if TERMUX:
        check('termux-services', shutil.which('sv') and shutil.which('svlogd') and units_dir.is_dir(),
              'Run: pkg install termux-services, then close and reopen Termux.')
    elif DARWIN:
        result = probe('launchctl', 'print', f'gui/{os.getuid()}')
        check('launchd GUI session', result is not None and result.returncode == 0,
              'Log in to the Mac desktop as this user, and run the installer from Terminal there, not over SSH.')
    else:
        result = probe('systemctl', '--user', 'show-environment')
        check('systemd user session', result is not None and result.returncode == 0,
              'Start a Linux login session with a reachable systemd user manager.')
        legacy_files = {path.name for path in units_dir.glob('mobile-tmux*.service')}
        result = probe('systemctl', '--user', 'list-unit-files', 'mobile-tmux*.service', '--no-legend', '--no-pager')
        if result and result.returncode == 0:
            legacy_files.update(row.split()[0] for row in result.stdout.splitlines()
                                if row.split() and row.split()[0].startswith('mobile-tmux') and row.split()[0].endswith('.service'))
    legacy = sorted(legacy_files)
    legacy_config = {unit: legacy_settings(unit, units_dir) for unit in LEGACY_UNITS
                     if unit in legacy and not args.uninstall and (args.replace_legacy or options['voice'])}
    commands = []
    files = {}
    if args.uninstall:
        installed = [name for name in unit_names
                     if unit_path(units_dir, name).exists() or name in saved.get('unit_names', [])]
        if TERMUX:
            commands = [['sv', 'down', str(units_dir / runit_name(name))] for name in installed]
        elif DARWIN:
            commands = [['launchctl', 'bootout', f'gui/{os.getuid()}/{launchd_label(name)}'] for name in installed]
        else:
            commands = [['systemctl', '--user', 'disable', '--now', name] for name in installed]
            commands.append(['systemctl', '--user', 'daemon-reload'])
    else:
        check('tmux', bool(shutil.which('tmux')) or DARWIN and any(
            os.access(path, os.X_OK) for path in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux')),
            'Run: pkg install tmux' if TERMUX else 'Install tmux and place it on PATH.')
        if TERMUX and options['voice']:
            check('voice', False, 'Voice is not supported on Termux; use --no-voice.')
        if TERMUX and options['updates'] == 'auto':
            check('automatic updates', False, 'Termux has no timer service; use --updates notify.')
        try:
            valid_host = ipaddress.ip_address(options['host']).version == 4
        except ValueError:
            valid_host = options['host'] == 'localhost'
        check('host', valid_host, 'Choose an IPv4 address or localhost with --host; the server uses IPv4.')
        loopback = options['host'] == 'localhost' or (valid_host and ipaddress.ip_address(options['host']).is_loopback)
        check('proxy authentication', options['auth'] != 'proxy' or loopback,
              'Use --host 127.0.0.1 with --auth proxy.')
        check('ttyd', not options['terminal'] or bool(shutil.which('ttyd')),
              'Run: pkg install ttyd, or use --no-terminal.' if TERMUX else 'Install ttyd on PATH or use --no-terminal.')
        build = needs_build(repo) if options['build_ui'] is None else options['build_ui']
        check('UI build', build or (repo / 'ui/dist/index.html').is_file(),
              'Build ui/dist with npm, or use --build-ui after installing Node.js and npm.')
        check('node/npm', not build or bool(shutil.which('node') and shutil.which('npm')),
              'Run: pkg install nodejs' if TERMUX else 'Install Node.js and npm to build the UI; no usable dist exists or a build was requested.')
        try:
            taken = occupied_ports()
        except OSError:
            taken = set()
            if not DARWIN and not TERMUX:
                check('Linux socket table', False, 'Make /proc/net/tcp and /proc/net/tcp6 readable on Linux.')
        reserved = set()
        legacy_chat = LEGACY_UNITS[0] if args.replace_legacy and LEGACY_UNITS[0] in legacy else None
        if args.port is None and legacy_chat:
            legacy_port = legacy_config[legacy_chat][0]
            if legacy_port is not None:
                options['port'] = legacy_port
                if 'port' in reused:
                    reused.remove('port')
        if options['port'] is None:
            options['port'] = pick_port(7683, taken, reserved)
        port = options['port']
        own = unit_path(units_dir, UNIT_NAMES[0]).exists() and previous.get('port') == port
        check('chat port', free_port(port, taken, reserved, UNIT_NAMES[0] if own else None, legacy_chat),
              f'Port {port} is taken or invalid. Choose a free port with --port.')
        reserved.add(port)
        for key, enabled, start, unit in (('terminal_port', options['terminal'], 7682, UNIT_NAMES[1]),
                                          ('voice_port', options['voice'], 7684, UNIT_NAMES[2])):
            options[key] = None
            if enabled:
                legacy_unit = LEGACY_UNITS[UNIT_NAMES.index(unit)] if args.replace_legacy and not DARWIN and not TERMUX else None
                legacy_port = legacy_config.get(legacy_unit, (None, {}))[0]
                old = legacy_port if legacy_port is not None else previous.get(key)
                if old and free_port(old, taken, reserved, unit if unit_path(units_dir, unit).exists() else None,
                                     legacy_unit):
                    options[key] = old
                else:
                    options[key] = pick_port(start, taken, reserved)
                reserved.add(options[key])
                check(key, True, '')
        # Homebrew's /opt/homebrew/bin/python3 links into a versioned Cellar folder that `brew upgrade` deletes.
        python = os.path.abspath(sys.executable) if DARWIN or TERMUX else str(Path(sys.executable).resolve())
        env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
               'MOBILE_TMUX_HOST': options['host'], 'MOBILE_TMUX_PORT': port,
               'MOBILE_TMUX_AUTH': options['auth'], 'TMUX_CHAT_DIST': service_repo / 'ui/dist',
               'MOBILE_TMUX_CONFIG': os.environ.get('MOBILE_TMUX_CONFIG') or home / '.config/mobile-tmux',
               # Disabled features must not connect to another user's default side ports.
               'MOBILE_TMUX_TTYD_PORT': options['terminal_port'] or 0,
               'MOBILE_TMUX_TRANSCRIBE_URL': f'http://127.0.0.1:{options["voice_port"] or 0}/transcribe'}
        env['TETHER_RESOURCES'] = '1' if options['resources'] else '0'
        if DARWIN:
            for directory in ('/opt/homebrew/bin', '/usr/local/bin'):
                if directory not in env['PATH'].split(os.pathsep):
                    env['PATH'] += os.pathsep + directory
        if TERMUX:
            directory = str(Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr')) / 'bin')
            if directory not in env['PATH'].split(os.pathsep):
                env['PATH'] += os.pathsep + directory
        for key in ('TETHER_INSTALL_STATE', 'TETHER_REPO'):
            if os.environ.get(key):
                env[key] = os.environ[key]
        server_python = python
        # images: Pillow makes small WebP copies of backdrop photos (server/image_shrink.py). Without it ffmpeg is tried.
        if options['voice'] or options['push'] or options['images']:
            venv = home / '.local/share/tether' / ('whisper-venv' if options['voice'] else 'server-venv')
            vpython = str(venv / 'bin/python')
            if not (venv / 'bin/python').exists():
                commands.append([python, '-m', 'venv', str(venv)])
            packages = ((['faster-whisper', 'av>=16,<17'] if options['voice'] else []) + (['pywebpush'] if options['push'] else [])
                        + (['pillow'] if options['images'] else []))
            commands.append([vpython, '-m', 'pip', 'install', *packages])
            if options['push'] or options['images']:
                server_python = vpython
            if options['voice']:
                voice_env = {'MOBILE_TMUX_TRANSCRIBE_PORT': options['voice_port']}
                if DARWIN:
                    voice_env['PATH'] = env['PATH']
                legacy_env = legacy_config.get(LEGACY_UNITS[2], (None, {}))[1]
                # Keep the prompt across re-applies and release updates: the installed unit wins over legacy.
                current_env = {}
                voice_file = unit_path(units_dir, UNIT_NAMES[2])
                if voice_file.is_file() and not TERMUX:
                    current_env = (plistlib.loads(voice_file.read_bytes()).get('EnvironmentVariables', {}) if DARWIN
                                   else legacy_settings(UNIT_NAMES[2], units_dir)[1])
                for source in (os.environ, current_env, legacy_env):
                    for key in ('TETHER_WHISPER_PROMPT', 'MOBILE_TMUX_WHISPER_PROMPT'):
                        if key in source:
                            voice_env['TETHER_WHISPER_PROMPT'] = source[key]
                            break
                    if 'TETHER_WHISPER_PROMPT' in voice_env:
                        break
                files[UNIT_NAMES[2]] = service_file(UNIT_NAMES[2], 'Tether voice worker',
                    [vpython, service_repo / 'server/transcribe_worker.py'], voice_env)
        if build:
            commands.extend([['npm', 'ci'], ['npm', 'run', 'build']])
        files[UNIT_NAMES[0]] = service_file(UNIT_NAMES[0], 'Tether chat', [server_python, service_repo / 'server/server.py'], env)
        if options['terminal']:
            files[UNIT_NAMES[1]] = service_file(UNIT_NAMES[1], 'Tether terminal',
                [shutil.which('ttyd') or 'ttyd', '-W', '-i', '127.0.0.1', '-p', str(options['terminal_port']),
                 '-O', '-a', '-b', '/terminal/live', '-t', 'titleFixed=Terminal', '-t', 'fontSize=13',
                 '-t', 'rendererType=dom', '-t', 'scrollback=10000', python, service_repo / 'server/attach.py'],
                {'PATH': env['PATH']} if DARWIN or TERMUX else {})
        if options['updates'] == 'auto' and not TERMUX:
            files[UPDATE_UNITS[0]] = service_file(UPDATE_UNITS[0], 'Tether automatic update',
                [python, service_repo / 'tether', 'update', '--auto'], env, oneshot=True)
            if not DARWIN:
                files[UPDATE_UNITS[1]] = ('[Unit]\nDescription=Check Tether releases daily\n\n[Timer]\n'
                                         'OnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n')
        if TERMUX:
            commands.extend([['sv', 'down', str(units_dir / runit_name(name))] for name in unit_names
                             if name not in files and (units_dir / runit_name(name)).exists()])
            commands.extend([['sv', 'restart', str(units_dir / runit_name(name))] for name in sorted(files)])
        elif DARWIN:
            removed = [name for name in unit_names if name not in files and unit_path(units_dir, name).exists()]
            commands.extend([['launchctl', 'bootout', f'gui/{os.getuid()}/{launchd_label(name)}']
                             for name in sorted(set(files) | set(removed))])
            commands.extend([['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(unit_path(units_dir, name))]
                             for name in sorted(files)])
        else:
            if args.replace_legacy:
                commands.extend([['systemctl', '--user', 'disable', '--now', name] for name in legacy])
            commands.extend([['systemctl', '--user', 'disable', '--now', name] for name in (*UNIT_NAMES, *UPDATE_UNITS)
                             if name not in files and (units_dir / name).exists()])
            commands.append(['systemctl', '--user', 'daemon-reload'])
            for name in sorted(files):
                if name == UPDATE_UNITS[0]:
                    continue
                commands.extend([['systemctl', '--user', 'enable', '--now', name],
                                 ['systemctl', '--user', 'restart', name]])
    if args.defer_restart:
        commands = [command for command in commands
                    if (command[0] != 'sv' if TERMUX else command[0] != 'launchctl' if DARWIN else
                        command[:2] != ['systemctl', '--user'] or command[2] == 'daemon-reload')]
    hooks = []
    old_repo = Path(saved.get('repo', service_repo))
    relocating = old_repo != service_repo and not args.uninstall
    if options['hooks'] or relocating or args.uninstall:
        for folder, script, extra in (('.claude', 'claude_usage.py', {}),
                                     ('.gemini/antigravity-cli', 'antigravity_status.py', {'enabled': True, 'stack_with_default': True})):
            if args.uninstall and folder != '.claude':
                continue
            directory = home / folder
            if not directory.is_dir():
                continue
            settings = directory / 'settings.json'
            original = None
            try:
                data = json.loads(settings.read_text()) if settings.exists() else {}
                status = 'preserve'
                if isinstance(data, dict):
                    value = data.get('statusLine')
                    if args.uninstall:
                        original = wrapped_original(value, old_repo / 'server' / script)
                        if original is not None:
                            status = 'unwrap'
                    elif relocating and owned_command(value, old_repo / 'server' / script):
                        status = 'replace'
                        original = wrapped_original(value, old_repo / 'server' / script)
                    elif 'statusLine' not in data and options['hooks']:
                        status = 'add'
                    elif (options['hooks'] and folder == '.claude' and isinstance(value, dict)
                          and value.get('type') == 'command' and isinstance(value.get('command'), str)
                          and 'claude_usage.py' not in value['command']):
                        status = 'wrap'
                        original = value['command']
            except ValueError:
                status = 'preserve invalid JSON'
            command = [sys.executable, str(service_repo / 'server' / script)]
            if original is not None:
                command.extend(['--then', original])
            hooks.append(dict(path=str(settings), action=status, old_repo=str(old_repo), script=script,
                              value=dict(type='command', command=shlex.join(command), **extra)))
            if original is not None:
                hooks[-1]['original'] = original
    if options['hooks'] or args.uninstall or relocating:
        settings = home / '.claude/settings.json'
        if settings.parent.is_dir():
            entries = claude_hook_entries(Path(saved.get('repo', service_repo)) if args.uninstall else service_repo)
            try:
                data = json.loads(settings.read_text()) if settings.exists() else {}
                original = data
                if relocating and not options['hooks'] and merge_claude_hooks(data, entries) is not None:
                    # Move handlers that remain from a prior hook install, without enabling new ones.
                    entries = {event: entry for event, entry in entries.items()
                               if any(owned_command(handler, old_repo / 'server/claude_hooks.py')
                                      for group in data.get('hooks', {}).get(event, [])
                                      for handler in group['hooks'])}
                if relocating:
                    data = merge_claude_hooks(data, claude_hook_entries(old_repo), remove=True)
                merged = merge_claude_hooks(data, entries, remove=args.uninstall) if data is not None else None
                status = 'preserve' if merged is None or merged == original else 'remove' if args.uninstall else 'merge'
            except ValueError:
                status = 'preserve invalid JSON'
            hooks.append(dict(path=str(settings), action=status, entries=entries,
                              old_entries=claude_hook_entries(old_repo) if relocating else None))
    # Separate registration keeps passive one-second hooks and their ordering intact.
    # Always reconcile this entry so --no-ask-bridge also removes a prior pilot.
    settings = home / '.claude/settings.json'
    if settings.parent.is_dir():
        bridge_remove = args.uninstall or not options['ask_bridge']
        entries = ask_bridge_entries(old_repo if args.uninstall else service_repo, options['ask_bridge_wait'])
        old_entries = ask_bridge_entries(old_repo) if relocating else None
        try:
            data = json.loads(settings.read_text()) if settings.exists() else {}
            original = data
            if old_entries:
                data = merge_claude_hooks(data, old_entries, remove=True)
            if data is not None and not bridge_remove:
                data = merge_claude_hooks(data, entries, remove=True)
            merged = merge_claude_hooks(data, entries, remove=bridge_remove) if data is not None else None
            status = 'preserve' if merged is None or merged == original else 'remove' if bridge_remove else 'merge'
        except ValueError:
            status = 'preserve invalid JSON'
        hooks.append(dict(path=str(settings), action=status, entries=entries,
                          old_entries=old_entries, remove=bridge_remove, replace=True))
    cli_link = cli_link_plan(home, service_repo, args.uninstall)
    warnings = []
    if cli_link:
        if cli_link['action'] == 'preserve foreign path':
            warnings.append(f'Existing {cli_link["path"]} belongs to another install; kept unchanged. Use {cli_link["target"]}.')
        if str(home / '.local/bin') not in os.environ.get('PATH', '').split(os.pathsep):
            warnings.append('~/.local/bin is not on PATH. Add this shell line: export PATH="$HOME/.local/bin:$PATH"')
    return dict(cli_link=cli_link, warnings=warnings, version=VERSION, action='uninstall' if args.uninstall else 'install', repo=str(service_repo), build_repo=str(repo),
                options=options, reused_saved_choices=sorted(set(reused)), checks=checks,
                legacy_units=legacy, legacy_action='disable and stop' if args.replace_legacy and not args.uninstall else 'leave untouched',
                units=files, units_dir=str(units_dir), commands=commands, hooks=hooks, state_file=str(state_file),
                remove_units=[name for name in unit_names if name not in files and
                              (units_dir / runit_name(name) if TERMUX else unit_path(units_dir, name)).exists()],
                url=f'http://{"127.0.0.1" if options["host"] in ("localhost", "0.0.0.0") else options["host"]}:{options["port"]}/chat/' if not args.uninstall else None,
                token_file=str(Path(os.environ.get('MOBILE_TMUX_CONFIG') or home / '.config/mobile-tmux') / 'token'))


def write_units(plan, units_dir):
    if DARWIN:
        (units_dir.parent / 'Logs/tether').mkdir(parents=True, exist_ok=True)
    units_dir.mkdir(parents=True, exist_ok=True)
    for name in plan['remove_units']:
        if TERMUX:
            shutil.rmtree(units_dir / runit_name(name), ignore_errors=True)
        else:
            unit_path(units_dir, name).unlink(missing_ok=True)
    for name, content in plan['units'].items():
        path = unit_path(units_dir, name)
        if TERMUX:
            (path.parent / 'log').mkdir(parents=True, exist_ok=True)
            prefix = Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr'))
            log = shlex.quote(str(prefix / 'var/log/sv' / runit_name(name)))
            logger = path.parent / 'log/run'
            logger.write_text(f'#!{prefix}/bin/sh\nmkdir -p {log}\nexec svlogd -tt {log}\n')
            logger.chmod(0o755)
        path.write_text(content)
        if TERMUX:
            path.chmod(0o755)


def apply_plan(plan, home=None):
    home = home or Path.home()
    failed = [item for item in plan['checks'] if not item['ok']]
    if failed:
        raise ValueError('Required checks failed:\n' + '\n'.join(f'{index}. {item["fix"]}' for index, item in enumerate(failed, 1)))
    units_dir = service_units_dir(home)
    written = False
    replacing = plan['action'] == 'install' and plan['legacy_action'] == 'disable and stop'
    # Only legacy services running now are stopped by this run, so only they are brought back.
    running = []
    for name in plan['legacy_units'] if replacing and not DARWIN and not TERMUX else []:
        result = probe('systemctl', '--user', 'is-active', name)
        if result and result.stdout.strip() == 'active':
            running.append(name)
    for command in plan['commands']:
        if TERMUX and command[0] == 'sv':
            if command[1] == 'restart' and not written:
                write_units(plan, units_dir)
                written = True
            probe(*command)
            continue
        if DARWIN and command[:2] == ['launchctl', 'bootout']:
            probe(*command)
            continue
        if DARWIN and command[0] == 'launchctl' and not written:
            write_units(plan, units_dir)
            written = True
        if not DARWIN and command[:3] == ['systemctl', '--user', 'daemon-reload']:
            if running:
                taken = occupied_ports()
                owners = {'port': UNIT_NAMES[0], 'terminal_port': UNIT_NAMES[1], 'voice_port': UNIT_NAMES[2]}
                for key, unit in owners.items():
                    port = plan['options'][key]
                    # A port held by this user's own Tether unit is fine (repeat apply).
                    if port is not None and not free_port(port, taken, set(), unit):
                        failed = []
                        for name in running:
                            try:
                                run(['systemctl', '--user', 'enable', '--now', name], stdout=sys.stderr)
                            except (OSError, subprocess.SubprocessError):
                                failed.append(name)
                        state = ('could not restart ' + ', '.join(failed)) if failed else 'restarted ' + ', '.join(running)
                        raise ValueError(f'Port {port} is still taken after stopping legacy services. '
                                         f'Tether services were not started; {state}.')
            write_units(plan, units_dir)
            written = True
        # Keep stdout machine-readable for --apply --json, including pip/npm logs.
        run(command, cwd=Path(plan.get('build_repo', plan['repo'])) / 'ui' if command[0] == 'npm' else None, stdout=sys.stderr)
    if not written:
        if DARWIN or TERMUX:
            write_units(plan, units_dir)
        else:
            raise ValueError('Plan has no unit reload step.')
    state = Path(plan['state_file'])
    for hook in plan['hooks']:
        if 'entries' not in hook or hook['action'] not in ('merge', 'remove'):
            continue
        settings = Path(hook['path'])
        try:
            data = json.loads(settings.read_text()) if settings.exists() else {}
        except ValueError:
            continue
        original = data
        if hook.get('old_entries'):
            data = merge_claude_hooks(data, hook['old_entries'], remove=True)
        if data is not None and hook.get('replace') and not hook['remove']:
            data = merge_claude_hooks(data, hook['entries'], remove=True)
        merged = merge_claude_hooks(data, hook['entries'], remove=hook.get('remove', plan['action'] == 'uninstall')) if data is not None else None
        if merged is None or merged == original:
            continue
        if settings.exists():
            shutil.copy2(settings, settings.with_name(settings.name + '.before-tether'))
        settings.write_text(json.dumps(merged, indent=2) + '\n')
    for hook in plan['hooks']:
        if hook['action'] not in ('add', 'replace', 'wrap', 'unwrap'):
            continue
        settings = Path(hook['path'])
        try:
            data = json.loads(settings.read_text()) if settings.exists() else {}
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        value = hook['value']
        if hook['action'] == 'unwrap':
            original = wrapped_original(data.get('statusLine'), Path(hook['old_repo']) / 'server' / hook['script'])
            if original is None:
                continue
            value = dict(command=original)
        elif hook['action'] == 'wrap':
            existing = data.get('statusLine')
            if not isinstance(existing, dict) or existing.get('command') != hook['original']:
                continue
        elif hook['action'] == 'replace':
            if not owned_command(data.get('statusLine'), Path(hook['old_repo']) / 'server' / hook['script']):
                continue
        elif 'statusLine' in data:
            continue
        if settings.exists():
            shutil.copy2(settings, settings.with_name(settings.name + '.before-tether'))
        data['statusLine'] = (dict(data['statusLine'], command=value['command'])
                              if hook['action'] != 'add' else value)
        settings.write_text(json.dumps(data, indent=2) + '\n')
    if plan['action'] == 'uninstall':
        # Services have stopped, so they cannot reinstall these hooks during cleanup.
        run([sys.executable, str(ROOT / 'server/window_size.py'), 'uninstall'], stdout=sys.stderr)
        state.unlink(missing_ok=True)
        return
    cli_link = cli_link_plan(home, Path(plan['repo']))
    if cli_link:
        if cli_link['action'] == 'create':
            link = Path(cli_link['path'])
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(cli_link['target'])
            except FileExistsError:
                print(f'Existing {link} kept unchanged.', file=sys.stderr)
        elif cli_link['action'] == 'preserve foreign path':
            print(f'Existing {cli_link["path"]} kept unchanged. Use {cli_link["target"]}.', file=sys.stderr)
    for warning in plan.get('warnings', []):
        print(warning, file=sys.stderr)
    state.parent.mkdir(parents=True, exist_ok=True)
    record = dict(version=VERSION, repo=plan['repo'], options=plan['options'], updates=plan['options']['updates'],
                  unit_names=sorted(plan['units']), timestamp=datetime.now(timezone.utc).isoformat())
    temp = state.with_suffix('.json.new')
    temp.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    temp.replace(state)


def print_plan(plan):
    print(f'Tether {plan["action"]} plan: {plan["repo"]}')
    if plan['reused_saved_choices']:
        print('Reuse saved choices: ' + ', '.join(plan['reused_saved_choices']))
    if plan.get('cli_link'):
        print('CLI link: ' + json.dumps(plan['cli_link'], sort_keys=True))
    for warning in plan.get('warnings', []):
        print('Warning: ' + warning)
    for item in plan['checks']:
        print(f'{"PASS" if item["ok"] else "FAIL"}: {item["name"]}')
    for name in plan['legacy_units']:
        print(f'Legacy {name}: {plan["legacy_action"]}')
    folder = Path('$PREFIX/var/service' if TERMUX else '~/Library/LaunchAgents' if DARWIN else '~/.config/systemd/user')
    for name, content in sorted(plan['units'].items()):
        print(f'Write {unit_path(folder, name)}:\n{content}')
    for name in plan['remove_units']:
        print(f'Remove {folder / runit_name(name) if TERMUX else unit_path(folder, name)}')
    for command in plan['commands']:
        print(shlex.join(command) + (' (in ui/)' if command[0] == 'npm' else ''))
    for hook in plan['hooks']:
        print(f'Hook {hook["path"]}: {hook["action"]}')
    print(f'{"Remove" if plan["action"] == "uninstall" else "Save choices to"} {plan["state_file"]}')
    if plan['action'] == 'uninstall':
        print('Keep the repository, status lines, user hooks, tokens, dependencies, and data.')
    else:
        print(f'URL: {plan["url"]}\nToken: {plan["token_file"]}')
        print('Termux services run while Termux runs. Run termux-wake-lock so Android does not stop them. '
              'Install the Termux:Boot app to start them after a reboot.' if TERMUX else
              'LaunchAgents run while you are logged in to the Mac desktop.' if DARWIN else
              'loginctl enable-linger $USER keeps services running after logout; it may need admin. This installer does not run it.')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        plan = make_plan(args)
        if args.json:
            print(json.dumps(plan, sort_keys=True, indent=2))
        else:
            print_plan(plan)
        failed = [item for item in plan['checks'] if not item['ok']]
        if failed and not args.json:
            print('Fix required checks:\n' + '\n'.join(
                f'{index}. {item["fix"]}' for index, item in enumerate(failed, 1)))
        if args.apply:
            apply_plan(plan)
            if not args.json:
                print('Uninstalled Tether services.' if args.uninstall else f'Installed. URL: {plan["url"]}\nToken: {plan["token_file"]}')
        return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
