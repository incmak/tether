"""One-time cleanup to the rule "one stack per main session" (auto_stacks.py). Run it with --dry-run first.

  python3 auto_stacks_cleanup.py --dry-run --proof PROOF.tsv
  python3 auto_stacks_cleanup.py --proof PROOF.tsv

Per tmux session:
1. Group the tabs by root (auto_stacks.root_of). A tab whose root has closed, or is in another session, stays where it
   is and is listed.
2. A family (a root and its helpers) goes to one stack: the root's stack, else the first Tether-made stack of a
   helper, else a new stack. One stack holds one family. Tabs the user placed (@group_by user) and tabs in user stacks
   never move.
3. Tether-made stacks that empty go away. Their notes are copied into the stack their tabs went to first.
4. Each Tether-made stack gets its name by rule 4 (auto_stacks.stack_name), its old name as the --stack name.

Only a Tether-made stack moves, changes or goes: one marked "auto", or one listed in PROOF.tsv (lines "session id<TAB>
group id<TAB>evidence", for example the agent command that made it). The cleanup marks a proven stack "auto". Every
other stack is not touched, nor are its tabs, and the report lists it."""
import argparse
import shutil
import subprocess
import sys

import auto_stacks
import groups
import projects
import paths

FIELDS = '#{pane_id}\t#{@group}\t#{@group_by}\t#{' + auto_stacks.NAME_BY + '}'


def snapshot(session_id, entries, run=subprocess.run):
    """The data plan() needs for one session: groups, panes and roots."""
    shown = run([paths.TMUX, 'list-panes', '-s', '-t', session_id + ':', '-F', FIELDS],
                capture_output=True, text=True, timeout=5)
    if shown.returncode:
        raise groups.GroupError(404, groups.GONE_SESSION)
    by_id = {entry['id']: entry for entry in entries}
    panes = []
    for line in shown.stdout.splitlines():
        pane, group, by, name_by = (line.split('\t') + ['', '', ''])[:4]
        panes.append({'id': pane, 'group': group, 'by_user': by == 'user', 'named_by_user': name_by == 'user'})
    roots = {pane['id']: auto_stacks.root_of(pane['id'], lambda p: by_id.get(p, {}).get('thread'), run)
             for pane in panes}
    return {'session': session_id, 'groups': groups.read(session_id, run), 'panes': panes, 'roots': roots,
            'entries': by_id}


def _source(entry, pane):
    """The root's data for stack_name."""
    out = dict(entry or {'id': pane['id']})
    out['named_by_user'] = pane['named_by_user']
    out.setdefault('folder', out.get('cwd'))
    return out


def plan(snap, proven=()):
    """What the cleanup does in one session. Pure: it reads snap and changes nothing."""
    order = [g['id'] for g in snap['groups']]
    names = {g['id']: g['name'] for g in snap['groups']}
    proven = set(proven)
    auto = {g['id'] for g in snap['groups'] if g.get('auto') or (g['id'] in proven and g.get('by') != 'user')}
    unproven = [{'group': g['id'], 'name': g['name'], 'by': g.get('by') or 'unknown'}
                for g in snap['groups'] if g['id'] not in auto]
    panes = {pane['id']: pane for pane in snap['panes']}
    families, orphans = {}, []
    for pane in snap['panes']:
        root = snap['roots'][pane['id']]
        if root not in panes:
            orphans.append({'pane': pane['id'], 'root': root, 'group': names.get(pane['group'], '')})
            continue
        families.setdefault(root, []).append(pane['id'])
    for members in families.values():
        members.sort(key=lambda p: (p != snap['roots'][p], int(p[1:])))  # the root first, then by age
    place = {pane['id']: pane['group'] for pane in snap['panes']}
    claimed, moves, made, kept = set(), [], [], []
    rank = lambda gid: order.index(gid) if gid in order else len(order)  # noqa: E731

    def movable(pane):
        return not panes[pane]['by_user'] and (not place[pane] or place[pane] in auto)

    helped = sorted((root for root, members in families.items() if len(members) > 1),
                    key=lambda root: (rank(place[root]) if place[root] else len(order), int(root[1:])))
    for root in helped:
        members = families[root]
        target = place[root] if place[root] and place[root] not in claimed else None
        if place[root] and place[root] not in auto:
            kept += [{'pane': p, 'group': names.get(place[p], ''), 'why': 'its root is in a stack not proven Tether-made'}
                     for p in members if place[p] != place[root]]
            continue  # the cleanup never adds tabs to such a stack
        if place[root] and not target and not movable(root):
            target = place[root]  # the user placed the root here: its family joins it
        if target is None:
            target = next((place[p] for p in sorted(members[1:], key=lambda p: (rank(place[p]), int(p[1:])))
                           if place[p] in auto and place[p] not in claimed and movable(p)), None)
        if target is None:
            first = next((names[place[p]] for p in members if place[p] in names), None)
            target = f'new:{root}'
            made.append({'key': target, 'root': root, 'stack': first})
            names[target] = None
            auto.add(target)
        claimed.add(target)
        for pane in members:
            if place[pane] == target:
                continue
            if movable(pane):
                moves.append({'pane': pane, 'from': place[pane], 'to': target, 'root': root})
                place[pane] = target
            else:
                kept.append({'pane': pane, 'group': names.get(place[pane], ''),
                             'why': 'placed by the user' if panes[pane]['by_user'] else 'in a user stack'})
    used = {gid for gid in place.values() if gid}
    drops = [gid for gid in order if gid in auto and gid not in used]
    notes = []
    for gid in drops:  # the notes go where most of its tabs went (the first move on a tie)
        went = [move['to'] for move in moves if move['from'] == gid]
        if went:
            notes.append({'from': gid, 'to': max(dict.fromkeys(went), key=went.count)})
    renames, owners, taken = [], {}, {names[g].lower() for g in order if g not in drops and g not in auto}
    for gid in [g for g in order if g in auto and g not in drops] + [m['key'] for m in made]:
        inside = [p for p in place if place[p] == gid]
        roots = {snap['roots'][p] for p in inside} & set(families)
        root = next(iter(roots)) if len(roots) == 1 else None
        old = names[gid]
        stack = old if old is not None else next((m['stack'] for m in made if m['key'] == gid), None)
        helpers = [p for p in families.get(root, []) if p != root]
        task = next((auto_stacks.tab_task(snap['entries'].get(p) or {}) for p in helpers
                     if auto_stacks.tab_task(snap['entries'].get(p) or {})), None)
        source = _source(snap['entries'].get(root), panes[root]) if root else {}
        if root:
            owners[gid] = root  # groups.py "root": the main session the stack holds
        if not root and old is not None:
            name = auto_stacks.goal(old) or old  # several families, or none with a live root: tidy the name only
        else:
            name = auto_stacks.stack_name(source, stack, task)
        name = groups.free_name([{'name': n} for n in taken], name)
        taken.add(name.lower())
        if name != old:
            renames.append({'group': gid, 'old': old, 'new': name})
    mark = [g for g in order if g in auto and not next(x for x in snap['groups'] if x['id'] == g).get('auto')]
    return {'session': snap['session'], 'moves': moves, 'made': made, 'drops': drops, 'notes': notes,
            'renames': renames, 'mark_auto': mark, 'roots': owners,
            'root_threads': {root: (snap['entries'].get(root) or {}).get('thread') for root in families}, 'orphans': orphans, 'kept': kept, 'names': names,
            'unproven': unproven}


def report(result, before, proof=None):
    proof = proof or {}
    names = result['names']
    final = {r['group']: r['new'] for r in result['renames']}
    label = lambda gid: final.get(gid) or names.get(gid) or 'Unstacked'  # noqa: E731
    old = lambda gid: names.get(gid) or 'Unstacked'  # noqa: E731
    lines = [f"## Session {result['session']}", f'Stacks before: {before}']
    lines += [f"- NOT PROVEN, not touched: {u['name']} ({u['group']}, maker {u['by']})" for u in result['unproven']]
    lines += [f"- mark Tether-made: {old(g)} ({g}; proof: {proof.get(g, 'marked auto')})" for g in result['mark_auto']]
    lines += [f"- new stack for root {m['root']}" for m in result['made']]
    lines += [f"- move {m['pane']}: {old(m['from'])} -> {label(m['to'])}" for m in result['moves']]
    lines += [f"- copy notes: {old(n['from'])} -> {label(n['to'])}" for n in result['notes']]
    lines += [f"- remove empty stack: {old(g)}" for g in result['drops']]
    lines += [f"- rename: {r['old'] or '(new)'} -> {r['new']}" for r in result['renames']]
    lines += [f"- keep {k['pane']} in {k['group'] or 'Unstacked'} ({k['why']})" for k in result['kept']]
    lines += [f"- root not found for {o['pane']} (root {o['root']}, in {o['group'] or 'Unstacked'}): left in place"
              for o in result['orphans']]
    after = before - len(result['drops']) + len(result['made'])
    lines.append(f'Stacks after: {after}')
    return '\n'.join(lines), after


def _copy_notes(source_pid, target_pid, label):
    """Copy a stack's notes into another stack's folder. A name that exists gets the old stack's name in front."""
    source = projects.bundle(source_pid) / 'notes'
    if not source.is_dir():
        return
    target = projects.bundle(target_pid) / 'notes'
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for note in sorted(source.glob('*.md')):
        out, number = target / note.name, 1
        while out.exists():
            if out.read_bytes() == note.read_bytes():
                break
            number += 1
            out = target / (f'{label}-{note.name}' if number == 2 else f'{label}-{number - 1}-{note.name}')
        else:
            shutil.copy2(note, out)


def _step(action, *args):
    """One change of apply. A step that tmux or the stack rules refuse (the user changed something since the plan) is
    skipped and reported; the rest goes on."""
    try:
        action(*args)
        return None
    except (groups.GroupError, OSError, subprocess.SubprocessError) as exc:
        return f'skipped {action.__name__}{tuple(a for a in args if isinstance(a, str))}: {exc}'


def _rename_all(session_id, renames, run):
    """Rename in an order where no new name is still in use: A -> B waits for B -> C. A loop (A -> B, B -> A) goes
    through a free temporary name first."""
    pending, skipped = [dict(r) for r in renames], []
    while pending:
        used = {g['id']: g['name'].lower() for g in groups.read(session_id, run)}
        ready = next((r for r in pending
                      if not any(name == r['new'].lower() and gid != r['group'] for gid, name in used.items())), None)
        if ready is None:  # the first new name is held by another stack
            first = pending[0]
            holder = next(gid for gid, name in used.items() if name == first['new'].lower() and gid != first['group'])
            error = None
            if holder in {r['group'] for r in pending}:  # a loop (A -> B, B -> A): park the holder under a free name
                park = groups.free_name([{'name': n} for n in used.values()], first['new'] + ' old')
                error = _step(groups.rename, session_id, holder, park, run, False)
                if error is None:
                    continue
            skipped.append(error or f"skipped rename to {first['new']}: a stack the user made holds that name")
            pending.remove(first)
            continue
        pending.remove(ready)
        skipped.append(_step(groups.rename, session_id, ready['group'], ready['new'], run, False))
    return [s for s in skipped if s]


def _live_roots(result, thread_of, run):
    """The family roots of the plan that still hold the same conversation in the session."""
    live = set()
    for root, thread in result.get('root_threads', {}).items():
        try:
            if groups.group_of_pane(root, run)[0] != result['session']:
                continue
        except groups.GroupError:
            continue  # closed
        now = thread_of(root)
        if not (thread and now and now != thread):
            live.add(root)
    return live


def apply(result, run=subprocess.run, thread_of=lambda pane: None):
    """Do the plan. It re-reads the stacks first and acts only on stacks that are still Tether's: a stack the user
    renamed or made since the plan (by "user") is not marked, moved into, renamed, removed or copied from. A family
    whose root has closed or holds another conversation since the plan is left in place, and a tab whose root changed
    does not move. thread_of: a pane's conversation now. Returns the skipped steps."""
    session_id, skipped = result['session'], []
    live = _live_roots(result, thread_of, run)
    skipped += [f'skipped family of {root}: its root closed or changed since the plan'
                for root in result.get('root_threads', {}) if root not in live]
    with groups.LOCK():
        current = groups.read(session_id, run)
        for group in current:
            if group['id'] in result['mark_auto'] and group.get('by') != 'user':
                group['auto'] = True
            if group.get('auto') and result.get('roots', {}).get(group['id']):
                group['root'] = result['roots'][group['id']]
        groups.write(session_id, current, run)
        ours = {g['id'] for g in groups.read(session_id, run) if g.get('auto')}
        skipped += [f"skipped {g}: no longer Tether's" for g in result['mark_auto'] if g not in ours]
        ids = {}
        for made in result['made']:
            if made['root'] not in live:
                continue
            ids[made['key']] = groups.create_unique(session_id, 'Work', run, root=made['root'])['id']
            ours.add(ids[made['key']])
        by_id = {g['id']: g for g in groups.read(session_id, run)}
        for note in result['notes']:
            target = by_id.get(ids.get(note['to'], note['to']))
            source = by_id.get(note['from'])
            if source and target and source['project'] and source['id'] in ours and target['id'] in ours:
                pid = target['project'] or groups.context(session_id, target['id'], run)
                slug = ''.join(c if c.isalnum() else '-' for c in source['name'].lower()).strip('-') or 'stack'
                _copy_notes(source['project'], pid, slug)
        for move in result['moves']:
            to = ids.get(move['to'], move['to'])
            if move.get('root') not in live or auto_stacks.root_of(move['pane'], thread_of, run) != move['root']:
                skipped.append(f"skipped move {move['pane']}: its root closed or changed since the plan")
                continue
            try:
                now = groups.group_of_pane(move['pane'], run)[1]
            except groups.GroupError as exc:
                skipped.append(f"skipped move {move['pane']}: {exc}")
                continue
            if groups.placed_by_user(move['pane'], run) or (now['id'] if now else '') != (move['from'] or '') \
                    or (move['from'] and move['from'] not in ours) or to not in ours:
                skipped.append(f"skipped move {move['pane']}: it or its stack changed since the plan")
                continue
            skipped.append(_step(groups.move, session_id, move['pane'], to, run))
        for gid in result['drops']:
            skipped.append(_step(groups.drop_if_empty, session_id, gid, run))
        roots = result.get('roots', {})
        kept = [r for r in result['renames'] if (r['group'] not in roots or roots[r['group']] in live)
                and (not r['group'].startswith('new:') or r['group'] in ids)]
        skipped += [f"skipped rename {r['old'] or '(new)'} -> {r['new']}: its root closed or changed"
                    for r in result['renames'] if r not in kept]
        renames = [dict(r, group=ids.get(r['group'], r['group'])) for r in kept]
        skipped += [f"skipped rename {r['old']}: no longer Tether's" for r in renames if r['group'] not in ours]
        skipped += _rename_all(session_id, [r for r in renames if r['group'] in ours], run)
    return [s for s in skipped if s]


def main(argv=None, run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--dry-run', action='store_true', help='print the plan and change nothing')
    parser.add_argument('--proof', metavar='FILE', required=True,
                        help='lines "session id<TAB>group id<TAB>evidence": stacks proven to be Tether-made')
    args = parser.parse_args(argv)
    proof = {}
    with open(args.proof, encoding='utf-8') as lines:
        for line in lines:
            fields = line.rstrip('\n').split('\t', 2)
            if len(fields) == 3 and groups.SESSION.fullmatch(fields[0]) and groups.GROUP.fullmatch(fields[1]) and fields[2].strip():
                proof[(fields[0], fields[1])] = fields[2].strip()
    import server  # the agent list (threads, names); server.py starts nothing on import
    entries = server.sessions()
    shown = run([paths.TMUX, 'list-sessions', '-F', '#{session_id}'], capture_output=True, text=True, timeout=5)
    total_before = total_after = 0
    for session_id in shown.stdout.split():
        snap = snapshot(session_id, entries, run)
        if not snap['groups']:
            continue
        mine = {gid: text for (session, gid), text in proof.items() if session == session_id}
        result = plan(snap, mine)
        text, after = report(result, len(snap['groups']), mine)
        total_before += len(snap['groups'])
        total_after += after
        print(text + '\n')
        if not args.dry_run:
            threads = {entry['id']: entry.get('thread') for entry in server.sessions()}
            for line in apply(result, run, threads.get):
                print('- ' + line)
    print(f"Total stacks: {total_before} -> {total_after}{' (dry run: nothing changed)' if args.dry_run else ''}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
