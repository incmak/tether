#!/usr/bin/env python3
"""Check Stop against the installed Claude Code in a private tmux session."""
import argparse
from contextlib import contextmanager
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from model_picker import visible
import paths
import procinfo
import subagent_stop as stop
import subagents
import task_process
import terminal_input

FIXTURES = ROOT / 'server' / 'fixtures'
VERSION = r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?'
KINDS = ('agent', 'shell', 'monitor')
AGENT_NAME = 'Stop canary agent'
MONITOR_NAME = 'Stop canary monitor'
SHELL_COMMAND = 'sleep 600'
MONITOR_COMMAND = 'sleep 599'
AGENT_PROMPT = 'Run Bash `sleep 601` with run_in_background true and timeout 600000. Wait for that shell to finish before you reply. Then reply done.'
PROMPT = (
    f'Start exactly three background tasks for a local Stop test. Use Agent with description '
    f'"{AGENT_NAME}", subagent_type general-purpose, model haiku, and run_in_background true. '
    f'Its prompt must be: {json.dumps(AGENT_PROMPT)}. '
    'Use that quoted text verbatim as the Agent prompt. The subagent must not start any other tools. '
    f'Use Bash with command "{SHELL_COMMAND}", run_in_background true and timeout 600000. '
    f'Use Monitor with description "{MONITOR_NAME}", command "{MONITOR_COMMAND}" and timeout_ms 600000. '
    'Do not substitute tools or commands. After starting the three tasks, reply without waiting.'
)
TRIGGER = 'Run with --if-changed STATE_FILE from the Claude Code post-update step.'
WORK_SECONDS = 290  # Leave ten seconds within the five-minute budget for cleanup and reporting.
TIMEOUT_MESSAGE = 'The canary reached its five-minute limit.'


class CanaryError(Exception):
    pass


def timed_out(_signum, _frame):
    raise CanaryError(TIMEOUT_MESSAGE)


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def installed_version(run=subprocess.run):
    output = run(['claude', '--version'], capture_output=True, text=True, check=True, timeout=5).stdout
    match = re.fullmatch(rf'({VERSION})\s+\(Claude Code\)', output.strip())
    if not match:
        raise CanaryError('Could not identify the installed Claude Code version.')
    return match[1]


def prepare_config(config, workspace):
    """Reuse the login in a throwaway config, without loading the user's hooks or plugins."""
    source = Path(os.environ['CLAUDE_CONFIG_DIR']) if os.environ.get('CLAUDE_CONFIG_DIR') else paths.CLAUDE
    account_file = (source if os.environ.get('CLAUDE_CONFIG_DIR') else paths.HOME) / '.claude.json'
    account = {}
    if account_file.is_file():
        account = json.loads(account_file.read_text()).get('oauthAccount') or {}
    config.mkdir(mode=0o700)
    (config / '.claude.json').write_text(json.dumps({
        'oauthAccount': account, 'hasCompletedOnboarding': True, 'autoUpdates': False, 'theme': 'dark',
        'projects': {str(workspace): {'hasTrustDialogAccepted': True}},
    }))
    credentials = source / '.credentials.json'
    if not credentials.is_file():
        return None
    try:
        original = credentials.read_bytes()
    except FileNotFoundError:
        return None
    write_atomic(config / credentials.name, original)
    return credentials, original


def finish_credentials(copy, credentials):
    """Keep a refresh only if the login file still matches the original copy."""
    try:
        if credentials is None:
            return None
        source, original = credentials
        try:
            current = copy.read_bytes()
        except FileNotFoundError:
            return None
        if current != original and not write_atomic(source, current, expected=original):
            return 'The canary and the Claude login credentials changed. Kept the login file as it is.'
    finally:
        copy.unlink(missing_ok=True)


def process_alive(process):
    try:
        fields = procinfo.stat_fields(process.pid)
        return fields[19] == process.ticks and fields[0] not in ('Z', 'X')
    except (FileNotFoundError, ProcessLookupError):
        return False


class TmuxClaude:
    """Live tmux and Claude adapter. All pane commands use the same private socket."""

    def __init__(self, root, run=subprocess.run):
        self.root = root
        self.thread = str(uuid.uuid4())
        self.socket = f'tether-canary-{os.getpid()}'
        self.config = root / 'claude'
        self.tasks_root = root / f'claude-{os.getuid()}'
        self.process_run = run
        self.env = dict(os.environ, TMUX_TMPDIR=str(root), CLAUDE_CONFIG_DIR=str(self.config),
                        CLAUDE_CODE_TMPDIR=str(root), TMPDIR=str(root), DISABLE_AUTOUPDATER='1')
        for name in ('TMUX', 'TMUX_PANE', 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT'):
            self.env.pop(name, None)
        self.entry = None
        self.cleanup_error = None
        self.credentials_warning = None

    def run(self, args, **kwargs):
        if args[0] != paths.TMUX:
            raise CanaryError('The canary runner accepts only tmux commands.')
        return self.process_run([args[0], '-L', self.socket, '-f', os.devnull, *args[1:]], env=self.env, **kwargs)

    @contextmanager
    def session(self):
        workspace = self.root / 'work'
        workspace.mkdir()
        credentials = None
        saved_paths = subagents.TASKS_TMP, subagents.PROJECTS, stop.SESSIONS, task_process.LEDGER
        subagents.TASKS_TMP, subagents.PROJECTS = self.tasks_root, self.config / 'projects'
        stop.SESSIONS, task_process.LEDGER = self.config / 'sessions', self.root / 'task-stops.json'
        try:
            credentials = prepare_config(self.config, workspace)
            started = self.run([
                paths.TMUX, 'new-session', '-d', '-s', 'canary', '-x', '120', '-y', '40',
                '-c', str(workspace), '-P', '-F', '#{pane_id}\t#{pane_pid}',
                'claude', '--model', 'haiku', '--session-id', self.thread, '--strict-mcp-config',
                '--setting-sources', '', '--settings', '{"disableAllHooks":true}',
                '--permission-mode', 'dontAsk', '--tools', 'Agent,Bash,Monitor', '--allowedTools',
                f'Agent(general-purpose),Bash({SHELL_COMMAND}),Bash({MONITOR_COMMAND}),Monitor', '--', PROMPT,
            ], capture_output=True, text=True, check=True, timeout=5)
            pane, raw_pid = started.stdout.strip().split('\t')
            pid = int(raw_pid)
            self.entry = {'id': pane, 'owner_pid': pid, 'process_start': procinfo.start_ticks(pid),
                          'agent': 'claude', 'thread': self.thread, 'writable': True,
                          'registry': stop.SESSIONS / f'{pid}.json'}
            yield self.entry
        finally:
            # Let cleanup finish even if a timeout or Ctrl-C arrives during shutdown.
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGALRM})
            try:
                self.cleanup()
            except (CanaryError, OSError, subprocess.SubprocessError) as exc:
                self.cleanup_error = str(exc)
            finally:
                # Remove the copy before workspace cleanup, even if shutdown left the workspace in place.
                try:
                    self.credentials_warning = finish_credentials(self.config / '.credentials.json', credentials)
                except OSError as exc:
                    detail = f'Could not reconcile or remove the canary credentials. {exc.strerror}'
                    self.cleanup_error = f'{self.cleanup_error} {detail}' if self.cleanup_error else detail
                finally:
                    subagents.TASKS_TMP, subagents.PROJECTS, stop.SESSIONS, task_process.LEDGER = saved_paths
                    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)

    def cleanup(self, sleep=time.sleep, clock=time.monotonic):
        processes = []
        try:
            processes = self.pane_processes()
            # These processes use a disposable config. Stop writes before closing tmux.
            for process in processes:
                if process_alive(process):
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        continue
        finally:
            try:
                result = self.run([paths.TMUX, 'kill-server'], capture_output=True, text=True, timeout=5)
                if result.returncode and not ('no server running' in result.stderr or
                                              'No such file or directory' in result.stderr):
                    raise CanaryError('Could not shut down the private tmux server.')
            finally:
                deadline = clock() + 3
                while any(process_alive(process) for process in processes):
                    remaining = deadline - clock()
                    if remaining <= 0:
                        raise CanaryError('The canary pane still has running processes after shutdown.')
                    sleep(min(0.05, remaining))

    def pane_processes(self):
        """Pin Claude and its descendants before shutdown can reparent them."""
        if self.entry is None:
            return []
        pending = [task_process.Process(self.entry['owner_pid'], self.entry['process_start'])]
        processes = []
        while pending:
            parent = pending.pop()
            if not process_alive(parent):
                continue
            processes.append(parent)
            for pid in task_process.children(parent.pid):
                try:
                    child = task_process.Process(pid, procinfo.start_ticks(pid))
                    if procinfo.parent(pid) == parent.pid and process_alive(parent):
                        pending.append(child)
                except (FileNotFoundError, ProcessLookupError):
                    continue
        return processes

    def listing(self, entry):
        if not entry.get('transcript'):
            transcript = next(subagents.PROJECTS.glob(f'*/{self.thread}.jsonl'), None)
            if (transcript is None or not entry['registry'].is_file()
                    or not any(self.tasks_root.glob(f'*/{self.thread}/tasks'))):
                self.run([paths.TMUX, 'has-session', '-t', '=canary'], check=True, capture_output=True, timeout=5)
                return {'agents': [], 'tasks': []}
            entry['transcript'] = str(transcript)
        return subagents.listing(entry)


def expected_tasks(listed, *, waiting_agent=False):
    matches = {
        'agent': [item for item in listed['agents'] if item['background'] and item['description'] == AGENT_NAME],
        'shell': [item for item in listed['tasks'] if item['kind'] == 'shell' and item['command'] == SHELL_COMMAND],
        'monitor': [item for item in listed['tasks'] if item['kind'] == 'monitor' and
                    item['description'] == MONITOR_NAME and item['command'] == MONITOR_COMMAND],
    }
    if all(len(matches[kind]) == 1 and matches[kind][0]['state'] == 'running' for kind in KINDS):
        if not waiting_agent or 'waiting' in matches['agent'][0]:
            return {kind: matches[kind][0] for kind in KINDS}
    return None


def redact(text, root):
    private_paths = sorted({str(root), str(Path.home()), str(paths.HOME), str(paths.CLAUDE)},
                           key=len, reverse=True)
    hostname = re.escape(socket.gethostname())
    absolute_paths = re.compile(r'(?<![\w:/~])(?:/[\w.+@%~=-]+){2,}/?')

    def hide_private_text(part):
        for path in private_paths:
            if path != '/':
                part = part.replace(path, '~/x')
        part = absolute_paths.sub('~/x', part)
        part = re.sub(r'~/x(?:/[\w.+@%~=-]+)+/?', '~/x', part)
        part = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[email]', part)
        return re.sub(rf'(?<![\w.-]){hostname}(?![\w.-])', '[host]', part)

    parts = []
    start = 0
    for path in absolute_paths.finditer(text):
        if path[0].startswith('/dev/'):
            parts.extend((hide_private_text(text[start:path.start()]), path[0]))
            start = path.end()
    parts.append(hide_private_text(text[start:]))
    return ''.join(parts)


def fixture_text(screen, root):
    lines = screen.strip('\n').splitlines()
    start = next((i for i in reversed(range(len(lines))) if lines[i].strip().startswith('▔▔')), 0)
    text = '\n'.join(line.rstrip() for line in lines[start:]).rstrip()
    return redact(text, root) + '\n'


class ScreenTrace:
    """Keep every pane read in order, including frames the Stop parser rejects."""

    def __init__(self, run, root, screens):
        self.run_command = run
        self.root = root
        self.screens = screens
        self.step = 'preflight'

    def run(self, args, **kwargs):
        result = self.run_command(args, **kwargs)
        if args[1] == 'capture-pane' and result.returncode == 0:
            screen = terminal_input.ANSI_SGR.sub('', result.stdout)
            self.screens.append((self.step, redact(screen, self.root)))
        return result


def capture_details(entry, tasks, trace, sleep, clock, save, report):
    run = trace.run

    def capture(kind, item):
        target = stop.task_target(entry, item, kind != 'agent')
        agent_selected = False

        def verify_list(_screen, rows):
            if sum(row['selected'] for row in rows) != 1:
                raise stop.StopError('Could not verify one selected row in the /tasks list.')
            matches = [row for row in rows if stop.is_target(target, row)]
            if len(matches) > 1:
                raise stop.StopError(f'Found {len(matches)} {kind} rows in /tasks. Expected one.')
            if not matches:
                if kind == 'agent' and any(agent['id'] == item['id'] and agent['state'] == 'running'
                                           for agent in subagents.listing(entry)['agents']):
                    raise stop.StopError(f'Claude Code {report["version"]} does not list this subagent in /tasks '
                                         '(it waits on a background shell).')
                raise stop.StopError(f'Could not find the {kind} row in /tasks.')
            row = matches[0]
            if row['label'] not in target['labels']:
                raise stop.StopError(f'Could not verify the full {kind} row label in /tasks.')
            return row

        def observe_list(screen, rows):
            nonlocal agent_selected
            save('list.txt', screen, first=True)
            row = verify_list(screen, rows)
            if kind == 'agent' and row['selected']:
                agent_selected = True
                save('list_agent_selected.txt', screen)

        screen = stop.open_task_detail(entry, target, run, sleep, clock, observe_list=observe_list)
        save(f'{kind}_detail.txt', screen)
        named, runtime, body = stop.detail_facts(screen, target)
        if not named or stop.seconds(runtime or '') is None or not body:
            raise stop.StopError(f'Could not parse the {kind} detail.')
        if kind == 'agent' and not agent_selected:
            raise stop.StopError('Could not verify the agent selection in the /tasks list.')
        return verify_list

    terminal_input.verify(entry, run)
    verified = {}
    for kind, item in tasks.items():
        if kind == 'agent' and 'waiting' in item:
            continue  # The Stop driver captures the agent bar while verifying the waiting agent.
        if terminal_input.live_status(entry, run)['state'] not in ('Idle', 'Working'):
            raise CanaryError('Claude needs input before the canary can open /tasks.')
        if any(terminal_input.prompt_draft(entry, run)):
            raise CanaryError('Claude has an unsent draft.')
        trace.step = f'inspect-{kind}'
        try:
            verified[kind] = capture(kind, item)
        except stop.StopError as exc:
            report['tasks'][kind]['error'] = str(exc)
            save(f'unparsed_{kind}.txt', visible(entry, run))
        finally:
            stop.close(entry, run, sleep)
    return verified


def fixture_diff(fixtures, captures, surface='tasks'):
    versions = []
    for folder in fixtures.glob(f'claude_*_{surface}'):
        match = re.fullmatch(rf'claude_({VERSION})_{surface}', folder.name)
        if match and folder.is_dir():
            version = match[1]
            numeric = re.match(r'\d+\.\d+\.\d+', version)[0]
            versions.append((tuple(map(int, numeric.split('.'))), version == numeric, version, folder))
    baseline = max(versions)[-1] if versions else None
    changes = {}
    for name, capture in sorted(captures.items()):
        old = baseline / name if baseline else None
        exists = old is not None and old.is_file()
        previous = old.read_text() if exists else ''
        diff = ''.join(difflib.unified_diff(previous.splitlines(keepends=True), capture.splitlines(keepends=True),
                                            f'{baseline.name}/{name}' if exists else '/dev/null', name))
        if diff:
            changes[name] = diff
    return baseline.name if baseline else None, changes


def write_atomic(path, content, *, expected=None):
    with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content.encode('utf-8') if isinstance(content, str) else content)
            stream.close()
            if expected is not None:
                try:
                    if path.read_bytes() != expected:
                        return False
                except FileNotFoundError:
                    return False
            staged.replace(path)
            return True
        finally:
            staged.unlink(missing_ok=True)


def exercise(adapter, entry, report, save, trace, sleep, clock):
    version = tuple(map(int, re.match(r'\d+\.\d+\.\d+', report['version'])[0].split('.')))
    # A live 2.1.330 agent briefly appears in /tasks before it waits on its child shell.
    while (tasks := expected_tasks(adapter.listing(entry), waiting_agent=version >= (2, 1, 330))) is None:
        sleep(0.5)
    registry = json.loads(Path(entry['registry']).read_text())
    if registry.get('version') != report['version']:
        raise CanaryError('The Claude Code version changed between the probe and the launch.')
    verified = capture_details(entry, tasks, trace, sleep, clock, save, report)
    report['screens_parsed'] = len(verified) == len(tasks)
    bar_screens = set()

    def observe_bar(screen, bar):
        if bar['focus'] == 'prompt':
            name = 'after_escape' if 'agent_stopped' in bar_screens else 'prompt_waiting_shell'
        elif bar['focus'] == 'pill':
            name = 'pill_focus'
        else:
            name = {'stop': 'agent_selected', 'clear': 'agent_stopped', None: 'main_selected'}[bar['action']]
        save(f'agent_bar/{name}.txt', screen, first=True)
        bar_screens.add(name)
        if name == 'after_escape':
            report['screens_parsed'] = len(verified) + 1 == len(tasks)

    waiting = 'waiting' in tasks['agent']
    for kind in tasks:
        use_bar = kind == 'agent' and waiting
        if not use_bar and kind not in verified:
            continue
        trace.step = f'stop-{kind}'
        result = report['tasks'][kind]
        try:
            reply = stop.stop(entry, tasks[kind]['id'], trace.run, sleep, clock, allow_process_fallback=False,
                              observe_list=verified.get(kind), observe_bar=observe_bar if use_bar else None)
            result['state'] = reply['state']
            result['passed'] = reply['ok'] and reply['state'] == 'stopped'
            if not result['passed']:
                result['error'] = f'Claude reported {reply["state"]}; expected stopped.'
        except stop.StopError as exc:
            result['error'] = str(exc)


def write_report(path, report, screens):
    if not report['passed']:
        directory = path.with_name(f'{path.stem}-screens')
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            report['screens_dir'] = str(directory.absolute())
            # The same JSON path can be reused. List this run's files in order.
            report['screens'] = names = []
            width = max(2, len(str(len(screens))))
            for index, (step, screen) in enumerate(screens, start=1):
                name = f'{index:0{width}}-{step}.txt'
                write_atomic(directory / name, screen)
                names.append(name)
        except OSError:
            report['screens_error'] = 'Could not save all captured screens.'
    write_atomic(path, json.dumps(report, indent=2, ensure_ascii=False) + '\n')


def record(args, report, fixtures, captures, screens):
    try:
        tasks = {name: screen for name, screen in captures.items() if '/' not in name}
        report['baseline'], report['diffs'] = fixture_diff(fixtures, tasks)
        bar = {name.split('/', 1)[1]: screen for name, screen in captures.items() if name.startswith('agent_bar/')}
        if bar:
            report['agent_bar_baseline'], changes = fixture_diff(fixtures, bar, 'agent_bar')
            report['diffs'].update({f'agent_bar/{name}': diff for name, diff in changes.items()})
        if args.save_fixtures and report['screens_parsed']:
            for surface, files in (('tasks', tasks), ('agent_bar', bar)):
                if not files:
                    continue
                directory = fixtures / f'claude_{report["version"]}_{surface}'
                directory.mkdir(exist_ok=True)
                for name, screen in files.items():
                    write_atomic(directory / name, screen)
    except OSError:
        report['error'] = 'Could not read or save the fixtures.'
    for result in report['tasks'].values():
        if not result['passed'] and 'error' not in result:
            reason = report.get('error', 'The canary did not complete this Stop.')
            result['error'] = f'Stop skipped. {reason}'
    report['passed'] = ('error' not in report and 'cleanup_error' not in report and report['screens_parsed']
                        and all(result['passed'] for result in report['tasks'].values()))
    if args.json:
        write_report(args.json, report, screens)
    if args.if_changed and report['passed']:
        try:
            write_atomic(args.if_changed, report['version'] + '\n')
        except OSError:
            report.update(passed=False, error='Could not save the last passing version.')
            if args.json:
                write_report(args.json, report, screens)


@contextmanager
def temporary_workspace(report):
    root = Path(tempfile.mkdtemp(prefix='tether-canary-')).resolve()
    try:
        yield root
    finally:
        if 'cleanup_error' in report:
            report['cleanup_warning'] = 'Kept the temporary canary directory because shutdown could not be verified.'
        else:
            try:
                shutil.rmtree(root)
            except OSError as exc:
                detail = fixture_text(str(exc), root).strip()
                report['cleanup_warning'] = f'Could not remove the temporary canary directory. {detail}'


def main(argv=None, *, version_probe=installed_version, adapter_factory=TmuxClaude, fixtures=FIXTURES,
         sleep=time.sleep, clock=time.time, work_seconds=WORK_SECONDS):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', type=Path, metavar='PATH', help='write the report and save captured screens on failure')
    parser.add_argument('--save-fixtures', action='store_true', help='save the complete set of parsed screens')
    parser.add_argument('--if-changed', type=Path, metavar='STATE_FILE', help='skip the last version that passed')
    args = parser.parse_args(argv)
    if args.json and args.if_changed and args.json.resolve() == args.if_changed.resolve():
        parser.error('--json and --if-changed require different paths')

    report = {'version': None, 'passed': False, 'screens_parsed': False,
              'tasks': {kind: {'passed': False} for kind in KINDS}, 'trigger': TRIGGER}
    captures = {}
    screens = []
    old_alarm = signal.signal(signal.SIGALRM, timed_out)
    old_term = signal.signal(signal.SIGTERM, interrupted)
    deadline = time.monotonic() + work_seconds + 10
    signal.setitimer(signal.ITIMER_REAL, work_seconds)
    try:
        try:
            version = report['version'] = version_probe()
            if args.if_changed and args.if_changed.exists() and args.if_changed.read_text().strip() == version:
                return 0
            with temporary_workspace(report) as root:
                def save(name, screen, first=False):
                    if not first or name not in captures:
                        captures[name] = fixture_text(screen, root)

                adapter = adapter_factory(root)
                trace = ScreenTrace(adapter.run, root, screens)
                try:
                    with adapter.session() as entry:
                        exercise(adapter, entry, report, save, trace, sleep, clock)
                finally:
                    if cleanup_error := getattr(adapter, 'cleanup_error', None):
                        report['cleanup_error'] = fixture_text(cleanup_error, root).strip()
                    if credentials_warning := getattr(adapter, 'credentials_warning', None):
                        report['credentials_warning'] = credentials_warning
        except KeyboardInterrupt:
            report['error'] = 'Interrupted.'
        except subprocess.CalledProcessError as exc:
            report['error'] = f'{Path(exc.cmd[0]).name} exited with status {exc.returncode}.'
        except subprocess.TimeoutExpired as exc:
            report['error'] = f'{Path(exc.cmd[0]).name} exceeded its command timeout.'
        except (CanaryError, OSError, RuntimeError, ValueError) as exc:
            report['error'] = fixture_text(str(exc), Path(tempfile.gettempdir())).strip()
        except Exception as exc:
            # A failure in a called parser must not discard screens read before it failed.
            report['error'] = redact(f'{type(exc).__name__}: {exc}', Path(tempfile.gettempdir())).strip()
        signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic()))
        record(args, report, fixtures, captures, screens)
    except (CanaryError, OSError, KeyboardInterrupt) as exc:
        report['passed'] = False
        report['error'] = ('Interrupted.' if isinstance(exc, KeyboardInterrupt)
                           else fixture_text(str(exc), Path(tempfile.gettempdir())).strip())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)

    outcome = 'PASS' if report['passed'] else 'FAIL'
    tasks = ', '.join(f'{kind} {"pass" if result["passed"] else "fail"}' for kind, result in report['tasks'].items())
    screens = 'parsed' if report['screens_parsed'] else 'unverified'
    print(f'Claude Code {report["version"] or "unknown"} {outcome} ({tasks}, screens {screens}).')
    if report.get('error'):
        print(report['error'])
    if report.get('cleanup_error'):
        print(report['cleanup_error'])
    if report.get('cleanup_warning'):
        print(report['cleanup_warning'])
    if report.get('credentials_warning'):
        print(report['credentials_warning'])
    for kind, result in report['tasks'].items():
        if not result['passed'] and ('error' in result or 'state' in result):
            print(f'{kind}: {result.get("error") or result["state"]}')
    if 'diffs' in report:
        names = ', '.join(report['diffs']) or 'no differences'
        print(f'Fixture diff against {report["baseline"] or "no baseline"}: {names}.')
    if 'screens_dir' in report:
        print(f'Captured screens: {report["screens_dir"]}')
    if 'screens_error' in report:
        print(report['screens_error'])
    print(TRIGGER)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
