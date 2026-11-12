"""Explicit, local setup actions for autonomy hooks and urgent notifications."""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import agent_browser_setup
import paths
import urgent_ping

_LOCK = threading.Lock()
_LAST_TEST = None
MENTION = re.compile(r'[A-Za-z0-9._@-]{1,64}')
SPACES = ('team', 'qa', 'release', 'bots')


def _hook_entry():
    script = Path(__file__).resolve().with_name('autonomy.py')
    return {'type': 'command', 'command': shlex.join([sys.executable, str(script), 'hook']), 'timeout': 5}


def _claude_data(path):
    try:
        data = json.loads(path.read_bytes(), parse_constant=agent_browser_setup._invalid_json_constant)
    except FileNotFoundError:
        data = {}
    except (ValueError, UnicodeError):
        raise ValueError('Claude settings contain invalid JSON.') from None
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError(agent_browser_setup.CLAUDE_REASON)
    groups = data.get('hooks', {}).get('UserPromptSubmit', [])
    if not isinstance(groups, list) or any(not isinstance(group, dict)
            or not isinstance(group.get('hooks'), list)
            or any(not isinstance(hook, dict) for hook in group['hooks']) for group in groups):
        raise ValueError(agent_browser_setup.CLAUDE_REASON)
    return data, groups


def _hook_path(hook):
    if hook.get('type') != 'command' or not isinstance(hook.get('command'), str):
        return None
    try:
        args = shlex.split(hook['command'])
    except ValueError:
        return None
    if len(args) >= 2 and args[-1] == 'hook' and Path(args[-2]).name == 'autonomy.py':
        return Path(args[-2])
    return None


def hook_status(path=None):
    path = Path(path) if path is not None else agent_browser_setup.claude_settings_path()
    result = {'state': 'missing', 'path': str(path), 'entry': _hook_entry()}
    if not path.parent.exists():
        return dict(result, state='none', reason='Claude settings folder does not exist.')
    try:
        _, groups = _claude_data(path)
        script = Path(__file__).resolve().with_name('autonomy.py')
        if any(_hook_path(hook) == script for group in groups for hook in group['hooks']):
            result['state'] = 'ok'
    except (OSError, ValueError) as exc:
        result.update(state='manual', reason=str(exc) if isinstance(exc, ValueError)
                      else agent_browser_setup.CLAUDE_REASON)
    return result


def install_hook(path=None):
    with _LOCK:
        status = hook_status(path)
        if status['state'] == 'ok':
            return
        if status['state'] in ('manual', 'none'):
            raise ValueError(status['reason'])
        path = Path(status['path'])
        data, groups = _claude_data(path)
        kept = []
        for group in groups:
            hooks = [hook for hook in group['hooks'] if _hook_path(hook) is None]
            if hooks or not group['hooks']:
                kept.append(dict(group, hooks=hooks))
        data.setdefault('hooks', {})['UserPromptSubmit'] = [*kept, {'hooks': [status['entry']]}]
        agent_browser_setup._write(path, (json.dumps(data, indent=2) + '\n').encode('utf-8'))


def _ping_path():
    return paths.CONFIG / 'urgent-ping.json'


def _briskbot_path():
    """The BriskBot script named in CONFIG/briskbot.json ({"script": path}), or None when it is not set up."""
    try:
        data = json.loads((paths.CONFIG / 'briskbot.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    script = data.get('script') if isinstance(data, dict) else None
    if not isinstance(script, str) or not script:
        return None
    path = Path(script).expanduser()
    return path if path.is_absolute() and path.is_file() else None


def ping_status():
    config = urgent_ping._config()
    return {'state': 'on' if urgent_ping._argv({'pane': '%0', 'name': 'Test'}) is not None else 'off',
            'preset': config.get('preset') if isinstance(config.get('preset'), str) else None,
            'fields': config.get('fields') if isinstance(config.get('fields'), dict) else {},
            'base_url': config.get('base_url') if isinstance(config.get('base_url'), str) else '',
            'presets': [{'id': 'briskbot', 'name': 'BriskBot (Microsoft Teams)',
                         'available': _briskbot_path() is not None}]}


def _write_ping(config):
    path = _ping_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.urgent-ping-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(config, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_ping(preset, fields, base_url):
    if preset != 'briskbot' or _briskbot_path() is None:
        raise ValueError('Choose an available ping preset.')
    if not isinstance(fields, dict):
        raise ValueError('Enter the ping fields.')
    mention, space = fields.get('mention'), fields.get('space', 'team')
    if not isinstance(mention, str) or not MENTION.fullmatch(mention):
        raise ValueError('Enter a mention of 1 to 64 letters, numbers, dots, underscores, @ signs or hyphens.')
    if space not in SPACES:
        raise ValueError('Choose team, qa, release or bots.')
    try:
        if not isinstance(base_url, str) or any(char.isspace() or ord(char) < 32 for char in base_url):
            raise ValueError
        url = urlsplit(base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.password is not None or '?' in base_url or '#' in base_url):
            raise ValueError
        url.port  # Reject malformed ports as well as malformed hosts.
    except ValueError:
        raise ValueError('Enter an http or https base URL with a host and no userinfo, query or fragment.') from None
    command = [sys.executable, str(_briskbot_path()), '{title}', 'Needs you', '--space', space,
               '--mention', mention, '--urgent', '--tone', 'attention', '--topic', 'tether-urgent:{pane}',
               '--force', '--line', 'Question|{question}', '--button', 'Open the tab|{url}']
    with _LOCK:
        _write_ping({'enabled': True, 'preset': preset, 'fields': {'mention': mention, 'space': space},
                     'command': command, 'base_url': base_url})


def disable_ping():
    with _LOCK:
        config = urgent_ping._config()
        config['enabled'] = False
        _write_ping(config)


def test_ping():
    global _LAST_TEST
    with _LOCK:
        now = time.monotonic()
        if _LAST_TEST is not None and now - _LAST_TEST < 60:
            raise ValueError('Wait a minute before the next test.')
        _LAST_TEST = now
    return urgent_ping.run_test()
