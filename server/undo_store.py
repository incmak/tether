"""The undo history: the Tether steps that changed tabs, sessions and stacks, newest last.

One JSON file under ~/.tether, so a reload, the phone and the desktop see the same list. Separate processes write it
(the server, the agent launcher, tmux merge), so each read-modify-write holds a file lock.

An entry: {"id", "at", "who": {"by": "user"|"tether"|"agent", "tab": name}, "label", "actions": [{"kind", ...data}],
"state": "done"|"undone", "redo": bool, "undone_at"}. actions run in order; undo runs their inverses in reverse order.
undo.py holds the inverses and the checks. This module only stores.
"""
import contextlib
import contextvars
import json
import os
import secrets
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:  # Windows: the server is the only writer there
    fcntl = None

import paths

LIMIT = 50
MAX_AGE = 24 * 3600
LABEL_LIMIT = 160
_THREAD = threading.RLock()
# Off until the server turns it on (enable): a test or a tool that imports a route must not write the real history.
_ENABLED = False

# Who acts now, and whether recording is on. A context variable, so each server thread has its own.
_ACTOR = contextvars.ContextVar('undo_actor', default=None)
_QUIET = contextvars.ContextVar('undo_quiet', default=False)
_STEP = contextvars.ContextVar('undo_step', default=None)


class LockError(OSError):
    """The history file lock could not be taken: nothing is read or written without it."""


def _path():
    return paths.TETHER / 'undo' / 'history.json'


@contextlib.contextmanager
def _locked():
    with _THREAD:
        handle = None
        if fcntl is not None:  # without fcntl (Windows) the server is the only writer, and the thread lock is enough
            try:
                _path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                handle = open(_path().parent / 'history.lock', 'a')
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError as error:
                if handle:
                    handle.close()
                raise LockError('Tether could not lock the undo history. Try again.') from error
        try:
            yield
        finally:
            if handle:
                handle.close()


def _valid(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('id'), str) and isinstance(entry.get('label'), str)
            and isinstance(entry.get('at'), (int, float)) and isinstance(entry.get('actions'), list) and entry['actions']
            and all(isinstance(a, dict) and isinstance(a.get('kind'), str) for a in entry['actions'])
            and entry.get('state') in ('done', 'undone') and isinstance(entry.get('who'), dict))


def _read():
    try:
        with _path().open(encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return []
    return [entry for entry in data.get('entries', []) if _valid(entry)] if isinstance(data, dict) else []


def _write(entries):
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='history.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({'version': 1, 'entries': entries}, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prune(entries, now):
    fresh = [entry for entry in entries if now - entry['at'] <= MAX_AGE]
    return fresh[-LIMIT:]


def entries(now=None):
    """The kept entries, oldest first."""
    with _locked():
        return _prune(_read(), now or time.time())


def edit(change, now=None):
    """Run change(entries) on the stored list under the lock and save it. Returns change's result."""
    with _locked():
        found = _prune(_read(), now or time.time())
        result = change(found)
        _write(_prune(found, now or time.time()))
        return result


def find(found, entry_id):
    return next((entry for entry in found if entry['id'] == entry_id), None)


def enable():
    """Record steps from now on. server.py calls it at start."""
    global _ENABLED
    _ENABLED = True


# Who acts, for the entries recorded inside the block.

@contextlib.contextmanager
def acting(by, tab=None):
    """by: 'user' (a route the person used), 'agent' (agent_tab_cli; tab: its name), or 'tether' (the default)."""
    token = _ACTOR.set({'by': by, **({'tab': tab} if tab else {})})
    try:
        yield
    finally:
        _ACTOR.reset(token)


@contextlib.contextmanager
def quiet():
    """Record nothing inside: an undo or redo, or Tether putting back a failed start."""
    token = _QUIET.set(True)
    try:
        yield
    finally:
        _QUIET.reset(token)


def recording():
    return _ENABLED and not _QUIET.get()


@contextlib.contextmanager
def step(label=None):
    """Collect the actions recorded inside into one entry (an agent start that also made a stack and moved two tabs is
    one step). label: the entry's label; None keeps the first action's label (title() changes it). A step inside a step
    joins the outer one. When the block raises, the step failed: its actions are dropped (its own clean-up put things
    back). The entry's 'who' is the actor of its first action."""
    if not recording():
        yield
        return
    outer = _STEP.get()
    if outer is not None:
        mark = len(outer['actions'])
        try:
            yield
        except BaseException:
            del outer['actions'][mark:], outer['labels'][mark:]
            raise
        return
    holder = {'label': label, 'actions': [], 'labels': [], 'who': None, 'at': None, 'server': None, 'spoiled': False}
    token = _STEP.set(holder)
    try:
        yield
    except BaseException:
        holder['actions'] = []
        raise
    finally:
        _STEP.reset(token)
        if holder['actions'] and not holder['spoiled']:
            _save(holder['label'] or holder['labels'][0], holder['actions'], holder['who'], holder['at'],
                  holder['server'])


def flush():
    """Save the open step now, so the answer to the request can name it (server.py respond). Returns the saved entry
    or None. Actions recorded later in the same step become an entry of their own."""
    holder = _STEP.get()
    if holder is None or not holder['actions'] or holder['spoiled']:
        return None
    actions, labels, who, at, server = (holder['actions'], holder['labels'], holder['who'], holder['at'],
                                        holder['server'])
    holder.update(actions=[], labels=[], who=None, at=None, server=None)
    return _save(holder['label'] or labels[0], actions, who, at, server)


def offer_checkpoint(identifier, label):
    """Let the response name a step that the close already saved to disk."""
    holder = _STEP.get()
    if holder is not None and identifier is not None:
        holder['checkpoint'] = {'id': identifier, 'label': label}


def offered_checkpoint():
    holder = _STEP.get()
    return holder.get('checkpoint') if holder is not None else None


def spoil():
    """A part of the open step could not be recorded: save none of the step. Undo of the other parts alone would put
    things back wrong."""
    holder = _STEP.get()
    if holder is not None:
        holder['spoiled'] = True


def title(label):
    """Set the label of the open step (the tab start, not the stack it made first)."""
    holder = _STEP.get()
    if holder is not None and recording():
        holder['label'] = label


def record(kind, label, **data):
    """Add one action to the history (or to the open step). Errors are ignored: undo must never break the action."""
    if not recording():
        return None
    action = dict(data, kind=kind)
    holder = _STEP.get()
    if holder is not None:
        if holder['spoiled']:
            return None
        # When and on which tmux server the change happened (under its lock), not when the step is saved: tmux can
        # restart before the request ends. Actions of one step on two servers get no server, so undo refuses them.
        server = _server()
        if not holder['actions']:
            holder['who'] = _ACTOR.get()
            holder['at'] = time.time()
            holder['server'] = server
        elif holder['server'] != server:
            holder['server'] = None
        holder['actions'].append(action)
        holder['labels'].append(label)
        return None
    try:
        return _save(label, [action])
    except (OSError, ValueError, TypeError):
        return None



def checkpoint(identifier, label, actions, *, server):
    """Persist the current Undo of a write before its next irreversible operation."""
    if not recording():
        return None
    if identifier is None:
        if not actions:
            return None
        entry = _save(label, actions, server=server)
        if entry is None:
            raise LockError('Tether could not save Undo. Review the task before trying again.')
        return entry['id']

    def update(found):
        entry = find(found, identifier)
        if entry is None:
            raise LockError('Tether could not update Undo. Review the task before trying again.')
        if actions:
            entry['actions'] = actions
        else:
            found.remove(entry)

    edit(update)
    return identifier if actions else None

def _server():
    """The tmux server id ('pid:start_time'): pane and session ids repeat after a tmux restart, so undo checks it."""
    try:
        import auto_resume  # here, not at the top: the store loads in small processes too
        kind, identifier = auto_resume.server_id()
    except (ImportError, OSError):
        return None
    return identifier if kind == 'up' else None


def _save(label, actions, who=None, at=None, server=False):
    """at: when the first action happened. A step is saved when its request ends, after its locks are free, so two
    requests can save out of order: the entry goes in its place by that time, and Cmd+Z undoes the newest change."""
    entry = {'id': 'u' + secrets.token_hex(6), 'at': at or time.time(), 'who': who or _ACTOR.get() or {'by': 'tether'},
             'label': ' '.join(str(label).split())[:LABEL_LIMIT], 'actions': actions, 'state': 'done', 'redo': False,
             'server': _server() if server is False else server}

    def change(found):
        if entry['who']['by'] == 'user':
            # A new step of the person ends redo, as in every editor; Tether's own steps do not. Only for the undos
            # before its change: a step saved late does not end the redo of an undo that came after it.
            for old in found:
                if not (old.get('undone_at') or 0) > entry['at']:
                    old['redo'] = False
        place = len(found)
        while place and found[place - 1]['at'] > entry['at']:
            place -= 1
        found.insert(place, entry)
    try:
        edit(change)
    except (OSError, ValueError, TypeError):
        return None
    return entry
