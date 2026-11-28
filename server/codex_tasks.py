"""Check an idle Codex tab for live unified-exec terminals."""
from contextlib import closing
import sqlite3
import subprocess

import agents
import procinfo


class Unchecked(RuntimeError):
    """The process scan could not confirm that the tab is clear."""


_GONE = (FileNotFoundError, ProcessLookupError)


def _process_table():
    table = {}
    for pid in procinfo.pids():
        try:
            fields = procinfo.stat_fields(pid)
        except _GONE:
            continue
        table[pid] = int(fields[1]), fields[0]
    return table


def _daemon_pids(table, owner):
    for pid, (_, state) in table.items():
        if pid == owner or state == 'Z':
            continue
        try:
            if procinfo.comm(pid) == 'codex' and agents.is_codex_daemon_command(procinfo.argv(pid)):
                yield pid
        except _GONE:
            continue


def _environment(pid):
    return dict(item.split(b'=', 1) for item in procinfo.environ(pid) or () if b'=' in item)


def _has_tagged_parent(pid, table, tagged):
    seen = {pid}
    while (parent := table[pid][0]) in table and parent not in seen:
        if parent in tagged:
            return True
        seen.add(parent)
        pid = parent
    return False


def background_terminals(entry, *, table=None):
    """Return one process per tagged terminal subtree. Call only when the tab is idle."""
    owner, thread = entry['owner_pid'], entry['thread']
    try:
        table = _process_table() if table is None else table
        if owner not in table or table[owner][1] == 'Z':
            raise ProcessLookupError(owner)
        root_env = {owner: _environment(owner)}
        with closing(agents.codex_database()) as db:
            rollout = agents.open_rollout(db, owner, strict=True)
        roots = {owner: None}
        if not rollout or rollout[0] != thread:
            roots.update((pid, thread.encode()) for pid in _daemon_pids(table, owner))

        scopes = {}
        for root, thread_id in roots.items():
            if root != owner:
                root_env[root] = _environment(root)
            for pid in agents.descendants(root, table) - {root}:
                if table[pid][1] != 'Z':
                    # The owner tree includes embedded subagents with their own thread ids.
                    scopes.setdefault(pid, thread_id)

        tagged = {}
        for pid, thread_id in scopes.items():
            try:
                env = root_env[pid] if pid in root_env else _environment(pid)
                if env.get(b'CODEX_TOOL_CALL_ID') and (thread_id is None or env.get(b'CODEX_THREAD_ID') == thread_id):
                    tagged[pid] = procinfo.comm(pid)
            except _GONE:
                continue
    except (OSError, subprocess.SubprocessError, sqlite3.Error, ValueError, IndexError) as exc:
        raise Unchecked('Could not read the Codex process tree.') from exc

    return [{'pid': pid, 'command': tagged[pid]} for pid in sorted(tagged)
            if not _has_tagged_parent(pid, table, tagged)]
