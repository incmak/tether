#!/usr/bin/env python3
"""Reliability checklist (B209): the rows of docs/reliability-checklist.md that a diff hits.

Each row comes from a past incident. It has a diff trigger (a regex on changed paths, a regex on added lines, or
both) and the check to make. The checklist is read from the base ref (origin/main), not from the branch under
review: a branch must not remove the rows that apply to it.

Row format in the Markdown file:

    ## R3: Worker tabs start without a trust prompt
    - Paths: `^server/project_launch\\.py$`
    - Text: `--add-dir`
    - Check: Start one worker tab in a new worktree. It must reach its prompt with no question.
    - Incident: B188, commit 5d59421

Paths and Text are Python regular expressions (re.search). A row needs at least one of them, a Check and an Incident.
A row hits when a changed path matches Paths (no Paths: every path) and, when Text is given, an added line of such a
file matches Text or the file could not be scanned.

Results: 'clear' (no row hit), 'hit', 'absent' (the base ref has no checklist), 'unchecked' (git or the checklist
could not be read; treat it as not checked, never as clear).

Usage: reliability_checklist.py [--repo DIR] [--base REF] [--head REV] [--file PATH] [--json]
       reliability_checklist.py --validate [--file PATH | --repo DIR --base REF]
  --head REV    check the commits base...REV. Without it: the working tree of DIR against the merge base, with
                uncommitted and untracked files.
  --file PATH   read the checklist from a file, not from REF:docs/reliability-checklist.md.
Exit codes: 0 clear, 1 rows hit, 2 absent or unchecked (or bad arguments, or an invalid checklist with --validate).
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

FILE = 'docs/reliability-checklist.md'
BASE = 'origin/main'
TIMEOUT = 5
MAX_DIFF = 4_000_000    # bytes of patch text read; a larger diff is unchecked
MAX_UNTRACKED = 200     # untracked files read as added text
MAX_FILE = 2_000_000    # bytes scanned from one untracked file
MAX_UNTRACKED_BYTES = 20_000_000
MAX_PATHS = 500         # changed paths listed in the result
HEADING = re.compile(r'^##\s+([A-Z]+\d+)\s*:\s*(.+?)\s*$')
FIELD = re.compile(r'^\s*[-*]\s*(Paths|Text|Check|Incident)\s*:\s*(.*?)\s*$')


class Unchecked(Exception):
    """The diff or the checklist could not be read."""


def parse(text):
    """(rows, errors). A row: {'id', 'title', 'paths', 'text', 'check', 'incident'}; paths and text are regex sources
    or ''. Rows with errors are left out and named in errors."""
    rows, errors, row = [], [], None

    def close():
        if row is None:
            return
        problems = []
        for key in ('paths', 'text'):
            try:
                re.compile(row[key])
            except re.error as error:
                problems.append(f'{key} is not a valid regular expression ({error})')
        if not row['paths'] and not row['text']:
            problems.append('has no Paths and no Text trigger')
        if not row['check']:
            problems.append('has no Check')
        if not row['incident']:
            problems.append('has no Incident')
        if any(other['id'] == row['id'] for other in rows):
            problems.append('is defined twice')
        if problems:
            errors.extend(f"{row['id']} {problem}" for problem in problems)
        else:
            rows.append(row)

    for line in str(text or '').splitlines():
        heading = HEADING.match(line)
        if heading:
            close()
            row = {'id': heading[1], 'title': heading[2], 'paths': '', 'text': '', 'check': '', 'incident': ''}
            continue
        if line.startswith('#'):  # another section ends the row
            close()
            row = None
            continue
        field = FIELD.match(line)
        if field and row is not None:
            key, value = field[1].lower(), field[2]
            if key in ('paths', 'text') and len(value) >= 2 and value[0] == value[-1] == '`':
                value = value[1:-1]
            row[key] = value
    close()
    return rows, errors


def hits(rows, changes):
    """Rows hit by changes ({path: added lines or None if unscanned}), with the paths that hit each row."""
    found = []
    for row in rows:
        paths = re.compile(row['paths']) if row['paths'] else None
        text = re.compile(row['text']) if row['text'] else None
        files = [path for path, lines in changes.items()
                 if (paths is None or paths.search(path))
                 and (text is None or lines is None or any(text.search(line) for line in lines))]
        if files:
            found.append({**row, 'files': sorted(files)})
    return found


def git(repo, *args, binary=False):
    try:
        result = subprocess.run(['git', '-c', 'core.quotepath=off', *args], cwd=repo, capture_output=True,
                                timeout=TIMEOUT, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        raise Unchecked(f'git {args[0]} did not run: {error.__class__.__name__}') from None
    # Git can return success after skipping unreadable paths, with only a warning on stderr.
    if result.returncode != 0 or result.stderr:
        message = result.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise Unchecked(f'git {args[0]} failed: {message[-1][:200] if message else result.returncode}')
    return result.stdout if binary else result.stdout.decode('utf-8', 'replace')


def added_lines(patch, names):
    """Map each patch section to the corresponding path from git diff --name-only -z."""
    sections, in_hunk = [], False
    for line in patch.split('\n'):
        if line.startswith('diff --git '):
            sections.append([])
            in_hunk = False
        elif sections and line.startswith('@@'):
            in_hunk = True
        elif in_hunk and line.startswith('+'):
            sections[-1].append(line[1:].removesuffix('\r'))
    if len(sections) != len(names):
        raise Unchecked('git diff paths and patch sections do not match')
    return dict(zip(names, sections))


def top_level(repo, allow_bare=False):
    """The worktree root, or the Git directory for a commit check in a bare repository."""
    if allow_bare and git(repo, 'rev-parse', '--is-bare-repository').strip() == 'true':
        return os.fsdecode(git(repo, 'rev-parse', '--absolute-git-dir', binary=True).removesuffix(b'\n'))
    return os.fsdecode(git(repo, 'rev-parse', '--show-toplevel', binary=True).removesuffix(b'\n'))


def untracked_changes(repo):
    """Scan untracked files within the byte budget. Mark unreadable or oversized files with None."""
    names = git(repo, 'ls-files', '--others', '--exclude-standard', '-z', binary=True).split(b'\0')
    names = [os.fsdecode(name) for name in names if name]
    if len(names) > MAX_UNTRACKED:
        raise Unchecked(f'more than {MAX_UNTRACKED} untracked files')
    changes, remaining = {}, MAX_UNTRACKED_BYTES
    for name in names:
        path = Path(repo) / name
        try:
            if path.is_symlink():
                data = os.fsencode(os.readlink(path))
            else:
                with path.open('rb') as stream:
                    data = stream.read(min(MAX_FILE, remaining) + 1)
        except OSError:
            changes[name] = None
            continue
        remaining -= len(data)
        if remaining < 0:
            raise Unchecked(f'the untracked scan exceeds {MAX_UNTRACKED_BYTES} bytes')
        changes[name] = None if len(data) > MAX_FILE else data.decode('utf-8', 'replace').splitlines()
    return changes


def collect(repo, base=BASE, head=None):
    """(base commit, changes) for base...head or the working tree, from the repository top level."""
    start = git(repo, 'merge-base', base, head or 'HEAD').strip()
    options = ('--no-renames', '--no-relative', '--no-ext-diff', '--text', '--no-textconv')

    def scan(*span):
        names = git(repo, 'diff', '--name-only', '-z', *options, *span, '--', binary=True).split(b'\0')
        patch = git(repo, 'diff', '--no-color', '-U0', *options, *span, '--', binary=True)
        if len(patch) > MAX_DIFF:
            raise Unchecked(f'the diff is larger than {MAX_DIFF} bytes')
        return added_lines(patch.decode('utf-8', 'replace'), [os.fsdecode(name) for name in names if name])

    if head is not None:
        return start, scan(start, head)
    # The working copy and the index can differ: the next commit takes the staged version. Scan both.
    changes = scan(start)
    # The commits since the base too: a review names HEAD, and a staged or uncommitted revert must not hide what
    # HEAD holds.
    for more in (scan('--cached', start), scan(start, 'HEAD')):
        for name, lines in more.items():
            changes[name] = changes.get(name, []) + lines
    # Join, never replace: a path can be in a commit and, after `git mv` or `git rm --cached`, untracked again.
    for name, lines in untracked_changes(repo).items():
        changes[name] = None if lines is None else changes.get(name, []) + lines
    return start, changes


def checklist_text(repo, base=BASE, file=None):
    """The checklist text, or None when the base ref has no checklist."""
    if file is not None:
        try:
            return Path(file).read_text(encoding='utf-8')
        except (OSError, ValueError) as error:
            raise Unchecked(f'{file} could not be read: {error.__class__.__name__}') from None
    commit = git(repo, 'rev-parse', '--verify', '--quiet', '--end-of-options', f'{base}^{{commit}}').strip()
    if not git(repo, 'ls-tree', '--name-only', commit, '--', FILE):
        return None
    try:
        return git(repo, 'show', f'{commit}:{FILE}', binary=True).decode('utf-8')
    except UnicodeError as error:
        raise Unchecked(f'{FILE} could not be read: {error.__class__.__name__}') from None


def check(repo, base=BASE, head=None, file=None):
    """{'status', 'rows', 'paths', 'base', 'reason'} for the diff. Never raises for git or checklist errors."""
    result = {'status': 'unchecked', 'rows': [], 'paths': [], 'base': None, 'reason': ''}
    try:
        repo = top_level(repo, allow_bare=head is not None)
        result['base'], changes = collect(repo, base, head)
        result['paths'] = sorted(changes)[:MAX_PATHS]
        text = checklist_text(repo, base, file)
        if text is None:
            return {**result, 'status': 'absent', 'reason': f'{base} has no {FILE}'}
        rows, errors = parse(text)
        if errors:
            raise Unchecked('the checklist has invalid rows: ' + '; '.join(errors)[:300])
        if not rows:
            raise Unchecked('the checklist has no rows')
        found = [{key: row[key] for key in ('id', 'title', 'check', 'incident', 'files')} for row in hits(rows, changes)]
        return {**result, 'status': 'hit' if found else 'clear', 'rows': found}
    except Unchecked as error:
        return {**result, 'reason': str(error)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', default='.', help='a folder in the repository (default: the current folder)')
    parser.add_argument('--base', default=BASE, help=f'the ref the work is compared with (default: {BASE})')
    parser.add_argument('--head', help='check the commits base...HEAD_REV, not the working tree')
    parser.add_argument('--file', help='read the checklist from this file')
    parser.add_argument('--json', action='store_true', help='print the result as one JSON object')
    parser.add_argument('--validate', action='store_true', help='check the checklist itself; read no diff')
    args = parser.parse_args(argv)
    if args.validate:
        try:
            repo = top_level(args.repo, allow_bare=True) if args.file is None else args.repo
            text = checklist_text(repo, args.base, args.file)
        except Unchecked as error:
            parser.exit(2, f'Not checked: {error}\n')
        if text is None:
            parser.exit(2, f'{args.base} has no {FILE}\n')
        rows, errors = parse(text)
        if errors or not rows:
            parser.exit(2, '\n'.join(errors or ['The checklist has no rows.']) + '\n')
        print(f'{len(rows)} rows, all valid.')
        return 0
    result = check(args.repo, args.base, args.head, args.file)
    if args.json:
        print(json.dumps(result))
    elif result['status'] == 'hit':
        print(f"{len(result['rows'])} checklist row(s) hit:")
        for row in result['rows']:
            files = ', '.join(row['files'][:5]) + (f" and {len(row['files']) - 5} more" if len(row['files']) > 5 else '')
            print(f"- {row['id']} {row['title']}\n    Check: {row['check']}\n    Incident: {row['incident']}\n    Files: {files}")
    elif result['status'] == 'clear':
        print('No checklist row hit.')
    else:
        print(f"Not checked: {result['reason']}", file=sys.stderr)
    return {'clear': 0, 'hit': 1}.get(result['status'], 2)


if __name__ == '__main__':
    sys.exit(main())
