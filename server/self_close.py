"""Close the calling tab once its turn ends, when the user asks it to ("close this tab").

A tab cannot end its own pane while it still writes the reply, so the server waits until the tab has been quiet for
STEADY seconds, then closes it through helper_close.close_pane, the same cleanup as other closes. A tab that keeps
working past WAIT seconds stays open.
"""
import sys
import threading
import time

import helper_close

WAIT = 600
STEADY = 3
POLL = 1

_pending = set()
_lock = threading.Lock()


def schedule(entry, run, status, *, sessions, lock, fresh, start=None, sleep=time.sleep, now=time.monotonic):
    pane = entry['id']
    with _lock:
        if pane not in _pending:
            _pending.add(pane)
            job = lambda: watch(entry, run, status, sessions=sessions, lock=lock, fresh=fresh, sleep=sleep, now=now)
            (start or (lambda work: threading.Thread(target=work, name=f'self-close {pane}', daemon=True).start()))(job)
    return {'closing': pane, 'when': 'after this reply ends'}


def same_tab(entry, sessions):
    """The live entry for this pane when it still runs the same agent and conversation, else None.
    tmux can reuse a pane id, and a pane can change conversations while the close waits."""
    current = next((item for item in sessions() if item['id'] == entry['id']), None)
    if current is None or current.get('agent') != entry.get('agent') or current.get('thread') != entry.get('thread'):
        return None
    return current


def watch(entry, run, status, *, sessions, lock, fresh, sleep=time.sleep, now=time.monotonic):
    """'closed', 'gone' (the pane ended or changed first), 'timeout' or 'failed'."""
    pane = entry['id']
    try:
        deadline, quiet_since = now() + WAIT, None
        while True:
            sleep(POLL)
            state = status(entry).get('state')
            if now() >= deadline:
                break
            if state == 'Unavailable':
                return 'gone'
            if state not in helper_close.QUIET:
                quiet_since = None
                continue
            quiet_since = now() if quiet_since is None else quiet_since
            if now() - quiet_since < STEADY:
                continue
            # The last check holds the send lock and reads the screen again, so a message sent a moment ago keeps the tab.
            if not lock.acquire(timeout=2):
                continue
            try:
                current = same_tab(entry, sessions)
                if current is None:
                    return 'gone'
                if fresh(current).get('state') not in helper_close.QUIET:
                    quiet_since = None
                    continue
                helper_close.close_pane(pane, run)
                return 'closed'
            finally:
                lock.release()
        print(f'self close {pane}: still working after {WAIT} s, left open', file=sys.stderr)
        return 'timeout'
    except Exception as exc:  # a failed close must not kill the server thread silently
        print(f'self close {pane} failed: {exc!r}', file=sys.stderr)
        return 'failed'
    finally:
        with _lock:
            _pending.discard(pane)
