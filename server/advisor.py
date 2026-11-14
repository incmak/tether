"""User-selected advisor and provider-owned latest model choices."""
import copy
import json
import os
import threading

import codex_level
import delegate
import paths
import model_catalog

OFF = {'agent': 'none', 'model': 'latest', 'effort': 'auto'}
DEFAULT = {'version': 2, 'main': {'agent': 'claude', 'model': 'latest', 'effort': 'high'},
           'second': {'agent': 'codex', 'model': 'latest', 'effort': 'high'}}


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
        rows = model_catalog.claude()['models']
        if any(row['slug'] == model and (row.get('available') is False or row.get('access') == 'checking') for row in rows):
            return False
        return (model in ('latest', *(row['slug'] for row in rows),
                         *(slug for slug, _ in delegate.CLAUDE_MODELS))
                and effort in ('auto', *delegate.EFFORTS))
    if agent != 'codex':
        return False
    models = model_catalog.codex()['models']
    chosen = models[0] if models and model == 'latest' else next(
        (row for row in models if row['slug'] == model), None)
    if models and chosen is None:
        return False
    efforts = chosen.get('efforts', []) if chosen else []
    return effort == 'auto' or effort in efforts


def _valid_choice(choice):
    if not isinstance(choice, dict) or set(choice) != set(OFF):
        return False
    if choice['agent'] == 'none':
        return choice == OFF
    return valid_choice(choice['agent'], choice['model'], choice['effort'])


def _valid(settings):
    return (isinstance(settings, dict) and set(settings) == set(DEFAULT)
            and type(settings['version']) is int and settings['version'] == 2
            and _valid_choice(settings['main']) and _valid_choice(settings['second'])
            and (settings['main']['agent'] != 'none' or settings['second']['agent'] == 'none'))


def read():
    try:
        settings = json.loads(path().read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT)
    if (isinstance(settings, dict) and set(settings) == {'agent', 'model', 'effort', 'second'}
            and isinstance(settings['second'], bool)):
        main = {key: settings[key] for key in OFF}
        other = 'codex' if main['agent'] == 'claude' else 'claude'
        second = ({'agent': other, 'model': 'latest', 'effort': 'high'}
                  if settings['second'] and main['agent'] != 'none' else dict(OFF))
        settings = {'version': 2, 'main': main, 'second': second}
    return settings if _valid(settings) else copy.deepcopy(DEFAULT)


def write(settings):
    # HTTP clients do not need to send the on-disk format version.
    if isinstance(settings, dict) and set(settings) == {'main', 'second'}:
        settings = dict(settings, version=2)
    if isinstance(settings, dict):
        for choice in (settings.get('main'), settings.get('second')):
            if isinstance(choice, dict):
                require_available(choice.get('agent'), choice.get('model'))
    if not _valid(settings):
        raise LevelError(400, 'Choose an advisor, a model and an effort.')
    _write(path(), json.dumps(settings) + '\n')


def resolve(agent, model):
    if model != 'latest':
        return model
    latest = (model_catalog.claude() if agent == 'claude' else model_catalog.codex())['latest']
    return latest['launch'] if latest else None


def second_opinion(settings):
    if settings['main']['agent'] == 'none' or settings['second']['agent'] == 'none':
        return None
    second = settings['second']
    return dict(second, model=resolve(second['agent'], second['model']))


def latest_names():
    return {agent: value['latest']['name'] if value['latest'] else None
            for agent, value in model_catalog.catalog().items()}


def require_available(agent, model):
    if agent == 'claude' and any(row['slug'] == model and (row.get('available') is False or row.get('access') == 'checking')
                                 for row in model_catalog.claude()['models']):
        raise LevelError(400, 'That model is not available for your account.')
