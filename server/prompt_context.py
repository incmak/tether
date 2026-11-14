"""Send the standing rules once per conversation, not on every prompt (B74).

The autonomy and Codex level hooks add about 6,000 characters of rules to each prompt. The agent keeps earlier hook
text in its context, so the same text again only fills the context window. once() returns the full text the first time
a conversation sees it, and again when the text changes. Otherwise it returns a one-line reminder: the first line of the
rules (the level) and a note that the rules still apply.

Both Claude Code and Codex fire SessionStart again after /clear, a resume and a compaction. claude_hooks.py then calls
forget(), so the next prompt carries the full text again. After a compaction it also adds the saved full texts at
once (saved()), and marks them as sent again only after its output is printed (mark()). It reads them from the records
and does not run the prompt hooks again: SessionStart has a one-second timeout.
As a safety net (a rewind or an undo can drop the earlier text), the full text also goes out every FULL_EVERY prompts.

Records: paths.CACHE/prompt-context/<session_id>.<kind>: a line "<sha256> <prompts since the full text>", then the
full text. A Codex subagent (agent_id in the input) always gets the full text: Codex 0.166 runs no SessionStart hook
when a subagent compacts (core/src/hook_runtime.rs), so nothing would tell this module that its context lost the rules.
"""
import hashlib
import os
import re
import tempfile
import time

import paths

KINDS = ('autonomy', 'codex-level')
FULL_EVERY = 25
KEEP_DAYS = 14
SAME = 'The full rules are unchanged since they were shown earlier in this conversation. Follow them.'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}')


def folder():
    return paths.CACHE / 'prompt-context'


def conversation(data):
    """The record key for one hook input, or None when the input names no conversation."""
    if not isinstance(data, dict):
        return None
    session, agent = data.get('session_id'), data.get('agent_id')
    if not isinstance(session, str) or not ID.fullmatch(session):
        return None
    if agent:
        return None  # a Codex subagent: see the module notes
    return session


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def once(data, kind, text):
    """text, or a one-line reminder when this conversation already has this exact text. Any error sends the full text."""
    key = conversation(data)
    if key is None or kind not in KINDS or not text:
        return text
    digest = hashlib.sha256(text.encode()).hexdigest()
    path = folder() / f'{key}.{kind}'
    try:
        seen, count, _ = _read(path)
        if seen == digest and count + 1 < FULL_EVERY:
            _write(path, f'{digest} {count + 1}\n{text}')
            return f'{text.splitlines()[0]} {SAME}'
        _write(path, f'{digest} 0\n{text}')
    except OSError:
        pass
    return text


def _read(path):
    """(digest, count, full text) of one record; ('', 0, '') when it is missing or broken."""
    try:
        head, _, text = path.read_text().partition('\n')
        seen, _, count = head.partition(' ')
        return seen, int(count), text
    except (OSError, ValueError):
        return '', 0, ''


def saved(data):
    """{kind: the full text this conversation last got}, in KINDS order. Read it before forget()."""
    key = conversation(data)
    found = {}
    for kind in KINDS if key is not None else ():
        seen, _, text = _read(folder() / f'{key}.{kind}')
        if text and hashlib.sha256(text.encode()).hexdigest() == seen:
            found[kind] = text
    return found


def mark(data, kind, text):
    """Record that the full text was just sent outside once(), for example at SessionStart."""
    key = conversation(data)
    if key is None or kind not in KINDS or not text:
        return
    try:
        _write(folder() / f'{key}.{kind}', hashlib.sha256(text.encode()).hexdigest() + ' 0\n' + text)
    except OSError:
        pass


def forget(data, now=time.time):
    """The conversation lost or replaced its context: the next prompt sends the full text. Also drops old records."""
    key = conversation(data)
    for kind in KINDS:
        if key is not None:
            try:
                (folder() / f'{key}.{kind}').unlink()
            except OSError:
                pass
    try:
        old = now() - KEEP_DAYS * 86400
        for entry in os.scandir(folder()):
            if entry.stat().st_mtime < old:
                os.unlink(entry.path)
    except OSError:
        pass
