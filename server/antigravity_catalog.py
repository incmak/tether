"""Read-only catalog for Antigravity CLI panes."""
import getpass
import json
import os
from pathlib import Path
import re

from catalog_metadata import front_matter, metadata_text, short
import paths


# Antigravity CLI 1.5.19 slash menu, in menu order. Argument syntax and secondary aliases
# come from https://antigravity.google/docs/cli/reference#core-slash-commands.
COMMANDS = (
    ('add-dir', 'Add a folder to this workspace', (), '<path>'),
    ('agents', 'List available custom agents', (), ''),
    ('artifact', 'View and review artifacts', (), ''),
    ('btw', 'Ask a side question while the main task runs', (), '<question>'),
    ('changelog', 'View Antigravity CLI release notes', (), ''),
    ('clear', 'Start a new conversation', ('new',), ''),
    ('codesearch', 'Search code in the workspace', ('cs', 'search'), '<query>'),
    ('config', 'Open Antigravity settings', ('settings',), ''),
    ('context', 'View context usage', (), ''),
    ('copy', 'Copy an agent response or side answer', (), '[n|btw]'),
    ('credits', 'View AI credits and purchase links', (), ''),
    ('diff', 'Review changes and diffs by turn', (), ''),
    ('effort', 'View or set reasoning effort', (), '[level]'),
    ('exit', 'Exit Antigravity CLI', ('quit',), ''),
    ('feedback', 'Open the feedback panel', (), ''),
    ('fork', 'Branch the current conversation', ('branch',), '[name]'),
    ('help', 'View commands and keyboard shortcuts', (), ''),
    ('hooks', 'View and manage lifecycle hooks', (), ''),
    ('keybindings', 'Edit keyboard shortcuts', (), ''),
    ('logout', 'Sign out of Antigravity CLI', (), ''),
    ('mcp', 'Open the MCP server manager', (), ''),
    ('model', 'Choose a model or run one prompt with another model', (), '[model] [prompt]'),
    ('open', 'Open a file in your editor', (), '<path>'),
    ('permissions', 'View and manage tool permissions', (), ''),
    ('plugin', 'Browse and manage plugins', ('plugins',), '[action]'),
    ('projects', 'Create and edit projects', (), ''),
    ('record', 'Dictate a prompt', (), ''),
    ('remote-control', 'Toggle Remote Control for this session', (), '[on|off]'),
    ('rename', 'Rename the current conversation', (), '<name>'),
    ('resume', 'Browse and resume past conversations', ('switch', 'conversation'), ''),
    ('rewind', 'Return to an earlier message', ('undo',), ''),
    ('skills', 'Browse available skills or reload them from disk', (), '[reload]'),
    ('statusline', 'Configure the terminal status line', (), ''),
    ('tasks', 'View background tasks', (), ''),
    ('thinking', 'Toggle streaming of the model’s thinking', (), ''),
    ('title', 'Toggle updates to the terminal title', (), '[on|off]'),
    ('usage', 'View model quota usage', ('quota',), ''),
    ('voice', 'Dictate a prompt or start a live voice session', (), '[live|dictate]'),
    ('goal', 'Run until the specified goal is complete', (), '<objective>'),
    ('compact', 'Summarize the conversation to free context', (), '[message]'),
    ('schedule', 'Schedule a one-time or recurring instruction', (), '<when> <task>'),
    ('browser', 'Ask the browser agent to complete a web task', (), '<task>'),
    ('plan', 'Explore a task and draft a plan', (), '[task]'),
    ('grill-me', 'Discuss a plan one question at a time', (), '[task]'),
    ('learn', 'Propose reusable skills or rules from this conversation', (), '[instruction]'),
)
BUNDLED_SKILLS = (
    ('agy-customizations', 'Get help with rules, skills, plugins, hooks and MCP', '[request]'),
    ('antigravity-guide', 'Get help with Antigravity from Google’s documentation', '[question]'),
    ('run-workflow', 'Write and run a dynamic workflow', '[task]'),
    ('swarm', 'Run a swarm of agents or open the swarm board', '[board|task]'),
)

CUSTOM_DIRS = ('.agents', '.agent', '_agents', '_agent')
SKILL_FILE = ('SKILL.md',)
PLUGIN_FILES = ('.antigravity-plugin/plugin.json', 'plugin.json', '.gemini-plugin/plugin.json')
COMMAND_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')
MANIFEST_LIMIT = 1024 * 1024


def object_value(value):
    return value if isinstance(value, dict) else {}


def configuration(path):
    try:
        value = json.loads(metadata_text(path, MANIFEST_LIMIT, whole=True))
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def string_set(value):
    return {part for part in value if isinstance(part, str)} if isinstance(value, list) else set()


def entries(config, key):
    value = config.get(key)
    return (entry for entry in value if isinstance(entry, dict)) if isinstance(value, list) else ()


def workspace_roots(cwd):
    """Yield customization directories in precedence order, from the repository root inward."""
    if not cwd:
        return
    try:
        workspace = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return
    lineage = (workspace, *workspace.parents)
    repo = next((folder for folder in lineage if (folder / '.git').exists()), workspace)
    config = configuration(repo / '.gemini' / 'config.json') or {}
    personal = config.get('personal_customization_dir')
    if isinstance(personal, str) and personal and '\0' not in personal:
        user = os.environ.get('USER') or os.environ.get('USERNAME') or getpass.getuser()
        yield repo / personal.replace('{user}', user) / '.agents', repo, repo, 'personal workspace'
    for folder in reversed(lineage[:lineage.index(repo) + 1]):
        for custom in CUSTOM_DIRS:
            yield folder / custom, folder, repo, 'workspace'


def marker(folder, filenames):
    try:
        return next((folder / name for name in filenames if (folder / name).is_file()), None)
    except OSError:
        return None


def customizations(folder, filenames, include=()):
    """List marked folders one level deep, plus explicitly named nested folders."""
    try:
        if folder.name == Path(filenames[0]).name and folder.is_file():
            parent = folder.parent
            if parent.name in ('.antigravity-plugin', '.gemini-plugin'):
                parent = parent.parent
            return [(parent.name, parent)]
        if marker(folder, filenames):
            return [(folder.name, folder)]
        found = {child.name: child for child in folder.iterdir() if marker(child, filenames)}
    except OSError:
        return []
    for name in include:
        path = Path(name)
        if len(path.parts) > 1 and not path.is_absolute() and '..' not in path.parts and marker(folder / path, filenames):
            found[path.as_posix()] = folder / path
    return sorted(found.items())


def declared_path(value, base, fallback):
    if not isinstance(value, str) or not value or '\0' in value:
        return None
    local = base / value
    try:
        return local if local.exists() else fallback / value
    except OSError:
        return None


def accepted(found, options):
    include = string_set(options.get('include_only'))
    exclude = string_set(options.get('exclude'))
    for relative, folder in found:
        names = {relative, folder.name}
        if (not include or names & include) and not names & exclude:
            yield relative, folder


def manifest_folders(path, base, fallback, filenames, hidden, cache, active=()):
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return ()
    # An inheritance cycle (including through a symlink) must not break the catalog.
    if resolved in active or len(active) >= 32:
        return ()
    key = resolved, base, fallback
    if key in cache:
        return cache[key]
    config = configuration(path) or {}
    hidden.update(string_set(config.get('exclude')))
    found = {}
    for entry in entries(config, 'inherits'):
        inherited = declared_path(entry.get('path'), base, fallback)
        if inherited is None or inherited.suffix != '.json':
            continue
        directory = inherited.parent
        anchor = directory.parent if directory.name in CUSTOM_DIRS else directory
        included = manifest_folders(inherited, anchor, anchor, filenames, hidden, cache, (*active, resolved))
        found.update(dict.fromkeys(accepted(included, entry)))
    for entry in entries(config, 'entries'):
        location = declared_path(entry.get('path'), base, fallback)
        if location is not None:
            folders = customizations(location, filenames, string_set(entry.get('include_only')))
            found.update(dict.fromkeys(accepted(folders, entry)))
    result = cache[key] = tuple(found)
    return result


def discover(roots, kind, filenames):
    hidden = set()
    found = []
    cache = {}
    for folder, base, fallback, source in roots:
        locations = customizations(folder / kind, filenames)
        locations.extend(manifest_folders(folder / (kind + '.json'), base, fallback, filenames, hidden, cache))
        found.extend((relative, location, source) for relative, location in locations)
    return found, hidden


def add_skill(commands, folder, prefix='', hidden=(), relative=''):
    meta = front_matter(folder / 'SKILL.md')
    name = meta.get('name') or folder.name
    description = short(meta.get('description'))
    command = prefix + name
    if description and COMMAND_NAME.fullmatch(name) and not any(key in hidden for key in (name, command, folder.name, relative)):
        commands.setdefault(command, {'name': command, 'description': description, 'aliases': [],
                                      'hint': meta.get('argument-hint', ''), 'kind': 'skill'})


def add_mcp(mcp, path, source, prefix=''):
    config = configuration(path) or {}
    for name, server in object_value(config.get('mcpServers')).items():
        if name and isinstance(server, dict):
            name = prefix + name
            mcp.setdefault(name, {'name': name, 'source': source})


def build(cwd):
    gemini = paths.ANTIGRAVITY.parent
    global_config = gemini / 'config'
    roots = list(workspace_roots(cwd))
    global_root = (global_config, global_config, global_config, 'global')
    commands = {name: {'name': name, 'description': text, 'aliases': list(aliases), 'hint': hint, 'kind': 'builtin'}
                for name, text, aliases, hint in COMMANDS}
    for folder in (paths.ANTIGRAVITY / 'skills', gemini / 'skills'):
        for _, skill in customizations(folder, SKILL_FILE):
            add_skill(commands, skill)
    skills, hidden = discover([*roots, global_root], 'skills', SKILL_FILE)
    skills.extend((relative, skill, 'global')
                  for relative, skill in customizations(paths.HOME / '.agents' / 'skills', SKILL_FILE))
    for relative, skill, _ in skills:
        add_skill(commands, skill, hidden=hidden, relative=relative)

    plugins, mcp = {}, {}
    for folder, _, _, source in roots:
        add_mcp(mcp, folder / 'mcp_config.json', source)
    add_mcp(mcp, global_config / 'mcp_config.json', 'global')
    preference = object_value((configuration(global_config / 'config.json') or {}).get('plugins'))
    plugin_dirs, hidden_plugins = discover([*roots, global_root], 'plugins', PLUGIN_FILES)
    plugin_dirs.extend((relative, folder, 'built-in') for relative, folder in
                       customizations(paths.ANTIGRAVITY / 'builtin' / 'plugins', PLUGIN_FILES))
    for relative, folder, source in plugin_dirs:
        name = folder.name  # CLI namespaces skill names and keys plugin preferences by folder name.
        if name in plugins or name in hidden_plugins or relative in hidden_plugins or not COMMAND_NAME.fullmatch(name):
            continue
        manifest = marker(folder, PLUGIN_FILES)
        config = configuration(manifest) if manifest is not None else None
        if config is None:
            continue
        enabled = object_value(preference.get(name)).get('enabled')
        if not isinstance(enabled, bool):
            enabled = config.get('disabled') is not True
        plugins[name] = {'name': name, 'source': source, 'enabled': enabled}
        if enabled:
            for _, skill in customizations(folder / 'skills', SKILL_FILE):
                add_skill(commands, skill, prefix=name + ':', hidden=hidden)
            add_mcp(mcp, folder / 'mcp_config.json', 'plugin ' + name, prefix=name + '_')

    for name, text, hint in BUNDLED_SKILLS:
        if name not in hidden:
            commands.setdefault(name, {'name': name, 'description': text, 'aliases': [], 'hint': hint, 'kind': 'skill'})
    return {'commands': list(commands.values()), 'plugins': list(plugins.values()), 'mcp': list(mcp.values()),
            'note': 'Based on local files and Antigravity CLI 1.5.19. Check the terminal for your active commands and servers.'}
