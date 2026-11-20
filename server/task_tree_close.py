"""Sweep Done tasks using continuous idle, activity and view observations."""
import subprocess
import sys
import time

import helper_tabs
import task_tree
import task_tree_api


class Closer:
    def __init__(self, now=time.monotonic):
        self.now = now
        self.timers = {}
        self.generation = 0

    def reset(self):
        self.generation += 1

    def _signature(self, pane, tree, backlog, user_prompt_at, activity, viewed):
        members = [pane, *sorted(tree.children(pane))]
        if pane in task_tree.pending_roots(tree.options, tree.entries, tree.fields):
            return None
        root = tree.entries[pane]
        if not task_tree.is_done(root, tree.status_row(pane),
                                 [tree.status_row(child) for child in members[1:]],
                                 tree.options.get(pane), backlog, user_prompt_at):
            return None
        signature = []
        for member in members:
            entry, options = tree.entries[member], tree.options.get(member)
            thread = helper_tabs.verified_thread(entry)
            viewing, last_view = viewed(member)
            version = activity(entry)
            if (viewing or not thread or options is None or options.session != entry.get('tmux_session')
                    or entry.get('agent') not in ('claude', 'codex') or version is None):
                return None
            signature.append((member, thread, options, version, last_view))
        return tuple(signature)

    def observe(self, tree, fresh, *, enabled, lock, activity, viewed, draft, changed, user_prompt_at):
        now = self.now()
        previous, self.timers = self.timers, {}
        if not enabled():
            return []
        backlog = task_tree.done_items()
        candidates = [pane for pane in tree.options
                      if pane in tree.entries and pane not in tree.fields
                      and not task_tree.placement(pane, tree.options, tree.entries)]
        generation = self.generation
        closed = []
        for pane in candidates:
            signature = self._signature(pane, tree, backlog, user_prompt_at, activity, viewed)
            if signature is None:
                continue
            key = (generation, signature)
            since, last = previous.get(pane, (now, None))
            if key != last:
                since = now
            self.timers[pane] = since, key
            if now - since < task_tree.CLOSE_AFTER or not helper_tabs.SEND_LOCK.acquire(timeout=2):
                continue
            if not lock.acquire(timeout=2):
                helper_tabs.SEND_LOCK.release()
                continue
            try:
                live = fresh()
                if pane not in live.entries or pane in live.fields:
                    continue

                def recheck():
                    if (self.generation != generation or not enabled()
                            or self._signature(pane, live, task_tree.done_items(), user_prompt_at, activity, viewed)
                            != signature):
                        raise helper_tabs.Stale('A task or helper changed during the auto-close check.')

                recheck()
                for member, *_ in signature:
                    if draft(live.entries[member]):
                        raise ValueError('A task or helper has an unsent draft.')
                result = task_tree_api.close_task(pane, live, set(live.children(pane)), recheck=recheck)
                closed.extend(result['closed'])
            except helper_tabs.Stale:
                pass
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                print(f'task auto-close {pane}: {exc}', file=sys.stderr)
            finally:
                self.timers.pop(pane, None)
                changed()
                lock.release()
                helper_tabs.SEND_LOCK.release()
        return closed


def clear_obsolete_marks(tree, lock, user_prompt_at):
    """Forget marks once discovery proves a new conversation or a later user prompt."""
    for pane, options in tree.options.items():
        if not (options.done or options.reopened):
            continue
        entry = tree.entries.get(pane)
        thread = helper_tabs.verified_thread(entry)
        if not thread:
            continue
        values = {}
        if options.done and (options.done != thread or not task_tree.manual_mark(entry, options, user_prompt_at)):
            values.update(done=None, done_at=None)
        if options.reopened and options.reopened != thread:
            values['reopened'] = None
        if values and lock.acquire(timeout=2):
            try:
                current = helper_tabs.read_panes(tree.run).get(pane)
                if current == options:
                    task_tree.set_marks(pane, tree.run, **values)
            finally:
                lock.release()
