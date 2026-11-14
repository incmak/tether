#!/usr/bin/env python3
"""Codex level: how much Claude hands to Codex, from 0 (off) to 5 (Codex does everything). The person sets it. Nothing raises it.

  paths.CONFIG/codex-level              the global level: one digit, 0-5. Missing or bad: 0.
  paths.CONFIG/codex-run.json           how Claude runs Codex: {"model": "auto"|slug, "effort": "auto"|level, "fast": bool}.
  paths.TETHER/projects/<id>/codex-level  a project override: {"override": true, "level": 3}. With "override": false
                                          the project keeps its level but uses the global one.

The Claude Code UserPromptSubmit hook (`codex_level.py hook`) prints the active level's rules on each prompt, so a change
applies on the next prompt of every session. The hook finds the project from TMUX_PANE (the session's @project, like
project_launch.py), or else from the hook's cwd when exactly one project has a pane in that folder. It only reads tmux.

runs() counts Codex runs for the UI: today = Codex exec sessions in today's ~/.codex/sessions folder, running = live
`codex exec` and codex-lane processes, by tmux pane when a pane's process is an ancestor."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import paths  # noqa: E402
import procinfo  # noqa: E402
import prompt_context  # noqa: E402

LEVELS = (
    ('Off', 'No Codex.'),
    ('Second opinion', 'Codex reviews plans, diffs and hard bugs. Claude does the work.'),
    ('Assist', 'Codex does research, tests and small tasks with a clear spec.'),
    ('Split', 'Claude plans and writes specs. Codex implements them. Claude reviews.'),
    ('Codex first', 'Codex does the implementation. Claude plans, writes specs and does the final review.'),
    ('Codex does everything', 'Codex does all the work, UI included. Claude gives Codex its tasks and checks the results.'),
)
RULES = (
    'Do not use Codex. Use it only when the user asks for it in this chat.',
    'Use Codex only for a second opinion: a review of a plan, a diff or a hard bug. Do all the work yourself.',
    'Give Codex research, tests and small tasks that have a clear spec. Do the design, the main code and the review '
    'yourself.',
    'Plan the work and write a spec for each part. Codex implements each spec. Review each Codex change and run the '
    'checks yourself.',
    'Plan the work, write the specs and do the final review. Codex does the implementation, the tests and the research. '
    'Write code yourself only for small fixes after a review.',
    'Give all the work to Codex: research, design, UI design and prototyping, code and tests. You only split the work '
    'into tasks, give each task to Codex, check its results and run the checks. Do a task yourself only when Codex '
    'cannot do it, or a small fix is faster than a Codex run.',
)
UI_RULE = 'UI design and prototyping stay with you. Do not give them to Codex.'
EVERYTHING = len(RULES) - 1
COMMON = (
    'Run Codex as `codex-lane -- <codex exec options> "<prompt>"`. codex-lane adds `exec` itself; do not repeat it. '
    'Or run `codex exec ... </dev/null`. Without one of these, Codex waits on stdin forever.',
    'Give Codex a full spec: the goal, the files, the limits and the checks. Read its result before you use it.',
    'Do not change the level. The user sets it in Tether.',
)
PROJECT_ID = re.compile(r'[0-9a-f]{12}')


class LevelError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---- the files ----

def global_path():
    return paths.CONFIG / 'codex-level'


def project_path(pid):
    if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
        raise LevelError(400, 'Choose a project.')
    return paths.TETHER / 'projects' / pid / 'codex-level'


def _valid(level):
    return isinstance(level, int) and not isinstance(level, bool) and 0 <= level < len(LEVELS)


def read_global():
    try:
        text = global_path().read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return 0
    return int(text) if re.fullmatch(r'[0-5]', text) else 0


def read_project(pid):
    """{'override': bool, 'level': int}, or None when the project has no usable file."""
    try:
        data = json.loads(project_path(pid).read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError, LevelError):
        return None
    if not isinstance(data, dict) or not _valid(data.get('level')) or not isinstance(data.get('override'), bool):
        return None
    return {'override': data['override'], 'level': data['level']}


def _write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_global(level):
    if not _valid(level):
        raise LevelError(400, f'Choose a level from 0 to {len(LEVELS) - 1}.')
    _write(global_path(), f'{level}\n')


def set_project(pid, override, level):
    if not isinstance(override, bool) or not _valid(level):
        raise LevelError(400, f'Choose a level from 0 to {len(LEVELS) - 1}.')
    _write(project_path(pid), json.dumps({'override': override, 'level': level}) + '\n')


def resolve(pid=None):
    """The level that applies, and where it comes from. An override that is on beats the global level."""
    base = read_global()
    project = read_project(pid) if pid else None
    if project and project['override']:
        return {'level': project['level'], 'source': 'project', 'global': base, 'project': project}
    return {'level': base, 'source': 'global', 'global': base, 'project': project}


def rules(resolved):
    level = resolved['level']
    where = 'for this project' if resolved['source'] == 'project' else 'for all projects'
    head = f'Codex level {level} of {len(LEVELS) - 1} ({LEVELS[level][0]}), set by the user {where}.'
    lines = [head, RULES[level]]
    if level:
        if level != EVERYTHING:
            lines.append(UI_RULE)
        lines += COMMON
        lines.append(run_rule(read_run()))
    return '\n'.join(lines)


# ---- how Claude runs Codex: model, effort, fast ----

RUN_DEFAULT = {'model': 'auto', 'effort': 'auto', 'fast': False}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


def run_path():
    return paths.CONFIG / 'codex-run.json'


def models(codex_home=None):
    """[{'slug', 'name', 'efforts', 'fast'}] from Codex's own model cache, in Codex's order. Hidden models are left out."""
    home = Path(codex_home or os.environ.get('CODEX_HOME') or paths.CODEX)
    try:
        data = json.loads((home / 'models_cache.json').read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return []
    out = []
    for item in data.get('models', []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get('visibility') != 'list' or not SLUG.fullmatch(str(item.get('slug', ''))):
            continue
        efforts = [level.get('effort') for level in item.get('supported_reasoning_levels') or []
                   if isinstance(level, dict) and level.get('effort') in EFFORTS]
        out.append({'slug': item['slug'], 'name': str(item.get('display_name') or item['slug'])[:60], 'efforts': efforts,
                    'fast': 'fast' in (item.get('additional_speed_tiers') or [])})
    return out


def _valid_run(run):
    return (isinstance(run, dict) and set(run) == set(RUN_DEFAULT) and isinstance(run['fast'], bool)
            and isinstance(run['model'], str) and (run['model'] == 'auto' or SLUG.fullmatch(run['model']))
            and run['effort'] in ('auto', *EFFORTS))


def read_run():
    try:
        run = json.loads(run_path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return dict(RUN_DEFAULT)
    return run if _valid_run(run) else dict(RUN_DEFAULT)


def set_run(run):
    if not _valid_run(run):
        raise LevelError(400, 'Choose a model, an effort and fast on or off.')
    _write(run_path(), json.dumps(run) + '\n')


def run_rule(run):
    """One line that tells Claude which codex exec options to pass."""
    options = []
    if run['model'] != 'auto':
        options.append(f'-m {run["model"]}')
    if run['effort'] != 'auto':
        options.append(f'-c model_reasoning_effort="{run["effort"]}"')
    if run['fast']:
        options.append('-c service_tier="fast"')
    parts = []
    if options:
        parts.append(f'Pass these options to each Codex run: `{" ".join(options)}`.')
    auto = [name for name, key in (('model', 'model'), ('effort', 'effort')) if run[key] == 'auto']
    if auto:
        parts.append(f'Choose the {" and the ".join(auto)} for each task: a smaller effort (low, medium) for lookups and '
                     'small edits, a larger one (high, xhigh) for hard bugs and big changes.')
    if not run['fast']:
        parts.append('Do not turn on fast mode.')
    return ' '.join(parts)


# ---- the hook ----

def _tmux(*args):
    return subprocess.run([paths.TMUX, *args], capture_output=True, text=True, timeout=2)


def hook_project(cwd, env=os.environ, tmux=_tmux):
    """The Tether project of the Claude session that runs the hook, or None. Read only: it never sets @project."""
    pane = env.get('TMUX_PANE', '')
    if env.get('TMUX') and re.fullmatch(r'%[0-9]+', pane):
        shown = tmux('display-message', '-p', '-t', pane, '#{@project}')
        pid = shown.stdout.strip()
        if not shown.returncode and PROJECT_ID.fullmatch(pid):
            return pid
        return None
    if not isinstance(cwd, str) or not cwd.startswith('/'):
        return None
    shown = tmux('list-panes', '-a', '-F', '#{pane_current_path}\t#{@project}')
    if shown.returncode:
        return None
    found = {fields[1] for fields in (line.split('\t') for line in shown.stdout.splitlines())
             if len(fields) == 2 and fields[0] == cwd and PROJECT_ID.fullmatch(fields[1])}
    return found.pop() if len(found) == 1 else None


def hook_output(data, env=os.environ, tmux=_tmux, event='UserPromptSubmit'):
    """The hook's stdout for one UserPromptSubmit input, as a JSON string.

    The rules go out once per conversation (prompt_context.py). With event='SessionStart' (after a compaction) it
    returns the full rules as plain text and records them as sent."""
    cwd = data.get('cwd') if isinstance(data, dict) else None
    try:
        pid = hook_project(cwd, env, tmux)
    except (OSError, subprocess.SubprocessError):
        pid = None
    text = rules(resolve(pid))
    if event == 'SessionStart':
        prompt_context.mark(data, 'codex-level', text)
        return text
    text = prompt_context.once(data, 'codex-level', text)
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}})


def hook():
    """Fail open: any problem prints nothing and exits 0, so the prompt always goes through."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        out = hook_output(data)
    except Exception:  # noqa: BLE001 - a hook must never stop a prompt
        return
    print(out)


# ---- Codex runs ----

INTERPRETERS = {'node', 'bash', 'sh', 'zsh', 'dash', 'env'}
VALUE_OPTIONS = {'-c', '--config', '-p', '--profile', '-m', '--model', '-C', '--cd', '-s', '--sandbox', '-a',
                 '--ask-for-approval', '-i', '--image', '--enable', '--disable'}
CACHE_SECONDS = 5
_cache = {'at': 0.0, 'value': None}
_today_seen = {}  # rollout file name -> True when it is a Codex exec session (a file never changes its first line)
_lock = threading.Lock()


def is_run(argv):
    """True for `codex exec ...` or `codex-lane ...`, also run through node or a shell script line. `grep codex exec`
    is not a run: the program must be argv[0], or argv[1] after an interpreter."""
    if not argv:
        return False
    start = 1 if os.path.basename(argv[0]) in INTERPRETERS and len(argv) > 1 else 0
    if start and argv[1].startswith('-'):
        return False
    name = os.path.basename(argv[start])
    if name == 'codex-lane':
        return True
    if name not in ('codex', 'codex.js'):
        return False
    rest = argv[start + 1:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in VALUE_OPTIONS:
            index += 2
            continue
        if arg.startswith('-'):
            index += 1
            continue
        return arg in ('exec', 'e')
    return False


def processes(proc=Path('/proc')):
    """{pid: (ppid, argv)} for every process we can read."""
    if procinfo.DARWIN:
        out = {}
        for pid in procinfo.pids():
            try:
                argv = procinfo.argv(pid)
                out[pid] = (procinfo.parent(pid), [part.decode(errors='replace') for part in argv if part])
            except OSError:
                continue
        return out
    out = {}
    for pid in procinfo.pids(proc):
        try:
            stat = procinfo.read_text(proc / str(pid) / 'stat')
            argv = procinfo.argv(pid, proc)
        except OSError:
            continue
        ppid = int(stat.rsplit(')', 1)[1].split()[1])
        out[pid] = (ppid, [part.decode(errors='replace') for part in argv if part])
    return out


def running(procs, pane_pids):
    """(total, {pane id: count}). A run inside another run (codex-lane -> codex exec, node -> native codex) counts once."""
    owner = {pid: pane for pane, pid in pane_pids.items()}
    total, by_pane = 0, {}
    for pid, (ppid, argv) in procs.items():
        if not is_run(argv):
            continue
        parent, pane, nested, seen = ppid, owner.get(pid), False, set()
        while parent > 1 and parent in procs and parent not in seen:
            seen.add(parent)
            if is_run(procs[parent][1]):
                nested = True
                break
            pane = pane or owner.get(parent)
            parent = procs[parent][0]
        if nested:
            continue
        total += 1
        if pane:
            by_pane[pane] = by_pane.get(pane, 0) + 1
    return total, by_pane


def today_count(now=None, codex_home=None):
    """Codex exec sessions started today (local date): one rollout file each, in sessions/YYYY/MM/DD/."""
    home = Path(codex_home or os.environ.get('CODEX_HOME') or paths.CODEX)
    folder = home / 'sessions' / time.strftime('%Y/%m/%d', time.localtime(now))
    count = 0
    try:
        names = [entry.name for entry in os.scandir(folder) if entry.name.endswith('.jsonl')]
    except OSError:
        return 0
    for key in [key for key in _today_seen if not key.startswith(f'{folder}/')]:
        del _today_seen[key]  # yesterday's files
    for name in names:
        key = f'{folder}/{name}'
        if key not in _today_seen:
            try:
                with open(folder / name, 'rb') as source:
                    head = source.read(4096)
            except OSError:
                continue
            exec_run = b'"originator":"codex_exec"' in head or b'"source":"exec"' in head
            if not exec_run and b'\n' not in head:  # the first line is not complete yet: look again next time
                continue
            _today_seen[key] = exec_run
        count += _today_seen[key]
    return count


def pane_list(tmux=_tmux):
    """[(pane id, pane pid, label)] for every tmux pane."""
    shown = tmux('list-panes', '-a', '-F', '#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_index}.#{pane_index}')
    if shown.returncode:
        return []
    out = []
    for line in shown.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) == 4 and re.fullmatch(r'%[0-9]+', fields[0]) and fields[1].isdigit():
            out.append((fields[0], int(fields[1]), f'{fields[2]} {fields[3]}'))
    return out


def runs(now=None, tmux=_tmux, proc=Path('/proc'), codex_home=None):
    """{'today', 'running', 'panes': [{'pane', 'label', 'running'}]}, cached for a few seconds."""
    clock = time.monotonic()
    with _lock:
        if _cache['value'] is not None and clock - _cache['at'] < CACHE_SECONDS:
            return _cache['value']
        try:
            panes = pane_list(tmux)
        except (OSError, subprocess.SubprocessError):
            panes = []
        total, by_pane = running(processes(proc), {pane: pid for pane, pid, _ in panes})
        labels = {pane: label for pane, _, label in panes}
        value = {'today': today_count(now, codex_home), 'running': total,
                 'panes': [{'pane': pane, 'label': labels.get(pane, pane), 'running': count}
                           for pane, count in sorted(by_pane.items(), key=lambda item: int(item[0][1:]))]}
        _cache.update(at=clock, value=value)
        return value


# ---- HTTP (/mobile/api/codex-level). server.py checks sign-in, Origin and X-Codex-Mobile first. ----

PATH = '/mobile/api/codex-level'


def handles(path):
    return path.split('?', 1)[0] == PATH


def state(pid=None):
    resolved = resolve(pid)
    return {'levels': [{'level': index, 'name': name, 'summary': summary} for index, (name, summary) in enumerate(LEVELS)],
            'global': resolved['global'], 'level': resolved['level'], 'source': resolved['source'],
            'project': resolved['project'], 'runs': runs(), 'run': read_run(), 'models': models()}


def get(handler):
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(handler.path).query)
    pid = query.get('project', [None])[0]
    try:
        if pid is not None:
            project_path(pid)
        handler.respond(200, state(pid))
    except LevelError as exc:
        handler.respond(exc.status, {'error': str(exc)})


def post(handler):
    """{level} sets the global level. {project, override, level} sets a project override. {run} sets the Codex options."""
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 4096:
            raise LevelError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise LevelError(400, 'Invalid request.')
        pid = body.get('project')
        if 'run' in body:
            set_run(body['run'])
        elif pid is None:
            set_global(body.get('level'))
        else:
            set_project(pid, body.get('override'), body.get('level'))
        handler.respond(200, state(pid))
    except LevelError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not save the Codex level.'})


if __name__ == '__main__':
    if sys.argv[1:] == ['hook']:
        hook()
    elif sys.argv[1:] == ['show']:
        print(rules(resolve(hook_project(os.getcwd()))))
    else:
        sys.exit(__doc__)
