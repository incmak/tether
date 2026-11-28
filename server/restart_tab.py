"""Restart a moved agent in its existing tmux pane."""
import os
import subprocess
import time
from time import monotonic

import audit
import auto_resume
import codex_provider
import codex_tasks
import delegate
import paths
import procinfo
import projects
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


def _started(run, pane, commands, sleep, *, timeout=10):
    deadline = monotonic() + timeout
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
    project, session = body.get('project'), body.get('session')
    # An empty project is the offer for a session that gets its project on the agent's next start.
    if (not isinstance(project, str) or (project and not projects.ID.fullmatch(project))
            or not isinstance(session, str) or not 0 < len(session) <= 200):
        raise ValueError('Choose a valid tab.')
    forget_sessions()
    entry = next((item for item in sessions() if item['id'] == pane), None)
    if entry is None:
        raise Refused(404, 'That tab has closed.')
    agent = entry['agent']
    if agent not in ('claude', 'codex'):
        raise Refused(409, 'Only Claude and Codex tabs can restart.')
    if entry.get('thread') != thread or entry.get('pending_conversation'):
        raise Refused(409, 'That tab now holds another chat. Reload and try again.')
    if entry.get('friday_draft'):
        raise Refused(409, 'Ask Friday to make the prompt again.')
    info = tmux_merge.restart_info(run, pane, entry['tmux_session'])
    if info is None:
        raise Refused(409, "This tab already uses its session's project.")
    if info['project'] != project or info['session'] != session:
        raise Refused(409, 'This tab moved again. Reload and try again.')
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
    if agent == 'codex':
        try:
            terminals = codex_tasks.background_terminals(entry)
        except codex_tasks.Unchecked as exc:
            version = terminal_input._agent_version(entry) or 'unknown'
            raise Refused(409, f'Tether could not check this tab for Codex {version} background terminals, '
                               'so it did not restart it.') from exc
        if terminals:
            raise Refused(409, 'Stop its background terminals first: a restart ends them. '
                               'Type /stop in the tab, or /ps to see them.')
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
    shell = _pane_format(run, pane, '#{default-shell}')
    if not shell:
        raise RuntimeError('Tether could not find the default shell.')

    audit.record('tab.restart', f'Restarted tab {delegate.source_title(entry, fallback=pane)}', pane=pane)
    # tmux expands formats in -c. A process working directory is literal.
    run([paths.TMUX, 'respawn-pane', '-k', '-t', pane, '-c', _tmux_literal(folder.replace('#', '##')),
         _tmux_literal(shell), '-l'],
        check=True, capture_output=True, timeout=5)
    reply = {'pane': pane, 'thread': thread, 'agent': agent, 'started': False}
    if not _started(run, pane, (os.path.basename(shell),), sleep, timeout=5):
        reply['warning'] = 'The tab restarted, but its shell did not start. Open the tab to check.'
        return reply
    run([paths.TMUX, 'send-keys', '-t', pane, '-l', command], check=True, capture_output=True, timeout=5)
    run([paths.TMUX, 'send-keys', '-t', pane, 'Enter'], check=True, capture_output=True, timeout=5)
    reply['started'] = _started(run, pane, ('claude',) if agent == 'claude' else ('node', 'codex'), sleep)
    if not reply['started']:
        name = 'Claude' if agent == 'claude' else 'Codex'
        reply['warning'] = f'The tab restarted, but {name} did not start yet. Open the tab to check.'
    return reply
