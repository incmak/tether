"""B204: the commit gate. Real git in temp folders; the mode files, the log and the session state are isolated."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import fcntl
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import commit_gate
import stamp
import paths

PID = 'abcdef012345'
STAMP = 'Verified: tests(server 412 ok) review(codex APPROVED)'
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def heredoc(message, tag="'EOF'"):
    return f'git commit -m "$(cat <<{tag}\n{message}\nEOF\n)"'


class Handler:
    def __init__(self, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = path
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name, value in (('CONFIG', self.root / 'config'), ('TETHER', self.root / 'tether'),
                            ('CACHE', self.root / 'cache'), ('COMMIT_GATE_LOG', self.root / 'log' / 'commit-gate.log')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.real_project = commit_gate._project
        project = mock.patch.object(commit_gate, '_project', return_value=(PID, True))
        self.lookup_project = project.start()
        self.addCleanup(project.stop)
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.commit_file('first', f'first\n\n{STAMP}')
        self.calls = 0

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def commit_file(self, name, message):
        (self.repo / name).write_text(name)
        self.git('add', name)
        self.git('commit', '-q', '-m', message)

    def rows(self):
        try:
            return [json.loads(line) for line in paths.COMMIT_GATE_LOG.read_text().splitlines()]
        except FileNotFoundError:
            return []

    def pre(self, command, session='s1', now=1000.0, **extra):
        self.calls += 1
        data = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'session_id': session, 'cwd': str(self.repo),
                'tool_use_id': f'tool-{self.calls}', 'tool_input': {'command': command}, **extra}
        return data, commit_gate.check(data, now=now)

    def run_commit(self, message, session='s1', now=1000.0):
        """One Bash call as Claude Code runs it: the PreToolUse check, the commit when allowed, then PostToolUse."""
        data, out = self.pre(heredoc(message), session, now)
        if not out:
            self.commit_file(f'file-{self.calls}', message)
            commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=now + 1)
        return out


class Modes(Base):
    def test_missing_or_bad_file_means_observe(self):
        self.assertEqual(commit_gate.resolve(PID), {'mode': 'observe', 'source': 'global', 'global': 'observe', 'project': None})
        for text in ('', 'on', 'ENFORCE', 'enforce now', '2'):
            commit_gate.global_path().parent.mkdir(parents=True, exist_ok=True)
            commit_gate.global_path().write_text(text)
            self.assertEqual(commit_gate.read_global(), 'observe', text)

    def test_global_and_project_override(self):
        commit_gate.set_global('enforce')
        self.assertEqual(commit_gate.resolve(PID)['mode'], 'enforce')
        commit_gate.set_project(PID, True, 'off')
        self.assertEqual(commit_gate.resolve(PID), {'mode': 'off', 'source': 'project', 'global': 'enforce',
                                                    'project': {'override': True, 'mode': 'off'}})
        self.assertEqual(commit_gate.resolve()['mode'], 'enforce')
        commit_gate.set_project(PID, False, 'off')
        resolved = commit_gate.resolve(PID)
        self.assertEqual((resolved['mode'], resolved['source'], resolved['project']['mode']), ('enforce', 'global', 'off'))

    def test_bad_values_are_refused(self):
        for call in (lambda: commit_gate.set_global('strict'), lambda: commit_gate.set_global(None),
                     lambda: commit_gate.set_project(PID, 'yes', 'off'), lambda: commit_gate.set_project(PID, True, 1),
                     lambda: commit_gate.set_project('../etc', True, 'off')):
            with self.assertRaises(commit_gate.GateError):
                call()
        self.assertFalse(commit_gate.global_path().exists())

    def test_bad_project_file_is_ignored(self):
        path = commit_gate.project_path(PID)
        path.parent.mkdir(parents=True)
        for text in ('{', '[]', '{"override": 1, "mode": "off"}', '{"override": true, "mode": "strict"}'):
            path.write_text(text)
            self.assertIsNone(commit_gate.read_project(PID), text)


class Evaluate(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name).resolve()
        (self.cwd / 'sub').mkdir()

    def verdict(self, command, cwd=None):
        found = commit_gate.evaluate(command, str(self.cwd) if cwd is None else cwd)
        return found and (found['verdict'], found['reason'])

    def test_not_a_commit(self):
        for command in ('git status', 'git log --grep commit', 'echo "git commit -m x"', 'git commit-tree abc -m x',
                        "grep -rn 'git commit' docs", 'gh pr create --body "run git commit -m x first"',
                        'git config commit.gpgsign false', 'git show commit', 'git commit --dry-run -m x',
                        'git commit --help', 'git commit -h', 'npm run commit', '# git commit -m x\nls', None, 7):
            with self.subTest(command=command):
                self.assertIsNone(commit_gate.evaluate(command, str(self.cwd)))

    def test_quoted_operators_are_words(self):
        for command in ("printf '%s\\n' ';' 'git' 'commit' '-m' 'Title'",
                        "echo 'a && git commit -m x'",
                        "printf '%s' '>' 'git' 'commit' '-m' 'Title'"):
            with self.subTest(command=command):
                self.assertIsNone(commit_gate.evaluate(command, str(self.cwd)))
        self.assertEqual(self.verdict("git commit -m ';'"), ('missing', 'no-line'))

    def test_compound_shell_commands_cannot_be_missing(self):
        for tail in ("if true; then git commit --allow-empty -m 'Verified: none'; fi",
                     "for item in one two; do git commit -m 'Verified: none'; done",
                     "(git commit -m 'Verified: none')",
                     "{ git commit -m 'Verified: none'; }",
                     "git commit -m x # git commit -m 'Verified: none'"):
            with self.subTest(tail=tail):
                self.assertEqual(self.verdict(f'git commit --allow-empty -m First && {tail}')[0], 'unknown')
        for command in ('for item in one two; do git commit -m x; done', '(git commit -m x)', '{ git commit -m x; }'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command)[0], 'unknown')

    def test_shell_launchers_cannot_be_missing(self):
        for launcher in ('eval', 'source', '.', 'exec', 'xargs', 'sh', '/bin/bash', 'zsh', 'env -S', 'env -i -S'):
            with self.subTest(launcher=launcher):
                self.assertEqual(self.verdict(f"git commit -m first; {launcher} 'git commit -m second'"),
                                 ('unknown', 'parse'))
        self.assertEqual(self.verdict('SHELL=/bin/bash git commit -m x'), ('missing', 'no-line'))

    def test_word_plainness_follows_quoting(self):
        for command in ('git commit -m Fix*',                         'git commit -m Fix -- files[12]'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('unknown', 'shell'))
        for command in ("git commit -m 'Fix $ISSUE * # ; ( )'", 'git commit -m "Fix * # ; ( )"',
                        'git commit -m "Fix\\nIssue"',  # inside double quotes the shell keeps this backslash
                        'git com\\\nmit -m Fix', 'git commit -m Fix 2>&1'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('missing', 'no-line'))

    def test_nonplain_file_arguments_are_never_read(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, message in (('msg[12]', 'Fix'), ('msg1', STAMP), ('msg?.txt', 'Fix'), ('msg1.txt', STAMP),
                                  ('msg.txt', 'Fix')):
                Path(folder, name).write_text(message)
            with mock.patch.dict(os.environ, {'HOME': folder}), \
                    mock.patch.object(commit_gate, '_read_message_file', wraps=commit_gate._read_message_file) as read:
                for name in ('msg[12]', 'msg?.txt', '~other/msg.txt', '~+/msg.txt'):
                    with self.subTest(name=name):
                        self.assertEqual(self.verdict(f'git commit -F {name}', folder), ('unknown', 'shell'))
                read.assert_not_called()
                # A plain ~/ is the home folder, for the shell and for the gate.
                self.assertEqual(self.verdict('git commit -F ~/msg.txt', folder), ('missing', 'no-line'))
                self.assertEqual(self.verdict('git commit -F ~/msg1.txt', folder), ('pass', ''))
            self.assertEqual(self.verdict("git commit -F 'msg[12]'", folder), ('missing', 'no-line'))

    def test_messages_with_a_valid_line_pass(self):
        for command in (
                f'git commit -m "Fix" -m "{STAMP}"',
                f"git commit -qam 'Fix\n\n{STAMP}'",
                heredoc(f'Fix the "quoted" thing, it\'s done (really)\n\n{STAMP}\nCo-Authored-By: x <x@example.com>'),
                heredoc(f'Fix\n\n{STAMP}', tag='EOF'),
                f'cd sub && {heredoc("Fix" + chr(10) + chr(10) + "Verified: none (docs only)")} && git push',
                "git commit -m 'Subject#123' -m 'Verified: none'",
                f'cd sub && GIT_EDITOR=true git -c user.name=x --no-pager commit -q --amend -m "Fix\n\n{STAMP}"',
                f"git commit -F - <<'EOF'\nFix it's\n\n{STAMP}\nEOF\ngit log -1",
                f'git commit -m "Fix\n\n{STAMP}" 2>&1 | tail -3; git status',
                f'/usr/bin/git commit -sm"Fix\n\n{STAMP}" -- a.txt',
                f'git commit --author "A <a@example.com>" --date now -m "Fix\n\n{STAMP}"'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('pass', ''))

    def test_messages_read_in_full_with_no_valid_line_are_missing(self):
        for command in (
                'git commit -m "Fix the parser"',
                'git commit -am "Fix" -m "Verified: yes"',
                'git commit -m "Fix\n\nVerified: tested"',
                heredoc('Fix\n\nVerified:'),
                heredoc('Fix\n\nCo-Authored-By: x <x@example.com>'),
                "git add . && git commit -qm 'wip' && git push",
                "git commit -F - <<'EOF'\nFix\nEOF",
                'echo "Verified: tests(ok)"; git commit -m "Fix"',
                'git commit -m subject --cleanup=strip',
                'git -C sub commit --message=Fix',
                'git commit -m first && git commit -m second'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('missing', 'no-line'))

    def test_messages_the_gate_cannot_read_are_unknown(self):
        for command, reason in (
                ('git commit -m "$MSG"', 'shell'),
                ('git commit -m "$(cat msg.txt)"', 'shell'),
                ('git commit -m `cat msg.txt`', 'shell'),
                ('git commit -m $(cat msg.txt)', 'shell'),
                (heredoc('Fix $ISSUE', tag='EOF'), 'shell'),
                ('git commit --amend --no-edit', 'reuse'),
                ('git commit -C HEAD~1', 'shell'),
                ('git commit --reuse-message=abc123', 'reuse'),
                ('git commit --fixup=abc123', 'fixup'),
                ('git commit --squash abc123 -m "note"', 'fixup'),
                ('git commit', 'editor'),
                ('git commit -a --no-edit', 'editor'),
                ('git commit -F -', 'file'),
                ('git commit -F /nonexistent/message.txt', 'file'),
                ('git commit -F "$S/message.txt"', 'shell'),
                ("git commit -m subject --trailer 'Verified=none'", 'open'),
                # git builds the trailer lines; with a Verified trailer the gate does not decide.
                ("git commit -m subject --trailer 'Verified: tests(ok)'", 'open'),
                ("git commit -m s --trailer 'Verified: tests(ok)' --trailer 'Verified: pending'", 'open'),
                (f'git commit -m "s\n\n{STAMP}" --trailer "Verified: pending"', 'open'),
                ("git commit --amend --no-edit --trailer 'Reviewed-by: A'", 'reuse'),
                ('git commit -m subject --edit', 'open'),
                ('git commit -em subject', 'editor'),
                ('git commit --mess=subject', 'editor'),
                ('git commit --frobnicate -m subject', 'open'),
                ('git commit -m x # note', 'parse'),
                ('# it is a note\ngit commit -m x', 'parse'),
                ("true || git commit -m skipped; git commit -m 'Verified: none'", 'cleanup'),
                ('git commit -m', 'editor'),
                ('git commit -Zm "Fix"', 'editor'),
                ('git commit -m "Fix', 'parse')):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('unknown', reason))

    def test_message_file(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'stamped.txt').write_text(f'Fix\n\n{STAMP}\n')
            Path(folder, 'msg.txt').write_text('Fix\n')
            Path(folder, 'sub').mkdir()
            self.assertEqual(self.verdict('git commit -F stamped.txt', folder), ('pass', ''))
            for command in ('git commit -F msg.txt', 'git commit --file=msg.txt',
                            'cd sub && git commit -F ../msg.txt', 'git -C sub commit -F ../msg.txt'):
                with self.subTest(command=command):
                    self.assertEqual(self.verdict(command, folder), ('missing', 'no-line'))
            self.assertEqual(self.verdict('cd sub && git commit -F ../stamped.txt', folder), ('pass', ''))
            self.assertEqual(self.verdict('git -C sub commit -F ../stamped.txt', folder), ('pass', ''))
            # The command writes the file first: what is on disk now is not the message.
            for command in ("cat > msg.txt <<'EOF'\nFix\nEOF\ngit commit -F msg.txt",
                            'cp stamped.txt msg.txt && git commit -F ./msg.txt',
                            'git log -1 --format=%B > stamped.txt; git commit --amend -F stamped.txt',
                            'cd "$HERE" && git commit -F stamped.txt', 'git --git-dir=x/.git commit -F stamped.txt'):
                with self.subTest(command=command):
                    self.assertEqual(self.verdict(command, folder), ('unknown', 'file'))

    def test_message_file_must_be_regular_and_within_the_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'sub').mkdir()
            message = Path(folder, 'msg.txt')
            message.write_bytes(b'x' * commit_gate.FILE_BYTES)
            self.assertEqual(self.verdict('git commit -F msg.txt', folder), ('missing', 'no-line'))
            with message.open('a') as stream:
                stream.write(f'\n\n{STAMP}\n')
            for name in ('msg.txt', 'sub'):
                with self.subTest(name=name):
                    self.assertEqual(self.verdict(f'git commit -F {name}', folder), ('unknown', 'file'))
            os.mkfifo(Path(folder, 'pipe'))
            code = ('import commit_gate, sys; '
                    'print(commit_gate.evaluate("git commit -F pipe", sys.argv[1])["verdict"])')
            ran = subprocess.run([sys.executable, '-c', code, folder], cwd=Path(commit_gate.__file__).parent,
                                 check=True, capture_output=True, text=True, timeout=1)
            self.assertEqual(ran.stdout.strip(), 'unknown')

    def test_message_file_size_is_checked_after_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            message = Path(folder, 'msg.txt')
            message.write_bytes(b'x' * commit_gate.FILE_BYTES)
            before_growth = message.stat()
            with message.open('a') as stream:
                stream.write(f'\n\n{STAMP}\n')
            with mock.patch.object(os, 'fstat', return_value=before_growth):
                self.assertEqual(self.verdict('git commit -F msg.txt', folder), ('unknown', 'file'))

    def test_later_input_redirect_can_replace_a_heredoc_message(self):
        for redirect in ('< msg.txt', '0< msg.txt', '0\\\n>&1', "<<< 'new input'", "00<<< 'new input'"):
            command = f"git commit -F - <<'EOF' {redirect}\nFix\nEOF"
            with self.subTest(redirect=redirect):
                self.assertEqual(self.verdict(command), ('unknown', 'file'))

    def test_heredocs_follow_their_file_descriptors(self):
        cases = (
            (f"<<MSG 10<<OTHER\n{STAMP}\nMSG\nOther text\nOTHER", ('pass', '')),
            (f"<<MSG 10<<OTHER\nNo stamp\nMSG\n{STAMP}\nOTHER", ('missing', 'no-line')),
            (f"3<<OTHER 0<<-MSG\nOther text\nOTHER\n\t{STAMP}\n\tMSG", ('pass', '')),
            (f"1\\\n0<<OTHER 00<<MSG\nOther text\nOTHER\n{STAMP}\nMSG", ('pass', '')),
            (f"<<MSG 10<<<other\n{STAMP}\nMSG", ('pass', '')),
            (f"<<MSG 10<other.txt\n{STAMP}\nMSG", ('pass', '')),
        )
        for redirects, expected in cases:
            with self.subTest(redirects=redirects):
                self.assertEqual(self.verdict(f'git commit -F - {redirects}'), expected)

    def test_heredoc_delimiter_uses_shell_quoting_and_whitespace(self):
        for command in (heredoc(f'Fix\n EOF\n\n{STAMP}'),
                        f"git commit -F - <<'EOF'\nFix\nEOF \n\n{STAMP}\nEOF",
                        f"git commit -F - <<-'EOF'\n\tFix\n\t\n\t{STAMP}\n\tEOF"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('pass', ''))
        for command in ("git commit -m 'Title\n<<EOF\nFix\nEOF'",
                        """git commit -m 'Title $(cat <<"EOF"\nFix\nEOF\n)'"""):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('missing', 'no-line'))

    def test_several_commits_require_agreement(self):
        good = f'git commit -m "A\n\n{STAMP}"'
        found = commit_gate.evaluate(f'{good} && git commit -m "B"', str(self.cwd))
        self.assertEqual((found['verdict'], found['reason'], found['count'], found['folder']),
                         ('unknown', 'mixed', 2, str(self.cwd)))
        found = commit_gate.evaluate(f'{good}; cd sub; git commit --amend --no-edit', str(self.cwd))
        self.assertEqual((found['verdict'], found['count'], found['folder']), ('unknown', 2, None))
        self.assertIsNone(commit_gate.evaluate(good, 'relative')['folder'])

    def test_folder_requires_plain_cd_commands_and_safe_separators(self):
        for prefix in ('cd sub &&\n', 'cd sub;\n', 'cd sub\n\n', 'cd -P sub && ', 'cd sub || exit 1; '):
            with self.subTest(prefix=prefix):
                self.assertEqual(commit_gate.evaluate(prefix + 'git commit -m Title', str(self.cwd))['folder'],
                                 str(self.cwd / 'sub'))
        for prefix in ('false || cd sub;', 'cd sub ||\n', 'cd sub |\n', 'cd sub |& ', 'cd sub &\n',
                       'cd -P; ', 'cd "$HERE"; ', 'cd; ', 'cd sub >/dev/null; ', 'pushd sub; ', 'popd; ',
                       'true | cd sub; '):
            with self.subTest(prefix=prefix):
                self.assertIsNone(commit_gate.evaluate(prefix + 'git commit -m Title', str(self.cwd))['folder'])
        # An ordinary command before the commit does not move the shell: the usual `git add . && git commit` form.
        for prefix in ('git add . && ', 'git add -- source && ', r"printf '%s\n' GIT_DIR=example; ",
                       'npm test; ', 'git add -A\n', '>/dev/null; ', 'git status --short | head -5; ',
                       'git diff --cached --stat | tail -2 && ', 'npm test || true; ', 'sleep 1 & ', 'set -e\n'):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(prefix + 'git commit -m Title', str(self.cwd))
                self.assertEqual((found['verdict'], found['folder']), ('missing', str(self.cwd)))

    def test_folder_is_unknown_after_a_prefixed_moving_command(self):
        cwd = str(self.cwd)
        commit = 'git commit -m Title'
        for prefix in ('X=1 cd sub; ', 'CDPATH=/alt cd sub && ', 'A=1 B=2 pushd sub; ',
                       'X=1 command cd sub; ', 'X=1 builtin cd sub; ', 'X=1 source f; ', 'X=1 . f; ',
                       'X=1 eval "cd sub"; ', "X='one two' popd; ", 'time X=1 cd sub; '):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(prefix + commit, cwd)
                self.assertIsNone(found['folder'])
                self.assertNotEqual(found['verdict'], 'missing')
        for prefix in ('X=1 true; ', 'X=1 '):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(prefix + commit, cwd)
                self.assertEqual((found['folder'], found['verdict']), (cwd, 'missing'))

    def test_folder_is_unknown_after_moving_words_beyond_simple_prefixes(self):
        bash = ('X+=1 cd ./sub', 'X[0]=1 cd ./sub', 'X[0]+=1 cd ./sub',
                'X=1 command -p cd ./sub', 'X=1 command -- cd ./sub', 'time -p cd ./sub',
                '{fd}</dev/null cd ./sub', '&>/dev/null cd ./sub', '&>>/dev/null cd ./sub',
                'command -p -- . ./script', '{fd}</dev/null . ./script',
                'X=${UNSET:-one two} . ./script', 'X[1 + 0]=v . ./script',
                'X=$(printf value)suffix cd ./sub', 'X=$(printf value)suffix . ./script')
        zsh = ('noglob cd ./sub', 'nocorrect cd ./sub', 'nocorrect noglob . ./script',
               'repeat 1 cd ./sub', '- cd ./sub', 'X=1 repeat 2 - . ./script')
        for shell, prefixes in (('bash', bash), ('zsh', zsh)):
            for prefix in prefixes:
                with self.subTest(shell=shell, prefix=prefix):
                    found = commit_gate.evaluate(f'{prefix}; git commit -m Title', str(self.cwd))
                    self.assertEqual((found['folder'], found['verdict']), (None, 'unknown'))

    def test_staging_all_stays_exact_in_uncertain_commands(self):
        for command in ('git add . && git commit -m Title # Save the change',
                        'if git add .; then git commit -m Title; fi', 'git add . ; git commit -m Title # x'):
            with self.subTest(command=command):
                self.assertEqual(commit_gate.evaluate(command, str(self.cwd))['folder'], str(self.cwd))

    def test_command_lookup_does_not_move_the_shell(self):
        for prefix in ('command -v cd', 'command -pV pushd', 'X=1 command -v . '):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix}; git commit -m Title', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']), (str(self.cwd), 'missing'))

    def test_prefixed_shell_options_make_following_cd_unknown(self):
        deep = self.cwd / 'real/deep'
        deep.mkdir(parents=True)
        (self.cwd / 'link').symlink_to(deep, target_is_directory=True)
        for prefix in ('X=1 set -P', 'command -p set -P', 'time -p set -P',
                       'noglob setopt CHASE_LINKS', 'nocorrect unsetopt CHASE_LINKS', 'X[1 + 0]=v set -P'):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix}; cd ./link/..; git commit -m Title', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']), (None, 'unknown'))

    def test_git_context_changes_beyond_simple_prefixes_make_folder_unknown(self):
        for prefix in ('GIT_DIR+=./sub/.git', 'GIT_DIR[0]=./sub/.git', 'GIT_DIR[0]+=./sub/.git',
                       'command export GIT_DIR=./sub/.git', 'command -- unset GIT_DIR',
                       'command -p declare -x GIT_DIR=./sub/.git', 'noglob typeset -x GIT_DIR=./sub/.git',
                       'declare -x GIT_CONFIG_GLOBAL=other.config',
                       'GIT_DIR=./sub/.git', 'export X=1 GIT_DIR=./sub/.git', 'unset GIT_DIR',
                       'X[1 + 0]=v export GIT_DIR=./sub/.git', 'X[1 + 0]=v unset GIT_DIR',
                       'X=$(printf value)suffix export GIT_DIR=./sub/.git'):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix}; git commit -m Title', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']), (None, 'unknown'))

    def test_stdout_and_stderr_redirects_consume_the_target(self):
        for operator in ('&>', '&>>', '&\\\n>\\\n>'):
            with self.subTest(operator=operator):
                command = f'{operator} /dev/null git commit -m Title'
                found = commit_gate.evaluate(command, str(self.cwd))
                self.assertIsNotNone(found)
                self.assertEqual((found['verdict'], found['reason'], found['folder']),
                                 ('unknown', 'parse', str(self.cwd)))

    def test_stdout_and_stderr_redirects_make_a_message_file_uncertain(self):
        (self.cwd / 'draft.txt').write_text(f'Title\n\n{STAMP}\n')
        for operator in ('&>', '&>>'):
            with self.subTest(operator=operator):
                command = f'git commit --allow-empty -F draft.txt {operator}draft.txt'
                self.assertEqual(self.verdict(command), ('unknown', 'parse'))

    def test_an_earlier_command_stops_the_message_file_read(self):
        (self.cwd / 'msg.txt').write_text('Old draft\n')
        self.assertEqual(commit_gate.evaluate('git commit -F msg.txt', str(self.cwd))['verdict'], 'missing')
        for prefix in ('cp other.txt msg.txt && ', 'git add . && ', 'true; '):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(prefix + 'git commit -F msg.txt', str(self.cwd))
                self.assertEqual((found['verdict'], found['reason'], found['folder']), ('unknown', 'file', str(self.cwd)))

    def test_cd_from_a_linked_start_folder_is_unknown(self):
        (self.cwd / 'real/sub').mkdir(parents=True)
        (self.cwd / 'alias').symlink_to(self.cwd / 'real', target_is_directory=True)
        found = commit_gate.evaluate('git commit -m Title', str(self.cwd / 'alias'))
        self.assertEqual((found['verdict'], found['folder']), ('missing', str(self.cwd / 'real')))
        for prefix in ('cd sub && ', 'cd .. && '):
            with self.subTest(prefix=prefix):
                self.assertIsNone(commit_gate.evaluate(prefix + 'git commit -m Title', str(self.cwd / 'alias'))['folder'])

    def test_invalid_directory_steps_make_folder_unknown(self):
        (self.cwd / 'file').touch()
        for prefix in ('cd missing; cd sub; ', 'cd file; ', 'cd missing/..; ', 'cd file/../sub; ',
                       'git -C missing -C sub ', 'git -C missing/.. ', 'git -C file/../sub ', 'git -C file '):
            with self.subTest(prefix=prefix):
                command = prefix + ('commit -m Title' if prefix.startswith('git ') else 'git commit -m Title')
                self.assertIsNone(commit_gate.evaluate(command, str(self.cwd))['folder'])
        self.assertIsNone(commit_gate.evaluate('git commit -m Title', str(self.cwd / 'missing'))['folder'])

    def test_cd_keeps_logical_path_across_symlink_steps(self):
        deep = self.cwd / 'real/deep'
        (deep / 'sub').mkdir(parents=True)
        (self.cwd / 'alias').symlink_to(deep, target_is_directory=True)
        (self.cwd / 'msg.txt').write_text(STAMP)
        (deep / 'sub/msg.txt').write_text('Unstamped')
        (deep.parent / 'only-physical').mkdir()
        cases = (('cd alias && cd ..', self.cwd, 'pass'),
                 ('cd alias && cd sub', deep / 'sub', 'missing'),
                 ('cd alias/..', self.cwd, 'pass'),
                 ('cd alias && cd ../only-physical', None, 'unknown'),
                 ('cd alias && cd missing/../sub', None, 'unknown'))
        for prefix, folder, verdict in cases:
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix} && git commit -F msg.txt', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']), (folder and str(folder), verdict))

    def test_ansi_c_messages_require_exact_escape_decoding(self):
        cases = (
            (r"git commit -m $'Fix\tlabels\\\'\"\e\E\a\b\f\v\?\n\x56\u0065\U00000072ified: none'", 'pass'),
            (r"git commit --message=$'Caf\xC3\xA9\012\126erified: tests(ok)'", 'pass'),
            (r"git commit -qm$'Expansion \$HOME\t\rword only'", 'unknown'),
            (r"git commit -qm$'Preserve $HOME\t\rword only'", 'missing'),
            (r"git commit -qm$'Subject\rVerified: none'", 'unknown'),
            (r"git commit -m $'Msg\xA\x56erified: none'", 'pass'),
            (r"git commit -m $'Fix\nVerified: none\z'", 'unknown'),
            (r"git commit -m $'Fix\nVerified: none\cA'", 'unknown'),
            (r"git commit -m $'Fix\0\nVerified: none'", 'unknown'),
            (r"git commit -m $'Fix\xFF\nVerified: none'", 'unknown'),
            (r"git commit -m $'Fix\u00E9\nVerified: none'", 'unknown'),
        )
        for command, verdict in cases:
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command)[0], verdict)

    def test_cd_option_terminator_and_physical_resolution(self):
        deep = self.cwd / 'real/deep'
        deep.mkdir(parents=True)
        (self.cwd / 'alias').symlink_to(deep, target_is_directory=True)
        (self.cwd / '-target').mkdir()
        cases = (('cd -- -target', self.cwd / '-target'), ('cd -P alias && cd ..', deep.parent),
                 ('cd alias && cd -P ..', deep.parent), ('cd -P -- alias/..', deep.parent),
                 ('cd -L alias', None), ('cd -Pe alias', None), ('cd -- -', None),
                 ('cd -- +1', None), ('cd -P -target', None))
        for prefix, folder in cases:
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix} && git commit -m Title', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']),
                                 (str(folder), 'missing') if folder else (None, 'unknown'))

    def test_unquoted_nbsp_is_literal_but_shell_specific_paths_are_unknown(self):
        for name, folder_known in (('a\N{NO-BREAK SPACE}b', True), ('a!b', False), ('a^b', False), ('=git', False)):
            folder = self.cwd / name
            folder.mkdir()
            (folder / 'msg.txt').write_text(STAMP)
            with self.subTest(name=name):
                found = commit_gate.evaluate(f'cd {name} && git commit -F msg.txt', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']),
                                 (str(folder), 'pass') if folder_known else (None, 'unknown'))

    def test_double_quoted_arithmetic_and_joined_dollar_are_unknown(self):
        for command in ('git commit -m "$[2 + 3]"', 'git commit -m "$\\\nMSG"',
                        'git commit -m "$\\\n\\\n[4 * 2]"',
                        'git commit -m "Verified: none ($[2 + 3])"',
                        'git commit -m "Verified: none ($\\\nMSG)"'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), ('unknown', 'shell'))

    def test_double_quoted_zsh_parameter_modifiers_are_unknown(self):
        for modifier in ('^', '=', '~', '+', '#', '^^', '==', '~~', '++', '##', '^=~#'):
            for expression in (f'${modifier}X', f'${{{modifier}X}}'):
                with self.subTest(expression=expression):
                    self.assertEqual(self.verdict(f'git commit -m "Verified: none (a{expression})"'),
                                     ('unknown', 'shell'))
        for expression in ('$~X', '$\\\n~X', '$é'):
            with self.subTest(expression=expression):
                self.assertEqual(self.verdict(f'git commit -m "Verified: {expression}"'), ('unknown', 'shell'))
        for quoted in ("$'Verified: none ($~X)'", '"Verified: none (\\$~X)"'):
            with self.subTest(quoted=quoted):
                self.assertEqual(self.verdict(f'git commit -m {quoted}'), ('pass', ''))

    def test_cd_guard_and_builtin_keep_the_message_file_readable(self):
        folder = self.cwd / 'sub'
        (folder / 'msg.txt').write_text(STAMP)
        for prefix in ('cd sub || exit 1;', 'cd sub || return\n', 'cd sub ||\nexit\n',
                       'builtin cd sub &&', 'builtin cd -- sub || exit;'):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix} git commit -F msg.txt', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']), (str(folder), 'pass'))
        for prefix in ('cd sub || true;', 'cd sub || exit 1 | cat;', 'cd sub || exit >log;',
                       'cd sub || exit 1 &&', 'false || cd sub || exit 1;', 'cd missing || return;',
                       'builtin cd sub >log;', 'builtin cd sub &'):
            with self.subTest(prefix=prefix):
                found = commit_gate.evaluate(f'{prefix} git commit -F msg.txt', str(self.cwd))
                self.assertEqual((found['folder'], found['verdict']), (None, 'unknown'))
        with self.subTest(prefix='redirection alone'):
            found = commit_gate.evaluate('>sub/msg.txt; builtin cd ./sub && git commit -F msg.txt', str(self.cwd))
            self.assertEqual((found['folder'], found['verdict'], found['reason']), (str(folder), 'unknown', 'file'))

    def test_new_cd_forms_refuse_a_possible_cdpath_redirect(self):
        elsewhere = self.cwd / 'elsewhere'
        (elsewhere / 'sub').mkdir(parents=True)
        with mock.patch.dict(os.environ, {'CDPATH': str(elsewhere)}):
            for target in ('sub', './sub'):
                with self.subTest(target=target):
                    found = commit_gate.evaluate(f'cd -- {target} && git commit -m Title', str(self.cwd))
                    self.assertEqual(found['folder'], None if target == 'sub' else str(self.cwd / 'sub'))

    def test_earlier_commands_can_set_cdpath_before_a_bare_relative_cd(self):
        elsewhere = self.cwd / 'elsewhere'
        (elsewhere / 'sub').mkdir(parents=True)
        with mock.patch.dict(os.environ):
            os.environ.pop('CDPATH', None)
            for prefix in (f'CDPATH+={elsewhere}', f'printf -v CDPATH %s {elsewhere}',
                           f': ${{CDPATH:={elsewhere}}}', f'CDPATH={elsewhere}', ':'):
                for target in ('sub', './sub'):
                    with self.subTest(prefix=prefix, target=target):
                        found = commit_gate.evaluate(f'{prefix}; cd {target} && git commit -m Title', str(self.cwd))
                        expected = (None, 'unknown') if target == 'sub' else (str(self.cwd / 'sub'), 'missing')
                        self.assertEqual((found['folder'], found['verdict']), expected)
            found = commit_gate.evaluate(f'cd sub && git commit -m Title; CDPATH={elsewhere}', str(self.cwd))
            self.assertEqual((found['folder'], found['verdict']), (str(self.cwd / 'sub'), 'missing'))


class Check(Base):
    def test_prefixed_cd_does_not_probe_head_in_start_repository(self):
        commit_gate.set_global('enforce')
        commit_gate.install_hook(self.repo)
        (self.repo / 'sub').mkdir()
        with mock.patch.object(commit_gate, '_head', wraps=commit_gate._head) as head:
            self.assertEqual(self.pre('X=1 cd ./sub && git commit -m Title')[1], '')
        head.assert_not_called()
        row = self.rows()[-1]
        self.assertEqual((row['verdict'], row['reason'], row['folder']), ('unknown', 'folder', None))

    def test_cleanup_is_unknown_after_commands_that_can_change_config(self):
        commit = 'git commit -q --allow-empty -m "Msg\n\nVerified: none (x)"'
        write = r"printf '\n[commit]\n\tcleanup = strip\n[core]\n\tcommentChar = V\n' >> .git/config"
        for prefix in (write, '> .git/config', 'cp other.config .git/config', 'git add -A'):
            with self.subTest(prefix=prefix):
                result = commit_gate.evaluate(f'{prefix}; {commit}', str(self.repo))
                self.assertEqual((result['verdict'], result['reason']), ('unknown', 'cleanup'))
        malformed = commit.replace('none (x)', 'pending')
        result = commit_gate.evaluate(f'{write}; {malformed}', str(self.repo))
        self.assertEqual((result['verdict'], result['reason']), ('unknown', 'cleanup'))
        for command in (f'{commit}; {write}', f'cd . && {commit}'):
            with self.subTest(command=command):
                result = commit_gate.evaluate(command, str(self.repo))
                self.assertEqual((result['verdict'], result['reason']), ('pass', ''))

    def test_empty_comment_char_makes_cleanup_unknown(self):
        for cleanup in ('default', 'strip', 'verbatim'):
            with self.subTest(cleanup=cleanup):
                command = f'git -c core.commentChar= commit --cleanup={cleanup} -m Msg -m "{STAMP}"'
                result = commit_gate.evaluate(command, str(self.repo))
                self.assertEqual((result['verdict'], result['reason']), ('unknown', 'cleanup'))

    def test_cleanup_reads_the_commit_repository_and_command_overrides(self):
        sub = self.repo / 'sub'
        sub.mkdir()
        subject = f'-m Change -m "{STAMP}"'
        for repo_cleanup, option, comment, cleanup, verdict in (
                ('strip', '', 'V', '', 'unknown'),
                ('default', '', 'V', '', 'pass'),
                ('scissors', '', 'V', '', 'pass'),
                ('whitespace', '-c commit.cleanup=strip', 'V', '', 'unknown'),
                ('strip', '-c core.commentChar=V', '#', '', 'unknown'),
                ('strip', '-c core.commentChar=V -c core.commentChar=Q', '#', '', 'pass'),
                ('strip', '', 'V', '--cleanup=verbatim', 'pass'),
                ('strip', '-c commit.cleanup=strip', 'V', '--cleanup default', 'pass'),
                ('whitespace', '', 'V', '--cleanup=strip', 'unknown'),
                ('whitespace', '', 'V', '--cleanup strip', 'unknown'),
                ('strip', '', 'auto', '', 'pass'),
                ('strip', '', 'V', '--cleanup=unrecognized', 'unknown')):
            with self.subTest(repo_cleanup=repo_cleanup, option=option, comment=comment, cleanup=cleanup):
                self.git('config', 'commit.cleanup', repo_cleanup)
                self.git('config', 'core.commentChar', comment)
                command = f'git -C repo/sub {option} commit --allow-empty {subject} {cleanup}'
                result = commit_gate.evaluate(command, str(self.root))
                self.assertEqual((result['folder'], result['verdict']), (str(sub), verdict))
                if cleanup != '--cleanup=unrecognized':
                    subprocess.run(['bash', '-c', command], cwd=self.root, check=True, capture_output=True)
                    landed = stamp.message_status(self.git('log', '-1', '--format=%B'))
                    self.assertEqual(landed, 'verified' if verdict == 'pass' else 'missing')

    def test_cleanup_keeps_a_potential_comment_string_unknown(self):
        self.git('config', 'commit.cleanup', 'strip')
        for prefix, message in (('Verified: tests(', STAMP), ('Verified: pending', f'{STAMP}\nVerified: pending')):
            with self.subTest(prefix=prefix):
                self.git('config', 'core.commentString', prefix)
                self.assertEqual(self.pre(heredoc(f'Change\n\n{message}'))[1], '')
                self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))

    def test_cd_or_fallback_does_not_read_backup_message(self):
        backup = self.root / 'backup'
        backup.mkdir()
        (backup / 'commit.txt').write_text('Wrong message\n')
        (self.repo / 'commit.txt').write_text(f'Change\n\n{STAMP}\n')
        command = (f'cd {shlex.quote(str(self.repo))} || cd {shlex.quote(str(backup))}; '
                   'git commit --allow-empty -F commit.txt')
        commit_gate.set_global('enforce')
        with mock.patch.object(commit_gate, '_read_message_file', wraps=commit_gate._read_message_file) as read:
            self.assertEqual(self.pre(command)[1], '')
            read.assert_not_called()
        row = self.rows()[-1]
        self.assertEqual((row['verdict'], row['reason'], row['folder']), ('unknown', 'folder', None))
        subprocess.run(['bash', '-c', command], cwd=self.root, check=True, capture_output=True)
        self.assertTrue(self.git('log', '-1', '--format=%B').strip().endswith(STAMP))

    def test_git_c_and_cd_resolve_symlinks_before_parent_steps(self):
        (self.root / 'app').mkdir()
        (self.root / 'app/commit.txt').write_text('Wrong message\n')
        (self.root / 'commit.txt').write_text('Wrong message\n')
        (self.repo / 'app').mkdir()
        (self.root / 'link').symlink_to(self.repo / 'app', target_is_directory=True)
        # The shell follows cd .. by name, while git -C follows the real path.
        found = commit_gate.evaluate('cd link && cd .. && git commit --allow-empty -F commit.txt', str(self.root))
        self.assertEqual((found['verdict'], found['folder']), ('missing', str(self.root)))
        for command, folder in (('git -C link/../app commit --allow-empty -F commit.txt', self.repo / 'app'),
                                ('git -C link -C .. commit --allow-empty -F commit.txt', self.repo)):
            with self.subTest(command=command):
                (folder / 'commit.txt').write_text(f'Change\n\n{STAMP}\n')
                found = commit_gate.evaluate(command, str(self.root))
                self.assertEqual((found['verdict'], found['folder']), ('pass', str(folder)))
                subprocess.run(['bash', '-c', command], cwd=self.root, check=True, capture_output=True)
                self.assertTrue(self.git('log', '-1', '--format=%B').strip().endswith(STAMP))

    def test_observe_logs_and_never_blocks(self):
        self.assertEqual(self.run_commit('No stamp'), '')
        self.assertEqual(self.run_commit(f'Good\n\n{STAMP}'), '')
        rows = self.rows()
        self.assertEqual([(row['event'], row.get('verdict') or row.get('landed')) for row in rows],
                         [('check', 'would_block'), ('landed', 'missing'), ('check', 'pass'), ('landed', 'verified')])
        self.assertEqual((rows[0]['mode'], rows[0]['project'], rows[0]['session'], rows[0]['folder']),
                         ('observe', PID, 's1', str(self.repo)))
        self.assertNotIn('No stamp', paths.COMMIT_GATE_LOG.read_text())  # the log keeps no message text

    def test_off_checks_and_logs_nothing(self):
        commit_gate.set_global('off')
        self.assertEqual(self.run_commit('No stamp'), '')
        self.assertEqual(self.rows(), [])

    def test_enforce_blocks_twice_then_lets_the_third_through(self):
        commit_gate.set_global('enforce')
        first, second = self.run_commit('No stamp'), self.run_commit('No stamp')
        for out, text in ((first, 'block 1 of 2'), (second, 'block 2 of 2')):
            decision = json.loads(out)['hookSpecificOutput']
            self.assertEqual((decision['hookEventName'], decision['permissionDecision']), ('PreToolUse', 'deny'))
            self.assertIn(text, decision['permissionDecisionReason'])
            self.assertIn('Verified: none (reason)', decision['permissionDecisionReason'])
        self.assertIn('lets the next commit through', json.loads(second)['hookSpecificOutput']['permissionDecisionReason'])
        self.assertNotIn('lets the next commit through', json.loads(first)['hookSpecificOutput']['permissionDecisionReason'])
        self.assertEqual(self.run_commit('No stamp'), '')
        self.assertEqual(self.run_commit('No stamp'), '')  # no third denial in this session
        self.assertEqual([row.get('verdict') or row['landed'] for row in self.rows()],
                         ['block', 'block', 'let_through', 'missing', 'let_through', 'missing'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').strip(), '3')

    def test_verified_inside_a_line_is_not_a_stamp(self):
        commit_gate.set_global('enforce')
        for number, message in enumerate(('Document Verified: none', 'Explain the syntax Verified: tests(ok)',
                                          'Fix\n\nSee Verified: tests(ok) in the docs', 'Fix\n\n  Verified: tests(ok)',
                                          'Fix\n\nVerified: Verified: tests(ok)', 'Fix\n\nVerified: verified: none (docs)',
                                          'Fix\n\nVerified: tests(ok) Verified: none')):
            _, out = self.pre(heredoc(message), session=f'inline-{number}')
            self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny', message)
            _, out = self.pre(f'git commit -m "{message}"', session=f'inline-m-{number}')
            self.assertTrue(out, message)
        self.assertEqual(self.pre('git commit -m "Fix" -m "Verified: none (copy)"', session='line-start')[1], '')
        # The landed commit is judged the same way.
        data, _ = self.pre('git commit -m "$MSG"', session='landed-inline')
        self.commit_file('inline', 'Document Verified: none')
        commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1001)
        self.assertEqual(self.rows()[-1]['landed'], 'missing')

    def test_backslashes_inside_double_quotes_are_read(self):
        """The shell decides these escapes alone, so the gate reads the message in full. Checked against bash."""
        cases = (('git commit --allow-empty -m "Fix \\"quoted\\" labels"', 'missing'),
                 ('git commit --allow-empty -m "Preserve C:\\Users paths"', 'missing'),
                 ('git commit --allow-empty -m "Cost \\$5 and a \\`tick\\`"', 'missing'),
                 (f'git commit --allow-empty -m "Fix \\"it\\"" -m "{STAMP}"', 'pass'),
                 ('git commit --allow-empty -m "Fix" -m "\\Verified: none"', 'missing'),
                 ('git commit --allow-empty -m "Fix quoted \\\nlabels"', 'missing'),
                 ("git commit --allow-empty -m 'Describe paths \\\nin docs'", 'missing'),
                 ('git commit --allow-empty -m "Fix" -m "Verified: tests(a) \\\nreview(b)"', 'pass'),
                 ("git commit --allow-empty -m 'Fix' -m 'Verified: tests(a) \\\nreview(b)'", 'missing'),
                 ("git commit --allow-empty -m 'subject\\\nVerified: none'", 'pass'),
                 ('git commit --allow-empty -m "Fix\\\nIssue"', 'missing'),
                 ('git commit --allow-empty -q -a -m "Fix $ handling in the parser"', 'missing'),
                 ('git commit --allow-empty -m "Costs 5$"', 'missing'),
                 ('git commit --allow-empty -m "a $ b" -m "Verified: none ($ kept)"', 'pass'),
                 ('git commit --allow-empty -m Fix\\ it', 'missing'),
                 ('git commit --allow-empty -m Fix\\*\\ \\$HOME\\ \\#1 -m Verified:\\ none', 'pass'),
                 ('git commit --allow-empty -m Verified:\\ none\\;x', 'missing'))
        for command, verdict in cases:
            with self.subTest(command=command):
                self.assertEqual(commit_gate.evaluate(command, str(self.repo))['verdict'], verdict)
                subprocess.run(['bash', '-c', command], cwd=self.repo, check=True, capture_output=True)
                landed = stamp.message_status(self.git('log', '-1', '--format=%B'))
                self.assertEqual(landed != 'missing', verdict == 'pass')
        commit_gate.set_global('enforce')
        _, out = self.pre(cases[0][0], session='escapes')
        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_heredoc_message_attached_to_the_option_or_to_text(self):
        """Checked against bash: the message is the same wherever the substitution sits inside the double quotes."""
        body = "$(cat <<'EOF'\nDescribe the parser\n{}EOF\n)"
        for form in ('git commit --allow-empty -m"{}"', 'git commit --allow-empty --message="{}"',
                     'git commit --allow-empty -qm"{}"', 'git commit --allow-empty -m "Subject: {}"',
                     'git commit --allow-empty -m "{} (draft)"'):
            for extra in ('', f'\n{STAMP}\n'):
                command = form.format(body.format(extra))
                with self.subTest(command=command):
                    subprocess.run(['bash', '-c', command], cwd=self.repo, check=True, capture_output=True)
                    landed = stamp.message_status(self.git('log', '-1', '--format=%B'))
                    # Text after the stamp on its line ("... (draft)") makes it invalid, for git and for the gate.
                    self.assertEqual(landed != 'missing', bool(extra) and not form.endswith('(draft)"'))
                    self.assertEqual(commit_gate.evaluate(command, str(self.repo))['verdict'],
                                     'missing' if landed == 'missing' else 'pass')
        # A body the shell expands, or a substitution outside double quotes, stays unknown.
        for command in ('git commit -m"$(cat <<EOF\nFix $ISSUE\nEOF\n)"', "git commit -m$(cat <<'EOF'\nFix\nEOF\n)"):
            self.assertEqual(commit_gate.evaluate(command, str(self.repo))['verdict'], 'unknown', command)

    def test_heredoc_edge_forms_match_bash(self):
        """Delimiters with a dash or digits, blank lines before the closing parenthesis, and the newlines that the
        shell removes at the end of a command substitution."""
        forms = ("git commit --allow-empty -m \"$(cat <<'END-MESSAGE'\nDescribe\n{}END-MESSAGE\n)\"",
                 'git commit --allow-empty -m "$(cat <<"42"\nDescribe\n{}42\n)"',
                 "git commit --allow-empty -m \"$(cat <<'EOF'\nDescribe\n{}EOF\n\n)\"",
                 "git commit --allow-empty -m\"$(cat <<'EOF'\nDescribe\n{}\nEOF\n) later\"",
                 "git commit --allow-empty --message=\"$(cat <<'EOF'\nDescribe\n{}\n\nEOF\n)\"")
        for form in forms:
            for extra in ('', '\nVerified: none (draft)\n'):
                command = form.format(extra)
                with self.subTest(command=command):
                    subprocess.run(['bash', '-c', command], cwd=self.repo, check=True, capture_output=True)
                    landed = stamp.message_status(self.git('log', '-1', '--format=%B'))
                    self.assertEqual(commit_gate.evaluate(command, str(self.repo))['verdict'],
                                     'missing' if landed == 'missing' else 'pass')
        self.assertEqual(landed, 'none')  # the last form with the stamp is valid

    def test_cd_path_with_backslash_escapes_is_followed(self):
        commit_gate.set_global('enforce')
        work = self.root / 'review work (local)'
        work.mkdir()
        subprocess.run(['git', '-C', str(work), 'init', '-q'], check=True)
        accented = self.root / 'café review'
        accented.mkdir()
        subprocess.run(['git', '-C', str(accented), 'init', '-q'], check=True)
        data = self.pre('cd café\\ review && git commit -q -m "Update the index"', cwd=str(self.root), session='accent')
        self.assertEqual(json.loads(data[1])['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.rows()[-1]['folder'], str(accented))
        # Characters that a shell acts on outside quotes keep the word unread.
        for word in ('Fix!', 'Fix^2', '=Fix', 'a{b,c}', '~Fix', 'Fix#1', 'x?', 'Fix$'):
            self.assertEqual(commit_gate.evaluate(f'git commit -m {word}', str(self.root))['verdict'], 'unknown', word)
        for word in ('Fix=1', 'Fix:é', 'a+b,c%d@e'):
            self.assertEqual(commit_gate.evaluate(f'git commit -m {word}', str(self.root))['verdict'], 'missing', word)
        for number, target in enumerate(('review\\ work\\ \\(local\\)', "'review work (local)'")):
            data = self.pre(f'cd {target} && git add -A && git commit -q -m "Update the index"', cwd=str(self.root),
                            session=f'escaped-cd-{number}')
            self.assertEqual(json.loads(data[1])['hookSpecificOutput']['permissionDecision'], 'deny', target)
            self.assertEqual(self.rows()[-1]['folder'], str(work))

    def test_cd_to_the_home_folder_is_followed(self):
        with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
            for command in ('cd ~/repo && git add -A && git commit -m x', 'cd ~ && cd repo; git commit -m x'):
                found = commit_gate.evaluate(command, '/')
                self.assertEqual((found['verdict'], found['folder']), ('missing', str(self.repo)), command)
            for command in ('cd ~nobody/repo && git commit -m x', 'cd "~/repo" && git commit -m x'):
                self.assertIsNone(commit_gate.evaluate(command, '/')['folder'], command)

    def test_a_cd_that_may_not_run_is_not_followed(self):
        other = self.root / 'other'
        other.mkdir()
        for prefix in ('git diff --cached --stat | grep -q . && cd ../other; ', 'test -f x && cd ../other; ',
                       'cd ../other && printf ready > status.log & ', 'source ./goto-other.sh; ', '. ./goto.sh\n',
                       'export HOME=/alternate; cd ~/repo && ', 'HOME=/alternate\ncd ~ && ', 'eval "$GO"; '):
            with self.subTest(prefix=prefix):
                self.assertIsNone(commit_gate.evaluate(prefix + 'git commit -m Title', str(self.repo))['folder'])
        for command in ('export HOME=/alternate; git -C ~/repo commit -m Title',
                        'command cd ../other && git commit -m Title'):
            with self.subTest(command=command):
                self.assertIsNone(commit_gate.evaluate(command, str(self.repo))['folder'])
        # With a new HOME the gate does not read ~/file either.
        self.assertEqual(commit_gate.evaluate('HOME=/alternate git commit -F ~/msg.txt', str(self.repo))['verdict'], 'unknown')
        # A chain of plain cd commands is followed.
        found = commit_gate.evaluate('cd .. && cd other && git add -A && git commit -m Title', str(self.repo))
        self.assertEqual(found['folder'], str(other))

    def test_enforce_blocks_after_an_earlier_pipe(self):
        commit_gate.set_global('enforce')
        _, out = self.pre('git status --short | head -5; git add -A && git commit -m "No stamp"', session='piped')
        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.rows()[-1]['folder'], str(self.repo))

    def test_cd_into_a_linked_repository_is_followed(self):
        commit_gate.set_global('enforce')
        (self.root / 'repo link').symlink_to(self.repo, target_is_directory=True)
        data = self.pre("cd 'repo link' && git commit --allow-empty -m 'Refresh the index'", cwd=str(self.root))
        self.assertEqual(json.loads(data[1])['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.rows()[-1]['folder'], str(self.repo))
        # After a link, cd .. goes to the logical parent.
        found = commit_gate.evaluate("cd 'repo link' && cd .. && git commit -m x", str(self.root))
        self.assertEqual(found['folder'], str(self.root))

    def test_other_trailers_do_not_change_the_verdict(self):
        good = f'git commit -m "s\n\n{STAMP}" --trailer "Reviewed-by: A <a@example.com>"'
        self.assertEqual(commit_gate.evaluate(good, str(self.repo))['verdict'], 'pass')
        self.assertEqual(commit_gate.evaluate('git commit -m s --trailer "Reviewed-by: A"', str(self.repo))['verdict'],
                         'unknown')

    def test_enforce_blocks_the_usual_add_then_commit_form(self):
        commit_gate.set_global('enforce')
        for command in ('git add -A && git commit -m "No stamp"', f'cd {self.repo} && git add . && git commit -qm wip && git push'):
            _, out = self.pre(command, session=f's-{len(command)}')
            self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny', command)

    def test_block_count_is_per_session_and_a_good_commit_always_passes(self):
        commit_gate.set_global('enforce')
        self.assertTrue(self.run_commit('No stamp', session='s1'))
        self.assertTrue(self.run_commit('No stamp', session='s1'))
        self.assertTrue(self.run_commit('No stamp', session='s2'))
        self.assertEqual(self.run_commit(f'Good\n\nVerified: none (copy)', session='s2'), '')
        self.assertEqual(self.rows()[-1]['landed'], 'none')

    def test_enforce_never_blocks_what_it_cannot_read(self):
        commit_gate.set_global('enforce')
        for command in ('git commit -m "$MSG"', 'git commit --amend --no-edit', 'git commit -m "Fix'):
            self.assertEqual(self.pre(command)[1], '', command)
        self.assertEqual({row['verdict'] for row in self.rows()}, {'unknown'})

    def test_unstamped_heredoc_on_fd_10_does_not_block_a_stamped_commit(self):
        commit_gate.set_global('enforce')
        command = f'git commit -F - <<MSG 10<<OTHER\n{STAMP}\nMSG\nNo stamp\nOTHER'
        self.assertEqual(self.pre(command)[1], '')
        self.assertEqual(self.rows()[-1]['verdict'], 'pass')

    def test_invalid_utf8_message_files_are_unknown_in_enforce_mode(self):
        commit_gate.set_global('enforce')
        message = self.repo / 'msg.txt'
        for content in (b'Legacy\n\nVerified: none\xa0\n', b'Legacy: \xc3\n\nVerified: none\n'):
            message.write_bytes(content)
            for command in ('git commit -F msg.txt', 'git commit -F - < msg.txt'):
                with self.subTest(content=content, command=command):
                    self.assertEqual(self.pre(command)[1], '')
                    row = self.rows()[-1]
                    self.assertEqual((row['verdict'], row['reason']), ('unknown', 'file'))

    def test_utf8_message_files_still_use_the_stamp(self):
        commit_gate.set_global('enforce')
        message = self.repo / 'msg.txt'
        for ending, verdict in (('Verified: none (café)', 'pass'), ('Verified: café', 'block')):
            with self.subTest(ending=ending):
                message.write_text(f'Café\n\n{ending}\n', encoding='utf-8')
                _, output = self.pre('git commit -F msg.txt')
                self.assertEqual(bool(output), verdict == 'block')
                self.assertEqual(self.rows()[-1]['verdict'], verdict)

    def test_message_hooks_that_can_run_make_the_verdict_unknown(self):
        commit_gate.set_global('enforce')
        for name, mode, verdict, reason in (('commit-msg', 0o700, 'unknown', 'hook'),
                                            ('prepare-commit-msg', 0o700, 'unknown', 'hook'),
                                            ('commit-msg', 0o600, 'block', 'no-line'),
                                            ('commit-msg.sample', 0o700, 'block', 'no-line')):
            with self.subTest(name=name, mode=mode):
                hook = self.repo / '.git/hooks' / name
                self.addCleanup(hook.unlink, missing_ok=True)
                hook.write_text('#!/bin/sh\nexit 0\n')
                hook.chmod(mode)
                try:
                    _, out = self.pre('git commit -m x')
                    row = self.rows()[-1]
                    self.assertEqual((row['verdict'], row['reason']), (verdict, reason))
                    if verdict == 'block':
                        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny')
                    else:
                        self.assertEqual(out, '')
                finally:
                    hook.unlink()

    def test_hooks_path_keeps_trailing_whitespace(self):
        commit_gate.set_global('enforce')
        for suffix, name in ((' ', 'prepare-commit-msg'), ('\t', 'commit-msg'), ('\r', 'prepare-commit-msg'),
                             ('\n', 'commit-msg')):
            with self.subTest(suffix=suffix):
                hooks = self.repo / ('hooks' + suffix)
                hooks.mkdir()
                hook = hooks / name
                hook.write_text('#!/bin/sh\nexit 0\n')
                hook.chmod(0o700)
                self.git('config', 'core.hooksPath', str(hooks))
                self.assertEqual(self.pre('git commit -m x')[1], '')
                row = self.rows()[-1]
                self.assertEqual((row['verdict'], row['reason']), ('unknown', 'hook'))

    def test_failed_head_probe_never_denies(self):
        commit_gate.set_global('enforce')
        for tool in ('tool-1', None):
            with self.subTest(tool=tool), mock.patch.object(commit_gate, '_head', return_value=None) as head:
                self.assertEqual(self.pre('git commit -m x', tool_use_id=tool)[1], '')
                head.assert_called_once_with(str(self.repo))
                row = self.rows()[-1]
                self.assertEqual((row['verdict'], row['reason']), ('unknown', 'folder'))

    def test_failed_worktree_probe_creates_no_pending_entry(self):
        commit_gate.set_global('enforce')
        git = commit_gate._git

        def probe(folder, *args, **kwargs):
            if args == ('rev-parse', '--show-toplevel'):
                return subprocess.CompletedProcess([], 128)
            return git(folder, *args, **kwargs)

        with mock.patch.object(commit_gate, '_git', side_effect=probe):
            for command, verdict, reason in (('git commit -m Draft', 'unknown', 'folder'),
                                             (f'git commit -m Change -m "{STAMP}"', 'pass', '')):
                with self.subTest(verdict=verdict):
                    data, out = self.pre(command)
                    self.assertEqual(out, '')
                    commit_gate.landed(data, now=1001)
                    row = self.rows()[-1]
                    self.assertEqual((row['event'], row['verdict'], row['reason']), ('check', verdict, reason))
        self.assertFalse(commit_gate._session_path('s1').exists())

    def test_command_git_config_can_select_a_different_hook(self):
        commit_gate.set_global('enforce')
        hooks = self.repo / 'hooks'
        hooks.mkdir()
        hook = hooks / 'commit-msg'
        hook.write_text('#!/bin/sh\nprintf "\\n\\nVerified: none\\n" >> "$1"\n')
        hook.chmod(0o700)
        with mock.patch.dict(os.environ, {'TEST_HOOKS': 'hooks'}):
            for option in ('-c core.hooksPath=hooks', '--config-env=core.hooksPath=TEST_HOOKS'):
                with self.subTest(option=option):
                    self.assertEqual(self.pre(f'git {option} commit -m x')[1], '')
                    self.assertEqual(self.rows()[-1]['verdict'], 'unknown')
                    self.git(*option.split(), 'commit', '--allow-empty', '-qm', 'x')
                    self.assertIn('Verified: none', self.git('log', '-1', '--format=%B'))
        config = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': 'hooks'}
        prefix = ' '.join(f'{key}={value}' for key, value in config.items())
        self.assertEqual(self.pre(f'{prefix} git commit -m x')[1], '')
        self.assertEqual(self.rows()[-1]['verdict'], 'unknown')
        with mock.patch.dict(os.environ, config):
            self.git('commit', '--allow-empty', '-qm', 'x')
            self.assertIn('Verified: none', self.git('log', '-1', '--format=%B'))

    def test_git_cleanup_can_leave_an_earlier_valid_stamp(self):
        commit_gate.set_global('enforce')
        self.git('config', 'commit.cleanup', 'strip')
        for comment in ('# Verified: bogus', '# Verified:'):
            with self.subTest(comment=comment):
                message = f'subject\n\n{STAMP}\n{comment}'
                self.assertEqual(self.pre(heredoc(message))[1], '')
                # A comment line is not a stamp line ("Verified:" must start the line), so the valid line counts.
                self.assertEqual(self.rows()[-1]['verdict'], 'pass')
                self.commit_file(f'cleaned-{self.calls}', message)
                self.assertTrue(self.git('log', '-1', '--format=%B').strip().endswith(STAMP))
        # A valid line and a later malformed one: the last line decides, for the gate and for the landed commit.
        message = f'subject\n\n{STAMP}\nVerified: pending'
        self.assertTrue(self.pre(heredoc(message), session='last-line')[1])
        self.assertEqual(self.rows()[-1]['verdict'], 'block')
        self.assertTrue(self.pre('git commit -q -a -m "Fix" -m "Verified: tests(ok)" -m "Verified: pending"',
                                 session='last-line-m')[1])
        self.commit_file('last-line', message)
        self.assertEqual(stamp.message_status(self.git('log', '-1', '--format=%B')), 'missing')

    def test_a_merge_commit_counts_once(self):
        """The commits of the merged branch were made before: only the merge commit itself landed."""
        base = self.git('rev-parse', '--abbrev-ref', 'HEAD').strip()
        self.git('checkout', '-q', '-b', 'side')
        self.commit_file('side-1', 'Side work, no stamp')
        self.commit_file('side-2', 'More side work, no stamp')
        self.git('checkout', '-q', base)
        self.commit_file('main-1', f'Main work\n\n{STAMP}')
        self.git('merge', '-q', '--no-ff', '--no-commit', 'side')
        message = f'Merge side\n\n{STAMP}'
        data, out = self.pre(heredoc(message))
        self.assertEqual(out, '')
        self.git('commit', '-q', '-m', message)
        commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1001)
        self.assertEqual((self.rows()[-1]['landed'], self.rows()[-1]['count']), ('verified', 1))

    def test_a_message_hook_makes_a_pass_undecided(self):
        hook = self.repo / '.git/hooks/commit-msg'
        hook.write_text('#!/bin/sh\nprintf "Rewritten\\n" > "$1"\n')
        hook.chmod(0o755)
        self.assertEqual(self.run_commit(f'Good\n\n{STAMP}'), '')
        rows = self.rows()
        self.assertEqual((rows[-2]['verdict'], rows[-2]['reason'], rows[-1]['landed']), ('unknown', 'hook', 'missing'))
        self.assertEqual(commit_gate.stats(now=2000)['stamped'], 0)

    def test_unknown_project_never_denies(self):
        commit_gate.set_global('enforce')
        for pid, override in ((PID, False), (None, False), (PID, True)):
            with self.subTest(pid=pid, override=override):
                commit_gate.set_project(PID, override, 'off')
                self.lookup_project.return_value = (pid, False)
                self.assertEqual(self.pre('git commit -m x')[1], '')
                row = self.rows()[-1]
                self.assertEqual((row['project'], row['mode'], row['verdict']), (pid, 'enforce', 'would_block'))
                self.assertIs(row['project_unsure'], True)

    def test_enforce_does_not_count_without_a_session(self):
        commit_gate.set_global('enforce')
        for session in (None, '../x', ''):
            self.assertEqual(self.pre('git commit -m "No stamp"', session=session)[1], '')
        self.assertEqual([row['verdict'] for row in self.rows()], ['would_block'] * 3)
        self.assertEqual(list((paths.CACHE / 'commit-gate').glob('*.json')), [])

    def test_project_override_beats_global(self):
        commit_gate.set_global('enforce')
        commit_gate.set_project(PID, True, 'observe')
        self.assertEqual(self.run_commit('No stamp'), '')
        self.assertEqual(self.rows()[0]['verdict'], 'would_block')

    def test_subagent_is_marked(self):
        self.pre('git commit -m "x"', agent_id='agent-1')
        self.assertIs(self.rows()[0]['subagent'], True)

    def test_landed_needs_a_commit(self):
        data, _ = self.pre('git commit -m "No stamp"')
        commit_gate.landed(dict(data, hook_event_name='PostToolUseFailure'), now=1001)  # the commit failed
        self.assertEqual(self.rows()[-1]['landed'], 'no-commit')
        commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1002)  # a second result for one call
        self.assertEqual(len(self.rows()), 2)

    def test_landed_in_a_new_repository_and_with_two_commits(self):
        fresh = self.root / 'fresh'
        fresh.mkdir()
        subprocess.run(['git', '-C', str(fresh), 'init', '-q'], check=True)
        data, _ = self.pre(f'git commit -m "A\n\n{STAMP}"', cwd=str(fresh))
        (fresh / 'a').write_text('a')
        subprocess.run(['git', '-C', str(fresh), 'add', 'a'], check=True)
        subprocess.run(['git', '-C', str(fresh), 'commit', '-q', '-m', f'A\n\n{STAMP}'], check=True)
        commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1001)
        self.assertEqual((self.rows()[-1]['landed'], self.rows()[-1]['count']), ('verified', 1))
        data, _ = self.pre(f'git commit -m "B\n\n{STAMP}"')
        self.commit_file('b', f'B\n\n{STAMP}')
        self.commit_file('c', 'C')
        commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1002)
        self.assertEqual((self.rows()[-1]['landed'], self.rows()[-1]['count']), ('missing', 2))

    def test_no_result_when_the_folder_is_not_known(self):
        commit_gate.set_global('enforce')
        (self.repo / 'other').mkdir()
        for command in ('cd "$X" && git commit -m x', 'false || cd other; git commit -m Title'):
            with self.subTest(command=command):
                data, out = self.pre(command)
                self.assertEqual(out, '')
                commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1001)
                row = self.rows()[-1]
                self.assertEqual((row['event'], row['verdict'], row['reason'], row['folder']),
                                 ('check', 'unknown', 'folder', None))

    def test_cleanup_expires_only_zero_block_state(self):
        commit_gate.set_global('enforce')
        now = (commit_gate.STATE_DAYS + 2) * 86400
        for session, age, state in (('unused', commit_gate.STATE_DAYS + 1, '{"blocks": 0, "pending": {}}'),
                                    ('old', commit_gate.STATE_DAYS + 1, '{"blocks": 2, "pending": {}}'),
                                    ('corrupt', commit_gate.STATE_DAYS + 1, '{'),
                                    ('recent', 8, '{"blocks": 1, "pending": {}}')):
            modified = now - age * 86400
            path = commit_gate._session_path(session)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(state)
            os.utime(path, (modified, modified))
        self.assertTrue(self.pre('git commit -m x', session='new', now=now)[1])
        self.assertFalse(commit_gate._session_path('unused').exists())
        for session in ('old', 'corrupt', 'recent'):
            self.assertTrue(commit_gate._session_path(session).exists())
        self.assertEqual(self.pre('git commit -m x', session='old', now=now)[1], '')
        self.assertEqual(self.rows()[-1]['verdict'], 'let_through')
        _, out = self.pre('git commit -m x', session='recent', now=now)
        self.assertIn('block 2 of 2', json.loads(out)['hookSpecificOutput']['permissionDecisionReason'])

    def test_unreadable_session_state_raises(self):
        commit_gate.set_global('enforce')
        path = commit_gate._session_path('s1')
        read = Path.read_text

        def unreadable(source, *args, **kwargs):
            if source == path:
                raise PermissionError('unreadable state')
            return read(source, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', unreadable), self.assertRaises(PermissionError):
            self.pre('git commit -m x')
        self.assertEqual(self.rows(), [])

    def test_corrupt_session_state_raises(self):
        commit_gate.set_global('enforce')
        path = commit_gate._session_path('s1')
        path.parent.mkdir(parents=True)
        for content in ('{', '[]', '{"blocks": NaN, "pending": {}}', '{"blocks": true, "pending": {}}',
                        '{"blocks": 0, "pending": []}',
                        '{"blocks": 0, "pending": {"tool": {"at": "bad"}}}'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    self.pre('git commit -m x')
                self.assertEqual(path.read_text(), content)
        self.assertEqual(self.rows(), [])

    def test_check_times_out_when_another_descriptor_holds_the_lock(self):
        commit_gate.set_global('enforce')
        folder = paths.CACHE / 'commit-gate'
        folder.mkdir(parents=True)
        with (folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                self.pre('git commit -m x')
            self.assertLess(time.monotonic() - started, 1.0)

    def test_pending_keeps_only_the_newest_calls(self):
        tools = []
        for offset in range(commit_gate.PENDING_MAX + 2):
            data, _ = self.pre('git commit -m x', now=1000 + offset)
            tools.append(data['tool_use_id'])
        state = json.loads(commit_gate._session_path('s1').read_text())
        self.assertEqual(list(state['pending']), tools[2:])

    def test_big_log_is_rotated(self):
        paths.COMMIT_GATE_LOG.parent.mkdir(parents=True)
        paths.COMMIT_GATE_LOG.write_text('x' * (commit_gate.LOG_BYTES + 1))
        self.pre('git commit -m x')
        self.pre('git commit -m x')
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(paths.COMMIT_GATE_LOG.with_name('commit-gate.log.1').exists())


class ProjectLookup(Base):
    def test_folder_lookup_without_a_matching_pane_is_unsure(self):
        for output in ('', f'{self.root}\t{PID}\n'):
            with self.subTest(output=output):
                tmux = mock.Mock(return_value=subprocess.CompletedProcess([], 0, output, ''))
                self.assertEqual(self.real_project(str(self.repo), {}, tmux), (None, False))
        for code, output, error in ((0, f'{self.repo}\t\n', ''), (1, '', 'no server running')):
            tmux = mock.Mock(return_value=subprocess.CompletedProcess([], code, output, error))
            self.assertEqual(self.real_project(str(self.repo), {}, tmux), (None, True))

    def test_display_message_project_lookup(self):
        env = {'TMUX': 'present', 'TMUX_PANE': '%1'}
        for output, expected in ((PID + '\n', (PID, True)), ('\n', (None, True)), ('bad-id\n', (None, False))):
            with self.subTest(output=output):
                tmux = mock.Mock(return_value=subprocess.CompletedProcess([], 0, output, ''))
                self.assertEqual(self.real_project(str(self.repo), env, tmux), expected)
                tmux.assert_called_once_with('display-message', '-p', '-t', '%1', '#{@project}')

    def test_display_message_failure_or_timeout_is_unsure(self):
        env = {'TMUX': 'present', 'TMUX_PANE': '%1'}
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, '', 'cannot find pane'))
        timed_out = mock.Mock(side_effect=subprocess.TimeoutExpired('tmux', 0.4))
        for tmux in (failed, timed_out):
            self.assertEqual(self.real_project(str(self.repo), env, tmux), (None, False))

    def test_display_message_with_invalid_project_file_is_unsure(self):
        env = {'TMUX': 'present', 'TMUX_PANE': '%1'}
        tmux = mock.Mock(return_value=subprocess.CompletedProcess([], 0, PID, ''))
        path = commit_gate.project_path(PID)
        path.parent.mkdir(parents=True)
        path.write_text('not JSON')
        self.assertEqual(self.real_project(str(self.repo), env, tmux), (PID, False))

    def test_folder_lookup_with_conflicting_or_invalid_project_is_unsure(self):
        for projects in ((PID, ''), ('bad-id',), (PID, '0123456789ab')):
            with self.subTest(projects=projects):
                output = ''.join(f'{self.repo}\t{pid}\n' for pid in projects)
                tmux = mock.Mock(return_value=subprocess.CompletedProcess([], 0, output, ''))
                self.assertEqual(self.real_project(str(self.repo), {}, tmux), (None, False))


class Stats(Base):
    def test_empty_log(self):
        self.assertEqual(commit_gate.stats(now=1000), {
            'days': 7, 'commits': 0, 'passed': 0, 'would_block': 0, 'blocked': 0, 'let_through': 0, 'unknown': 0,
            'stamped': 0, 'unstamped': 0, 'false_blocks': 0, 'rate': None})

    def test_counts_and_false_block_rate(self):
        self.run_commit(f'Good 1\n\n{STAMP}')
        self.run_commit(f'Good 2\n\n{STAMP}')
        self.run_commit('No stamp')
        # A verdict that the landed commit proves wrong: the gate saw no line, the commit has one.
        data, _ = self.pre('git commit -m "No stamp here"')
        self.commit_file('surprise', f'Surprise\n\n{STAMP}')
        commit_gate.landed(dict(data, hook_event_name='PostToolUse'), now=1001)
        self.pre('git commit --amend --no-edit')
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['commits'], stats['passed'], stats['would_block'], stats['unknown']), (5, 2, 2, 1))
        self.assertEqual((stats['stamped'], stats['unstamped'], stats['false_blocks'], stats['rate']), (3, 1, 1, 0.3333))
        self.assertEqual(commit_gate.stats(PID, now=2000), stats)
        self.assertEqual(commit_gate.stats('0123456789ab', now=2000)['commits'], 0)
        self.assertEqual(commit_gate.stats(now=1000 + 8 * 86400)['commits'], 0)  # older than 7 days

    def test_multi_commit_command_is_excluded_from_the_rate(self):
        data, _ = self.pre('git commit -m x && git commit -m y')
        self.commit_file('one', f'Good\n\n{STAMP}')
        commit_gate.landed(data, now=1001)
        check, result = self.rows()
        self.assertEqual((check['verdict'], check['count'], result['landed']), ('would_block', 2, 'verified'))
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['would_block'], stats['stamped'], stats['false_blocks']), (1, 0, 0))

    def test_multi_commit_result_is_excluded_from_the_rate(self):
        data, _ = self.pre('git commit -m x')
        for name in ('one', 'two'):
            self.commit_file(name, f'Good\n\n{STAMP}')
        commit_gate.landed(data, now=1001)
        check, result = self.rows()
        self.assertEqual((check['verdict'], check['count']), ('would_block', 1))
        self.assertEqual((result['landed'], result['count']), ('verified', 2))
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['stamped'], stats['false_blocks'], stats['rate']), (0, 0, None))

    def test_overlapping_calls_in_one_session_have_shared_results_and_are_excluded_from_the_rate(self):
        first, _ = self.pre('git commit -m x')
        second, _ = self.pre('git commit -m y')
        self.commit_file('shared', f'Shared\n\n{STAMP}')
        for data in (first, second):
            commit_gate.landed(data, now=1001)
            row = self.rows()[-1]
            self.assertEqual((row['id'], row['landed'], row['count'], row['shared']),
                             (data['tool_use_id'], 'verified', 1, True))
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['would_block'], stats['stamped'], stats['false_blocks'], stats['rate']), (2, 0, 0, None))

    def test_overlapping_calls_from_worktree_subdirectory_are_shared(self):
        sub = self.repo / 'ui'
        sub.mkdir()
        self.assert_shared_worktree_results(sub)

    def test_overlapping_calls_through_symlink_to_worktree_are_shared(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assert_shared_worktree_results(alias)

    def assert_shared_worktree_results(self, second_folder):
        draft = 'git commit -m Draft -- missing.txt'
        change = "git commit --allow-empty -m Change -m 'Verified: tests(ok)'"
        first, _ = self.pre(draft)
        second, _ = self.pre(change, cwd=str(second_folder))
        pending = json.loads(commit_gate._session_path('s1').read_text())['pending']
        for data in (first, second):
            self.assertEqual(pending[data['tool_use_id']]['tree'], str(self.repo))
        failed = subprocess.run(shlex.split(draft), cwd=self.repo, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        subprocess.run(shlex.split(change), cwd=second_folder, check=True, capture_output=True)
        for data, event in ((first, 'PostToolUseFailure'), (second, 'PostToolUse')):
            commit_gate.landed(dict(data, hook_event_name=event), now=1001)
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['stamped'], stats['false_blocks']), (0, 0))
        for row, data in zip(self.rows()[-2:], (first, second), strict=True):
            self.assertEqual(row['id'], data['tool_use_id'])
            self.assertEqual((row['event'], row['landed'], row['count'], row['shared']), ('landed', 'verified', 1, True))

    def test_pending_call_without_worktree_identity_stays_out_of_the_rate(self):
        data, _ = self.pre('git commit -m Draft')
        path = commit_gate._session_path('s1')
        state = json.loads(path.read_text())
        del state['pending'][data['tool_use_id']]['tree']
        path.write_text(json.dumps(state))
        self.commit_file('change', f'Change\n\n{STAMP}')
        commit_gate.landed(data, now=1001)
        self.assertIs(self.rows()[-1]['shared'], True)
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['stamped'], stats['false_blocks']), (0, 0))

    def test_check_during_git_read_marks_both_pending_calls_shared(self):
        read = commit_gate._landed
        for message, verdict in ((f'Stamped\n\n{STAMP}', 'verified'), ('Unstamped', 'missing')):
            with self.subTest(verdict=verdict):
                first, _ = self.pre('git commit -m x')
                self.commit_file(f'first-{self.calls}', message)
                second = None

                def overlap(folder, head):
                    nonlocal second
                    second, _ = self.pre('git commit -m y', now=1001)
                    return read(folder, head)

                with mock.patch.object(commit_gate, '_landed', side_effect=overlap):
                    commit_gate.landed(first, now=1001)
                row = self.rows()[-1]
                self.assertEqual((row['id'], row['landed'], row['shared']), (first['tool_use_id'], verdict, True))
                self.commit_file(f'second-{self.calls}', message)
                commit_gate.landed(second, now=1002)
                self.assertIs(self.rows()[-1]['shared'], True)
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['would_block'], stats['stamped'], stats['unstamped'], stats['rate']), (4, 0, 0, None))

    def test_rows_read_both_logs_under_one_lock_and_parse_after_release(self):
        events = []
        parse = json.loads
        current = paths.COMMIT_GATE_LOG
        rotated = current.with_name(current.name + '.1')
        lock = mock.MagicMock()
        lock.__enter__.side_effect = lambda: events.append('lock')
        lock.__exit__.side_effect = lambda *args: events.append('unlock')

        def read(source):
            events.append(source)
            return b'{"at": 1000}\n'

        def load(line):
            events.append('parse')
            return parse(line)

        with mock.patch.object(Path, 'read_bytes', read), mock.patch.object(json, 'loads', side_effect=load), \
                mock.patch.object(commit_gate, '_Lock', return_value=lock):
            self.assertEqual(list(commit_gate._rows()), [{'at': 1000}] * 2)
            self.assertEqual(events, ['lock', rotated, current, 'unlock', 'parse', 'parse'])

    def test_stats_read_both_log_generations_if_rotation_interrupts_an_unlocked_read(self):
        read = Path.read_bytes

        def record(verdict):
            return {'at': 1000, 'event': 'check', 'via': 'commit-msg', 'verdict': verdict}

        for has_rotated in (True, False):
            with self.subTest(has_rotated=has_rotated):
                current = self.root / f'gate-{has_rotated}.log'
                rotated = current.with_name(current.name + '.1')
                if has_rotated:
                    rotated.write_text(json.dumps(record('unknown')) + '\n')
                current.write_text(''.join(json.dumps(record(verdict)) + '\n' for verdict in ('pass', 'would_block')))
                did_rotate = False

                def read_then_rotate(source):
                    nonlocal did_rotate
                    try:
                        return read(source)
                    finally:
                        if source == rotated and not did_rotate:
                            did_rotate = True
                            commit_gate._log(record('pass'), locked=True)

                with mock.patch.object(paths, 'COMMIT_GATE_LOG', current), \
                        mock.patch.object(commit_gate, 'LOG_BYTES', 0), \
                        mock.patch.object(commit_gate._Lock, '__enter__', side_effect=TimeoutError), \
                        mock.patch.object(Path, 'read_bytes', read_then_rotate):
                    stats = commit_gate.stats(now=1000)
                self.assertTrue(did_rotate)
                self.assertEqual((stats['commits'], stats['passed'], stats['would_block'], stats['unknown']),
                                 (3, 2, 1, 0))

    def test_stats_are_unavailable_if_the_log_keeps_rotating(self):
        read = Path.read_bytes
        current = paths.COMMIT_GATE_LOG
        rotated = current.with_name(current.name + '.1')
        row = {'at': time.time(), 'event': 'check', 'via': 'commit-msg', 'verdict': 'pass'}
        rotations = 0
        with mock.patch.object(commit_gate, 'LOG_BYTES', 0):
            commit_gate._log(row)
            commit_gate._log(row)

            def read_then_rotate(source):
                nonlocal rotations
                content = read(source)
                if source == rotated:
                    commit_gate._log(row, locked=True)
                    rotations += 1
                return content

            with mock.patch.object(commit_gate._Lock, '__enter__', side_effect=TimeoutError), \
                    mock.patch.object(Path, 'read_bytes', read_then_rotate):
                self.assertIsNone(commit_gate.state()['stats'])

        self.assertEqual(rotations, 3)
        self.assertEqual(commit_gate.state()['stats']['commits'], 2)

    def test_unknown_with_a_stamped_result_is_excluded_from_the_rate(self):
        data, _ = self.pre('git commit -m "$MSG"')
        self.commit_file('expanded', f'Expanded\n\n{STAMP}')
        commit_gate.landed(data, now=1001)
        self.assertEqual(self.rows()[-1]['landed'], 'verified')
        stats = commit_gate.stats(now=2000)
        self.assertEqual((stats['unknown'], stats['stamped'], stats['false_blocks'], stats['rate']), (1, 0, 0, None))

    def test_bad_log_lines_are_skipped(self):
        self.run_commit('No stamp')
        with open(paths.COMMIT_GATE_LOG, 'a') as stream:
            stream.write('not json\n[]\n{"at": "x"}\n{"at": 1000, "event": "check", "verdict": "new-kind"}\n')
        self.assertEqual(commit_gate.stats(now=1000)['commits'], 1)


class Http(Base):
    def test_get_and_post(self):
        handler = Handler(commit_gate.PATH)
        commit_gate.get(handler)
        self.assertEqual((handler.status, handler.body['global'], handler.body['mode'], handler.body['project']),
                         (200, 'observe', 'observe', None))
        self.assertEqual([item['mode'] for item in handler.body['modes']], ['off', 'observe', 'enforce'])
        self.assertEqual((handler.body['stats']['commits'], handler.body['max_blocks']), (0, 2))
        handler = Handler(commit_gate.PATH, {'mode': 'enforce'})
        commit_gate.post(handler)
        self.assertEqual((handler.status, handler.body['global']), (200, 'enforce'))
        handler = Handler(commit_gate.PATH, {'project': PID, 'override': True, 'mode': 'off'})
        commit_gate.post(handler)
        self.assertEqual((handler.status, handler.body['mode'], handler.body['source']), (200, 'off', 'project'))
        handler = Handler(f'{commit_gate.PATH}?project={PID}')
        commit_gate.get(handler)
        self.assertEqual(handler.body['project'], {'override': True, 'mode': 'off'})

    def test_bad_requests(self):
        for path, body, status in ((commit_gate.PATH, {'mode': 'strict'}, 400), (commit_gate.PATH, {}, 400),
                                   (commit_gate.PATH, [], 400), (commit_gate.PATH, None, 413),
                                   (commit_gate.PATH, {'project': 'nope', 'override': True, 'mode': 'off'}, 400),
                                   (commit_gate.PATH, {'project': PID, 'mode': 'off'}, 400)):
            handler = Handler(path, body)
            commit_gate.post(handler)
            self.assertEqual(handler.status, status, body)
        handler = Handler(f'{commit_gate.PATH}?project=nope')
        commit_gate.get(handler)
        self.assertEqual(handler.status, 400)
        self.assertTrue(commit_gate.handles(f'{commit_gate.PATH}?project={PID}'))
        self.assertFalse(commit_gate.handles(commit_gate.PATH + '/x'))

    def test_post_write_failure_returns_json_500(self):
        for body in ({'mode': 'enforce'}, {'project': PID, 'override': True, 'mode': 'off'}):
            with self.subTest(body=body), mock.patch.object(commit_gate, '_write', side_effect=OSError('disk full')):
                handler = Handler(commit_gate.PATH, body)
                commit_gate.post(handler)
                self.assertEqual(handler.status, 500)
                self.assertEqual(handler.body, {'error': 'Tether could not save the commit gate mode.'})


class HookProcess(Base):
    """claude_hooks.py as Claude Code runs it: one process per event, JSON on stdin."""

    def setUp(self):
        super().setUp()
        self.tmux = self.root / 'tmux'
        self.tmux.write_text('#!/bin/sh\nprintf "no server running\\n" >&2\nexit 1\n')
        self.tmux.chmod(0o700)

    def hook(self, data, agent=()):
        env = {**os.environ, 'MOBILE_TMUX_HOME': str(self.root / 'home'), 'MOBILE_TMUX_TMUX': str(self.tmux),
               'MOBILE_TMUX_CACHE': str(self.root / 'home/.cache/mobile-tmux')}
        for name in ('TMUX', 'TMUX_PANE', 'MOBILE_TMUX_CONFIG', 'TETHER_HOME', 'TETHER_COMMIT_GATE_LOG'):
            env.pop(name, None)
        script = Path(commit_gate.__file__).with_name('claude_hooks.py')
        return subprocess.run([sys.executable, str(script), *agent], input=json.dumps(data), env=env,
                              capture_output=True, text=True, timeout=20)

    def data(self, command, event='PreToolUse'):
        return {'hook_event_name': event, 'tool_name': 'Bash', 'session_id': 'abc123', 'cwd': str(self.repo),
                'tool_use_id': 'tool-1', 'tool_input': {'command': command}}

    def log(self):
        path = self.root / 'home/.cache/tether/commit-gate.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_observe_by_default_then_enforce(self):
        ran = self.hook(self.data('git commit -m "No stamp"'))
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (0, '', ''))
        self.commit_file('x', 'No stamp')
        self.hook(self.data('git commit -m "No stamp"', 'PostToolUse'))
        self.assertEqual([row.get('verdict') or row['landed'] for row in self.log()], ['would_block', 'missing'])
        config = self.root / 'home/.config/mobile-tmux'
        config.mkdir(parents=True)
        (config / 'commit-gate').write_text('enforce\n')
        ran = self.hook(self.data('git commit -m "No stamp"'))
        self.assertEqual(json.loads(ran.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.hook(self.data(f'git commit -m "Good\n\n{STAMP}"')).stdout, '')
        # The live state capture still ran for each event.
        self.assertTrue(any((self.root / 'home/.cache/mobile-tmux').rglob('*abc123*')))

    def test_other_commands_and_codex_are_not_checked(self):
        (self.root / 'home/.config/mobile-tmux').mkdir(parents=True)
        (self.root / 'home/.config/mobile-tmux/commit-gate').write_text('enforce\n')
        self.assertEqual(self.hook(self.data('git status && ls')).stdout, '')
        self.assertEqual(self.hook(self.data('git commit -m "No stamp"'), agent=('codex',)).stdout, '')
        self.assertEqual(self.hook(dict(self.data('git commit -m "No stamp"'), tool_name='Read')).stdout, '')
        self.assertEqual(self.log(), [])

    def test_a_broken_gate_lets_the_commit_through(self):
        config = self.root / 'home/.config/mobile-tmux'
        config.mkdir(parents=True)
        (config / 'commit-gate').write_text('enforce\n')
        log = self.root / 'home/.cache/tether'
        log.parent.mkdir(parents=True)
        log.write_text('a file where the log folder must be')
        ran = self.hook(self.data('git commit -m "No stamp"'))
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (0, '', ''))

    def test_claude_hook_prints_nothing_when_session_state_cannot_be_read(self):
        config = self.root / 'home/.config/mobile-tmux'
        config.mkdir(parents=True)
        (config / 'commit-gate').write_text('enforce\n')
        session = self.root / 'home/.cache/mobile-tmux/commit-gate/abc123.json'
        session.parent.mkdir(parents=True)
        session.write_text('{')
        ran = self.hook(self.data('git commit -m x'))
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (0, '', ''))
        self.assertEqual(session.read_text(), '{')
        session.unlink()
        session.mkdir()  # open raises IsADirectoryError even when this test runs as root.
        ran = self.hook(self.data('git commit -m x'))
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (0, '', ''))
        self.assertEqual(self.log(), [])

    def test_a_busy_gate_lock_lets_the_commit_through(self):
        config = self.root / 'home/.config/mobile-tmux'
        config.mkdir(parents=True)
        (config / 'commit-gate').write_text('enforce\n')
        lock_path = self.root / 'home/.cache/mobile-tmux/commit-gate/.lock'
        lock_path.parent.mkdir(parents=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ran = self.hook(self.data('git commit -m x'))
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (0, '', ''))
        self.assertEqual(self.log(), [])
        ran = self.hook(self.data('git commit -m x'))
        self.assertEqual(json.loads(ran.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')


if __name__ == '__main__':
    unittest.main()
