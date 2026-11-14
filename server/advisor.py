"""User-selected advisor and provider-owned latest model choices."""
import json
import os
import threading

import codex_level
import delegate
import paths

DEFAULT = {'agent': 'claude', 'model': 'latest', 'effort': 'high', 'second': True}


class LevelError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def path():
    return paths.CONFIG / 'advisor.json'


def _write(path, text):
    """Publish a complete owner-only file, including when the process umask is permissive."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def valid_choice(agent, model, effort):
    """Validate an explicit agent's model and effort against its own choices."""
    if not isinstance(model, str) or not codex_level.SLUG.fullmatch(model) or not isinstance(effort, str):
        return False
    if agent == 'claude':
        return (model in ('latest', *(slug for slug, _ in delegate.CLAUDE_MODELS))
                and effort in ('auto', *delegate.EFFORTS))
    if agent != 'codex':
        return False
    models = codex_level.models()
    chosen = models[0] if models and model == 'latest' else next(
        (row for row in models if row['slug'] == model), None)
    if models and chosen is None:
        return False
    efforts = chosen.get('efforts', []) if chosen else []
    return effort == 'auto' or effort in efforts


def _valid(settings):
    if not isinstance(settings, dict) or set(settings) != set(DEFAULT) or not isinstance(settings['second'], bool):
        return False
    if settings['agent'] == 'none':
        return settings == {'agent': 'none', 'model': 'latest', 'effort': 'auto', 'second': False}
    return valid_choice(settings['agent'], settings['model'], settings['effort'])


def read():
    try:
        settings = json.loads(path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return dict(DEFAULT)
    return settings if _valid(settings) else dict(DEFAULT)


def write(settings):
    if not _valid(settings):
        raise LevelError(400, 'Choose an advisor, a model and an effort.')
    _write(path(), json.dumps(settings) + '\n')


def resolve(agent, model):
    if model != 'latest':
        return model
    if agent == 'claude':
        return 'fable'
    models = codex_level.models()
    return models[0]['slug'] if models else None


def second_opinion(settings):
    if not settings['second'] or settings['agent'] == 'none':
        return None
    other = 'codex' if settings['agent'] == 'claude' else 'claude'
    return {'agent': other, 'model': resolve(other, 'latest'), 'effort': 'high'}


def latest_names():
    models = codex_level.models()
    return {'claude': 'Latest Fable', 'codex': models[0]['name'] if models else None}
