#!/usr/bin/env python3
"""Run from the worktree with TETHER_FRIDAY_NOTES set to a new empty directory.

This opens a real Claude draft chat, edits it, and closes it. Pass --chat NAME
to use a live Claude target in a trusted folder. The target receives no input.
"""
import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
import time


def wait_for_draft(read_draft, step):
    deadline = time.monotonic() + 7 * 60
    last = None
    while time.monotonic() < deadline:
        draft = read_draft()
        state = draft['state']
        if state != last:
            print(f'STATE {step} {state}', flush=True)
            last = state
        if state == 'ready':
            return draft
        if state == 'failed':
            raise RuntimeError(draft['error'] or 'The draft failed.')
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError('The draft did not finish in 7 minutes.')


def verify_gone(pane, tmux, run):
    # Live tmux 3.x answered `display-message -t <gone pane>` with exit 0 and no output, so list every pane instead.
    shown = run([tmux, 'list-panes', '-a', '-F', '#{pane_id}'], capture_output=True, text=True, timeout=5)
    stderr = shown.stderr.strip()
    no_server = shown.returncode != 0 and (
        stderr.startswith('no server running on ')
        or (stderr.startswith('error connecting to ') and stderr.endswith('(No such file or directory)')))
    if not no_server and (shown.returncode != 0 or pane in shown.stdout.split()):
        raise RuntimeError(f'Could not verify that {pane} is gone. '
                           f'returncode={shown.returncode}, stderr={shown.stderr!r}')
    print(f'PASS verify ({pane} is gone)', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chat', default='', metavar='NAME', help='a live Claude chat in a trusted folder')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    override = os.environ.get('TETHER_FRIDAY_NOTES')
    folder = Path(override).resolve() if override else None
    if Path.cwd() != root or folder is None or not folder.is_dir() or any(folder.iterdir()):
        print('FAIL setup (run from the worktree with TETHER_FRIDAY_NOTES set to a new empty directory)')
        return 1
    os.environ['TETHER_FRIDAY_NOTES'] = str(folder)
    sys.path.insert(0, str(root / 'server'))

    # server.py loads its assets and constructs module-level service objects on import.
    # Its __main__ block starts the HTTP server and background services.
    import friday_drafts
    import friday_notes
    import paths
    from server import friday_draft_runtime

    if paths.friday_notes() != folder:
        print('FAIL setup (the server did not select the temporary notes directory)')
        return 1
    runtime = friday_draft_runtime()
    tmux_run = runtime.run
    launch_folder = None

    def run(command, **kwargs):
        nonlocal launch_folder
        tmux_args = command[command.index(paths.TMUX) + 1:]
        if tmux_args[0] in ('new-session', 'new-window'):
            launch_folder = tmux_args[tmux_args.index('-c') + 1]
            print(f'Launch folder: {launch_folder}', flush=True)
        return tmux_run(command, **kwargs)

    runtime = replace(runtime, run=run)
    print('PASS setup', flush=True)
    topic = None
    failed = False
    trust_refusal = False
    step = 'hold'

    def read_draft():
        return next(item['draft'] for item in friday_notes.listing()['topics'] if item['id'] == topic)

    def show_revision(draft, label):
        revision = draft['revisions'][-1]
        print(f"PASS {label} rev {revision['rev']} summary={revision['summary']!r} text={revision['text'][:300]!r}",
              flush=True)
        return revision['rev']

    try:
        topic = friday_notes.hold('Draft live test', ['Write a prompt to check startup.',
                                                       'Include one acceptance check.'], chat=args.chat)['id']
        print(f'PASS hold (two notes, target chat={args.chat!r})', flush=True)
        step = 'start'
        friday_drafts.start(topic, runtime=runtime)
        rev = show_revision(wait_for_draft(read_draft, step), step)
        step = 'edit'
        friday_drafts.edit(topic, rev, 'Make it shorter.', runtime=runtime)
        edited = wait_for_draft(read_draft, step)
        if edited['revisions'][-1]['rev'] != rev + 1:
            raise RuntimeError('The edit did not add a revision.')
        show_revision(edited, step)
    except Exception as exc:
        trust_refusal = (not args.chat and step == 'start' and launch_folder is not None
                         and str(exc) == friday_drafts._trust_error(launch_folder))
        if not trust_refusal:
            print(f'FAIL {step} ({exc})', flush=True)
            failed = True
    finally:
        if topic is not None:
            step = 'close'
            try:
                draft = read_draft()
                if draft is None or draft['chat'] is None:
                    raise RuntimeError('No draft pane was recorded.')
                chat = draft['chat']
                if trust_refusal and not chat.get('gone'):
                    print('FAIL trust-refusal (startup did not close the draft pane)', flush=True)
                    failed = True
                friday_drafts.close(topic, chat['pane'], chat['thread'], runtime=runtime)
                print('PASS close', flush=True)
                step = 'verify'
                verify_gone(chat['pane'], paths.TMUX, runtime.run)
                if trust_refusal and not failed:
                    print('PASS trust-refusal', flush=True)
            except Exception as exc:
                print(f'FAIL {step} ({exc})', flush=True)
                failed = True
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
