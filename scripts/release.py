#!/usr/bin/env python3
"""Check and publish a release from an isolated worktree."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


def run(args, cwd, *, capture=False, **kwargs):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=capture, **kwargs)
    if result.returncode:
        detail = (result.stderr or '').strip().splitlines() if capture else []
        raise RuntimeError(f"{' '.join(args)} failed" + (f": {detail[-1]}" if detail else ''))
    return result.stdout.strip() if capture else None


def drop_tag(root, tag, commit):
    """Delete the local tag only when it is the one at `commit`. Returns text for the error message."""
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, text=True, capture_output=True)
    ref = f'refs/tags/{tag}'
    held = git('rev-parse', '--verify', '-q', ref).stdout.strip()  # read once; check and delete this same object
    if not held or git('rev-parse', '--verify', '-q', f'{held}^{{commit}}').stdout.strip() != commit:
        return ''  # no tag, or a tag that this run did not make
    # update-ref deletes only while the ref still holds this tag object.
    if git('update-ref', '-d', ref, held).returncode:
        return f'; the local tag {tag} could not be removed; run: git tag -d {tag} before the next run'
    return ''


def origin_state(root, tag, commit):
    """After a push with no clear result: (True, '') when origin has this run's tag, (False, why) when it does not,
    (None, what to do) when origin does not answer."""
    try:
        listed = run(['git', 'ls-remote', 'origin', f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}'], root, capture=True, timeout=60)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None, (f'; origin did not answer, so the result of the push is unknown and the local tag stays. '
                      f'Run: git ls-remote origin refs/tags/{tag}. If it prints nothing, run: git tag -d {tag}, '
                      'then run the release again')
    if not listed:
        return False, '; origin did not change; if main moved, run again'
    if commit in [line.split()[0] for line in listed.splitlines()]:
        return True, ''
    return False, f'; origin has a different {tag}; choose another version'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', help='X.Y.Z or X.Y.Z-rc.N')
    parser.add_argument('--ref', default='origin/main', help='release base (default: origin/main)')
    parser.add_argument('--no-push', action='store_true', help='commit and tag locally only')
    parser.add_argument('--keep', action='store_true', help='keep the release worktree')
    args = parser.parse_args()
    worktree = None
    pending = pushing = None
    try:
        if not re.fullmatch(r'\d+\.\d+\.\d+(-rc\.?\d+)?', args.version):
            raise RuntimeError('version must match X.Y.Z or X.Y.Z-rc.N')
        tag = f'v{args.version}'
        root = Path(run(['git', 'rev-parse', '--show-toplevel'],
                        Path(__file__).resolve().parent, capture=True))
        if run(['git', 'tag', '--list', tag], root, capture=True):
            raise RuntimeError(f'tag {tag} already exists locally')
        if run(['git', 'ls-remote', '--tags', 'origin', f'refs/tags/{tag}'], root, capture=True):
            raise RuntimeError(f'tag {tag} already exists on origin')
        run(['git', 'fetch', 'origin', '--tags'], root)
        if run(['git', 'tag', '--list', tag], root, capture=True):
            raise RuntimeError(f'tag {tag} already exists locally')
        destination = Path.home() / '.cache' / 'tether-release' / args.version
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            run(['git', 'worktree', 'remove', '--force', str(destination)], root)
        run(['git', 'worktree', 'add', '--detach', str(destination), args.ref], root)
        worktree = destination
        (worktree / 'VERSION').write_text(args.version + '\n')
        plugin = worktree / 'integrations/claude/tether/.claude-plugin/plugin.json'
        content, count = re.subn(r'("version"\s*:\s*")[^"]*(")',
                                 lambda m: m[1] + args.version + m[2], plugin.read_text())
        if count != 1:
            raise RuntimeError('expected one plugin version field')
        plugin.write_text(content)
        files = ['VERSION', str(plugin.relative_to(worktree))]
        if '-rc' not in args.version:
            for name in ('README.md', 'site/index.html', 'site/llms.txt'):
                path = worktree / name
                if not path.exists():
                    continue
                path.write_text(re.sub(r'raw\.githubusercontent\.com/incmak/tether/v[^/\s]+/',
                                       f'raw.githubusercontent.com/incmak/tether/{tag}/', path.read_text()))
                files.append(name)
        env = dict(os.environ, SHELL='/bin/bash', TETHER_REAL_BROWSER_TEST='1')
        # The Python tests load ui/src TypeScript, so install the UI packages first.
        run(['npm', 'ci'], worktree / 'ui', timeout=1200)
        run(['python3', '-m', 'unittest', 'discover', '-q'], worktree / 'server',
            env=env, timeout=1200)
        run(['/bin/bash', '-c', 'npx tsc -b && npx eslint . && npm run build'],
            worktree / 'ui', timeout=1200)
        run(['git', 'add', '--', *files], worktree)
        run(['git', 'commit', '-m', f'Release {args.version}'], worktree)
        commit = run(['git', 'rev-parse', 'HEAD'], worktree, capture=True)
        # This run's tag is the one at this run's commit. Until the push starts, a failure removes it.
        pending = None if args.no_push else commit
        run(['git', 'tag', '-a', tag, '-m', f'Release {args.version}'], worktree)
        if not args.no_push:
            pending, pushing = None, commit  # from here only origin can say whether the tag is published
            try:
                # One atomic push: origin takes the commit and the tag, or neither.
                run(['git', 'push', '--atomic', 'origin', 'HEAD:main', tag], worktree)
            except RuntimeError as error:
                # A push can reach origin and still report a failure here, so read origin before the tag goes.
                published, note = origin_state(root, tag, commit)
                if not published:
                    pending, pushing = (commit if published is False else None), None
                    raise RuntimeError(f'{error}{note}') from error
                print('The push reported a failure, but origin has the release commit and the tag.', flush=True)
            pushing = None
            if shutil.which('gh'):
                origin = run(['git', 'remote', 'get-url', 'origin'], root, capture=True)
                match = re.search(r'[:/]([^/:]+/[^/]+?)(?:\.git)?/?$', origin)
                if not match:
                    raise RuntimeError('cannot read owner/repo from origin URL')
                repo = match[1]
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    try:
                        result = subprocess.run(
                            ['gh', 'release', 'view', tag, '-R', repo, '--json', 'url', '--jq', '.url'],
                            text=True, capture_output=True, timeout=max(1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as error:
                        raise RuntimeError(f'release wait timed out; run: gh run list -R {repo} -w release') from error
                    if result.returncode == 0:
                        print(result.stdout.strip(), flush=True)
                        break
                    time.sleep(min(15, max(0, deadline - time.monotonic())))
                else:
                    raise RuntimeError(f'release wait timed out; run: gh run list -R {repo} -w release')
        if not args.keep:
            run(['git', 'worktree', 'remove', '--force', str(worktree)], root)
            worktree = None
        if worktree:
            print(f'Worktree kept: {worktree}')
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        message = str(error).replace('\n', ' ') or 'interrupted'
        if pushing:  # interrupted during the push
            published, note = origin_state(root, tag, pushing)
            if published:
                message += f'; origin has the release commit and {tag}: the release is published, only the wait was cut short'
            else:
                pending, message = (pushing if published is False else None), message + note
        if pending:  # else the next run stops at 'tag already exists locally'
            message += drop_tag(root, tag, pending)
        location = f'; worktree kept: {worktree}' if worktree else ''
        print(f'Release failed: {message}{location}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
