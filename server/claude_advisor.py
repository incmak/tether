#!/usr/bin/env python3
"""Claude Code's advisor tool for the Claude tabs that Tether starts: Off, Opus or Fable. The person sets it.

  paths.CONFIG/claude-advisor              the global choice: off, opus or fable. Missing or bad: off.
  paths.TETHER/projects/<id>/claude-advisor  a project override: {"override": true, "model": "opus"}. With
                                             "override": false the project keeps its choice but uses the global one.

project_launch.py adds the choice as `advisorModel` to the --settings JSON of each Claude tab it starts, and records it
in the pane option @advisor. It never passes --advisor: a bad --advisor value stops the tab at launch, while a setting
that cannot apply only leaves the advisor off (code.claude.com/docs/en/advisor). Off adds nothing, so an advisorModel
in the person's own Claude settings still applies. A change applies to tabs that start or restart after it.

The advisor runs server-side. Claude Code attaches it only when it can (code.claude.com/docs/en/advisor):
- the advisor ranks at or above the main model (a Fable main model takes no Opus advisor);
- feature flags are fetched (DISABLE_TELEMETRY and similar variables turn the fetch off);
- CLAUDE_CODE_DISABLE_ADVISOR_TOOL is not set;
- for Fable on some plans, the person accepted the usage-credits consent with /model fable.
Claude Code does not report these cases outside its own screen, so problems() checks the inputs it can read and
names each case. Tether never accepts the Fable consent: that bills usage credits, and it is the person's choice."""
import json
import os
import re
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import paths  # noqa: E402

MODELS = (
    ('off', 'Off', 'Tether adds no advisor.'),
    ('opus', 'Opus', 'Claude consults Opus before it commits to an approach, when an error repeats, and before it '
     'reports a task done. Advisor calls count toward your plan limits.'),
    ('fable', 'Fable', 'Claude consults Fable at the same points. On some plans, Fable advisor calls bill to usage '
     'credits, and Claude Code first needs your one-time consent from /model fable.'),
)
NAMES = {key: name for key, name, _ in MODELS}
PROJECT_ID = re.compile(r'[0-9a-f]{12}')
# Families from weakest to strongest for the advisor role (code.claude.com/docs/en/advisor#choose-an-advisor-model).
# Opus 5 and 5.5 accept Fable or Opus 5+; a Fable main model accepts only Fable. The aliases resolve to the newest
# version of each family, so only the family decides whether the main model outranks the advisor.
RANK = ('haiku', 'sonnet', 'opus', 'fable')
# Variables that turn the advisor off, or turn off the feature-flag fetch it needs
# (code.claude.com/docs/en/env-vars#features-that-need-feature-flag-fetching).
OFF_VARIABLES = ('CLAUDE_CODE_DISABLE_ADVISOR_TOOL',)
NO_FLAG_VARIABLES = ('CLAUDE_CODE_DISABLE_FEATURE_FLAGS', 'DISABLE_GROWTHBOOK', 'DISABLE_TELEMETRY', 'DO_NOT_TRACK',
                     'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC')


class AdvisorError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---- the files ----

def global_path():
    return paths.CONFIG / 'claude-advisor'


def project_path(pid):
    if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
        raise AdvisorError(400, 'Choose a project.')
    return paths.TETHER / 'projects' / pid / 'claude-advisor'


def _valid(model):
    return isinstance(model, str) and model in NAMES


def read_global():
    try:
        text = global_path().read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return 'off'
    return text if _valid(text) else 'off'


def read_project(pid):
    """{'override': bool, 'model': str}, or None when the project has no usable file."""
    try:
        data = json.loads(project_path(pid).read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError, AdvisorError):
        return None
    if not isinstance(data, dict) or not _valid(data.get('model')) or not isinstance(data.get('override'), bool):
        return None
    return {'override': data['override'], 'model': data['model']}


def _write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_global(model):
    if not _valid(model):
        raise AdvisorError(400, 'Choose Off, Opus or Fable.')
    _write(global_path(), f'{model}\n')


def set_project(pid, override, model):
    if not isinstance(override, bool) or not _valid(model):
        raise AdvisorError(400, 'Choose Off, Opus or Fable.')
    _write(project_path(pid), json.dumps({'override': override, 'model': model}) + '\n')


def resolve(pid=None):
    """The choice that applies, and where it comes from. An override that is on beats the global choice."""
    base = read_global()
    project = read_project(pid) if pid else None
    if project and project['override']:
        return {'model': project['model'], 'source': 'project', 'global': base, 'project': project}
    return {'model': base, 'source': 'global', 'global': base, 'project': project}


# ---- what Claude Code will do with it ----

def _json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set(value):
    return isinstance(value, str) and value.strip().lower() not in ('', '0', 'false', 'no', 'off')


def environment(env=os.environ, home=None):
    """{name: value} of the advisor variables a new tab gets: the server's environment, then the env block of the
    person's Claude settings, which Claude Code applies on top."""
    claude = Path(home) / '.claude' if home else paths.CLAUDE
    merged = {name: env.get(name) for name in (*OFF_VARIABLES, *NO_FLAG_VARIABLES) if env.get(name) is not None}
    block = _json(claude / 'settings.json').get('env')
    if isinstance(block, dict):
        merged.update({name: str(value) for name, value in block.items() if name in (*OFF_VARIABLES, *NO_FLAG_VARIABLES)})
    return {name: value for name, value in merged.items() if _set(value)}


def user_setting(home=None):
    """advisorModel in ~/.claude/settings.json (set by /advisor), or ''."""
    claude = Path(home) / '.claude' if home else paths.CLAUDE
    value = _json(claude / 'settings.json').get('advisorModel')
    return value if isinstance(value, str) else ''


def fable_consent(home=None):
    """True when ~/.claude.json records a Fable usage-credits consent for the active account that holds in every
    consent mode (fableOverageConsentV2, Claude Code 2.1.326). False otherwise, which means "may not attach": a plan
    that includes Fable needs no consent."""
    config = _json((Path(home) if home else paths.HOME) / '.claude.json')
    data, account = config.get('fableOverageConsentV2'), config.get('oauthAccount')
    if not isinstance(data, dict) or not isinstance(account, dict):
        return False
    # Claude Code keys the consent by the active organization or account. A server feature flag that Tether cannot
    # read decides whether it needs the key with the ":2" suffix. A consent given in that mode writes both keys, so
    # only a key pair holds in either mode; a plain key alone may be from an older consent.
    owners = [value for value in (account.get('organizationUuid'),
                                  f"acct:{account['accountUuid']}" if account.get('accountUuid') else None)
              if isinstance(value, str) and value]
    return any(data.get(owner) is True and data.get(owner + ':2') is True for owner in owners)


def family(model):
    """'haiku', 'sonnet', 'opus' or 'fable' for an alias, a model id or a display name; '' when unknown."""
    text = str(model or '').lower()
    found = [name for name in RANK if re.search(rf'(^|[^a-z]){name}([^a-z]|$)', text)]
    return found[0] if len(found) == 1 else ''


def outranked(advisor, main_model):
    """True when the main model ranks above the advisor, so Claude Code does not attach it to the main model."""
    have, main = family(advisor), family(main_model)
    return bool(have and main) and RANK.index(main) > RANK.index(have)


def env_problems(env=os.environ, home=None):
    """[{'code', 'text'}] for the variables that keep the advisor off in a tab started with this environment."""
    out = []
    found = environment(env, home)
    for name in OFF_VARIABLES:
        if name in found:
            out.append({'code': 'disabled', 'text': f'{name} is set, so Claude Code ignores the advisor.'})
    blocked = [name for name in NO_FLAG_VARIABLES if name in found]
    if blocked:
        out.append({'code': 'no-flags', 'text': f'{" and ".join(blocked)} turns off the feature-flag fetch that '
                                                'the advisor needs, so Claude Code starts without it.'})
    return out


def problems(model, env=os.environ, home=None, main_model='', started=None):
    """[{'code', 'text'}] for each known reason the advisor will not attach. Empty for Off.
    started: the environment problems a running tab recorded at launch; None checks the given env now."""
    if model == 'off':
        return []
    out = list(env_problems(env, home) if started is None else started)
    if model == 'fable' and not fable_consent(home):
        out.append({'code': 'consent', 'text': 'Claude Code has no record of a current Fable usage-credits consent for this account. If your '
                                               'plan needs it, Claude Code does not attach Fable. To review it, run '
                                               '/model fable in a Claude tab.'})
    if main_model and outranked(model, main_model):
        out.append({'code': 'outranked', 'text': f'The main model ({main_model}) ranks above {NAMES[model]}, so Claude '
                                                 'Code does not attach the advisor to it.'})
    return out


def launch_settings(pid=None):
    """The advisorModel entry for a new Claude tab's --settings JSON: {} for Off."""
    model = resolve(pid)['model']
    return {} if model == 'off' else {'advisorModel': model}


def mark(model, env=os.environ, home=None):
    """The pane option @advisor for a tab that starts with this model: "opus", or "opus!no-flags" when the launch
    environment keeps it off. The codes stay with the tab, since the server's environment can differ from it."""
    codes = [item['code'] for item in env_problems(env, home)]
    return model + ('!' + ','.join(codes) if codes else '')


MARK = re.compile(r'(opus|fable)(?:!((?:disabled|no-flags)(?:,(?:disabled|no-flags))?))?')
STARTED_TEXT = {'disabled': 'This tab started with CLAUDE_CODE_DISABLE_ADVISOR_TOOL set, so Claude Code ignores '
                            'the advisor.',
                'no-flags': 'This tab started with feature-flag fetching off (for example DISABLE_TELEMETRY), so '
                            'Claude Code started it without the advisor.'}


def pane_mark(pane):
    """The pane's @advisor value, or '' when it has none or tmux does not answer."""
    import subprocess
    if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
        return ''
    try:
        shown = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{@advisor}'], capture_output=True,
                               text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ''
    return shown.stdout.strip() if not shown.returncode else ''


def tab_state(value, main_model='', home=None):
    """{'model', 'name', 'source', 'problems'} for a Claude tab's @advisor value. source 'tether': Tether set it at
    launch. Without a Tether mark: source 'claude' when the person's own Claude settings name an advisor (the tab may
    use it), else None. A value Tether did not write is refused, never guessed."""
    found = MARK.fullmatch(value.strip()) if isinstance(value, str) else None
    if not found:
        own = user_setting(home)
        return {'model': own, 'name': own, 'source': 'claude', 'problems': []} if own else None
    model = found.group(1)
    started = [{'code': code, 'text': STARTED_TEXT[code]} for code in (found.group(2) or '').split(',') if code]
    return {'model': model, 'name': NAMES[model], 'source': 'tether',
            'problems': problems(model, home=home, main_model=main_model, started=started)}


def rule(model):
    """One line for the agent's prompt rules (autonomy.py) while its tab runs with the advisor."""
    if model not in NAMES or model == 'off':
        return ''
    return (f'Claude Code\'s advisor tool ({NAMES[model]}) is on in this tab. Consult it before you commit to an '
            'approach, when an error repeats, and before you report the task done. It checks your own reasoning. '
            'It does not replace the Tether advisor (agent_advisor_cli.py) for a material decision, or a reviewer tab.')


# ---- HTTP (/mobile/api/claude-advisor). server.py checks sign-in, Origin and X-Codex-Mobile first. ----

PATH = '/mobile/api/claude-advisor'


def handles(path):
    return path.split('?', 1)[0] == PATH


def state(pid=None):
    resolved = resolve(pid)
    return {'models': [{'model': key, 'name': name, 'summary': summary} for key, name, summary in MODELS],
            'global': resolved['global'], 'model': resolved['model'], 'source': resolved['source'],
            'project': resolved['project'], 'user_setting': user_setting(),
            'problems': {key: problems(key) for key in NAMES if key != 'off'}}


def get(handler):
    from urllib.parse import parse_qs, urlsplit
    pid = parse_qs(urlsplit(handler.path).query).get('project', [None])[0]
    try:
        if pid is not None:
            project_path(pid)
        handler.respond(200, state(pid))
    except AdvisorError as exc:
        handler.respond(exc.status, {'error': str(exc)})


def post(handler):
    """{model} sets the global choice. {project, override, model} sets a project override."""
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 4096:
            raise AdvisorError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise AdvisorError(400, 'Invalid request.')
        pid = body.get('project')
        if pid is None:
            set_global(body.get('model'))
        else:
            set_project(pid, body.get('override'), body.get('model'))
        handler.respond(200, state(pid))
    except AdvisorError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not save the advisor choice.'})
