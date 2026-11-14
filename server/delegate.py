"""Validate and launch a task in a new window beside its source chat."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import unicodedata
from uuid import uuid4

import codex_provider
import helper_tabs
import model_catalog
import paths
import review_links
from terminal_input import PROFILE_NAME, UUID, new_window_after


CLAUDE_MODELS = [('fable', 'Fable'), ('opus', 'Opus'), ('sonnet', 'Sonnet'), ('haiku', 'Haiku'),
                 ('claude-fable-5-2', 'Fable 5.2'), ('claude-opus-5-5', 'Opus 5.5'), ('claude-sonnet-5-5', 'Sonnet 5.5'),
                 ('claude-haiku-5-5', 'Haiku 5.5')]
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def task_models(home=None):
    codex = []
    if codex_provider.chosen() != 'foundry':
        try:
            rows = json.loads((Path(home or paths.HOME) / '.codex' / 'models_cache.json').read_text())['models']
            codex = [{'id': row['slug'], 'label': row.get('display_name') or row['slug']}
                     for row in sorted((row for row in rows if row.get('visibility') == 'list'),
                                       key=lambda row: row.get('priority', 0))]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    claude = [{'id': model, 'label': label} for model, label in CLAUDE_MODELS[:4]]
    claude += [{'id': row['slug'], 'label': row['name']} for row in model_catalog.claude()['models']]
    return {'claude': claude, 'codex': codex}


def normalize_review(value):
    if value is False:
        return None
    if value is True or (isinstance(value, str) and value == 'source'):
        return 'source'
    if isinstance(value, str) and value == 'new':
        return 'new'
    raise ValueError('Context and review must be true or false.')


def validate(body, entries, models=None):
    pane = body.get('session')
    if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
        raise ValueError('Select a chat tab for the new task.')
    entry = next((item for item in entries if item['id'] == pane), None)
    if entry is None:
        raise ValueError('That session has ended. Select another tab.')
    if body.get('thread') != entry.get('thread'):
        raise ValueError('This tab changed conversations. Reload it and try again.')
    if body.get('agent') not in ('claude', 'codex'):
        raise ValueError('Choose Claude or Codex for the new task.')
    model = body.get('model')
    if model is not None:
        models = task_models() if models is None else models
        allowed = [row['id'] for row in models.get(body['agent'], [])]
        if body['agent'] == 'claude':
            allowed += [slug for slug, _ in CLAUDE_MODELS]
        if not isinstance(model, str) or model not in allowed:
            raise ValueError('Choose a model from the list.')
    efforts = (*EFFORTS, 'ultra') if body['agent'] == 'codex' else EFFORTS
    if body.get('effort') is not None and body['effort'] not in efforts:
        raise ValueError('Choose an effort from the list.')
    review = normalize_review(body.get('review', False))
    if 'worktree' in body and review == 'new':
        raise ValueError("A reviewer tab stays in this tab's folder, so it can see the work. Turn off the new worktree or the new tab review.")
    message = body.get('message')
    if not isinstance(message, str) or not (0 if review == 'new' else 1) <= len(message.strip()) <= 20000:
        raise ValueError('Enter a task with 1 to 20000 characters.')
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in message):
        raise ValueError('The task contains terminal control characters.')
    if not isinstance(body.get('context', False), bool):
        raise ValueError('Context and review must be true or false.')
    if body.get('role') is not None and (not isinstance(body['role'], str) or body['role'] not in ROLES):
        raise ValueError('Choose a tab role from the list.')
    if body.get('role') and (review or 'name' in body):
        raise ValueError('A helper role names the tab itself. Do not add a name or a review.')
    if 'name' in body:
        name = body['name']
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 60
                or any(unicodedata.category(char) == 'Cc' for char in name)):
            raise ValueError('Enter a tab name with 1 to 60 characters and no control characters.')
    supported = entry.get('agent') in ('claude', 'codex')
    thread = entry.get('thread')
    if review == 'new' and (not supported or not entry.get('writable')
                            or not isinstance(thread, str) or not UUID.fullmatch(thread)
                            or entry.get('pending_conversation') or not entry.get('transcript')):
        raise ValueError('A reviewer tab needs a Claude or Codex chat with a saved conversation. Send a first message, then try again.')
    if body.get('context') and (not supported or not isinstance(thread, str)
                                or not UUID.fullmatch(thread) or entry.get('pending_conversation')):
        raise ValueError('Context needs a Claude or Codex chat with a saved conversation. Send a first message, then try again.')
    if review == 'source' and (not supported or not entry.get('writable')):
        raise ValueError('Review needs a writable Claude or Codex chat.')
    return entry


# Helper roles (agent_advisor_cli.py): the tab is named after the tab it helps (40 characters, the pane name limit), so two advisors in one session differ,
# and wears an icon that marks the role. @chat_icon_by "tether" tells the helper to keep that icon (projects.render).
ROLES = {'advisor': ('Advisor', 'lightbulb'), 'second-advisor': ('Advisor 2', 'lightbulb')}


def task_name(message):
    name = ' '.join(message.strip().split('\n', 1)[0].split())
    if len(name) <= 32:
        return name
    prefix = name[:32]
    if name[32] != ' ' and ' ' in prefix:
        prefix = prefix.rsplit(' ', 1)[0]
    return prefix.rstrip() + '…'


BRIEF_TURNS = 6       # recent user and assistant messages in a context brief
BRIEF_EACH = 600      # characters kept from each message
BRIEF_TOTAL = 3000    # characters for the whole brief: the new chat gets a short start, not the whole chat


def brief(messages):
    """The last few user and assistant texts, each cut short, oldest first. Tool output is left out."""
    picked, total = [], 0
    for message in reversed(messages or []):
        text = message.get('text') if isinstance(message, dict) else None
        role = message.get('role') if isinstance(message, dict) else None
        if role not in ('user', 'assistant') or not isinstance(text, str) or not text.strip():
            continue
        text = ' '.join(text.split())
        if len(text) > BRIEF_EACH:
            text = text[:BRIEF_EACH].rstrip() + '…'
        line = ('User: ' if role == 'user' else 'Agent: ') + text
        if total + len(line) > BRIEF_TOTAL:
            break
        picked.append(line)
        total += len(line)
        if len(picked) >= BRIEF_TURNS:
            break
    return '\n\n'.join(reversed(picked))


def prompt_text(entry, message, agent, *, context=False, review=False, recent=''):
    text = message.strip()
    if context:
        source = 'Claude' if entry['agent'] == 'claude' else 'Codex'
        head = f'Context: this task comes from another {source} chat in this project.'
        if recent:
            head += f' Its recent turns, cut short:\n\n{recent}\n\n'
        else:
            head += ' '
        head += f"Its full transcript (JSONL) is at {entry['transcript']}. Read it only if the task needs more.\n\nTask:\n"
        text = head + text
    if review:
        text += '\n\nWhen you finish, stop and wait: a reviewer in another tab reads your work and may send follow-ups.'
    return text


def source_title(entry):
    return entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or 'that tab'


def reviewer_prompt(entry, message, recent):
    agent = 'Claude' if entry['agent'] == 'claude' else 'Codex'
    text = (f'You are the reviewer of the {agent} tab “{source_title(entry)}” ({entry["id"]}) in this project.'
            f'\n\nWhat to check:\n{message.strip() or "Check the work in that tab."}')
    if recent:
        text += f'\n\nIts recent turns, cut short:\n\n{recent}'
    return (text + f'\n\nIts full transcript (JSONL) is at {entry["transcript"]}. Read it when you review.'
            '\n\nDo not change its work and do not send to that tab yourself. Reply only "Ready." now and stop. '
            'Tether sends you a review request after that tab finishes a turn, and it sends your reply back to that tab.')


def write_prompt(text):
    directory = paths.CACHE / 'delegate'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    cutoff = time.time() - 7 * 24 * 60 * 60
    for old in directory.iterdir():
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except FileNotFoundError:  # another request may have pruned it
            pass
    path = directory / f'{uuid4().hex}.md'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    return path


def _check_profile(profile):
    if profile is not None and (not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile)):
        raise ValueError('Choose a supported Codex provider.')


def launch_command(entry, agent, path, name, *, context=False, profile=None, model=None, effort=None):
    if agent == 'claude':
        command = 'claude'
        if model is not None:
            command += f' --model {shlex.quote(model)}'
        if effort is not None:
            command += f' --effort {shlex.quote(effort)}'
    else:
        _check_profile(profile)
        command = 'codex' + (f' -p {shlex.quote(profile)}' if profile is not None else '')
        if model is not None:
            command += f' -m {shlex.quote(model)}'
        if effort is not None:
            command += f' -c {shlex.quote("model_reasoning_effort=" + effort)}'
    return command + f' "$(cat {shlex.quote(str(path))})"'


def start(entry, body, folder, run=subprocess.run, recent=lambda: [], *, cwd=None, worktree=None, owner=None, group=None):
    """recent: returns source messages when context or a new reviewer needs them.
    group: None = the source tab's group; "" = Unstacked; a group id = that group (set before the launch command)."""
    # Call after validate, under the server's send lock. Only the new pane receives input.
    agent = body['agent']
    review = normalize_review(body.get('review', False))
    context = body.get('context', False) or review == 'new'
    if cwd is not None:
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError('Choose an absolute path to an existing directory.')
        folder = cwd
    profile = codex_provider.launch_profile() if agent == 'codex' else None
    _check_profile(profile)
    role = ROLES.get(body.get('role'))
    name = (body['name'].strip() if 'name' in body else
            f'{role[0]}: {source_title(entry)}'[:40] if role else
            ('Review: ' + source_title(entry))[:60] if review == 'new' else task_name(body['message']))
    pane = new_window_after(entry['id'], run, cwd=folder, group=group)
    options = [paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', name, ';',
               'set-option', '-p', '-t', pane, '@chat_name_at', str(int(time.time() * 1000))]
    if role:
        options += [';', 'set-option', '-p', '-t', pane, '@chat_icon', role[1],
                    ';', 'set-option', '-p', '-t', pane, '@chat_icon_by', 'tether',
                    ';', 'set-option', '-p', '-t', pane, '@tether_role', body['role']]
    elif review == 'new':
        # Tether closes a reviewer tab when its review ends (helper_close.py).
        options += [';', 'set-option', '-p', '-t', pane, '@tether_role', 'reviewer']
    if worktree is not None:
        options += [';', 'set-option', '-p', '-t', pane, '@worktree', worktree]
    run(options,
        check=True, capture_output=True, timeout=5)
    if owner is not None:
        helper_tabs.save(pane, owner, run)
    recent_text = brief(recent()) if context else ''
    prompt = (reviewer_prompt(entry, body['message'], recent_text) if review == 'new' else
              prompt_text(entry, body['message'], agent, context=context, review=review == 'source', recent=recent_text))
    path = write_prompt(prompt)
    command = launch_command(entry, agent, path, name, context=context, profile=profile, model=body.get('model'),
                             effort=body.get('effort'))
    # The pty buffers input even while the new shell starts.
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'], check=True, capture_output=True, timeout=5)
    if review == 'new':
        review_links.add(worker=entry['id'], reviewer=pane, reviewer_thread=None,
                         name=source_title(entry), agent=entry['agent'], seen_working=True)
    elif review == 'source':
        thread = entry.get('thread')
        review_links.add(worker=pane, reviewer=entry['id'],
                         reviewer_thread=thread if isinstance(thread, str) and UUID.fullmatch(thread) else None,
                         name=name, agent=agent)
    return pane, name
