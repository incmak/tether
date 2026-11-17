#!/usr/bin/env python3
"""Verification stamps (B189): every deploy line and every agent commit says how the work was verified.

A stamp is one line:
  Verified: tests(server 412 ok, ui 91/93) screenshots(24) review(codex APPROVED)
  Verified: none (docs only)
Each item is kind(detail). "none" is allowed, so unverified work stays visible and can be listed.

Commands:
  log --verified STAMP [--commit C] TEXT   add a DEPLOYED.md line (deploy-ui.py adds UI deploys itself)
  unverified [--days N]                    list deploys and commits with "Verified: none" or no stamp
  check STAMP                              exit 0 when STAMP is valid
"""
import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import subprocess
import sys

ITEM = r'[a-z][a-z-]*\([^()\n]+\)'
STAMP = re.compile(rf'none(?: \([^()\n]+\))?|{ITEM}(?: +{ITEM})*')
FOUND = re.compile(r'\bVerified:[ \t]*([^\n]*)')
DAY = re.compile(r'^\s*[-*]?\s*(\d{4})-(\d{2})-(\d{2})')
HERE = Path(__file__).resolve().parent


def valid(stamp):
    """The stamp text after "Verified:" with spaces tidied, or raise ValueError."""
    text = re.sub(r'\s+', ' ', str(stamp or '')).strip()
    if text.lower().startswith('verified:'):
        text = text[len('verified:'):].strip()
    if not STAMP.fullmatch(text):
        raise ValueError('Give a verification stamp: kind(detail) items such as '
                         '"tests(server 412 ok) screenshots(24) review(codex APPROVED)", or "none (reason)".')
    return text


def status(text):
    """'verified', 'none' or 'missing' for a DEPLOYED.md line or a commit message. A malformed stamp counts as missing."""
    found = FOUND.findall(text or '')
    if not found:
        return 'missing'
    try:
        stamp = valid(found[-1])
    except ValueError:
        return 'missing'
    return 'none' if stamp.startswith('none') else 'verified'


def git(folder, *args):
    try:
        result = subprocess.run(['git', '-C', str(folder), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def main_checkout(folder):
    """The main checkout of the repository that holds `folder`, also from a linked worktree. None outside git."""
    common = git(folder, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return Path(common.strip()).parent if common else None


def log_path(folder=HERE):
    """DEPLOYED.md: TETHER_DEPLOYED, else the top of the main checkout (where server/factory.py reads it)."""
    if os.environ.get('TETHER_DEPLOYED'):
        return Path(os.environ['TETHER_DEPLOYED'])
    root = main_checkout(folder)
    if root is None:
        raise ValueError(f'{folder} is not in a git checkout. Set TETHER_DEPLOYED to the deploy log.')
    return root / 'DEPLOYED.md'


def line(text, stamp, commit=None, now=None):
    now = now or datetime.now().astimezone()
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    if not text:
        raise ValueError('Say what was deployed.')
    if FOUND.search(text):
        raise ValueError('Give the stamp with --verified, not in the text.')
    head = now.strftime('%Y-%m-%d %H:%M ') + (now.strftime('%Z') or 'UTC')
    return f'- {head} · ' + (f'{commit} ' if commit else '') + f'{text} · Verified: {valid(stamp)}'


def append(path, text):
    """Add one line. O_APPEND keeps parallel writers from overwriting each other."""
    path = Path(path)
    data = path.read_bytes() if path.exists() else b''
    prefix = b'' if not data or data.endswith(b'\n') else b'\n'
    with open(path, 'ab') as stream:
        stream.write(prefix + text.encode() + b'\n')


def unverified_deploys(path, since):
    """[(line, 'none' | 'missing')] for dated DEPLOYED.md lines on or after `since` (a date)."""
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return []
    found = []
    for text in lines:
        day = DAY.match(text)
        if not day:
            continue
        try:
            when = datetime(*map(int, day.groups())).date()
        except ValueError:
            continue
        kind = status(text)
        if when >= since and kind != 'verified':
            found.append((text.strip(), kind))
    return found


def unverified_commits(folder, since, revision='HEAD'):
    """[(short hash, subject, 'none' | 'missing')] for non-merge commits on `revision` since `since`."""
    out = git(folder, 'log', '--no-merges', f'--since={since.isoformat()}', '--format=%h%x1f%s%x1f%B%x1e', revision)
    found = []
    for record in (out or '').split('\x1e'):
        parts = record.strip('\n').split('\x1f')
        if len(parts) != 3:
            continue
        kind = status(parts[2])
        if kind != 'verified':
            found.append((parts[0], parts[1], kind))
    return found


def unverified_commit_range(folder, start, end):
    """Commits in start..end without a stamp or with "none". [] when git cannot read the range."""
    out = git(folder, 'log', '--no-merges', '--format=%h%x1f%s%x1f%B%x1e', f'{start}..{end}')
    found = []
    for record in (out or '').split('\x1e'):
        parts = record.strip('\n').split('\x1f')
        if len(parts) == 3 and status(parts[2]) != 'verified':
            found.append((parts[0], parts[1], status(parts[2])))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    log = commands.add_parser('log', help='add a DEPLOYED.md line with its stamp')
    log.add_argument('--verified', required=True, help='kind(detail) items, or "none (reason)"')
    log.add_argument('--commit', help='the deployed commit (short hash)')
    log.add_argument('text')
    listing = commands.add_parser('unverified', help='list deploys and commits marked none or with no stamp')
    listing.add_argument('--days', type=int, default=7)
    listing.add_argument('--repo', default=str(HERE), help='a folder in the repository to read commits from')
    check = commands.add_parser('check', help='exit 0 when the stamp is valid')
    check.add_argument('stamp')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            print(f'Verified: {valid(args.stamp)}')
        elif args.command == 'log':
            commit = args.commit
            if commit is not None and not re.fullmatch(r'[0-9a-f]{7,40}', commit):
                raise ValueError('Give --commit as a hex commit hash.')
            text = line(args.text, args.verified, commit and commit[:7])
            path = log_path(HERE)
            append(path, text)
            print(f'Added to {path}:\n{text}')
        else:
            since = (datetime.now() - timedelta(days=args.days)).date()
            deploys = unverified_deploys(log_path(Path(args.repo)), since)
            commits = unverified_commits(args.repo, since)
            print(f'Deploys since {since} without verification: {len(deploys)}')
            for text, kind in deploys:
                print(f'  [{kind}] {text[:200]}')
            print(f'Commits since {since} without verification: {len(commits)}')
            for short, subject, kind in commits:
                print(f'  [{kind}] {short} {subject[:120]}')
    except ValueError as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    sys.exit(main())
