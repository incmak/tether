"""Ask Claude Code itself what it offers, the way T3 Code does: start `claude -p` in SDK mode, send control requests, never a prompt.

No prompt means no model request and no cost. Hooks are off, MCP servers are off and the session is not saved.
- initialize -> the exact slash commands and skills for a folder, and the signed-in account.
- get_usage  -> live plan limits (Anthropic's usage endpoint, about 15 s).
"""
from itertools import islice
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import paths

COMMAND = ['claude', '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
           '--settings', '{"disableAllHooks":true}', '--no-session-persistence',
           '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
INIT_TTL = 600  # seconds a folder's command list stays fresh
_lock = threading.Lock()
_init = {}  # folder -> {'at': time, 'result': dict | None, 'signature': dict, 'thread': Thread | None}
_usage = {'at': 0, 'report': None, 'error': None}
_usage_lock = threading.Lock()


def ask(cwd, requests, timeout, command=COMMAND):
    """Send control requests in order; return {subtype: response}. Raises RuntimeError on failure or timeout."""
    env = dict(os.environ, ENABLE_CLAUDEAI_MCP_SERVERS='false', CLAUDE_CODE_AUTO_CONNECT_IDE='0')
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    try:
        process = subprocess.Popen(command, cwd=cwd or None, env=env, text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError('Could not start claude: %s' % exc) from exc
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    replies = {}
    try:
        for request in requests:
            process.stdin.write(json.dumps({'type': 'control_request', 'request_id': request['subtype'], 'request': request}) + '\n')
            process.stdin.flush()
            while request['subtype'] not in replies:
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError('Claude stopped before it answered %s.' % request['subtype'])
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                response = message.get('response') or {}
                if message.get('type') == 'control_response' and response.get('request_id') == request['subtype']:
                    if response.get('subtype') != 'success':
                        raise RuntimeError(response.get('error') or 'Claude refused %s.' % request['subtype'])
                    replies[request['subtype']] = response.get('response') or {}
        return replies
    except (OSError, ValueError) as exc:
        raise RuntimeError('Claude did not answer: %s' % exc) from exc
    finally:
        timer.cancel()
        process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except OSError:
                pass


def summarize_init(reply):
    commands = []
    for item in reply.get('commands') or []:
        name = (item.get('name') or '').strip()
        if name:
            commands.append({'name': name, 'description': (item.get('description') or '').strip(), 'hint': (item.get('argumentHint') or '').strip()})
    account = reply.get('account') or {}
    return {'commands': commands, 'version': reply.get('claude_code_version'),
            'account': {'email': account.get('email'), 'plan': account.get('subscriptionType')}}


def _command_signature(cwd):
    from catalog import json_file, plugin_folder

    bases = [paths.CLAUDE]
    if cwd:
        project = Path(cwd).resolve()
        ancestors = [project, *project.parents]
        root = next((folder for folder in ancestors if (folder / '.git').exists()), project)
        bases.extend(folder / '.claude' for folder in ancestors[:ancestors.index(root) + 1])
    settings = json_file(paths.CLAUDE / 'settings.json')
    enabled = (settings.get('enabledPlugins') or {}).items()
    for key, active in islice(enabled, 32):
        if active:
            folder = plugin_folder(key, limit=32)
            if folder:
                bases.append(folder)

    signature = {}
    for base in bases:
        for kind in ('skills', 'commands'):
            folder = base / kind
            try:
                signature[folder] = folder.stat().st_mtime_ns
                with os.scandir(folder) as children:
                    for child in islice(children, 256):
                        try:
                            signature[child.path] = child.stat().st_mtime_ns
                            if kind == 'skills' and child.is_dir():
                                skill = os.path.join(child.path, 'SKILL.md')
                                signature[skill] = os.stat(skill).st_mtime_ns
                        except OSError:
                            pass
            except OSError:
                pass
    return signature


def _refresh_init(cwd, signature):
    try:
        result = summarize_init(ask(cwd, [{'subtype': 'initialize'}], 20)['initialize'])
    except RuntimeError:
        result = None
    with _lock:
        old = _init[cwd]
        if result is None and old['signature'] == signature:
            result = old['result']
        _init[cwd] = {'at': time.time(), 'result': result, 'signature': signature, 'thread': None}


def commands(cwd, wait=6.0):
    """Wait for the first list or a folder change. Refresh an expired list in the background."""
    deadline = time.monotonic() + wait
    while True:
        signature = _command_signature(cwd)
        with _lock:
            entry = _init.setdefault(cwd, {'at': 0, 'result': None, 'signature': None, 'thread': None})
            matches = entry['signature'] == signature
            if matches and time.time() - entry['at'] < INIT_TTL:
                return entry['result']
            thread = entry['thread']
            if thread is None:
                thread = threading.Thread(target=_refresh_init, args=(cwd, signature), daemon=True)
                entry['thread'] = thread
                thread.start()
            if matches and entry['result'] is not None:
                return entry['result']
        thread.join(max(0, deadline - time.monotonic()))
        with _lock:
            entry = _init[cwd]
            if entry['signature'] == signature:
                return entry['result']
        if time.monotonic() >= deadline:
            return None


def usage_report(reply, now=None):
    """get_usage -> Tether's report: {'windows': [{'label', 'percent', 'resets_at'}], 'reported_at', 'source'}."""
    limits = reply.get('rate_limits') or {}
    names = {'five_hour': '5h', 'seven_day': '7d', 'seven_day_opus': '7d Opus', 'seven_day_sonnet': '7d Sonnet'}
    windows = []
    for key, label in names.items():
        window = limits.get(key)
        if isinstance(window, dict) and window.get('utilization') is not None:
            windows.append({'label': label, 'percent': window['utilization'], 'resets_at': epoch(window.get('resets_at'))})
    return {'windows': windows, 'reported_at': now or time.time(), 'source': 'live', 'plan': reply.get('subscription_type')}


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def check_usage(max_age):
    """Live Claude limits, at most max_age seconds old. One check at a time; others reuse its result."""
    if time.time() - _usage['at'] <= max_age:
        return dict(_usage)
    with _usage_lock:
        if time.time() - _usage['at'] > max_age:
            try:
                reply = ask(None, [{'subtype': 'initialize'}, {'subtype': 'get_usage', 'skipBehaviors': True}], 40)['get_usage']
                _usage.update(at=time.time(), report=usage_report(reply) if reply.get('rate_limits_available') else None, error=None)
            except RuntimeError as exc:
                _usage.update(at=time.time(), error=str(exc))
        return dict(_usage)
