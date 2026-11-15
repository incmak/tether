#!/usr/bin/env python3
"""Install a clean UI build while preserving chunks used by open tabs.

B92: each build carries .build-commit (written by ui/vite.config.ts). The install records it as .live-commit in the
target. A build whose commit does not contain the live commit would remove deployed work, so it is refused.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

STAMP = '.build-commit'
RECORD = '.live-commit'
REPO = Path(__file__).resolve().parent.parent


class Refused(ValueError):
    """The guard stopped the install. --force overrides it."""


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)


def read_json(path: Path, label: str):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise Refused(f'{label} {path} is not valid JSON.') from None
    if not isinstance(data, dict) or not re.fullmatch(r'[0-9a-f]{40}', str(data.get('commit', ''))):
        raise Refused(f'{label} {path} has no full commit id.')
    return data


def resolve(repo: Path, commit: str) -> bool:
    return git(repo, 'cat-file', '-e', f'{commit}^{{commit}}').returncode == 0


def check(build: Path, target: Path, repo: Path) -> list[str]:
    """Return warnings, or raise Refused when the build would remove live work."""
    stamp = read_json(build / STAMP, 'The build stamp')
    live = read_json(target / RECORD, 'The live record')
    if stamp is None:
        raise Refused(f'The build has no {STAMP}, so its commit is unknown. Rebuild with the current '
                      'ui/vite.config.ts in a git checkout.')
    warnings = []
    if stamp.get('dirty'):
        changes = ', '.join(stamp.get('changes') or []) or 'not listed'
        raise Refused(f'The build came from a checkout with uncommitted changes ({changes}). The next deploy '
                      'would remove them without notice. Commit them, then rebuild.')
    built = stamp['commit']
    if live is None:
        return [f'No {RECORD} yet: this deploy records {built[:12]} as the live commit.']
    current = live['commit']
    if live.get('dirty'):
        warnings.append(f'The live build {current[:12]} had uncommitted changes. They are not in any commit.')
    for commit, label in ((current, 'live'), (built, 'build')):
        if not resolve(repo, commit):
            raise Refused(f'The {label} commit {commit} is not in {repo}. Run `git -C {repo} fetch origin`, '
                          'then deploy again.')
    ancestor = git(repo, 'merge-base', '--is-ancestor', current, built)
    if ancestor.returncode == 1:
        lost = git(repo, 'log', '--oneline', f'{built}..{current}').stdout.strip()
        raise Refused(f'The build drops live commits.\n  live:  {current}\n  build: {built}\n'
                      f'Commits that would be lost:\n{lost}\n'
                      'Fix: rebase on (or merge) origin/main and any deployed commits not yet pushed, then rebuild.')
    if ancestor.returncode != 0:
        raise Refused(f'git merge-base failed: {ancestor.stderr.strip()}')
    return warnings


def write_record(target: Path, record: dict):
    staged = target / f'{RECORD}.tmp'
    staged.write_text(json.dumps(record, indent=2) + '\n')
    os.replace(staged, target / RECORD)


def deploy(build: Path, target: Path, repo: Path = REPO, force: bool = False, dry_run: bool = False,
           warn=lambda text: print(f'Warning: {text}', file=sys.stderr)) -> int:
    build, target = build.resolve(), target.resolve()
    entry = build / 'index.html'
    if build == target or not entry.is_file():
        raise ValueError('Use a separate build folder with index.html.')
    assets = re.findall(r'(?:src|href)=["\'](/chat/assets/[^"\']+)["\']', entry.read_text())
    if not assets or any(not (build / url.removeprefix('/chat/')).is_file() for url in assets):
        raise ValueError('The entry page must reference files present in the build.')
    try:
        for text in check(build, target, repo):
            warn(text)
    except Refused as error:
        if not force:
            raise
        warn(f'--force: installing anyway. {error}')
    if dry_run:
        return 0
    try:
        stamp = read_json(build / STAMP, 'The build stamp')
    except Refused:
        stamp = None
    target.mkdir(parents=True, exist_ok=True)
    # Stage on the destination filesystem so each replacement is atomic.
    with tempfile.TemporaryDirectory(prefix='.ui-deploy-', dir=target.parent) as folder:
        stage = Path(folder)
        files = [path for path in build.rglob('*') if path.is_file() and path.relative_to(build) != Path(STAMP)]
        for source in files:
            relative = source.relative_to(build)
            staged = stage / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged)
        # Retain prior chunks. Publish the entry page only after its assets exist.
        for source in sorted(files, key=lambda path: path == entry):
            relative = source.relative_to(build)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / relative, destination)
    (target / '.live-ui').touch(exist_ok=True)
    if stamp is None:
        # A forced install of an unknown commit leaves nothing to compare with. The next stamped deploy records again.
        (target / RECORD).unlink(missing_ok=True)
        warn(f'The live commit is unknown, so {RECORD} was removed. The guard starts again with the next stamped build.')
    else:
        write_record(target, {**stamp, 'entry': assets[0].removeprefix('/chat/'),
                              'deployed': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    return len(files)


def record_live(commit: str, target: Path, repo: Path = REPO) -> str:
    """Record the commit of the build that is live now, for a target installed before B92."""
    found = git(repo, 'rev-parse', '--verify', '--quiet', f'{commit}^{{commit}}')
    if found.returncode != 0:
        raise ValueError(f'{commit} is not a commit in {repo}. Fetch it first.')
    target = target.resolve()
    if not (target / 'index.html').is_file():
        raise ValueError(f'{target} has no index.html.')
    full = found.stdout.strip()
    entry = re.search(r'src=["\']/chat/(assets/index-[^"\']+\.js)', (target / 'index.html').read_text())
    write_record(target, {'commit': full, 'dirty': False, 'changes': [], 'entry': entry[1] if entry else None,
                          'deployed': None, 'recorded': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    return full


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     usage='%(prog)s [--force] [--dry-run] BUILD TARGET\n'
                                           '       %(prog)s --record-live COMMIT TARGET')
    parser.add_argument('paths', nargs='+', type=Path, metavar='BUILD TARGET')
    parser.add_argument('--force', action='store_true', help='install even when the guard refuses the build')
    parser.add_argument('--dry-run', action='store_true', help='run the checks only; install nothing')
    parser.add_argument('--record-live', metavar='COMMIT',
                        help='record COMMIT as the live build in TARGET; install nothing')
    args = parser.parse_args()
    if len(args.paths) != (1 if args.record_live else 2):
        parser.error('give BUILD TARGET, or --record-live COMMIT TARGET')
    try:
        if args.record_live:
            print(f'Recorded {record_live(args.record_live, args.paths[0])} as the live commit.')
            return
        count = deploy(*args.paths, force=args.force, dry_run=args.dry_run)
    except Refused as error:
        parser.exit(1, f'Deployment refused: {error}\nUse --force only when you mean to remove that work.\n')
    except (OSError, ValueError) as error:
        parser.exit(1, f'Deployment failed: {error}\n')
    print('Checks passed; nothing installed (--dry-run).' if args.dry_run
          else f'Installed {count} files; prior chunks retained.')


if __name__ == '__main__':
    main()
