"""What the chat box can suggest: slash commands and skills for "/", files for "@", and the MCP servers and plugins in reach.
Read-only. Everything comes from the agents' own config folders; nothing here runs an agent."""
import json
import os
from pathlib import Path
import re
import subprocess
import time
try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

import claude_probe
import paths

HOME = Path.home()
CODEX_ADMIN = Path('/etc/codex')
# Every documented Claude Code command, with aliases. Made by update_claude_commands.py from the official reference.
COMMANDS_FILE = Path(__file__).with_name('claude_commands.json')


def claude_builtins():
    try:
        commands = json.loads(COMMANDS_FILE.read_text())['commands']
    except (OSError, ValueError, KeyError):
        return []
    return [{'name': item['name'], 'description': short(item['description']), 'aliases': item.get('aliases') or [], 'hint': item.get('hint', ''), 'kind': item.get('kind', 'builtin')}
            for item in commands]


# Visible Linux commands in codex-cli 0.166 popup order.
CODEX_BUILTINS = [
    ('model', 'choose what model and reasoning effort to use', [], '[model]'),
    ('presets', 'run or save reusable prompts and slash commands', [], ''),
    ('daybreak', 'turn Daybreak on or off', [], ''),
    ('ide', 'include current selection, open files, and other context from your IDE', [], '[context]'),
    ('permissions', 'choose what Codex is allowed to do', [], '[mode]'),
    ('keymap', 'remap TUI shortcuts', [], '[shortcut]'),
    ('vim', 'toggle Vim mode for the composer', [], ''),
    ('setup-default-sandbox', 'set up elevated agent sandbox', [], ''),
    ('experimental', 'toggle experimental features', [], ''),
    ('approve', 'approve one retry of a recent auto-review denial', [], ''),
    ('memories', 'configure memory use and generation', [], ''),
    ('skills', 'use skills to improve how Codex performs specific tasks', [], ''),
    ('import', 'import setup, this project, and recent chats from Claude Code', [], ''),
    ('hooks', 'view and manage lifecycle hooks', [], ''),
    ('review', 'review my current changes and find issues', [], '[instructions]'),
    ('rename', 'rename the current thread', [], '[title]'),
    ('new', 'start a new chat during a conversation', [], '[text]'),
    ('archive', 'archive this session', [], ''),
    ('delete', 'permanently delete this session', [], ''),
    ('resume', 'resume a saved chat', [], '[session]'),
    ('fork', 'fork the current chat', [], '[text]'),
    ('worktree', 'start or continue a conversation in a new worktree', [], ''),
    ('init', 'create an AGENTS.md file with instructions for Codex', [], ''),
    ('compact', 'summarize conversation to prevent hitting the context limit', [], ''),
    ('config', 'configure Codex preferences', [], ''),
    ('recap', 'summarize the current conversation now', [], ''),
    ('plan', 'switch to Plan mode', [], '[text]'),
    ('voice', 'start or stop voice; use /voice settings to choose a voice', [], '[settings]'),
    ('goal', 'set or view the goal for a long-running task', [], '[text]'),
    ('agents', 'open the agent command center', [], ''),
    ('side', 'start a side conversation in an ephemeral fork', [], '[question]'),
    ('btw', 'start a side conversation in an ephemeral fork', [], '[question]'),
    ('copy', 'copy the last response or part of it', [], ''),
    ('export', 'export the conversation as markdown', [], '[path]'),
    ('raw', 'toggle raw scrollback mode for copy-friendly terminal selection', [], '[mode]'),
    ('tui', 'choose the TUI mode for the next launch', [], ''),
    ('diff', 'show git diff (including untracked files)', [], ''),
    ('mention', 'mention a file', [], ''),
    ('status', 'show current session configuration and token usage', [], ''),
    ('daemon', 'Manage the local background server', [], ''),
    ('warnings', 'view retained warnings and diagnostic details', [], ''),
    ('cd', 'change the current working directory', [], '[path]'),
    ('pwd', 'show the current working directory', ['cwd'], '[options]'),
    ('usage', 'view account usage or use a usage limit reset', [], '[action]'),
    ('debug-config', 'show config layers and requirement sources for debugging', [], ''),
    ('title', 'configure which items appear in the terminal title', [], '[items]'),
    ('statusline', 'configure which items appear in the status line', [], '[items]'),
    ('theme', 'choose a syntax highlighting theme', [], ''),
    ('pets', 'choose or hide the terminal pet', ['pet'], '[pet]'),
    ('mcp', 'list MCP tools; use /mcp verbose or /mcp login <name>', [], '[action]'),
    ('apps', 'manage apps', [], ''),
    ('plugins', 'browse plugins', [], ''),
    ('logout', 'log out of Codex', [], ''),
    ('quit', 'exit Codex', [], ''),
    ('exit', 'exit Codex', [], ''),
    ('feedback', 'send logs to maintainers', [], ''),
    ('ps', 'list background terminals', [], ''),
    ('stop', 'stop all background terminals', ['clean'], ''),
    ('clear', 'clear the terminal and start a new chat', [], '[text]'),
    ('subagents', "switch between this session's subagents", [], ''),
]
FRONT = re.compile(r'^---\s*\n(.*?)\n---', re.S)


def front_matter(path):
    """Metadata from a SKILL.md or command file; tolerant of missing or odd front matter."""
    try:
        text = path.read_text(errors='ignore')[:4000]
    except OSError:
        return {}
    match = FRONT.match(text)
    result = {}
    for line in (match[1] if match else '').splitlines():
        key, _, value = line.partition(':')
        if key.strip() in ('name', 'description', 'argument-hint') and value.strip() and value.strip() not in ('>', '|', '>-', '|-'):
            result[key.strip()] = value.strip().strip('"\'')
    return result


def short(text, limit=140):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def skills_in(folder, prefix=''):
    items = []
    for skill in sorted(folder.glob('*/SKILL.md')) if folder.is_dir() else []:
        meta = front_matter(skill)
        items.append({'name': prefix + (meta.get('name') or skill.parent.name), 'description': short(meta.get('description')), 'hint': meta.get('argument-hint', ''), 'kind': 'skill'})
    return items


def commands_in(folder, prefix=''):
    items = []
    for command in sorted(folder.glob('*.md')) if folder.is_dir() else []:
        meta = front_matter(command)
        items.append({'name': prefix + command.stem, 'description': short(meta.get('description')), 'hint': meta.get('argument-hint', ''), 'kind': 'command'})
    return items


def transcript_skills(transcript):
    """Skills the CLI announced to the model in this conversation ("skill_listing" entries): bundled, claude.ai and plugin skills.
    The file only grows, so each call reads just the new bytes."""
    if not transcript:
        return {}
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return {}
    offset, found = _listings.get(transcript, (0, {}))
    if size < offset:
        offset, found = 0, {}
    if size > offset:
        with open(transcript, 'rb') as handle:
            handle.seek(offset)
            data = handle.read(size - offset)
        end = data.rfind(b'\n') + 1  # leave a half-written last line for the next call
        for line in data[:end].splitlines():
            if b'"skill_listing"' not in line:
                continue
            try:
                entry = json.loads(line).get('attachment') or {}
            except ValueError:
                continue
            if entry.get('type') != 'skill_listing':
                continue
            if entry.get('isInitial'):
                found = {}
            texts = dict(LISTING_LINE.findall(entry.get('content') or ''))
            for name in entry.get('names') or texts:
                found[name] = short(texts.get(name, found.get(name, '')))
        _listings[transcript] = (offset + end, found)
    return found


LISTING_LINE = re.compile(r'^- ([^:\s][^:\n]*(?::[^:\s][^:\n]*)?): ?(.*)$', re.M)
_listings = {}  # transcript -> (bytes read, {name: description})


def plugin_folder(key):
    """The newest cached copy of an enabled plugin ("name@marketplace")."""
    name, _, market = key.partition('@')
    versions = [path for path in (paths.CLAUDE / 'plugins' / 'cache' / market / name).glob('*') if path.is_dir()]
    return max(versions, key=lambda path: path.stat().st_mtime) if versions else None


def json_file(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def plugin_name(folder, key):
    """The prefix the CLI uses: the plugin's own manifest name, which can differ from the settings key (atlassian-mcp -> atlassian)."""
    return json_file(folder / '.claude-plugin' / 'plugin.json').get('name') or key.partition('@')[0]


def synced_plugins():
    """Plugins synced from claude.ai for the signed-in account: plugins/synced/<organization>_<account>/<plugin>/."""
    account = json_file(HOME / '.claude.json').get('oauthAccount') or {}
    if not account.get('organizationUuid') or not account.get('accountUuid'):
        return []
    root = paths.CLAUDE / 'plugins' / 'synced' / ('%s_%s' % (account['organizationUuid'], account['accountUuid']))
    return sorted(folder for folder in root.glob('*') if (folder / '.claude-plugin' / 'plugin.json').is_file())


# Commands that only open a panel in the terminal. Claude's SDK list leaves them out; Tether shows its own sheet for some.
TERMINAL = 'Opens in the terminal. '


def merged_commands(probe):
    """Claude's own list (exact for this folder), plus the documented terminal-only commands it leaves out, with aliases."""
    docs = {item['name']: item for item in claude_builtins()}
    items, seen = [], set()
    for command in probe['commands']:
        doc = docs.get(command['name'])
        text = re.sub(r'\s*\((user|project|bundled|plugin[^)]*|managed)\)$', '', command['description'])
        items.append({'name': command['name'], 'description': short(text or (doc or {}).get('description')),
                      'aliases': (doc or {}).get('aliases', []), 'hint': command.get('hint') or (doc or {}).get('hint', ''),
                      'kind': (doc or {}).get('kind', 'skill') if doc else 'skill'})
        seen.add(command['name'])
    for name, doc in docs.items():
        if name not in seen and doc['kind'] == 'builtin':
            items.append(dict(doc, description=short(TERMINAL + doc['description'])))
    return items


def claude_catalog(cwd, transcript=None, probe=True):
    """probe=False skips asking Claude (the disk-only list)."""
    base = paths.CLAUDE  # ~/.claude
    found = claude_probe.commands(cwd) if probe else None
    if found and found['commands']:
        result = claude_catalog(cwd, transcript, probe=False)
        result['commands'] = merged_commands(found)
        # A pane started under another account or before a change keeps the skills it loaded then.
        seen = {item['name'] for item in result['commands']}
        result['commands'] += [{'name': name, 'description': text, 'kind': 'skill'} for name, text in transcript_skills(transcript).items() if name not in seen]
        result['account'] = found['account']
        return result
    items = claude_builtins()
    items += commands_in(base / 'commands') + skills_in(base / 'skills')
    project = Path(cwd) if cwd else None
    if project:
        items += commands_in(project / '.claude' / 'commands') + skills_in(project / '.claude' / 'skills')
    settings = json_file(base / 'settings.json')
    plugins, mcp = [], []
    for key, enabled in (settings.get('enabledPlugins') or {}).items():
        folder = plugin_folder(key) if enabled else None
        name = plugin_name(folder, key) if folder else key.partition('@')[0]
        plugins.append({'name': name, 'source': key.partition('@')[2], 'enabled': bool(enabled)})
        if folder:
            items += commands_in(folder / 'commands', name + ':') + skills_in(folder / 'skills', name + ':')
            servers = json_file(folder / '.mcp.json')
            mcp += [{'name': server, 'source': 'plugin ' + name} for server in servers.get('mcpServers', servers)]
    for folder in synced_plugins():
        name = plugin_name(folder, folder.name)
        items += commands_in(folder / 'commands', name + ':') + skills_in(folder / 'skills', name + ':')
    seen = {item['name'] for item in items}
    items += [{'name': name, 'description': text, 'kind': 'skill'} for name, text in transcript_skills(transcript).items() if name not in seen]
    config = json_file(HOME / '.claude.json')
    mcp += [{'name': server, 'source': 'user'} for server in config.get('mcpServers') or {}]
    if project:
        mcp += [{'name': server, 'source': 'project'} for server in ((config.get('projects') or {}).get(str(project)) or {}).get('mcpServers') or {}]
        mcp += [{'name': server, 'source': '.mcp.json'} for server in json_file(project / '.mcp.json').get('mcpServers') or {}]
    return {'commands': items, 'plugins': plugins, 'mcp': mcp,
            'note': 'Connectors added on claude.ai do not show here. Run /mcp to see every server.'}


def codex_catalog(cwd):
    items = [{'name': name, 'description': text[:1].upper() + text[1:], 'aliases': aliases, 'hint': hint, 'kind': 'builtin'}
             for name, text, aliases, hint in CODEX_BUILTINS]
    items += commands_in(paths.CODEX / 'prompts', 'prompts:')
    folders = []
    if cwd:
        project = Path(cwd).resolve()
        ancestors = [project, *project.parents]
        root = next((folder for folder in ancestors if (folder / '.git').exists()), project)
        folders += [folder / '.agents' / 'skills' for folder in reversed(ancestors[:ancestors.index(root) + 1])]
        folders.append(project / '.codex' / 'skills')
    folders += [paths.CODEX / 'skills', HOME / '.agents' / 'skills', paths.CODEX / 'skills' / '.system', CODEX_ADMIN / 'skills']
    seen = set()
    for folder in folders:
        for skill in skills_in(folder, '$'):
            if skill['name'] not in seen:
                items.append(skill)
                seen.add(skill['name'])
    mcp = []
    if tomllib:
        try:
            config = tomllib.loads((paths.CODEX / 'config.toml').read_text())
        except (OSError, ValueError):
            config = {}
        mcp = [{'name': server, 'source': 'config.toml'} for server in config.get('mcp_servers') or {}]
    return {'commands': items, 'plugins': [], 'mcp': mcp, 'note': ''}


def catalog(agent, cwd, transcript=None):
    if agent == 'claude':
        return claude_catalog(cwd, transcript)
    if agent == 'codex':
        return codex_catalog(cwd)
    return {'commands': [], 'plugins': [], 'mcp': [], 'note': ''}


SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', '.next', '.cache', 'target'}
_files = {}  # folder -> (time, list)


def list_files(cwd, cap=20000):
    cached = _files.get(cwd)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    try:
        result = subprocess.run(['git', '-C', cwd, 'ls-files', '--cached', '--others', '--exclude-standard'],
                                capture_output=True, text=True, timeout=4)
        files = result.stdout.splitlines()[:cap] if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        files = None
    if files is None:
        files = []
        for root, folders, names in os.walk(cwd):
            folders[:] = [name for name in folders if name not in SKIP and not name.startswith('.')]
            if root[len(cwd):].count(os.sep) >= 4:
                folders[:] = []
            files += [os.path.relpath(os.path.join(root, name), cwd) for name in names]
            if len(files) >= cap:
                break
    folders = sorted({str(Path(name).parent) + '/' for name in files if '/' in name})
    entries = folders + files
    _files[cwd] = (time.time(), entries)
    return entries


def find_files(cwd, query, limit=30):
    """Paths under the pane's folder that contain the query, best match first: name start, then name, then path."""
    if not cwd or not os.path.isdir(cwd):
        return []
    query = query.lower().lstrip('@')
    scored = []
    for entry in list_files(cwd):
        lower = entry.lower()
        if query and query not in lower:
            continue
        name = lower.rstrip('/').rsplit('/', 1)[-1]
        rank = 0 if name.startswith(query) else 1 if query in name else 2
        scored.append((rank, entry.count('/'), len(entry), entry))
    return [entry for *_, entry in sorted(scored)[:limit]]
