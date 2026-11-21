"""Restart a moved agent in its existing tmux pane."""
import os
import subprocess
import time
from time import monotonic

import audit
import auto_resume
import codex_provider
import delegate
import paths
import procinfo
import terminal_input
import tmux_merge


class Refused(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _pane_format(run, pane, fmt):
    return run([paths.TMUX, 'display-message', '-p', '-t', pane, fmt],
               check=True, capture_output=True, text=True, timeout=5).stdout.removesuffix('\n')


def _tmux_literal(value):
    # tmux treats an unescaped semicolon at the end of any argument as a command separator.
    return value[:-1] + r'\;' if value.endswith(';') else value


def _started(run, pane, commands, sleep):
    deadline = monotonic() + 10
    while (remaining := deadline - monotonic()) > 0:
        try:
            result = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_current_command}'],
                         capture_output=True, text=True, timeout=min(5, remaining))
            if result.returncode == 0 and result.stdout.strip() in commands:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass  # Unconfirmed startup leaves the pane open for the user to check.
        pause = min(.1, deadline - monotonic())
        if pause > 0:
            sleep(pause)
    return False


def restart(body, *, sessions, forget_sessions, run=subprocess.run, sleep=time.sleep):
    pane, thread = body.get('pane'), body.get('thread')
    if not isinstance(pane, str) or not tmux_merge.PANE.fullmatch(pane):
        raise ValueError('Choose a pane.')
    if not isinstance(thread, str) or not terminal_input.UUID.fullmatch(thread):
        raise ValueError('Choose a valid chat id.')
    forget_sessions()
    entry = next((item for item in sessions() if item['id'] == pane), None)
    if entry is None:
        raise Refused(404, 'That tab has closed.')
    agent = entry['agent']
    if agent not in ('claude', 'codex'):
        raise Refused(409, 'Only Claude and Codex tabs can restart.')
    if entry.get('thread') != thread or entry.get('pending_conversation'):
        raise Refused(409, 'That tab now holds another chat. Reload and try again.')
    if tmux_merge.restart_info(run, pane, entry['tmux_session']) is None:
        raise Refused(409, "This tab already uses its session's project.")
    try:
        terminal_input.verify(entry, run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise Refused(409, str(exc)) from exc
    status = terminal_input.live_status(entry, run)
    if status.get('approval') or status.get('attention'):
        raise Refused(409, 'Answer the agent first.')
    if status.get('state') != 'Idle':
        raise Refused(409, 'Wait until the agent finishes its turn.')
    if status.get('tasks'):
        raise Refused(409, 'Stop its background tasks first: a restart ends them.')
    if status.get('crons'):
        raise Refused(409, 'This tab has scheduled jobs. A restart ends them.')
    try:
        terminal_input.empty_prompt(entry, run, read_only=True)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise Refused(409, str(exc)) from exc
    if not os.path.isfile(entry.get('transcript') or ''):
        raise Refused(409, "Tether cannot find this chat's history file.")

    try:
        folder = procinfo.cwd(entry['owner_pid'])
    except OSError:
        folder = ''
    if not os.path.isdir(folder):
        folder = _pane_format(run, pane, '#{pane_current_path}')
    if not folder:
        raise RuntimeError('Tether could not find the tab folder.')
    profile = None
    if agent == 'codex':
        saved = auto_resume.saved_tab(pane) or {}
        profile = saved['profile'] if 'profile' in saved else codex_provider.launch_profile()
    command = terminal_input.resume_command(thread, agent=agent, profile=profile)
    # With no argument, tmux reruns the pane's old command. Match new-window
    # by starting the session's default command or its default login shell.
    default_command = _pane_format(run, pane, '#{default-command}')
    if default_command:
        shell_command = [_tmux_literal(default_command)]
    else:
        shell = _pane_format(run, pane, '#{default-shell}')
        if not shell:
            raise RuntimeError('Tether could not find the default shell.')
        shell_command = [_tmux_literal(shell), '-l']

    audit.record('tab.restart', f'Restarted tab {delegate.source_title(entry, fallback=pane)}', pane=pane)
    # tmux expands formats in -c. A process working directory is literal.
    run([paths.TMUX, 'respawn-pane', '-k', '-t', pane, '-c', _tmux_literal(folder.replace('#', '##')), *shell_command],
        check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'], check=True, capture_output=True, timeout=5)
    started = _started(run, pane, ('claude',) if agent == 'claude' else ('node', 'codex'), sleep)
    reply = {'pane': pane, 'thread': thread, 'agent': agent, 'started': started}
    if not started:
        name = 'Claude' if agent == 'claude' else 'Codex'
        reply['warning'] = f'The tab restarted, but {name} did not start yet. Open the tab to check.'
    return reply
