#!/usr/bin/env python3
"""Check and publish a release from an isolated worktree."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


VERIFIED = 'Verified: tests(server unittest ok) checks(ui tsc, eslint, build ok)'
CI_FALLBACK = 'fix main, or run with --local-checks'
CI_ACTIVE = {'queued', 'in_progress', 'pending', 'requested', 'waiting'}
READ_TIMEOUT = 8


def run(args, cwd, *, capture=False, **kwargs):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=capture, **kwargs)
    if result.returncode:
        detail = (result.stderr or '').strip().splitlines() if capture else []
        raise RuntimeError(f"{' '.join(args)} failed" + (f": {detail[-1]}" if detail else ''))
    return result.stdout.strip() if capture else None


@contextmanager
def phase(timings, name):
    start = time.monotonic()
    try:
        yield
    finally:
        timings.append((name, time.monotonic() - start))


def duration(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    return f'{minutes}m {seconds}s' if minutes else f'{seconds} s'


def origin_repo(root):
    origin = run(['git', 'remote', 'get-url', 'origin'], root, capture=True)
    match = re.search(r'[:/]([^/:]+/[^/]+?)(?:\.git)?/?$', origin)
    if not match:
        raise RuntimeError('cannot read owner/repo from origin URL')
    return match[1]


def github_list(root, endpoint, key, timeout):
    try:
        output = run(['gh', 'api', endpoint, '--paginate', '--slurp'],
                     root, capture=True, timeout=timeout)
        return [item for page in json.loads(output) for item in page[key]]
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('GitHub did not answer in time') from error
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError('GitHub returned an invalid response') from error


def ci_verified(root, sha, wait):
    """Return the stamp for a passing push run. A wait of None is a single read."""
    if not shutil.which('gh'):
        raise RuntimeError(f'gh is missing; {CI_FALLBACK}')
    previous = None
    deadline = None
    timeout = READ_TIMEOUT
    try:
        repo = origin_repo(root)
        endpoint = f'repos/{repo}/actions/workflows/test.yml/runs?head_sha={sha}&event=push&per_page=100'
        while True:
            runs = github_list(root, endpoint, 'workflow_runs', timeout)
            runs = [item for item in runs if item['head_sha'] == sha and item['event'] == 'push']
            passed = next((item for item in runs if item['status'] == 'completed'
                           and item['conclusion'] == 'success'), None)
            if passed:
                print(f"CI: success {passed['html_url']}", flush=True)
                return f"Verified: ci(tests run {passed['id']} success at {sha[:7]})"
            if not runs:
                raise RuntimeError('no tests run for this commit')
            active = next((item for item in runs if item['status'] in CI_ACTIVE), None)
            if not active:
                failed = next((item for item in runs if item['conclusion'] not in ('cancelled', 'skipped')), runs[0])
                conclusion = failed['conclusion'] or failed['status']
                detail = f"{conclusion} {failed['html_url']}"
                if conclusion not in ('cancelled', 'skipped'):
                    jobs_endpoint = f"repos/{repo}/actions/runs/{failed['id']}/jobs?per_page=100"
                    try:
                        jobs = github_list(root, jobs_endpoint, 'jobs', READ_TIMEOUT)
                        names = [job['name'] for job in jobs if job['conclusion'] not in
                                 (None, 'success', 'cancelled', 'skipped', 'neutral')]
                        if names:
                            detail += f"; failed jobs: {', '.join(names)}"
                    except RuntimeError as error:
                        detail += f'; could not read jobs: {error}'
                raise RuntimeError(detail)
            state = f"running ({active['status']}) {active['html_url']}"
            if wait is None:
                raise RuntimeError(state)
            if state != previous:
                print(f'CI: {state}', flush=True)
                previous = state
            if deadline is None:
                deadline = time.monotonic() + wait
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'timed out after {wait} s')
            time.sleep(min(15, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'timed out after {wait} s')
            timeout = min(READ_TIMEOUT, remaining)
    except (OSError, RuntimeError, KeyError, TypeError) as error:
        last = f'; last seen {previous}' if previous else ''
        raise RuntimeError(f'{error}{last}; {CI_FALLBACK}') from error


def resolve_commit(root, ref):
    return run(['git', 'rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}'],
               root, capture=True)


def check_release(root, args, tag):
    """Read the current origin branch and tag without updating local refs."""
    local_tag = run(['git', 'tag', '--list', tag], root, capture=True)
    remote_ref = re.fullmatch(r'(?:refs/remotes/)?origin/([^~^:@{}]+)(.*)', args.ref)
    refs = [f'refs/tags/{tag}']
    if remote_ref:
        branch = remote_ref[1]
        refs.append('HEAD' if branch == 'HEAD' else f'refs/heads/{branch}')
    try:
        listed = run(['git', 'ls-remote', 'origin', *refs], root, capture=True, timeout=READ_TIMEOUT)
        remote = {ref: sha for sha, ref in (line.split('\t') for line in listed.splitlines())}
        tag_state = 'exists on origin' if refs[0] in remote else 'free on origin'
        free = not local_tag and refs[0] not in remote
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        remote, tag_state, free = {}, f'origin check failed ({error})', False
    local_state = 'exists locally' if local_tag else 'free locally'
    print(f'Tag: {tag} {local_state}, {tag_state}')
    try:
        if remote_ref:
            sha = remote.get(refs[1])
            if not sha:
                raise RuntimeError(f'cannot read {args.ref} from origin')
            if remote_ref[2]:
                sha = resolve_commit(root, sha + remote_ref[2])
        else:
            sha = resolve_commit(root, args.ref)
    except RuntimeError as error:
        print(f'Base: {error}')
        print('CI: not checked')
        return 1
    print(f'Base: {sha}')
    if args.local_checks:
        print('CI: not required with --local-checks')
    else:
        try:
            ci_verified(root, sha, None)
        except RuntimeError as error:
            print('CI: ' + ' '.join(str(error).splitlines()))
            return 1
    return 0 if free else 1


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


def bump_version(worktree, version, tag):
    (worktree / 'VERSION').write_text(version + '\n')
    plugin = worktree / 'integrations/claude/tether/.claude-plugin/plugin.json'
    content, count = re.subn(r'("version"\s*:\s*")[^"]*(")',
                             lambda m: m[1] + version + m[2], plugin.read_text())
    if count != 1:
        raise RuntimeError('expected one plugin version field')
    plugin.write_text(content)
    files = ['VERSION', str(plugin.relative_to(worktree))]
    if '-rc' not in version:
        for name in ('README.md', 'site/index.html', 'site/llms.txt'):
            path = worktree / name
            if not path.exists():
                continue
            path.write_text(re.sub(r'raw\.githubusercontent\.com/incmak/tether/v[^/\s]+/',
                                   f'raw.githubusercontent.com/incmak/tether/{tag}/', path.read_text()))
            files.append(name)
    return files


def local_checks(worktree):
    env = dict(os.environ, SHELL='/bin/bash', TETHER_REAL_BROWSER_TEST='1')
    # The Python tests load ui/src TypeScript, so install the UI packages first.
    run(['npm', 'ci'], worktree / 'ui', timeout=1200)
    run(['python3', '-m', 'unittest', 'discover', '-q'], worktree / 'server',
        env=env, timeout=1200)
    run(['/bin/bash', '-c', 'npx tsc -b && npx eslint . && npm run build'],
        worktree / 'ui', timeout=1200)


def wait_for_release(root, tag):
    repo = origin_repo(root)
    deadline = time.monotonic() + 600
    message = f'release wait timed out; run: gh run list -R {repo} -w release'
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ['gh', 'release', 'view', tag, '-R', repo, '--json', 'url', '--jq', '.url'],
                text=True, capture_output=True, timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(message) from error
        if result.returncode == 0:
            print(result.stdout.strip(), flush=True)
            return
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', help='X.Y.Z or X.Y.Z-rc.N')
    parser.add_argument('--ref', default='origin/main', help='release base (default: origin/main)')
    parser.add_argument('--no-push', action='store_true', help='commit and tag locally only')
    parser.add_argument('--keep', action='store_true', help='keep the release worktree')
    parser.add_argument('--local-checks', action='store_true', help='run the full checks locally instead of using CI')
    parser.add_argument('--ci-wait', type=int, default=600, metavar='SECONDS',
                        help='maximum wait for a running tests workflow (default: 600)')
    parser.add_argument('--check', action='store_true', help='read-only preflight with no wait for CI')
    args = parser.parse_args()
    if args.ci_wait < 0:
        parser.error('--ci-wait must be zero or greater')
    started = time.monotonic()
    timings = []
    worktree = None
    pending = pushing = None
    try:
        with phase(timings, 'Preflight'):
            valid = re.fullmatch(r'\d+\.\d+\.\d+(-rc\.?\d+)?', args.version)
            if args.check:
                if not valid:
                    print('Version: must match X.Y.Z or X.Y.Z-rc.N\nTag: not checked\nBase: not checked\nCI: not checked')
                    return 1
                print(f'Version: {args.version} valid')
            elif not valid:
                raise RuntimeError('version must match X.Y.Z or X.Y.Z-rc.N')
            tag = f'v{args.version}'
            root = Path(run(['git', 'rev-parse', '--show-toplevel'],
                            Path(__file__).resolve().parent, capture=True))
            if args.check:
                return check_release(root, args, tag)
            if run(['git', 'tag', '--list', tag], root, capture=True):
                raise RuntimeError(f'tag {tag} already exists locally')
            if run(['git', 'ls-remote', '--tags', 'origin', f'refs/tags/{tag}'], root, capture=True):
                raise RuntimeError(f'tag {tag} already exists on origin')
            run(['git', 'fetch', 'origin', '--tags'], root)
            if run(['git', 'tag', '--list', tag], root, capture=True):
                raise RuntimeError(f'tag {tag} already exists locally')
            sha = resolve_commit(root, args.ref)
        verified = VERIFIED
        if not args.local_checks:
            with phase(timings, 'CI check'):
                verified = ci_verified(root, sha, args.ci_wait)
        with phase(timings, 'Prepare worktree'):
            destination = Path.home() / '.cache' / 'tether-release' / args.version
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                run(['git', 'worktree', 'remove', '--force', str(destination)], root)
            run(['git', 'worktree', 'add', '--detach', str(destination), sha], root)
            worktree = destination
            files = bump_version(worktree, args.version, tag)
        if args.local_checks:
            with phase(timings, 'Local checks'):
                local_checks(worktree)
        with phase(timings, 'Commit and tag'):
            run(['git', 'add', '--', *files], worktree)
            run(['git', 'commit', '-m', f'Release {args.version}\n\n{verified}'], worktree)
            commit = run(['git', 'rev-parse', 'HEAD'], worktree, capture=True)
            # This run's tag is the one at this run's commit. Until the push starts, a failure removes it.
            pending = None if args.no_push else commit
            run(['git', 'tag', '-a', tag, '-m', f'Release {args.version}'], worktree)
        if not args.no_push:
            with phase(timings, 'Push'):
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
                with phase(timings, 'GitHub build'):
                    wait_for_release(root, tag)
        if not args.keep:
            with phase(timings, 'Cleanup'):
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
    finally:
        timings.append(('total', time.monotonic() - started))
        print(', '.join(f'{name} {duration(seconds)}' for name, seconds in timings), flush=True)


if __name__ == '__main__':
    sys.exit(main())
