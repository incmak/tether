#!/usr/bin/env python3
"""User-set autonomy level and reviewer choices.

  paths.CONFIG/autonomy-level               global level, 0-5. Missing or bad: 2.
  paths.TETHER/projects/<id>/autonomy-level  {"override": bool, "level": int}.
  paths.CONFIG/reviewers.json                critical, normal and light reviewer choices.

`autonomy.py hook` supplies UserPromptSubmit context. `autonomy.py show` prints it for cwd.
"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import codex_level  # noqa: E402
import advisor  # noqa: E402
import approval  # noqa: E402
import autonomy_setup  # noqa: E402
import decisions  # noqa: E402
import delegate  # noqa: E402
import paths  # noqa: E402
from codex_level import hook_project  # noqa: E402
from advisor import LevelError  # noqa: E402

LEVELS = (
    ('Check-in', 'Confirms the plan first, stops at each milestone, asks at each unclear choice.'),
    ('Guided', 'Confirms the plan, then works. Asks at real forks.'),
    ('Standard', 'Asks when a decision is material and has no sensible default.'),
    ('Trusted', 'Uses sensible defaults. Asks its questions together at the end of the turn.'),
    ('Autopilot', 'Does not ask about normal decisions. Lists its assumptions. Asks only for new authority.'),
    ('Full auto', 'Runs the task alone, opens its own tabs and reviewers, works the backlog. Asks only when blocked; '
     'urgent asks reach the user at once.'),
)
RULES = (
    'Write the plan and wait for the user to approve it before you start. Stop at each milestone with a short status '
    'and wait for the user. Ask about each unclear choice that changes the result. Do not ask about trivial details.',
    'Write the plan and wait for the user to approve it, then work without stopping. Ask only at real forks, where '
    'the choice changes the result.',
    'Ask only when a decision is material and you cannot resolve it from the request, the code or a sensible default. '
    'Otherwise continue.',
    'Decide from the evidence and continue. Do not stop mid-task to ask. Ask your open questions together at the end of '
    'the turn, each with the default you used.',
    'Do not ask about normal decisions. Decide from the evidence, list each decision in the final reply, and continue '
    'until the work is done and verified. Ask only when you need new authority or a decision that only the user can make.',
    'Work alone until the task is done and verified. Do not ask about normal decisions; list each decision in the '
    'final reply. Split large work into new tabs and start reviewers with agent_tab_cli.py. When the task is done, '
    'start the next open backlog item of this project, if there is one. Ask only when you are blocked: the task cannot '
    'continue without the user, or a critical bug needs a decision.',
)
EVIDENCE = ('Never guess. Before you decide or ask, check the evidence in this order: the request and this '
            'conversation; the project notes and saved answers; the code, tests and git history; the primary docs and '
            'standards (official docs, WCAG 2.2, platform guidelines). In the final reply, mark each material decision '
            'Verified (with its evidence) or Advisor (with the advisor answer).')
ADVISOR = ('When the evidence does not settle a material decision, ask the advisor ({who}): '
           '`python3 {cli} ask "QUESTION"`. Give the options and the evidence you found. Add --critical for '
           'security, data, money, release or architecture decisions; a second advisor ({second}) then answers too. '
           'Act on the answer only when the command exits 0 and, with two advisors, both answers agree. Otherwise ask '
           'the user. While you wait, do not change files that the answer affects.')
NO_ADVISOR = 'The user turned the advisor off. When the evidence does not settle a material decision, ask the user.'
ASK_USER = ('Ask the user, not the advisor, about taste or brand choices that no evidence settles, business, pricing '
            'or legal choices, facts only the user knows, new authority, and the items in the safety rule.')
PRODUCT = ('Own the product decisions in this task. Decide what the product and its users need: check how comparable '
           'products and the market solve it (web search when it changes the decision) and choose the best user '
           'experience, not the quickest change. Meet the standards in full: accessibility (WCAG 2.2 AA), layouts '
           'from 320 px wide, tests for changed behavior, loading, empty and error states, and the project '
           'conventions. Do not leave shortcuts, placeholders or TODOs in place of the work. Put larger follow-up '
           'work in the backlog.')
SAFETY = ('At every level, ask before you delete data, force-push, run a destructive migration, spend money, make a '
          'public release, or send a message to another person.')
URGENT = ('When you must ask, end the reply with "Needs you: yes". If the question blocks the work, put a line '
          '"Urgent: <the question in one sentence>" just before it.')
DECISIONS = ('The user saved answers for this project in {path}. Reuse one only when it answers the same question; '
             'if in doubt, ask.')
LAST = 'Do not change the level. The user sets it in Tether.'
PROJECT_ID = codex_level.PROJECT_ID
SLUG = codex_level.SLUG
TIERS = ('critical', 'normal', 'light')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
CLAUDE_REVIEW_MODELS = tuple(model for model, _ in delegate.CLAUDE_MODELS)  # what a new tab accepts
TIER_DEFAULT = {'agent': 'auto', 'model': 'auto', 'effort': 'auto'}
_LEVEL_LOCK = threading.Lock()


# ---- the files ----

def global_path():
    return paths.CONFIG / 'autonomy-level'


def project_path(pid):
    if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
        raise LevelError(400, 'Choose a project.')
    return paths.TETHER / 'projects' / pid / 'autonomy-level'


def _valid(level):
    return isinstance(level, int) and not isinstance(level, bool) and 0 <= level < len(LEVELS)


def read_global():
    try:
        text = global_path().read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return 2
    return int(text) if re.fullmatch(r'[0-5]', text) else 2


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
    """An enabled project override beats the global level, including a lower level."""
    base = read_global()
    project = read_project(pid) if pid else None
    if project and project['override']:
        return {'level': project['level'], 'source': 'project', 'global': base, 'project': project}
    return {'level': base, 'source': 'global', 'global': base, 'project': project}


def rules(resolved, pid=None):
    level = resolved['level']
    where = 'for this project' if resolved['source'] == 'project' else 'for all projects'
    head = f'Autonomy level {level} of {len(LEVELS) - 1} ({LEVELS[level][0]}), set by the user {where}.'
    lines = [head, RULES[level]]
    if level >= 3:
        lines.append(EVIDENCE)
        settings = advisor.read()
        if settings['agent'] == 'none':
            lines.append(NO_ADVISOR)
        else:
            model = advisor.resolve(settings['agent'], settings['model'])
            label = 'latest Fable' if settings['agent'] == 'claude' and settings['model'] == 'latest' else model or 'default model'
            who = f'{settings["agent"].title()}, {label}, {settings["effort"]} effort'
            second = advisor.second_opinion(settings)
            instruction = ADVISOR
            if second:
                second_label = f'{second["agent"].title()}, {second["model"] or "default model"}'
            else:
                second_label = 'off'
                instruction = instruction.replace('; a second advisor ({second}) then answers too.', '.')
            lines.append(instruction.format(who=who, second=second_label, cli=HERE / 'agent_advisor_cli.py'))
        lines.append(ASK_USER)
    if level >= 4:
        lines.append(PRODUCT)
    lines.append(SAFETY)
    if level >= 4:
        lines.append(URGENT)
    if level >= 3 and pid is not None:
        try:
            if decisions.entries(pid):
                lines.append(DECISIONS.format(path=decisions.path(pid).resolve()))
        except ValueError:
            pass  # The pane may still name a project that no longer exists.
    lines.append(LAST)
    return '\n'.join(lines)


# ---- reviewer choice ----

def reviewers_path():
    return paths.CONFIG / 'reviewers.json'


def _valid_reviewers(reviewers):
    if not isinstance(reviewers, dict) or set(reviewers) != set(TIERS):
        return False
    available = None
    for tier in reviewers.values():
        if not isinstance(tier, dict) or set(tier) != set(TIER_DEFAULT):
            return False
        agent, model, effort = tier['agent'], tier['model'], tier['effort']
        if (agent not in ('auto', 'claude', 'codex', 'none') or not isinstance(effort, str)
                or not isinstance(model, str) or not (model == 'auto' or SLUG.fullmatch(model))):
            return False
        if agent != 'codex' and effort not in ('auto', *EFFORTS):
            return False
        if agent == 'none' and (model != 'auto' or effort != 'auto'):
            return False
        if agent == 'auto' and model != 'auto':
            return False
        if agent == 'claude' and model not in ('auto', 'latest', *CLAUDE_REVIEW_MODELS):
            return False
        if agent == 'codex':
            if available is None:
                available = codex_level.models()
            chosen = available[0] if available and model in ('auto', 'latest') else next(
                (row for row in available if row['slug'] == model), None)
            if available and chosen is None:
                return False
            efforts = chosen.get('efforts', []) if chosen else EFFORTS
            if effort != 'auto' and effort not in efforts:
                return False
    return True


def read_reviewers():
    try:
        reviewers = json.loads(reviewers_path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return {tier: dict(TIER_DEFAULT) for tier in TIERS}
    return reviewers if _valid_reviewers(reviewers) else {tier: dict(TIER_DEFAULT) for tier in TIERS}


def set_reviewers(reviewers):
    if not _valid_reviewers(reviewers):
        raise LevelError(400, 'Choose a reviewer, a model and an effort for each tier.')
    _write(reviewers_path(), json.dumps(reviewers) + '\n')


def reviewer_rules(reviewers):
    lines = [
        'Choose a reviewer by the risk of the task:',
        '- Critical: security, auth, payments, data loss, migrations, deploys, public releases, concurrency, or a '
        'change of more than about 300 lines. Start a reviewer tab from the other agent (Claude reviews Codex work, '
        'Codex reviews Claude work) with the strongest model and high effort.',
        '- Normal: features and bug fixes. Start a reviewer tab from the other agent with its default model and '
        'medium effort.',
        '- Light: docs, copy, styles and small fixes. Do not start a reviewer tab. Review the change yourself.',
        f'Start a reviewer with `python3 {HERE / "agent_tab_cli.py"} start --reviews-me --agent AGENT '
        '[--model MODEL] [--effort EFFORT]`.',
    ]
    for name in TIERS:
        tier = reviewers[name]
        if tier == TIER_DEFAULT:
            continue
        if tier['agent'] == 'none':
            lines.append(f'The user set the {name.title()} reviewer: none. Do not start a reviewer for {name} tasks.')
        elif tier['agent'] == 'auto':
            lines.append(f'The user set the {name.title()} reviewer effort: {tier["effort"]}.')
        else:
            parts = ['Claude' if tier['agent'] == 'claude' else 'Codex']
            model = advisor.resolve(tier['agent'], tier['model'])
            if model and model != 'auto':
                parts.append(f'model {model}')
            if tier['effort'] != 'auto':
                parts.append(f'effort {tier["effort"]}')
            lines.append(f'The user set the {name.title()} reviewer: {", ".join(parts)}. This overrides the default.')
    return '\n'.join(lines)


# ---- the hook ----

def hook_output(data, env=os.environ, tmux=codex_level._tmux):
    """The hook's stdout for one UserPromptSubmit input, as a JSON string."""
    cwd = data.get('cwd') if isinstance(data, dict) else None
    try:
        pid = hook_project(cwd, env, tmux)
    except (OSError, subprocess.SubprocessError):
        pid = None
    text = rules(resolve(pid), pid) + '\n\n' + reviewer_rules(read_reviewers())
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}})


def hook():
    """Fail open: a hook must never stop a prompt."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        out = hook_output(data)
    except Exception:  # noqa: BLE001 - a hook must never stop a prompt
        return
    print(out)


# ---- HTTP (/mobile/api/autonomy). server.py checks sign-in, Origin and X-Codex-Mobile first. ----

PATH = '/mobile/api/autonomy'


def handles(path):
    return path.split('?', 1)[0] in (PATH, PATH + '/setup', PATH + '/decisions')


def state(pid=None):
    resolved = resolve(pid)
    models = [{'slug': row['slug'], 'name': row['name'], 'efforts': list(row['efforts'])}
              for row in codex_level.models()]
    result = {'levels': [{'level': index, 'name': name, 'summary': summary} for index, (name, summary) in enumerate(LEVELS)],
            'global': resolved['global'], 'level': resolved['level'], 'source': resolved['source'],
            'project': resolved['project'], 'reviewers': read_reviewers(), 'tiers': list(TIERS),
            'advisor': advisor.read(), 'latest': advisor.latest_names(),
            'models': {'claude': [{'slug': slug, 'name': name} for slug, name in delegate.CLAUDE_MODELS], 'codex': models}, 'efforts': list(EFFORTS)}
    if pid is not None:
        result['decisions'] = decisions.entries(pid)
    return result


def setup_state():
    return {'hook': autonomy_setup.hook_status(), 'ping': autonomy_setup.ping_status()}


def get(handler):
    from urllib.parse import parse_qs, urlsplit
    url = urlsplit(handler.path)
    if url.path == PATH + '/setup':
        handler.respond(200, setup_state())
        return
    if url.path == PATH + '/decisions':
        handler.respond(405, {'error': 'Use POST for saved answers.'})
        return
    query = parse_qs(url.query)
    pid = query.get('project', [None])[0]
    try:
        if pid is not None:
            project_path(pid)
        handler.respond(200, state(pid))
    except LevelError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except ValueError as exc:
        handler.respond(400, {'error': str(exc)})


def _setup(body):
    action = body.get('action')
    test = None
    if action == 'hook':
        autonomy_setup.install_hook()
    elif action == 'ping':
        autonomy_setup.save_ping(body.get('preset'), body.get('fields'), body.get('base_url'))
    elif action == 'ping-off':
        autonomy_setup.disable_ping()
    elif action == 'ping-test':
        test = autonomy_setup.test_ping()
    else:
        raise ValueError('Choose a setup action.')
    result = setup_state()
    if test is not None:
        result['test'] = test
    return result


def _decisions(body):
    if 'delete' in body:
        pid, entry_id = body.get('project'), body['delete']
        if not isinstance(entry_id, str) or not re.fullmatch(r'[0-9a-f]{8}', entry_id):
            raise ValueError('Choose a saved answer.')
        if not decisions.delete(pid, entry_id):
            raise LevelError(404, 'That saved answer is gone.')
        return {'decisions': decisions.entries(pid)}
    pane = body.get('pane')
    if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
        raise ValueError('Select a tab first.')
    pid = None
    try:
        shown = codex_level._tmux('display-message', '-p', '-t', pane, '#{@project}')
        value = shown.stdout.strip()
        if shown.returncode == 0 and PROJECT_ID.fullmatch(value):
            decisions.path(value)
            pid = value
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if pid is None:
        raise LevelError(409, 'This tab is not in a Tether project, so Tether cannot save the answer.')
    return {'entry': decisions.record(pid, body.get('question'), body.get('answer')), 'project': pid}


def post(handler):
    """Set autonomy options, perform an explicit setup action, or save/delete a project answer."""
    try:
        route = handler.path.split('?', 1)[0]
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 16384:
            raise LevelError(413, 'Invalid request.')
        try:
            body = json.loads(handler.rfile.read(size))
        except (ValueError, UnicodeError):
            raise LevelError(400, 'Invalid request.') from None
        if not isinstance(body, dict):
            raise LevelError(400, 'Invalid request.')
        payload = {key: value for key, value in body.items() if key != 'approval'}
        if route == PATH + '/setup':
            if body.get('action') in ('hook', 'ping'):
                approval.require(handler, 'setup-' + body['action'], payload, body.get('approval'))
            handler.respond(200, _setup(body))
            return
        if route == PATH + '/decisions':
            if 'delete' not in body:
                approval.require(handler, 'decision-save', payload, body.get('approval'))
            handler.respond(200, _decisions(body))
            return
        pid = body.get('project')
        if 'advisor' in body:
            advisor.write(body['advisor'])
        elif 'reviewers' in body:
            set_reviewers(body['reviewers'])
        else:
            with _LEVEL_LOCK:
                level = body.get('level')
                if not _valid(level) or (pid is not None and not isinstance(body.get('override'), bool)):
                    raise LevelError(400, f'Choose a level from 0 to {len(LEVELS) - 1}.')
                if pid is None:
                    raises = level > read_global()
                else:
                    project_path(pid)
                    current = resolve(pid)
                    effective = level if body['override'] else current['global']
                    raises = effective > current['level']
                if raises:
                    approval.require(handler, 'level-raise', payload, body.get('approval'))
                if pid is None:
                    set_global(level)
                else:
                    set_project(pid, body['override'], level)
        handler.respond(200, state(pid))
    except approval.ApprovalError as exc:
        approval.error(handler, exc)
    except LevelError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Tether could not save the autonomy settings.'})


if __name__ == '__main__':
    if sys.argv[1:] == ['hook']:
        hook()
    elif sys.argv[1:] == ['show']:
        pid = hook_project(os.getcwd())
        print(rules(resolve(pid), pid) + '\n\n' + reviewer_rules(read_reviewers()))
    else:
        sys.exit(__doc__)
