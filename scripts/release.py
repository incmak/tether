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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', help='X.Y.Z or X.Y.Z-rc.N')
    parser.add_argument('--ref', default='origin/main', help='release base (default: origin/main)')
    parser.add_argument('--no-push', action='store_true', help='commit and tag locally only')
    parser.add_argument('--keep', action='store_true', help='keep the release worktree')
    args = parser.parse_args()
    worktree = None
    made_tag = False
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
        made_tag = not args.no_push  # this run's tag, not on origin yet: a failure removes it
        run(['git', 'tag', '-a', tag, '-m', f'Release {args.version}'], worktree)
        if not args.no_push:
            try:
                # One atomic push: origin takes the commit and the tag, or neither.
                run(['git', 'push', '--atomic', 'origin', 'HEAD:main', tag], worktree)
            except RuntimeError as error:
                raise RuntimeError(f'{error}; origin did not change; if main moved, run again') from error
            made_tag = False
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
        if made_tag:  # else the next run stops at 'tag already exists locally'
            subprocess.run(['git', 'tag', '-d', tag], cwd=root, capture_output=True)
        location = f'; worktree kept: {worktree}' if worktree else ''
        print(f'Release failed: {message}{location}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
