#!/usr/bin/env python3
"""Install a clean UI build while preserving chunks used by open tabs.

B92: each build carries .build-commit (written by ui/vite.config.ts). The install records it as .live-commit in the
target. A build whose commit does not contain the live commit would remove deployed work, so it is refused.
B189: each install needs --verified STAMP ("tests(...) review(...)" or "none (reason)"). The stamp goes into the live
record and a DEPLOYED.md line. Deployed commits without a "Verified:" trailer give a warning only (observe mode).
B207: --shots DIR records screenshot proof. Missing or invalid proof gives a warning only.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / 'scripts'), str(REPO / 'server')]
import review_gate  # noqa: E402
import verification  # noqa: E402  (scripts/verification.py)

STAMP = '.build-commit'
RECORD = '.live-commit'
PENDING = '.live-commit.pending'
LOCK = '.deploy-lock'


class Refused(ValueError):
    """The guard stopped the install. --force overrides it."""


class ReviewRefused(ValueError):
    """The review gate stopped the install."""


def git(repo: Path, *args: str, timeout=30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, errors='replace',
                              timeout=timeout, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except subprocess.TimeoutExpired:
        raise Refused(f'git {args[0]} timed out') from None
    except OSError as error:
        raise Refused(f'git did not start: {error}') from None


@contextmanager
def locked(target: Path):
    """One deploy per target: two parallel deploys would both pass the check against the same live commit."""
    target.mkdir(parents=True, exist_ok=True)
    with open(target / LOCK, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'Another UI deploy to {target} is running. Wait for it, then run again.') from None
        yield


def read_json(path: Path, label: str):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise Refused(f'{label} {path} is not valid JSON.') from None
    if (not isinstance(data, dict) or not isinstance(data.get('commit'), str)
            or not review_gate.HASH.fullmatch(data['commit'])):
        raise Refused(f'{label} {path} has no full commit id.')
    return data


def resolve(repo: Path, commit: str) -> bool:
    return git(repo, 'cat-file', '-e', f'{commit}^{{commit}}').returncode == 0


def check(stamp: dict | None, target: Path, repo: Path) -> list[str]:
    """Return warnings, or raise Refused when the build would remove live work."""
    live = read_json(target / RECORD, 'The live record')
    if (target / PENDING).exists():
        raise Refused(f'An earlier deploy did not finish ({target / PENDING}), so the live commit is unknown. Check '
                      'which assets/index-*.js the live index.html loads, then run --record-live COMMIT for its commit.')
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


def review_base(repo: Path) -> str | None:
    try:
        result = git(repo, 'ls-remote', 'origin', review_gate.MAIN, timeout=review_gate.GIT_TIMEOUT)
        commit, sep, ref = result.stdout.rstrip('\n').partition('\t')
        if result.returncode == 0 and sep and ref == review_gate.MAIN and review_gate.HASH.fullmatch(commit):
            return commit if resolve(repo, commit) else None
    except Refused:
        pass
    return None


def check_review(stamp: dict | None, repo: Path):
    commit = stamp['commit'] if stamp else None
    try:
        pane = review_gate.pane_identity()
        links = review_gate.links_for(pane) if pane is not None else []
    except review_gate.GateRefused as error:
        short = f'Commit {commit[:12]}. ' if commit else ''
        raise ReviewRefused(f'{short}{error}') from None
    if not links:
        return
    if not stamp or stamp.get('dirty') is not False:
        reason = f'The build stamp for {commit[:12]} does not show a clean checkout.' if commit else (
            'The build has no valid commit stamp.')
        raise ReviewRefused(f'{reason} Rebuild from a clean checkout and get the commit approved, '
                            'or stop the review link with Mak.')
    base = review_base(repo) if review_gate.MERGE_FORWARD else None
    try:
        review_gate.check(repo, commit, pane=pane, links=links, base=base)
    except review_gate.GateRefused as error:
        raise ReviewRefused(str(error)) from None


def write_record(target: Path, record: dict, name: str = RECORD):
    staged = target / f'{name}.tmp'
    staged.write_text(json.dumps(record, indent=2) + '\n')
    os.replace(staged, target / name)


def deploy(build: Path, target: Path, repo: Path = REPO, force: bool = False, dry_run: bool = False,
           warn=lambda text: print(f'Warning: {text}', file=sys.stderr), *, verified: str | None = None,
           log: Path | None = None, shots: Path | None = None) -> int:
    """Install the build and log its verification stamp and any screenshots."""
    try:
        verified = verification.valid(verified)
    except ValueError as error:
        raise ValueError(f'--verified is missing or malformed. {error}') from None
    build, target = build.resolve(), target.resolve()
    log = Path(log) if log else verification.log_path(repo)
    if dry_run and not target.is_dir():
        return install(build, target, repo, force, dry_run, warn, verified, log, shots)
    with locked(target):
        return install(build, target, repo, force, dry_run, warn, verified, log, shots)


def observe_deploy(build: Path, target: Path, repo: Path, warn, has_shots: bool):
    """Warn about missing verification trailers and screenshot proof."""
    try:
        live = (read_json(target / RECORD, 'The live record') or {}).get('commit')
    except (OSError, Refused):
        return
    if not live:
        if not has_shots:
            warn('This deploy names no screenshots folder and there is no live record to compare with. '
                 'Add --shots DIR (observe mode, not refused).')
        return
    try:
        built = (read_json(build / STAMP, 'The build stamp') or {}).get('commit')
    except (OSError, Refused):
        return
    if not built or built == live:
        return
    found = verification.unverified_commit_range(repo, live, built)
    if found:
        shown = '\n'.join(f'  [{kind}] {short} {subject[:100]}' for short, subject, kind in found[:5])
        more = f'\n  and {len(found) - 5} more' if len(found) > 5 else ''
        warn(f'{len(found)} deployed commit(s) have no "Verified:" trailer or say "none" (observe mode, not '
             f'refused):\n{shown}{more}')
    if not has_shots:
        # The same files for which a review asks for screenshots (server/review_scope.py UI_PATH).
        paths = verification.git(repo, 'diff', '--name-only', '-z', '--no-ext-diff', live, built, '--',
                                 'ui/src', 'ui/public', 'ui/index.html', 'ui/vite.config.ts', 'ui/package.json',
                                 'ui/package-lock.json', 'server/ui_manifest.json',
                                 'server/index.html', 'server/app.js')
        if paths:
            count = paths.count('\0')
            warn(f'This deploy changes {count} UI file(s) and names no screenshots folder. '
                 'Add --shots DIR (observe mode, not refused).')


def install(build: Path, target: Path, repo: Path, force: bool, dry_run: bool, warn, verified: str, log: Path,
            shots: Path | None) -> int:
    entry = build / 'index.html'
    if build == target or not entry.is_file():
        raise ValueError('Use a separate build folder with index.html.')
    assets = re.findall(r'(?:src|href)=["\'](/chat/assets/[^"\']+)["\']', entry.read_text())
    if not assets or any(not (build / url.removeprefix('/chat/')).is_file() for url in assets):
        raise ValueError('The entry page must reference files present in the build.')
    stamp = None
    guard_error = None
    try:
        stamp = read_json(build / STAMP, 'The build stamp')
        warnings = check(stamp, target, repo)
    except Refused as error:
        guard_error = error
        warnings = []
    check_review(stamp, repo)
    if guard_error is not None:
        if not force:
            raise guard_error
        warn(f'--force: installing anyway. {guard_error}')
    for text in warnings:
        warn(text)
    # After the guard, and the same with --dry-run: an invalid folder is reported and counts as no proof.
    shown, count = verification.observe_shots(shots, repo, warn)
    proof = {'shots': shown, 'shots_count': count} if shown is not None else {}
    observe_deploy(build, target, repo, warn, bool(proof))
    if dry_run:
        return 0
    target.mkdir(parents=True, exist_ok=True)
    # Until the record is written, the live commit is unknown. If this run stops after the entry page is published,
    # the pending file makes the next deploy refuse instead of comparing against a stale record.
    # Until the new record is written, the live commit is unknown. The pending file makes the next deploy refuse if this
    # run stops after the entry page is published. Before that, the old entry page is still live, so the old record and
    # any earlier pending file are restored as they were.
    pending = target / PENDING
    earlier = pending.read_bytes() if pending.exists() else None
    write_record(target, stamp or {'commit': None}, PENDING)
    try:
        count = copy_build(build, target, entry)
    except BaseException:
        live_entry = target / 'index.html'
        if not (live_entry.is_file() and live_entry.read_bytes() == entry.read_bytes()):
            if earlier is None:
                pending.unlink(missing_ok=True)
            else:
                pending.write_bytes(earlier)
        raise
    if stamp is None:
        # A forced install of an unknown commit leaves nothing to compare with. The next stamped deploy records again.
        (target / RECORD).unlink(missing_ok=True)
        warn(f'The live commit is unknown, so {RECORD} was removed. The guard starts again with the next stamped build.')
    else:
        write_record(target, {**stamp, **proof, 'entry': assets[0].removeprefix('/chat/'), 'verified': verified,
                              'deployed': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    pending.unlink()
    commit = (stamp or {}).get('commit')
    try:
        subject = git(repo, 'log', '-1', '--format=%s', commit).stdout.strip() if commit else ''
    except Refused:
        subject = ''
    # A subject may itself say "Verified:" (a commit about this rule); only the stamp at the end may.
    subject = subject.replace('Verified:', 'Verified')
    text = f'UI deploy: {subject}' if subject else 'UI deploy (commit unknown)'
    try:
        verification.append(log, verification.line(text, verified, commit[:7] if commit else None,
                                                   shots=proof.get('shots')))
    except (OSError, ValueError) as error:  # the build is live now: report, never fail
        warn(f'Installed, but the DEPLOYED.md line was not written ({log}: {error}). Add it with: '
             f'scripts/verification.py log --verified "{verified}" "{text}"')
    return count


def copy_build(build: Path, target: Path, entry: Path) -> int:
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
    with locked(target):
        entry = re.search(r'src=["\']/chat/(assets/index-[^"\']+\.js)', (target / 'index.html').read_text())
        write_record(target, {'commit': full, 'dirty': False, 'changes': [], 'entry': entry[1] if entry else None,
                              'deployed': None, 'recorded': datetime.now(timezone.utc).isoformat(timespec='seconds')})
        (target / PENDING).unlink(missing_ok=True)
    return full


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     usage='%(prog)s --verified STAMP [--shots DIR] [--force] [--dry-run] '
                                           'BUILD TARGET\n'
                                           '       %(prog)s --record-live COMMIT TARGET')
    parser.add_argument('paths', nargs='+', type=Path, metavar='BUILD TARGET')
    parser.add_argument('--force', action='store_true', help='install even when the guard refuses the build')
    parser.add_argument('--dry-run', action='store_true', help='run the checks only; install nothing')
    parser.add_argument('--shots', metavar='DIR', type=Path,
                        help='folder with the before and after screenshots (invalid folders give a warning)')
    parser.add_argument('--verified', metavar='STAMP',
                        help='how this build was verified: "tests(server 412 ok) review(codex APPROVED)" or '
                             '"none (reason)". Required to install.')
    parser.add_argument('--record-live', metavar='COMMIT',
                        help='record COMMIT as the live build in TARGET; install nothing')
    args = parser.parse_args()
    if len(args.paths) != (1 if args.record_live else 2):
        parser.error('give BUILD TARGET, or --record-live COMMIT TARGET')
    try:
        if args.record_live:
            print(f'Recorded {record_live(args.record_live, args.paths[0])} as the live commit.')
            return
        count = deploy(*args.paths, force=args.force, dry_run=args.dry_run, verified=args.verified, shots=args.shots)
    except Refused as error:
        parser.exit(1, f'Deployment refused: {error}\nUse --force only when you mean to remove that work.\n')
    except ReviewRefused as error:
        parser.exit(1, f'Deployment refused by the review gate. {error}\n')
    except (OSError, ValueError) as error:
        parser.exit(1, f'Deployment failed: {error}\n')
    print('Checks passed; nothing installed (--dry-run).' if args.dry_run
          else f'Installed {count} files; prior chunks retained.')


if __name__ == '__main__':
    main()
