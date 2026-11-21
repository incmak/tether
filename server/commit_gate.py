#!/usr/bin/env python3
"""Commit gate: does a commit message carry a valid "Verified:" line (stamp.py)?

The person sets the mode in Settings. Nothing else changes it.
  off      no check, no log.
  observe  the default. Each commit is checked and logged; none is blocked.
  enforce  a commit with no valid line is denied, with the reason. Agents with a session get two denials.
           The third such commit passes and is logged. Agents without a session key also pass and are logged.

  paths.CONFIG/commit-gate                  the global mode: one word. Missing or bad: observe.
  paths.TETHER/projects/<id>/commit-gate    a project override: {"override": true, "mode": "enforce"}.
  paths.COMMIT_GATE_LOG                     one JSON line per check and per landed commit. No message text.
  paths.CACHE/commit-gate/<session>.json    denial count and pending commits.

The installed Git commit-msg hook checks the final message file for Claude, Codex and terminal commits.
It logs one verdict and skips merges, replays that invoke the hook, and fixup, squash and amend! commits.
The hook checks ordinary amendments. Claude's PreToolUse check can defer to the hook.
PostToolUse matches each hook row to one landed commit by worktree, tree, all parents, message HMAC and dates.
A merge skip row can lack the author date. PostToolUse checks uncovered commits and skips new merges.

Otherwise claude_hooks.py calls hook() for Bash commit commands and their PostToolUse results.
The gate reads the command, never the screen. It denies only when it read the whole message from the command, that
message has no valid line, and nothing can add one later. A message it cannot read for certain passes with the
verdict "unknown": a shell variable, --amend --no-edit, an editor, --trailer, an option the gate does not know, a
shell comment, a commit hook in the repository. Any error, a busy lock, a failed project lookup and a hook timeout
let the commit through.

After the command, the gate reads the commits that landed. A false block is a "would block" or "let through" verdict
whose commit has a valid line. The rate in Settings is false blocks / landed commits with a valid line, over the
calls where git can say which commit the call made (see stats()). Two sessions that commit in one working tree at
the same moment can still mix their results; sessions use their own worktrees.
Known limit: the row of an attempt that failed after the hook can cover a later silent commit in the same worktree.
That commit must copy the tree, parents, message and author date, and carry a hand-set committer date inside the
attempt's process lifetime (Linux dates a process from its creation, which can precede an exec of git).
The gate assumes one HEAD-moving git operation per deferred call, so several HEAD moves in one call (for example,
amend, reset and re-merge) can still mix."""
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import shlex
import signal
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from functools import cache
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import paths
    import stamp
except Exception:
    if __name__ == '__main__' and sys.argv[1:2] == ['commit-msg']:
        sys.exit(0)
    raise

MODES = (
    ('off', 'Off', 'Commits are not checked.'),
    ('observe', 'Observe', 'Each commit is checked and logged. No commit is blocked.'),
    ('enforce', 'Enforce', 'A commit with no valid "Verified:" line is blocked. '
                           'An agent with a session gets 2 blocks per session. The next one passes. '
                           'An agent without a session passes.'),
)
NAMES = tuple(mode for mode, _, _ in MODES)
DEFAULT = 'observe'
MAX_BLOCKS = 2  # denials in one session; the next commit with no line passes
DAYS = 7
LOG_BYTES = 2_000_000  # the log moves to commit-gate.log.1 above this size
LOCK_SECONDS = 0.3
HOOK_LOCK_SECONDS = 5
PENDING_SECONDS = 3600  # a commit command that has not ended after this long gets no result
PENDING_MAX = 50
LOG_TIME_TOLERANCE = 0.001  # allow for log timestamps rounded to milliseconds
REFLOG_BYTES = 256 * 1024
CLAIM_SECONDS = 86400  # keep hook row claims for one day
STATE_DAYS = 45  # cleanup age for sessions with no blocks
PROJECT_ID = re.compile(r'[0-9a-f]{12}')
SESSION = re.compile(r'[a-zA-Z0-9-]{1,80}')
HOOK_MARK = '# tether-commit-gate-hook v1'
CALL_MARK = '# tether-commit-gate-call v1'
HOOK_MESSAGE_BYTES = 1_000_000
FIXUP_PREFIXES = ('fixup! ', 'squash! ', 'amend! ')
SCISSORS = re.compile(r'. -{24} >8 -{24}')
GIT_OID = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')


class GateError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---- the mode files ----

def global_path():
    return paths.CONFIG / 'commit-gate'


def project_path(pid):
    if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
        raise GateError(400, 'Choose a project.')
    return paths.TETHER / 'projects' / pid / 'commit-gate'


def read_global():
    try:
        text = global_path().read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return DEFAULT
    return text if text in NAMES else DEFAULT


def read_project(pid):
    """{'override': bool, 'mode': str}, or None when the project has no usable file."""
    try:
        data = json.loads(project_path(pid).read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError, GateError):
        return None
    if not isinstance(data, dict) or data.get('mode') not in NAMES or not isinstance(data.get('override'), bool):
        return None
    return {'override': data['override'], 'mode': data['mode']}


def _write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_global(mode):
    if mode not in NAMES:
        raise GateError(400, 'Choose off, observe or enforce.')
    _write(global_path(), f'{mode}\n')


def set_project(pid, override, mode):
    if not isinstance(override, bool) or mode not in NAMES:
        raise GateError(400, 'Choose off, observe or enforce.')
    _write(project_path(pid), json.dumps({'override': override, 'mode': mode}) + '\n')


def resolve(pid=None):
    """The mode that applies, and where it comes from. An override that is on beats the global mode."""
    base = read_global()
    project = read_project(pid) if pid else None
    if project and project['override']:
        return {'mode': project['mode'], 'source': 'project', 'global': base, 'project': project}
    return {'mode': base, 'source': 'global', 'global': base, 'project': project}


# ---- read `git commit` from a shell command ----
#
# The rule: 'missing' (the only verdict that can block) needs a command the gate reads completely. Anything else that
# can change the message, or whether the commit runs, gives 'unknown': a shell comment, a variable, an option the
# gate does not know, --trailer, --edit, a message file that the command can write, a commit hook in the repository.

PUNCTUATION = '();<>|&\n'
# Outside quotes, these characters make a shell (bash or zsh) do something: expansion, a pattern, history, a comment.
# Every other character is itself, also a letter such as é. `=` is special only at the start of a word (zsh).
SHELL_SPECIAL = set('$`*?[]{}~!#^')
LINE_JOIN = r'(?:\\\n)*'
# zsh also accepts non-ASCII names and the unbraced modifiers ^, =, ~, + and #.
DOUBLE_EXPANSION = re.compile(rf'{LINE_JOIN}[\w{{(\[!#?*@$^=~+%-]')
ANSI_QUOTE = re.compile(r"\$'((?:[^'\\]|\\[\s\S])*)'")
ANSI_ESCAPE = re.compile(rb'\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|.)', re.S)
ANSI_BYTES = dict(zip(b"""abefnrtvE\\'"?""", b"""\a\b\x1b\f\n\r\t\v\x1b\\'"?"""))
REDIRECTION = re.compile(rf'(?P<fd>(?:[0-9]{LINE_JOIN})*)'
                         rf'(?P<op><{LINE_JOIN}<{LINE_JOIN}<|>{LINE_JOIN}[>&]|<{LINE_JOIN}&|[<>])'
                         rf'|&{LINE_JOIN}>{LINE_JOIN}>?')
COMPOUND_OPERATOR = re.compile(rf'&{LINE_JOIN}&|\|{LINE_JOIN}[|&]|;{LINE_JOIN}[;&]')
FOLDER_SEPARATORS = {None, '&&', ';', '\n'}
HEREDOC_MARK = '@@TETHER-HEREDOC-{}@@'
HEREDOC_FOUND = re.compile(r'@@TETHER-HEREDOC-(\d+)@@')
# "$(cat <<'EOF' ... EOF)": the form Claude Code uses for a commit message.
CAT_HEREDOC = re.compile(
    rf'''\$\(\s*cat[ \t]+<{LINE_JOIN}<(-)?[ \t]*(['"]?)([A-Za-z0-9_][A-Za-z0-9_.-]*)\2[ \t]*\n'''
    r'''(?:(.*?)\n)?(?(1)\t*)\3\n\s*\)''', re.S)
HEREDOC = re.compile(
    rf'''<{LINE_JOIN}<(-)?[ \t]*(['"]?)([A-Za-z0-9_][A-Za-z0-9_.-]*)\2(?=[ \t\n;&|()<>]|$)([^\n]*)\n'''
    r'''(?:(.*?)\n)?(?(1)\t*)\3(?=\n|$)''', re.S)
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=.*', re.S)
ASSIGNMENT_LIKE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)(?:\[.*\])?\+?=.*', re.S)
REDIRECTION_VARIABLE = re.compile(r'\{[A-Za-z_][A-Za-z0-9_]*\}')
WRAPPERS = {'command', 'env', 'time', 'nohup'}
COMMAND_WRAPPERS = {'command', 'builtin', 'time', 'noglob', 'nocorrect', 'exec', 'nohup'}
KEYWORDS = set('if then else elif fi for while until do done case esac function select time coproc ! [[ ]]'.split())
KEYWORD_PREFIXES = {'if', 'then', 'else', 'elif', 'while', 'until', 'do', 'time', '!', '{'}
MOVING_WORDS = {'cd', 'pushd', 'popd', 'source', '.', 'eval', 'builtin'}
SHELL_COMMANDS = {'eval', 'source', '.', 'exec', 'xargs', 'sh', 'bash', 'zsh'}
GIT_VALUE_OPTIONS = {'-c', '--namespace', '--super-prefix', '--config-env', '--exec-path'}
GIT_PLACE_OPTIONS = {'--git-dir', '--work-tree'}  # they move the repository: the folder is then not known
CLEANUP_CONFIG = ('commit.cleanup', 'core.commentchar', 'core.commentstring')
CLEANUP_PATTERN = '^(' + '|'.join(re.escape(key) for key in CLEANUP_CONFIG) + ')$'
GIT_CONFIG_ENV = re.compile(r'GIT_CONFIG.*|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|HOME|XDG_CONFIG_HOME')
# `git commit` options that cannot change the message. An option that is not listed here makes the verdict unknown.
COMMIT_VALUE_OPTIONS = {'--author', '--date', '--pathspec-from-file'}
COMMIT_FLAGS = {'--all', '--patch', '--signoff', '--no-signoff', '--no-verify', '--verify', '--allow-empty',
                '--allow-empty-message', '--quiet', '--verbose', '--amend', '--no-edit', '--no-gpg-sign', '--gpg-sign',
                '--include', '--only', '--no-post-rewrite', '--status', '--no-status', '--reset-author',
                '--untracked-files', '--pathspec-file-nul'}
SHORT_FLAGS = set('apsvqnzio')  # single letters of `git commit` that take no value and leave the message alone
FILE_BYTES = 65536
MESSAGE_HOOKS = ('prepare-commit-msg', 'commit-msg')


def _ansi_text(text):
    """Decode the bytes Bash and zsh agree on, or return None."""
    def escape(found):
        code = found[1]
        first = code[0]
        if first in ANSI_BYTES:
            return bytes([ANSI_BYTES[first]])
        if first in b'01234567':
            return bytes([int(code, 8) & 255])
        if first in b'xuU' and len(code) > 1:
            value = int(code[1:], 16)
            # Non-ASCII Unicode escapes depend on the command's locale.
            if first == ord('x') or value < 128:
                return bytes([value])
        raise ValueError('unsupported ANSI-C escape')

    try:
        decoded = ANSI_ESCAPE.sub(escape, text.encode('utf-8')).decode('utf-8')
        return decoded if '\0' not in decoded else None
    except ValueError:
        return None


def _heredocs(command, index, bodies, *, cat):
    """Replace a here-document at a shell opener. None as a body means the shell expands it."""
    found = (CAT_HEREDOC if cat else HEREDOC).match(command, index)
    if found is None:
        return None
    dash, quote = found[1], found[2]
    tail = '' if cat else found[4]
    if tail.endswith('\\'):
        return None
    body = found[4 if cat else 5] or ''
    if dash:
        body = '\n'.join(line.lstrip('\t') for line in body.split('\n'))
    if cat:
        body = body.rstrip('\n')  # the shell removes the newlines at the end of a command substitution
    bodies.append(body if quote or not re.search(r'[$`\\]', body) else None)
    return HEREDOC_MARK.format(len(bodies) - 1), found.end(), tail


class _Word(NamedTuple):
    text: str
    plain: bool
    bare: bool
    fill: bool = False


def _word(command, start, bodies, *, for_hook):
    parts, plain, bare, quote = [], True, True, ''
    literal_doc, fill = None, False
    index = start
    while index < len(command):
        char = command[index]
        pair = command[index:index + 2]
        if not quote and (char in PUNCTUATION or char in ' \t'):
            break
        if char == quote:
            quote = ''
        elif not quote and pair == "$'":
            doc = ANSI_QUOTE.match(command, index)
            if doc is None:
                raise ValueError('unclosed ANSI-C quote')
            decoded = _ansi_text(doc[1])
            parts.append(doc[0] if decoded is None else decoded)
            plain, bare = plain and decoded is not None, False
            index = doc.end()
            continue
        elif not quote and char in ('"', "'"):
            quote, bare = char, False
        elif pair == '\\\n':
            if quote == "'":  # literal inside single quotes; the shell removes it inside double quotes
                parts.append(pair)
            if not quote and index == literal_doc:
                literal_doc += 2
            index += 2
            continue
        elif char == '\\' and quote != "'":
            # The shell's rule is exact. Inside double quotes \" \\ \$ \` give the character and any other
            # backslash stays. Outside quotes the backslash gives the next character as it is.
            bare = False
            following = command[index + 1:index + 2]
            if not following:
                raise ValueError('unclosed shell escape')
            if not quote or following in '"\\$`':
                parts.append(following)
                index += 2
                continue
            parts.append(char)
        elif (pair == '$(' and quote != "'"
              and (doc := _heredocs(command, index, bodies, cat=True)) is not None):
            mark, end, _ = doc
            if quote == '"' and command[start:index] == '"' and command[end:end + 1] == '"' and bodies[-1] is not None:
                literal_doc = end + 1
            parts.append(mark)
            # Inside double quotes a literal body is plain text, wherever it is in the word (-m"$(cat <<'EOF' ...)").
            plain = plain and quote == '"' and bodies[-1] is not None
            bare, fill = False, True
            index = end
            continue
        else:
            if quote == '"':
                # A dollar sign starts an expansion only before a name, a digit or one of these characters.
                expands = char == '$' and (for_hook or DOUBLE_EXPANSION.match(command, index + 1))
                plain = plain and char != '`' and not expands
            elif (not for_hook and not quote and char == '~' and index == start and 'HOME=' not in command
                  and command[index + 1:index + 2] in ('', '/', ' ', '\t', '\n', ';', '&', '|', ')')):
                parts.append(os.path.expanduser('~'))  # the home folder, as the shell writes it for ~ and ~/x
                index += 1
                continue
            elif not quote:
                plain = (plain and char not in SHELL_SPECIAL and (char.isprintable() or char == '\N{NO-BREAK SPACE}')
                         and not (char == '=' and not parts))
                if char == '@' and HEREDOC_FOUND.match(command, index):
                    plain = False
            parts.append(char)
        index += 1
    if quote:
        raise ValueError('unclosed shell quote')
    return _Word(''.join(parts), plain or index == literal_doc, bare, fill), index


def _tokens(command, bodies, *, for_hook=False):
    index = 0
    while index < len(command):
        char = command[index]
        pair = command[index:index + 2]
        if char in ' \t':
            index += 1
        elif pair == '\\\n':
            index += 2
        elif char == '#':
            yield '#'
            end = command.find('\n', index)
            index = end if end != -1 else len(command)
        elif redirect := REDIRECTION.match(command, index):
            operator = redirect[0].replace('\\\n', '')
            start = redirect.end() - 1
            if redirect['op'] == '<' and (doc := _heredocs(command, start, bodies, cat=False)) is not None:
                mark, end, tail = doc
                yield operator
                yield _Word(mark, False, True, True)
                command = tail + command[end:]
                index = 0
            else:
                yield operator
                index = redirect.end()
        elif operator := COMPOUND_OPERATOR.match(command, index):
            yield operator[0].replace('\\\n', '')
            index = operator.end()
        elif char in PUNCTUATION:
            yield char
            index += 1
        else:
            word, index = _word(command, index, bodies, for_hook=for_hook)
            yield word


def _segments(tokens):
    """Return simple commands with the preceding operator, stdin and redirections, and flag uncertain syntax."""
    segments, words, stdin, index, uncertain, redirects_plain = [], [], None, 0, False, True
    preceding, redirected = None, False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if isinstance(token, _Word):
            uncertain = uncertain or (token.bare and token.text in ('{', '}'))
            words.append(token)
        elif redirect := REDIRECTION.fullmatch(token):
            redirected = True
            uncertain = uncertain or token in ('&>', '&>>')
            if index >= len(tokens) or not isinstance(tokens[index], _Word):
                uncertain = True
                continue
            target = tokens[index]
            index += 1
            fd = redirect['fd']
            on_stdin = not fd.strip('0') if fd else token.startswith('<')
            mark = HEREDOC_FOUND.fullmatch(target.text) if target.fill and target.bare else None
            if on_stdin:
                stdin = int(mark[1]) if mark else None
            redirects_plain = redirects_plain and (target.plain or mark is not None)
            uncertain = uncertain or (target.bare and target.text in ('{', '}'))
        elif token == '#':
            uncertain = True
        else:
            uncertain = uncertain or token not in (';', '&', '|', '&&', '||', '\n')
            if words or redirected:
                segments.append((preceding, words, stdin, redirects_plain, redirected))
                preceding = token
            elif preceding is None or (preceding in FOLDER_SEPARATORS and token != '\n'):
                # A continuation newline must not hide an operator such as || or |.
                preceding = token
            words, stdin, redirects_plain, redirected = [], None, True, False
    if words or redirected:
        segments.append((preceding, words, stdin, redirects_plain, redirected))
    return segments, uncertain


def _git_commit(words):
    """For git commit, collect arguments, global options, -C folders and readable config overrides."""
    text = [word.text for word in words]
    index, known = 0, True
    while index < len(text) and (ASSIGNMENT.fullmatch(text[index]) or text[index] in WRAPPERS):
        name = text[index].split('=', 1)[0]
        if GIT_CONFIG_ENV.fullmatch(name):
            known = False  # a command-local repository or config is not the one the hook probes
        index += 1
    if index >= len(text) or os.path.basename(text[index]) != 'git':
        return None
    index += 1
    options, folders, config = [], [], []
    while index < len(text) and text[index].startswith('-'):
        option = text[index]
        options.append(option)
        if option == '-C':
            if index + 1 >= len(words):
                return None
            folders.append(words[index + 1])
            index += 2
        else:
            name = option.split('=', 1)[0]
            if option == '-c' and index + 1 < len(words):
                entry = words[index + 1]
                key = entry.text.partition('=')[0].lower()
                if entry.plain and '=' in entry.text and key in (*CLEANUP_CONFIG, 'user.name', 'user.email'):
                    config.extend(('-c', entry.text))
                else:
                    known = False
            elif name in GIT_PLACE_OPTIONS or name in ('--config-env', '--bare'):
                known = False
            index += 2 if option in GIT_VALUE_OPTIONS or option in GIT_PLACE_OPTIONS else 1
    if index >= len(text) or text[index] != 'commit':
        return None
    return words[index + 1:], options, folders, known, config


def _commit_arguments(args, *, for_hook=False):
    """Extract commit messages, files, trailers, their options and the flags that affect checks.

    The 'open' flag means Git or an editor can still change the message, or the gate does not know the option."""
    found = {'messages': [], 'files': [], 'trailers': [], 'flags': set(), 'message_words': set(), 'cleanup': None}
    index = 0

    def value():
        nonlocal index
        index += 1
        if index >= len(args):
            found['flags'].add('open')
            return None
        return args[index]

    def take(kind, text):
        if text is not None:
            found[kind].append(text)
            if kind in ('messages', 'files'):
                found['message_words'].add(id(args[index]))

    while index < len(args):
        word = args[index]
        arg = word.text
        if arg == '--':
            break
        name, equals, attached = arg.partition('=')
        if arg.startswith('--'):
            if name in ('--message', '--file', '--trailer'):
                take({'--message': 'messages', '--file': 'files', '--trailer': 'trailers'}[name],
                     word._replace(text=attached) if equals else value())
            elif name in ('--reuse-message', '--reedit-message', '--fixup', '--squash'):
                found['flags'].add('fixup' if name in ('--fixup', '--squash') else 'reuse')
                if not equals:
                    value()
            elif name in ('--dry-run', '--help'):
                found['flags'].add('skip')
            elif name in COMMIT_VALUE_OPTIONS:
                if not equals:
                    value()
            elif arg.startswith('--no-v') and '--no-verify'.startswith(arg):
                found['flags'].add('no-verify')
            elif name == '--cleanup':
                mode = word._replace(text=attached) if equals else value()
                if mode is not None:
                    found['cleanup'] = mode
            elif for_hook and name == '--template':
                found['flags'].add('open')
                if not equals:
                    value()
            elif name in COMMIT_FLAGS:
                if name in ('--amend', '--no-edit'):
                    found['flags'].add(name[2:])
            else:  # --edit, --template, a short form of a long option, a new option
                found['flags'].add('open')
        elif arg.startswith('-') and len(arg) > 1:
            letters = arg[1:]
            for place, letter in enumerate(letters):
                rest = letters[place + 1:]
                if letter in 'mF':
                    take('messages' if letter == 'm' else 'files', word._replace(text=rest) if rest else value())
                    break
                if letter in 'Cc':
                    found['flags'].add('reuse')
                    if not rest:
                        value()
                    break
                if for_hook and letter == 't':
                    found['flags'].add('open')
                    if not rest:
                        value()
                    break
                if letter in 'uS':  # an optional value, attached only
                    break
                if letter == 'h':
                    found['flags'].add('skip')
                elif letter == 'n':
                    found['flags'].add('no-verify')
                elif for_hook and letter == 'e':
                    found['flags'].add('open')
                elif letter not in SHORT_FLAGS:
                    found['flags'].add('open')
                    break
        index += 1
    return found


def _fill(text, bodies):
    """`text` with each here-document mark replaced by its body. None when a body is not literal."""
    missing = False

    def body(found):
        nonlocal missing
        value = bodies[int(found[1])] if int(found[1]) < len(bodies) else None
        missing = missing or value is None
        return value or ''

    filled = HEREDOC_FOUND.sub(body, text)
    return None if missing else filled


def _read_message_file(folder, name):
    """The UTF-8 text of a message file, or None. Regular files only: a pipe would hold the hook."""
    try:
        path = Path(folder) / name
        with open(path, 'rb', opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK)) as source:
            found = os.fstat(source.fileno())
            if not stat_module.S_ISREG(found.st_mode) or found.st_size > FILE_BYTES:
                return None
            content = source.read(FILE_BYTES + 1)
            return content.decode('utf-8') if len(content) <= FILE_BYTES else None
    except (OSError, ValueError):
        return None


def _cleanup_uncertain(text, mode, folder, config, *, alone):
    """Could Git remove a Verified line? Caller has a literal message and no editor."""
    if mode is not None and (not mode.plain or mode.text not in ('strip', 'whitespace', 'verbatim', 'scissors', 'default')):
        return True
    lines = [line for line in text.split('\n') if line.startswith('Verified:')]
    if not lines:
        return False
    # Earlier commands can change any of these settings. Only git config honors the GIT_CONFIG override.
    if not alone or folder is None or os.environ.get('GIT_CONFIG') is not None:
        return True
    shown = _git(folder, *config, 'config', '--null', '--get-regexp', CLEANUP_PATTERN, text=False)
    if shown is not None and shown.returncode == 1 and not shown.stdout and not shown.stderr:
        return False  # no matching keys and the default comment prefix cannot remove a Verified line
    if shown is None or shown.returncode != 0 or not shown.stdout.endswith(b'\0'):
        return True
    try:
        settings = {}
        for entry in shown.stdout[:-1].decode('utf-8').split('\0'):
            key, _, value = entry.partition('\n')
            if key in CLEANUP_CONFIG[1:] and not value:
                return True  # Git can reject an empty comment prefix before it applies cleanup or later overrides.
            settings[key] = value
    except ValueError:
        return True
    cleanup = mode.text if mode is not None else settings.get(CLEANUP_CONFIG[0])
    if cleanup in (None, 'default', 'whitespace', 'verbatim', 'scissors'):
        return False
    if cleanup != 'strip':
        return True
    # Git 2.45+ treats these keys as aliases in config order. Consider either possible prefix.
    return any(line.startswith(prefix) for key in CLEANUP_CONFIG[1:]
               if (prefix := settings.get(key)) and prefix.lower() != 'auto' for line in lines)


def _one_commit(args, stdin, bodies, folder, *, alone, config):
    """('pass' | 'missing' | 'unknown' | 'skip', reason) for one `git commit`."""
    found = _commit_arguments(args)
    flags = found['flags']
    if 'skip' in flags:
        return 'skip', ''
    if 'fixup' in flags:
        return 'unknown', 'fixup'
    # A --trailer that names Verified changes which line is last: git builds it, so the gate does not decide.
    if any(not item.plain or item.text.lstrip().lower().startswith('verified') for item in found['trailers']):
        return 'unknown', 'open'
    text = None
    if found['messages']:
        if not all(part.plain for part in found['messages']):
            return 'unknown', 'shell'
        parts = [_fill(part.text, bodies) if part.fill else part.text for part in found['messages']]
        if any(part is None for part in parts):
            return 'unknown', 'shell'
        text = '\n\n'.join(parts)
    elif found['files']:
        word = found['files'][-1]
        name = word.text
        if word.plain and name == '-':
            text = bodies[stdin] if stdin is not None and stdin < len(bodies) else None
        elif word.plain and not HEREDOC_FOUND.search(name) and alone and folder is not None:
            text = _read_message_file(folder, name)
        if text is None:
            return 'unknown', 'file'
    elif 'reuse' in flags or ('amend' in flags and 'no-edit' in flags):
        return 'unknown', 'reuse'
    else:
        return 'unknown', 'editor'
    if 'open' in flags or 'reuse' in flags:
        return 'unknown', 'open'
    status = stamp.message_status(text)
    if '\r' in text and stamp.message_status(text.replace('\r\n', '\n').replace('\r', '\n')) != status:
        return 'unknown', 'encoding'  # a CR inside a Git line can look like a new line to the landed reader
    if _cleanup_uncertain(text, found['cleanup'], folder, config, alone=alone):
        return 'unknown', 'cleanup' if folder is not None else 'folder'
    if status != 'missing':
        return 'pass', ''
    if found['trailers']:
        return 'unknown', 'open'  # git, an editor or an option the gate does not know can still add the line
    return 'missing', 'no-line'


def _shell_launcher(words):
    for index, word in enumerate(words):
        if ASSIGNMENT.fullmatch(word.text):
            continue
        name = os.path.basename(word.text)
        if name in SHELL_COMMANDS:
            return True
        if name == 'env':
            for arg in words[index + 1:]:
                option = arg.text
                if option == '--' or not option.startswith('-'):
                    break
                if option.startswith('-S') or option.split('=', 1)[0] == '--split-string':
                    return True
        if name not in WRAPPERS:
            break
    return False


def _real_directory(path):
    try:
        # Check the original path too: realpath can simplify a path such as file/.. that chdir rejects.
        if os.path.isdir(path) and os.access(path, os.X_OK):
            return os.path.realpath(path, strict=True)
    except OSError:
        pass
    return None


def _cd(folder, logical, target, *, physical, alone):
    """Return the real folder and the shell's logical name, or (None, None)."""
    if (not alone or os.environ.get('CDPATH')) and not (target.startswith('/') or target.split('/')[0] in ('.', '..')):
        return None, None
    if physical:
        real = _real_directory(os.path.join(folder, target))
        return real, real
    path = '/' if target.startswith('/') else logical
    if path is None:
        return None, None
    for part in target.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if _real_directory(path) is None:
                return None, None  # cd missing/.. does not simply drop the missing component
            path = os.path.dirname(path)
        else:
            path = os.path.join(path, part)
    real = _real_directory(path)
    return (real, path) if real is not None else (None, None)


def _cd_target(words):
    """Recognize one literal target and the options common to Bash and zsh."""
    if not all(word.plain and not word.fill for word in words):
        return None
    args = [word.text for word in words]
    physical = args[:1] == ['-P']
    if physical:
        args = args[1:]
    explicit = args[:1] == ['--']
    if explicit:
        args = args[1:]
    if len(args) != 1:
        return None
    target = args[0]
    if not target or (target.startswith('-') and not explicit) or re.fullmatch(r'-(?:[0-9]+)?|\+[0-9]+', target):
        return None
    return target, physical


def _cd_guard(segment):
    preceding, words, _, _, redirected = segment
    return (preceding == '||' and not redirected and all(word.plain and word.bare for word in words)
            and [word.text for word in words] in (['exit'], ['exit', '1'], ['return']))


def _command_word(words):
    """Return the index of a command after known prefixes, or None if it cannot be identified."""
    wrapper_options = False
    for index, word in enumerate(words):
        text = word.text
        if word.plain and text in COMMAND_WRAPPERS:
            wrapper_options = True
        elif word.bare and (text in KEYWORD_PREFIXES or REDIRECTION_VARIABLE.fullmatch(text)):
            wrapper_options = False
        elif not word.plain:
            return None
        elif text == '--':
            wrapper_options = False
        elif wrapper_options and text.startswith('-'):
            continue
        elif ASSIGNMENT_LIKE.fullmatch(text):
            wrapper_options = False
        else:
            return index if text and not any(char in text for char in '[]=${}') else None
    return None


def evaluate(command, cwd=None):
    """None when the command has no `git commit`. Else {'verdict': 'pass' | 'missing' | 'unknown', 'reason': str,
    'count': the number of commits, 'folder': where git runs, or None when not known}.

    'missing' needs certainty: the gate read every commit in the command in full and none has a valid line."""
    if not isinstance(command, str):
        return None
    joined = command.replace('\\\n', '')
    if 'git' not in joined or 'commit' not in joined:
        return None
    folder = _real_directory(cwd) if isinstance(cwd, str) and cwd.startswith('/') else None
    # The shell's own folder name can go through a link. git runs in the real folder, but a `cd ..` would not.
    logical = folder if folder is not None and folder == os.path.normpath(cwd) else None
    bodies = []
    try:
        tokens = list(_tokens(command, bodies))
        segments, uncertain = _segments(tokens)
    except ValueError:
        # Quotes that do not close. Report it only when the text looks like a commit at all.
        if re.search(r'(?:^|[\s;&|(])git\s[^\n;|&]*\bcommit(?:\s|$)', command):
            return {'verdict': 'unknown', 'reason': 'parse', 'count': 1, 'folder': None}
        return None
    # `alone`: only followed cd commands ran before. Other commands can change a message file, config or CDPATH.
    verdicts, where, alone = [], None, True
    # A cd is followed only when it runs for certain, in the shell itself, to a folder the gate can name:
    # - not when the command has a background `&` (bash can put a whole && list in the background);
    # - not when the command sets HOME (the gate's ~ would be the old one);
    # - not after `x &&` unless x is a cd too (`test -f a && cd b; git commit` can skip the cd);
    # - not after || or in a pipe (`cd a || cd b`, `cd a | x`).
    steady = '&' not in tokens and not any(name in joined for name in ('HOME=', 'PWD='))
    guard = None
    git_context_changed = False
    for number, (preceding, words, stdin, redirects_plain, redirected) in enumerate(segments):
        if number == guard:
            continue
        if not words:
            alone = False  # a redirection alone can write a message file
            continue
        first = words[0]
        uncertain = uncertain or (first.bare and first.text in KEYWORDS)
        all_plain = redirects_plain and all(word.plain for word in words)
        cd = first.text == 'cd' or (first.text == 'builtin' and len(words) > 1 and words[1].text == 'cd')
        if cd:
            following = segments[number + 1][0] if number + 1 < len(segments) else None
            if following == '||' and _cd_guard(segments[number + 1]):
                after_guard = segments[number + 2][0] if number + 2 < len(segments) else None
                if after_guard in (None, ';', '\n'):
                    guard, following = number + 1, after_guard
            target = _cd_target(words[1 if first.text == 'cd' else 2:])
            if (folder is not None and steady and not uncertain and all_plain and not redirected
                    and following in FOLDER_SEPARATORS and target is not None
                    and (preceding in (None, ';', '\n') or (preceding == '&&' and alone))):
                folder, logical = _cd(folder, logical, target[0], physical=target[1], alone=alone)
            else:
                folder = logical = None
            alone = alone and folder is not None
            continue
        position = _command_word(words)
        name = words[position].text if position is not None else None
        git_words = words
        while git_words and git_words[0].bare and git_words[0].text in KEYWORD_PREFIXES:
            git_words = git_words[1:]
        uncertain = uncertain or _shell_launcher(git_words)
        commit = _git_commit(git_words) if git_words else None
        if commit:
            args, _, folders, known, config = commit
            here = folder if known and not git_context_changed else None
            for item in folders:
                here = (_real_directory(os.path.join(here, item.text))
                        if here is not None and item.plain and not item.fill else None)
            verdict, reason = _one_commit(args, stdin, bodies, here, alone=alone, config=config)
            if verdict != 'skip':
                if not all_plain:
                    verdict, reason = 'unknown', 'shell'
                elif here is None and verdict == 'missing':
                    verdict, reason = 'unknown', 'folder'
                verdicts.append((verdict, reason))
                where = here if len(verdicts) == 1 or where == here else None
        # An unrecognised prefix can hide the command word, so consider every word in that case.
        possible_commands = {word.text for word in words} if position is None else {name}
        if MOVING_WORDS.intersection(possible_commands):
            folder = logical = None
        if possible_commands.intersection(('set', 'setopt', 'unsetopt')):
            steady = False  # later cd and glob behavior can depend on shell options
        if commit is None and not git_context_changed:
            # These commands can explicitly change the Git worktree or hooks. Keep the folder unknown for probes.
            if position is None:
                assignments, env_names = words, words
            else:
                env_names = words[position + 1:] if name in ('export', 'unset', 'declare', 'typeset') else []
                assignments = words[:position] + env_names
            git_context_changed = (
                (any(os.path.basename(word.text) == 'git' for word in git_words[:-1])
                 and any(word.text == 'config' for word in git_words[1:]))
                or any(GIT_CONFIG_ENV.fullmatch(assignment[1]) for word in assignments
                       if (assignment := ASSIGNMENT_LIKE.fullmatch(word.text)))
                or any(GIT_CONFIG_ENV.fullmatch(word.text) for word in env_names))
        alone = False
    if not verdicts:
        return None
    kinds = {verdict for verdict, _ in verdicts}
    result = {'count': len(verdicts), 'folder': where}
    if kinds == {'pass'} and not uncertain:
        return dict(result, verdict='pass', reason='')
    if kinds == {'missing'} and not uncertain:
        return dict(result, verdict='missing', reason='no-line')
    reason = next((reason for verdict, reason in verdicts if verdict == 'unknown'), 'parse' if uncertain else 'mixed')
    return dict(result, verdict='unknown', reason=reason)


# ---- the log and the session state ----

def log_path():
    return paths.COMMIT_GATE_LOG


def _state_folder():
    return paths.CACHE / 'commit-gate'


def _message_key(*, create=False):
    path = _state_folder() / 'commit-gate.key'
    try:
        if create:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, 'wb') as stream:
                    key = os.urandom(32)
                    stream.write(key)
                return key
        with open(path, 'rb', opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK)) as stream:
            if not stat_module.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            key = stream.read(33)
    except OSError:
        return None
    return key if len(key) == 32 else None


def _normalize_message(lines):
    """Git stripspace without comment removal or a final newline."""
    normalized = []
    for line in lines:
        line = line.rstrip(' \t\r')  # Git's ASCII whitespace, with LF already split off.
        if line or (normalized and normalized[-1]):
            normalized.append(line)
    if normalized and not normalized[-1]:
        normalized.pop()
    return '\n'.join(normalized)


def _message_digest(key, normalized):
    return hmac.new(key, normalized.encode('utf-8'), 'sha256').hexdigest()[:32]


class _Lock:
    """An exclusive file lock with a deadline. The default protects sessions, claims and log rotation."""

    def __init__(self, path=None, seconds=LOCK_SECONDS):
        self.path = path
        self.seconds = seconds

    def __enter__(self):
        path = self.path
        if path is None:
            folder = _state_folder()
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = folder / '.lock'
        self.stream = open(path, 'a')
        deadline = time.monotonic() + self.seconds
        try:
            while True:
                try:
                    fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('commit gate lock') from None
                    time.sleep(min(0.005, remaining))
        except BaseException:
            self.stream.close()
            raise

    def __exit__(self, kind, error, trace):
        self.stream.close()


def _log(record, *, locked=False):
    path = log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = (json.dumps(record, separators=(',', ':')) + '\n').encode()
    with nullcontext() if locked else _Lock():
        try:
            if path.stat().st_size > LOG_BYTES:
                os.replace(path, path.with_name(path.name + '.1'))
        except OSError:
            pass
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)


def _session_path(session):
    return _state_folder() / f'{session}.json'


def _invalid_json_constant(value):
    raise ValueError(f'invalid JSON constant: {value}')


def _read_session(session, now):
    """{'blocks': int, 'pending': {tool id: {...}}}. Call with the lock held."""
    try:
        data = json.loads(_session_path(session).read_text(encoding='utf-8'), parse_constant=_invalid_json_constant)
    except FileNotFoundError:
        return {'blocks': 0, 'pending': {}}
    if not isinstance(data, dict) or type(data.get('blocks')) is not int or data['blocks'] < 0:
        raise ValueError('invalid commit gate block count')
    pending = data.get('pending')
    if not isinstance(pending, dict):
        raise ValueError('invalid commit gate pending calls')
    for value in pending.values():
        if (not isinstance(value, dict) or type(value.get('at')) not in (int, float)
                or not math.isfinite(value['at']) or not isinstance(value.get('folder'), str)
                or not isinstance(value.get('head'), str) or type(value.get('shared')) is not bool
                or ('tree' in value and not isinstance(value['tree'], str))
                or (value.get('project') is not None and not isinstance(value['project'], str))
                or type(value.get('deferred', False)) is not bool
                or (value.get('deferred') and value.get('mode') not in NAMES)
                or type(value.get('closing', False)) is not bool):
            raise ValueError('invalid commit gate pending call')
    return {'blocks': data['blocks'], 'pending': {key: value for key, value in pending.items()
                                                  if now - value['at'] < PENDING_SECONDS}}


def _write_session(session, state, now):
    """Call it with the lock held. A new session file is the moment to remove very old ones."""
    path = _session_path(session)
    if not path.exists():
        try:
            with os.scandir(path.parent) as entries:
                for entry in entries:
                    name, suffix = os.path.splitext(entry.name)
                    if suffix != '.json' or not SESSION.fullmatch(name):
                        continue
                    try:
                        if now - entry.stat().st_mtime > STATE_DAYS * 86400 and _read_session(name, now)['blocks'] == 0:
                            os.unlink(entry.path)
                    except (OSError, ValueError, RecursionError, OverflowError):
                        continue
        except OSError:
            pass
    while len(state['pending']) > PENDING_MAX:
        del state['pending'][min(state['pending'], key=lambda key: state['pending'][key]['at'])]
    _write(path, json.dumps(state))


def _git(folder, *args, text=True, env=None):
    try:
        return subprocess.run(['git', '-C', folder, *args], capture_output=True, text=text, timeout=0.5,
                              errors='replace' if text else None, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _head(folder):
    """HEAD's hash, '' in a repository with no commit yet, None when git cannot tell."""
    shown = _git(folder, 'rev-parse', '--verify', '--quiet', 'HEAD')
    if shown is None:
        return None
    if shown.returncode == 0 and re.fullmatch(r'[0-9a-f]{40,64}', shown.stdout.strip()):
        return shown.stdout.strip()
    inside = _git(folder, 'rev-parse', '--git-dir')
    return '' if inside is not None and inside.returncode == 0 else None


def _tree(folder):
    """The worktree root, or None when git cannot tell."""
    shown = _git(folder, 'rev-parse', '--show-toplevel', text=False)
    if shown is None or shown.returncode != 0:
        return None
    root = os.fsdecode(shown.stdout).removesuffix('\n')
    return root if os.path.isabs(root) else None


def _git_path(folder, *args):
    shown = _git(folder, 'rev-parse', '--path-format=absolute', *args, text=False)
    if shown is None or shown.returncode != 0:
        return None
    path = Path(os.fsdecode(shown.stdout).removesuffix('\n'))
    return path if path.is_absolute() else None


def _head_reflog_position(folder):
    """Record where the call's reflog entries will start."""
    path = _git_path(folder, '--git-path', 'logs/HEAD')
    if path is None:
        return None
    try:
        found = path.lstat()
    except FileNotFoundError:
        return {'size': 0, 'ino': None, 'dev': None}
    except OSError:
        return None
    if stat_module.S_ISREG(found.st_mode):
        return {'size': found.st_size, 'ino': found.st_ino, 'dev': found.st_dev}
    return None


def _head_reflog(folder, start):
    """Return the HEAD before each commit's first entry since start, or None if unavailable.

    The first entry is the one that made the commit. Later checkouts and resets cannot replace it."""
    if not isinstance(start, dict) or not {'size', 'ino', 'dev'} <= start.keys():
        return None
    offset, ino, dev = start['size'], start['ino'], start['dev']
    if type(offset) is not int or offset < 0:
        return None
    missing = offset == 0 and ino is None and dev is None
    if not missing and any(type(value) is not int or value < 0 for value in (ino, dev)):
        return None
    path = _git_path(folder, '--git-path', 'logs/HEAD')
    if path is None:
        return None
    try:
        if not stat_module.S_ISREG(path.lstat().st_mode):
            return None
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        with open(path, 'rb', opener=lambda path, mode: os.open(path, mode | flags)) as source:
            found = os.fstat(source.fileno())
            if not stat_module.S_ISREG(found.st_mode):
                return None
            length = found.st_size - offset
            if not 0 <= length <= REFLOG_BYTES:
                return None
            if not missing and (found.st_ino, found.st_dev) != (ino, dev):
                return None  # The file was replaced, even if it was empty.
            if offset:
                source.seek(offset - 1)
                if source.read(1) != b'\n':
                    return None
            tail = source.read(length + 1)
        if len(tail) != length or not tail.endswith(b'\n'):
            return None
        heads = {}
        for line in tail.split(b'\n')[:-1]:
            old, new, ident = line.split(b' ', 2)
            old, new = old.decode('ascii'), new.decode('ascii')
            if not GIT_OID.fullmatch(old) or not GIT_OID.fullmatch(new):
                return None
            if not re.fullmatch(rb'[^<>\0]* <[^<>\0]*> \d+ [+-]\d{4}(?:\t[^\0]*)?', ident):
                return None
            heads.setdefault(new, old)
        return heads
    except (OSError, ValueError):
        return None


def _message_hook(folder):
    """True when the repository has a hook that can change the message, False when it has none, None when git
    cannot tell."""
    hooks = _git_path(folder, '--git-path', 'hooks')
    if hooks is None:
        return None
    try:
        return any(os.access(hooks / name, os.X_OK) and (hooks / name).is_file() for name in MESSAGE_HOOKS)
    except OSError:
        return None


class _LandedCommit(NamedTuple):
    oid: str
    tree: str
    parent: str | None
    parent_list: tuple[str, ...]
    author_date: str
    committed: int
    committer_date: str
    status: str
    body: str | None


def _landed(folder, head):
    """Commits made since `head`, newest first. [] when none landed, None when Git cannot tell."""
    shown = _git(folder, 'log', '--first-parent', '-n', '20', '-z', '--date=raw',
                 '--format=%H%x00%T%x00%P%x00%ad%x00%cd%x00%B',
                 f'{head}..HEAD' if head else 'HEAD', text=False)
    if shown is None or shown.returncode != 0:
        return [] if head == '' else None
    fields = shown.stdout.split(b'\0')[:-1]
    commits = []
    for index in range(0, len(fields), 6):
        oid, tree, parents, author_date, raw_committer_date, raw = fields[index:index + 6]
        try:
            body = raw.decode('utf-8')
        except UnicodeDecodeError:
            body, status = None, 'unknown'
        else:
            status = stamp.message_status(body.replace('\r\n', '\n').replace('\r', '\n'))
        parent_list = tuple(parents.decode('ascii').split())
        parent = parent_list[0] if parent_list else None
        committer_date = raw_committer_date.decode('ascii')
        committed = int(committer_date.partition(' ')[0])
        commits.append(_LandedCommit(oid.decode('ascii'), tree.decode('ascii'), parent, parent_list,
                                     author_date.decode('ascii'), committed, committer_date, status, body))
    return commits


# ---- the hook ----

def candidate(data):
    """True for a hook input the gate must look at. Cheap: claude_hooks.py calls it for every event."""
    if not isinstance(data, dict) or data.get('tool_name') != 'Bash':
        return False
    if data.get('hook_event_name') not in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
        return False
    command = data.get('tool_input', {}).get('command') if isinstance(data.get('tool_input'), dict) else None
    return isinstance(command, str) and 'git' in command and 'commit' in command


def deny_reason(blocks):
    left = MAX_BLOCKS - blocks
    return ('Tether commit gate: this commit message has no valid "Verified:" line. End the message with one line '
            'that says how you checked the work, for example "Verified: tests(server 412 ok) review(codex APPROVED)". '
            'Each item is kind(detail). When you did not check the work, write "Verified: none (reason)". '
            f'This is block {blocks} of {MAX_BLOCKS} in this session'
            + ('. ' if left else ': the gate lets the next commit through and logs it. ')
            + 'If the message has a valid line, the gate read it wrong; tell the user.')


def _tmux(*args):
    return subprocess.run([paths.TMUX, *args], capture_output=True, text=True, timeout=0.4)


def _project_id(pid):
    if not pid:
        return None, True
    if not PROJECT_ID.fullmatch(pid):
        return None, False
    if project_path(pid).exists() and read_project(pid) is None:
        return pid, False  # a file that the gate cannot read
    return pid, True


def _no_tmux_server(shown):
    return 'no server running' in shown.stderr or 'No such file or directory' in shown.stderr


def _project(cwd, env, tmux=_tmux):
    """(project id or None, sure). `sure` is False when tmux or the project's file could not be read: the session may
    have an override that the gate cannot see, so it must not block."""
    try:
        pane = env.get('TMUX_PANE', '')
        if env.get('TMUX') and re.fullmatch(r'%[0-9]+', pane):
            shown = tmux('display-message', '-p', '-t', pane, '#{@project}')
            if shown.returncode:
                return None, False
            pid = shown.stdout.strip()
        else:
            if not isinstance(cwd, str) or not cwd.startswith('/'):
                return None, False
            shown = tmux('list-panes', '-a', '-F', '#{pane_current_path}\t#{@project}')
            if shown.returncode:
                # No tmux server: no session has a project. Any other failure is not an answer.
                return None, _no_tmux_server(shown)
            found = {fields[1] for fields in (line.split('\t') for line in shown.stdout.splitlines())
                     if len(fields) == 2 and fields[0] == cwd}
            if len(found) != 1:
                return None, False
            pid = next(iter(found))
    except (OSError, subprocess.SubprocessError):
        return None, False
    return _project_id(pid)


HOOK_GIT_OPTIONS = re.compile(r'-c|--bare|--(?:git-dir|work-tree|config-env).*', re.S)
HOOK_CONFIG_WORD = re.compile(r'GIT_|hooksPath', re.I)


def _owned_hook(hook):
    try:
        if not stat_module.S_ISREG(hook.lstat().st_mode):
            return None
        with open(hook, 'rb', opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
            if not stat_module.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            contents = stream.read()
        return contents if HOOK_MARK.encode() in contents.splitlines() else None
    except OSError:
        return None


def _hook_ready(contents):
    """The installed wrapper's helper, Python and gate file exist. PostToolUse checks if the hook logged a verdict."""
    for line in contents.decode('utf-8', errors='replace').splitlines():
        if not line.startswith('exec /bin/sh '):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            return False
        if len(words) != 7 or words[3] != '$1':
            return False
        helper, python, gate = (Path(word) for word in (words[2], words[4], words[5]))
        return (helper.is_file() and os.access(helper, os.R_OK) and gate.is_file() and os.access(gate, os.R_OK)
                and python.is_file() and os.access(python, os.X_OK))
    return False


def _defer_to_hook(tree, command, *, cwd_fallback=False):
    if tree is None:
        return False
    hooks = _git_path(tree, '--git-path', 'hooks')
    if hooks is None:
        return False
    hook = hooks / 'commit-msg'
    try:
        contents = _owned_hook(hook) if os.access(hook, os.X_OK) else None
        if contents is None or not _hook_ready(contents):
            return False
        tokens = list(_tokens(command, [], for_hook=True))
        if any(isinstance(token, str) and token not in ('&&', '\n') for token in tokens):
            return False
        segments, uncertain = _segments(tokens)
        if uncertain or len(segments) != 1 + tokens.count('&&'):
            return False
        if any(segment[0] != '&&' for segment in segments[1:]):
            return False

        # Only an optional literal cd, an optional literal git add, and the commit itself may run.
        *prefix, words = [segment[1] for segment in segments]
        if prefix and prefix[0][0].text == 'cd':
            if cwd_fallback or len(prefix[0]) != 2:
                return False
            # A relative folder can resolve through CDPATH to another repository than the one checked here.
            target = prefix[0][1].text
            if not (target.startswith('/') or target in ('.', '..') or target.startswith(('./', '../'))):
                return False
            prefix = prefix[1:]
        if prefix and (len(prefix) != 1 or [word.text for word in prefix[0][:2]] != ['git', 'add']):
            return False
        if os.path.basename(words[0].text) != 'git' or (commit := _git_commit(words)) is None:
            return False
        args, options, _, known, _ = commit
        if not known or any(HOOK_GIT_OPTIONS.fullmatch(option) for option in options):
            return False
        if cwd_fallback and '-C' in options:
            return False
        parsed = _commit_arguments(args, for_hook=True)
        if 'no-verify' in parsed['flags']:
            return False
        for word in tokens:
            if not isinstance(word, _Word):
                continue
            message = id(word) in parsed['message_words']
            if not word.plain or (word.fill and not message):
                return False
            if not message and HOOK_CONFIG_WORD.search(word.text):
                return False
    except (OSError, ValueError):
        return False
    return True


def check(data, env=os.environ, now=None):
    """PreToolUse: log the verdict. Returns the hook's stdout: a deny in enforce mode, else ''."""
    now = time.time() if now is None else now
    command, cwd = data['tool_input']['command'], data.get('cwd')
    found = evaluate(command, cwd)
    if found is None:
        return ''
    pid, sure = _project(cwd, env)
    mode = resolve(pid if sure else None)['mode']
    if mode == 'off':
        return ''
    verdict, reason, folder = found['verdict'], found['reason'], found['folder']
    session = data.get('session_id')
    session = session if isinstance(session, str) and SESSION.fullmatch(session) else None
    tool = data.get('tool_use_id') if isinstance(data.get('tool_use_id'), str) else None
    tree = _tree(folder) if folder is not None else None
    hook_tree = _tree(cwd) if folder is None and cwd else tree
    deferred = bool(session and tool and _defer_to_hook(hook_tree, command, cwd_fallback=folder is None))
    if deferred and folder is None:
        folder = tree = hook_tree
    head = _head(folder) if tree is not None else None
    deferred = deferred and head is not None
    if not deferred:
        if (verdict == 'missing' and head is None) or (reason == 'file' and folder is None):
            verdict, reason = 'unknown', 'folder'
        elif verdict == 'missing':
            hooked = _message_hook(folder)
            if hooked is not False:
                verdict, reason = 'unknown', 'hook' if hooked else 'folder'
        elif verdict == 'pass' and tree is not None and _message_hook(folder):
            verdict, reason = 'unknown', 'hook'  # the hook can also remove the line
    action, blocks = {'pass': 'pass', 'unknown': 'unknown', 'missing': 'would_block'}[verdict], 0
    if session:
        with _Lock():
            state = _read_session(session, now)
            before = json.dumps(state, sort_keys=True)
            if not deferred and verdict == 'missing' and mode == 'enforce' and sure:
                if state['blocks'] >= MAX_BLOCKS:
                    action = 'let_through'
                else:
                    state['blocks'] += 1
                    action = 'block'
                blocks = state['blocks']
            if action != 'block' and head is not None and tool:
                # Overlapping calls in one worktree cannot identify who made a commit. An old entry has no tree.
                shared = False
                for other in state['pending'].values():
                    if other.get('tree') in (None, tree):
                        other['shared'] = shared = True
                state['pending'][tool] = {'folder': folder, 'tree': tree, 'head': head, 'at': now, 'project': pid,
                                          'shared': shared}
                if (reflog := _head_reflog_position(folder)) is not None:
                    state['pending'][tool]['reflog'] = reflog
                if deferred:
                    state['pending'][tool].update(deferred=True, mode=mode)
            if json.dumps(state, sort_keys=True) != before:
                _write_session(session, state, now)
    if deferred:
        return ''
    record = {'at': round(now, 3), 'event': 'check', 'id': tool, 'session': session, 'project': pid, 'mode': mode,
              'verdict': action, 'reason': reason, 'count': found['count'], 'folder': folder}
    if not sure:
        record['project_unsure'] = True
    if data.get('agent_id'):
        record['subagent'] = True
    _log(record)
    if action != 'block':
        return ''
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                              'permissionDecisionReason': deny_reason(blocks)}})


def _read_claims(path):
    try:
        claims = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(claims, dict):
        raise ValueError('invalid commit gate claims')
    return claims


def _amend_parents(folder, head):
    """Return the replaced head's parents, or None when Git cannot prove it was replaced."""
    if not isinstance(head, str) or not GIT_OID.fullmatch(head):
        return None
    ancestor = _git(folder, 'merge-base', '--is-ancestor', head, 'HEAD')
    if ancestor is None or ancestor.returncode != 1:
        return None
    shown = _git(folder, 'show', '-s', '--format=%P', head)
    if shown is None or shown.returncode != 0:
        return None
    return tuple(shown.stdout.split())


def _uncovered_commits(waiting, commits, worktree, rows, now, amend_parents, reflog):
    """Claim covering hook rows and return uncovered commits. Call with the lock held."""
    commits = list(reversed(commits))
    if not waiting.get('tree'):
        return commits
    ledger = _state_folder() / f'claims.{hashlib.sha256(os.fsencode(worktree)).hexdigest()}.json'
    try:
        previous = _read_claims(ledger)
        claims = {row_key: oid for row_key, oid in previous.items()
                  if now - CLAIM_SECONDS <= float(row_key.partition(':')[0]) < math.inf}
    except (OSError, ValueError, RecursionError):
        return commits

    key = _message_key()
    uncovered = (commits if key is None
                 else _claim_covering_rows(waiting, commits, worktree, rows, key, claims, amend_parents, reflog))
    if claims != previous:
        try:
            _write(ledger, json.dumps(claims))
        except OSError:
            return commits
    return uncovered


def _claim_covering_rows(waiting, commits, worktree, rows, key, claims, amend_parents, reflog):
    since = waiting['at'] - LOG_TIME_TOLERANCE
    trees = {commit.tree for commit in commits}
    rows_by_tree = {}
    for row in rows:
        at = row.get('at')
        if (row.get('via') != 'commit-msg' or row.get('verdict') == 'block'
                or type(at) not in (float, int) or not since <= at <= sys.float_info.max):
            continue
        tree, folder, messages = row.get('tree'), row.get('folder'), row.get('messages')
        if not isinstance(tree, str) or tree not in trees or not isinstance(folder, str):
            continue
        if not isinstance(messages, list) or not messages or 'head' not in row or os.path.realpath(folder) != worktree:
            continue
        merge = row.get('verdict') == 'skip' and row.get('reason') == 'merge'
        merge_heads = ()
        if merge:
            heads = row.get('merge_heads')
            if not isinstance(heads, list) or not heads or not all(
                    isinstance(oid, str) and GIT_OID.fullmatch(oid) for oid in heads):
                continue
            merge_heads = tuple(heads)
        author_date = row.get('author_date')
        if not isinstance(author_date, str) and ('author_date' in row or not merge):
            continue
        committer_date, started = row.get('committer_date'), row.get('started')
        if 'committer_date' in row and not isinstance(committer_date, str):
            continue
        if 'committer_override' in row or type(started) is not int:
            started = None
        head = row['head']
        if head is None or (isinstance(head, str) and GIT_OID.fullmatch(head)):
            rows_by_tree.setdefault(tree, []).append(
                (at, f'{at}:{folder}', head, messages, author_date, committer_date, started, merge_heads))
    for candidates in rows_by_tree.values():
        candidates.sort(key=lambda row: row[0])

    uncovered = []
    for commit in commits:
        candidates = rows_by_tree.get(commit.tree)
        if not candidates or commit.body is None:
            uncovered.append(commit)
            continue
        digest = _message_digest(key, _normalize_message(commit.body.split('\n')))
        match = None
        for at, row_key, head, messages, author_date, committer_date, started, merge_heads in candidates:
            if author_date is not None and author_date != commit.author_date:
                continue
            if committer_date is not None:
                if committer_date != commit.committer_date:
                    continue
            elif (commit.committed > at + LOG_TIME_TOLERANCE
                  or (started is not None and commit.committed < started)):
                continue
            claimed = claims.get(row_key)
            if claimed not in (None, commit.oid) or digest not in messages:
                continue
            if match is not None and claimed is None:
                continue  # Keep the oldest eligible row unless a covering claim already exists.
            if head == commit.parent:
                if commit.parent_list[1:] != merge_heads:
                    continue
            else:
                if merge_heads or amend_parents(head) != commit.parent_list:
                    continue
                heads = reflog()  # Only the reflog proves an amend. Matching parents can also be a re-merge.
                if heads is None or heads.get(commit.oid) != head:
                    continue
            if claimed == commit.oid:
                match = row_key  # Reuse the existing claim on overlapping PostToolUse calls.
                break
            if match is None:
                match = row_key
        if match is None:
            uncovered.append(commit)
        else:
            claims[match] = commit.oid
    return uncovered


def _log_deferred_checks(commits, waiting, session, tool, now):
    """Claim hook rows and dedupe hook-silent rows under the same lock."""
    @cache
    def amend_parents(head):
        return _amend_parents(waiting['folder'], head)

    @cache
    def reflog():
        return _head_reflog(waiting['folder'], waiting.get('reflog'))

    worktree = os.path.realpath(waiting.get('tree') or waiting['folder'])
    rows = list(_rows(locked=True))
    uncovered = _uncovered_commits(waiting, commits, worktree, rows, now, amend_parents, reflog)
    if not uncovered:
        return
    logged = {row['commit'] for row in rows if row.get('reason') == 'hook-silent'
              and isinstance(row.get('commit'), str) and isinstance(row.get('folder'), str)
              and os.path.realpath(row['folder']) == worktree}
    # When coverage cannot be proved, a duplicate check is acceptable. Zero checks are not.
    for commit in uncovered:
        if commit.oid in logged:
            continue
        new_merge = len(commit.parent_list) >= 2
        if new_merge and (heads := reflog()) is not None:
            old = heads.get(commit.oid)
            new_merge = old == commit.parent or amend_parents(old) != commit.parent_list
        verdict = ('skip' if new_merge
                   else {'missing': 'would_block', 'unknown': 'unknown'}.get(commit.status, 'pass'))
        _log({'at': round(now, 3), 'event': 'check', 'via': 'landed', 'id': tool, 'session': session,
              'project': waiting.get('project'), 'mode': waiting['mode'], 'folder': worktree,
              'commit': commit.oid, 'reason': 'hook-silent', 'count': 1,
              'verdict': verdict}, locked=True)


def landed(data, now=None):
    """PostToolUse and PostToolUseFailure: log what the commits that landed carry."""
    now = time.time() if now is None else now
    session, tool = data.get('session_id'), data.get('tool_use_id')
    if not isinstance(session, str) or not SESSION.fullmatch(session) or not isinstance(tool, str):
        return
    if not _session_path(session).exists():
        return
    with _Lock():
        state = _read_session(session, now)
        waiting = state['pending'].get(tool)
        if waiting is None or waiting.get('closing'):
            return
        waiting['closing'] = True
        _write_session(session, state, now)
    commits = None
    try:
        commits = _landed(waiting['folder'], waiting['head'])
    finally:
        with _Lock():
            state = _read_session(session, now)
            waiting = state['pending'].pop(tool, None)
            if waiting is not None:
                _write_session(session, state, now)
                if commits and waiting.get('deferred'):
                    _log_deferred_checks(commits, waiting, session, tool, now)
    if commits is None or waiting is None:
        return
    statuses = {commit.status for commit in commits}
    worst = next((kind for kind in ('missing', 'unknown', 'none', 'verified') if kind in statuses), 'no-commit')
    record = {'at': round(now, 3), 'event': 'landed', 'id': tool, 'session': session, 'project': waiting.get('project'),
              'landed': worst, 'count': len(commits)}
    if waiting.get('shared') or 'tree' not in waiting:
        record['shared'] = True
    _log(record)


def hook(data, env=os.environ):
    """The text to print for one hook input. claude_hooks.py catches every error: the gate fails open."""
    if data.get('hook_event_name') == 'PreToolUse':
        return check(data, env)
    landed(data)
    return ''


# ---- Git's commit-msg hook ----

class _Ancestor(NamedTuple):
    pid: int
    comm: str
    started: float | None


def _boot_clock():
    try:
        ticks = os.sysconf('SC_CLK_TCK')
        with open('/proc/stat', 'rb') as proc:
            for line in proc:
                if line.startswith(b'btime ') and ticks > 0:
                    return int(line.split()[1]), ticks
    except (OSError, ValueError, IndexError):
        pass
    return None


def _ps_started(elapsed, now):
    found = re.fullmatch(r'(?:(?:(\d+)-)?(\d{1,2}):)?([0-5]\d):([0-5]\d)', elapsed)
    if found is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in found.groups())
    if hours >= 24:
        return None
    return now - (days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _ancestors():
    """Nearest parents first. A vanished process or a slow ps ends the walk."""
    pid = os.getppid()
    deadline = time.monotonic() + 0.05
    linux = sys.platform.startswith('linux')
    clock = _boot_clock() if linux else None
    for _ in range(40):
        remaining = deadline - time.monotonic()
        if pid < 1 or remaining <= 0:
            break
        started = None
        try:
            if linux:
                entry = Path(f'/proc/{pid}/stat').read_bytes()
                start, end = entry.index(b'('), entry.rindex(b')')
                comm = os.fsdecode(entry[start + 1:end])
                fields = entry[end + 1:].split()
                ppid = int(fields[1])
                if clock is not None:
                    boot, ticks = clock
                    try:
                        started = boot + int(fields[19]) / ticks  # starttime is field 22.
                    except (ValueError, IndexError):
                        pass
            else:
                shown = subprocess.run(['ps', '-o', 'ppid=,etime=,comm=', '-p', str(pid)], capture_output=True,
                                       text=True, check=True, timeout=remaining)
                parent, elapsed, comm = shown.stdout.split(maxsplit=2)
                comm = comm.strip()
                ppid, comm = int(parent), os.path.basename(comm)
                started = _ps_started(elapsed, time.time())
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            break
        yield _Ancestor(pid, comm, started)
        if ppid == pid:
            break
        pid = ppid


def _project_for_hook(cwd, env, ancestors=None):
    if env.get('TMUX_PANE'):
        return _project(cwd, {**env, 'TMUX': '1'})
    try:
        shown = _tmux('list-panes', '-a', '-F', '#{pane_pid}\t#{@project}')
    except (OSError, subprocess.SubprocessError):
        return None, False
    if shown.returncode:
        return None, _no_tmux_server(shown)
    panes = {}
    for line in shown.stdout.splitlines():
        pane, tab, project = line.partition('\t')
        if tab:
            panes.setdefault(pane, set()).add(project)
    for ancestor in _ancestors() if ancestors is None else ancestors:
        if projects := panes.get(str(ancestor.pid)):
            return _project_id(next(iter(projects))) if len(projects) == 1 else (None, False)
    return _project(cwd, env)


def _source_session(ancestors, env):
    for pid, comm, _ in ancestors:
        if comm == 'claude':
            source = 'claude'
            break
        if comm.startswith('codex'):
            source = 'codex'
            break
    else:
        if env.get('CODEX_THREAD_ID'):
            source = 'codex'
        elif env.get('CLAUDE_CODE_SESSION_ID') or env.get('CLAUDECODE') == '1':
            source = 'claude'
        else:
            return 'terminal', None
        pid = None
    session = env.get('CLAUDE_CODE_SESSION_ID' if source == 'claude' else 'CODEX_THREAD_ID', '')
    if SESSION.fullmatch(session):
        return source, session
    claude_pid = env.get('CLAUDE_PID', '')
    if source == 'claude' and re.fullmatch(r'[0-9]+', claude_pid):
        return source, f'claude-pid-{claude_pid}'
    if pid is not None:
        return source, f'{source}-pid-{pid}'
    if pane := re.fullmatch(r'%([0-9]+)', env.get('TMUX_PANE', '')):
        return source, f'{source}-pane-{pane[1]}'
    return source, None


def _message_verdict(lines):
    subject = next((line for line in lines if line.strip()), '')
    if subject.startswith(FIXUP_PREFIXES):
        return 'skip', 'fixup'
    return ('would_block', 'no-line') if stamp.message_status('\n'.join(lines)) == 'missing' else ('pass', '')


def _final_verdict(path, cwd, git_dir):
    skip = None
    if (git_dir / 'MERGE_HEAD').exists():
        skip = ('skip', 'merge')
    elif any((git_dir / name).exists() for name in ('CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply')):
        skip = ('skip', 'replay')
    try:
        with open(Path(cwd) / path, 'rb', opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK)) as stream:
            if not stat_module.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Git commit message is not a regular file')
            message = stream.read(HOOK_MESSAGE_BYTES + 1)
    except (OSError, ValueError):
        if skip:
            return *skip, ()
        raise
    if len(message) > HOOK_MESSAGE_BYTES:
        return *(skip or ('unknown', 'size')), ()
    try:
        lines = message.decode('utf-8').split('\n')
    except UnicodeDecodeError:
        return *(skip or ('unknown', 'encoding')), ()
    config = _git(cwd, 'config', '--null', '--get-regexp', r'^core\.comment(char|string)$', text=False)
    if config is None or config.returncode not in (0, 1):
        if skip:
            return *skip, ()
        raise GateError(400, 'Git could not read the commit comment configuration.')
    comments = {item.partition(b'\n')[2].decode('utf-8', errors='replace')
                for item in config.stdout.split(b'\0') if item}
    chars = {'#'} | {char for char in comments if len(char) == 1}
    cuts = {}
    for index, line in enumerate(lines):
        if SCISSORS.fullmatch(line):
            cuts.setdefault(line[0], index)
    readings = [lines]
    for char in chars:
        stripped = [line for line in lines if not line.startswith(char)]
        if len(stripped) != len(lines):
            readings.append(stripped)
        if char in cuts:
            truncated = lines[:cuts[char]]
            readings.extend((truncated, [line for line in truncated if not line.startswith(char)]))

    if skip:
        return *skip, readings
    verdict = _message_verdict(lines)
    # Git reads cleanup and comment settings before prepare-commit-msg can change them. Use a verdict only
    # when every reading agrees. An unknown verdict never counts as stamped or unstamped.
    if any(_message_verdict(reading) != verdict for reading in readings[1:]):
        return 'unknown', 'cleanup', readings
    # Git cleanup only deletes lines and trailing whitespace.
    # A landed stamp or autosquash subject must already be present on a raw line.
    if verdict[0] == 'would_block' and any(line.startswith(FIXUP_PREFIXES)
                                         or stamp.message_status(line.rstrip()) != 'missing' for line in lines):
        return 'unknown', 'cleanup', readings
    return *verdict, readings


def _hook_identity(cwd):
    # Git supplies GIT_INDEX_FILE to the hook, including the temporary index for partial commits.
    tree = _git(cwd, 'write-tree')
    if tree is None or tree.returncode or not GIT_OID.fullmatch(tree.stdout.strip()):
        return {}
    head = _git(cwd, 'rev-parse', '--verify', '-q', 'HEAD')
    if head is None:
        return {}
    if not head.returncode and GIT_OID.fullmatch(head.stdout.strip()):
        return {'tree': tree.stdout.strip(), 'head': head.stdout.strip()}
    if head.returncode == 1 and not (head.stdout or head.stderr):
        return {'tree': tree.stdout.strip(), 'head': None}
    return {}


def commit_msg(path, env=os.environ, cwd=None, now=None):
    """Return (exit code, stderr text). Only code 3 tells the shell wrapper to block."""
    record = {'event': 'check', 'via': 'commit-msg', 'source': None, 'id': None, 'session': None,
              'project': None, 'mode': None, 'count': 1, 'folder': None}
    try:
        now = time.time() if now is None else now
        record['at'] = now  # PostToolUse compares it with the start of the deferred call.
        cwd = os.path.abspath(os.getcwd() if cwd is None else cwd)
        record['folder'] = cwd  # Git starts the hook at the worktree root.
        ancestors = tuple(_ancestors())
        pid, sure = _project_for_hook(cwd, env, ancestors)
        record['project'] = pid
        if not sure:
            record['project_unsure'] = True
        mode = resolve(pid if sure else None)['mode']
        if mode == 'off':
            return 0, ''
        record['mode'] = mode
        source, session = _source_session(ancestors, env)
        record.update(source=source, session=session)
        record.update(_hook_identity(cwd))
        if author_date := re.fullmatch(r'@(\d+ [+-]\d{4})', env.get('GIT_AUTHOR_DATE', '')):
            record['author_date'] = author_date[1]
        git_process = next((ancestor for ancestor in ancestors if os.path.basename(ancestor.comm) == 'git'), None)
        if git_process is not None and git_process.started is not None:
            record['started'] = math.floor(git_process.started) - 1  # Allow for clock tick rounding.
        if env.get('GIT_COMMITTER_DATE'):
            ident = _git(cwd, 'var', 'GIT_COMMITTER_IDENT', env=env)
            date = (re.search(r' (\d+ [+-]\d{4})\Z', ident.stdout.rstrip('\n'))
                    if ident is not None and ident.returncode == 0 else None)
            if date is None:
                record['committer_override'] = True
            else:
                record['committer_date'] = date[1]
        git_dir = _git_path(cwd, '--git-dir')
        if git_dir is None:
            raise GateError(400, 'Git could not find the repository for the commit message.')
        verdict, reason, readings = _final_verdict(path, cwd, git_dir)
        if (verdict, reason) == ('skip', 'merge'):
            merge_head = _read_message_file(git_dir, 'MERGE_HEAD')
            if merge_head is not None:
                heads = merge_head.removesuffix('\n').split('\n')
                if all(GIT_OID.fullmatch(oid) for oid in heads):
                    record['merge_heads'] = heads
        if 'tree' in record and readings and (key := _message_key(create=True)) is not None:
            normalized = {_normalize_message(reading) for reading in readings}
            record['messages'] = sorted(_message_digest(key, message) for message in normalized)
        blocks = None
        if verdict == 'would_block' and mode == 'enforce' and source != 'terminal' and session is None:
            verdict, reason = 'let_through', 'no-session'
        elif verdict == 'would_block' and mode == 'enforce' and sure:
            verdict = 'block'
            if session is not None:
                with _Lock():
                    state = _read_session(session, now)
                    if state['blocks'] >= MAX_BLOCKS:
                        verdict = 'let_through'
                    else:
                        state['blocks'] += 1
                        blocks = state['blocks']
                        _write_session(session, state, now)
        _log(dict(record, verdict=verdict, reason=reason))
        if verdict == 'block':
            message = ('Tether commit gate: this commit message has no valid "Verified:" line. Fix: add a last line '
                       '"Verified: none (reason)", or a stamp such as "Verified: tests(server 412 ok)", and commit again.')
            if blocks is not None:
                message += f'\nBlock {blocks} of {MAX_BLOCKS} in this session.'
            return 3, message
    except Exception:
        try:
            _log(dict(record, verdict='unknown', reason='error'))
        except Exception:
            pass
    return 0, ''


def install_hook(repo, uninstall=False, gate=None, python=None, *, deadline=10):
    """Install the hook in Git's metadata or an external hooks directory."""
    if type(deadline) is not int or deadline <= 0:
        raise ValueError('Hook deadline must be a positive number of seconds.')
    tree = _tree(repo)
    if tree is None:
        raise GateError(400, 'Choose a Git work tree.')
    hooks = _git_path(tree, '--git-path', 'hooks')
    common = _git_path(tree, '--git-common-dir')
    if hooks is None or common is None:
        raise GateError(400, 'Git could not find the hooks directory.')
    hooks = hooks.resolve()
    if hooks.is_relative_to(Path(tree).resolve()) and not hooks.is_relative_to(common.resolve()):
        raise GateError(400, 'The hooks directory is inside the work tree. Choose a hooks directory outside it.')
    hook = hooks / 'commit-msg'
    lock = hooks / '.tether-commit-msg.lock'
    gate = shlex.quote(str(Path(__file__).resolve() if gate is None else Path(gate).absolute()))
    python = shlex.quote(str(Path(sys.executable if python is None else python).absolute()))
    helper = shlex.quote(str(HERE / 'commit_msg_hook.sh'))
    call = f'/bin/sh {helper} "$1"'
    refusal = (f'Tether cannot replace {hook}. Before the hook exits with status 0, add this line to your hook:\n'
               f'{call} || [ $? -ne 1 ] || exit 1  {CALL_MARK}')
    script = f'''#!/bin/sh
{HOOK_MARK}
# Written by Tether (server/commit_gate.py install-hook).
[ -f {helper} ] && [ -r {helper} ] || exit 0
exec {call} {python} {gate} {deadline}
'''
    hooks.mkdir(parents=True, exist_ok=True)
    try:
        with _Lock(lock, HOOK_LOCK_SECONDS):
            exists = os.path.lexists(hook)
            existing = _owned_hook(hook) if exists else None
            ours = existing is not None
            if uninstall:
                if not ours:
                    return 'No Tether commit-msg hook to uninstall.'
                # Move the hook aside before the final check, so a hook placed without our lock is never deleted.
                backup = hook.with_name(f'{hook.name}.tether-old.{os.getpid()}')
                if os.path.lexists(backup):
                    raise GateError(400, f'Tether cannot uninstall {hook} while {backup} exists.')
                os.rename(hook, backup)
                if _owned_hook(backup) is None:
                    try:
                        os.link(backup, hook, follow_symlinks=False)
                    except OSError as exc:
                        raise GateError(400, f'Tether could not restore {hook}. The hook is at {backup}.') from exc
                    backup.unlink()
                    return 'No Tether commit-msg hook to uninstall.'
                backup.unlink()
                return 'Uninstalled the Tether commit-msg hook.'
            if exists and not ours:
                raise GateError(400, refusal)
            if existing == script.encode() and os.access(hook, os.X_OK):
                return 'The Tether commit-msg hook is already installed.'
            with tempfile.NamedTemporaryFile(dir=hooks, prefix='.commit-msg.', mode='w', encoding='utf-8',
                                             delete=False) as out:
                temp = Path(out.name)
                try:
                    out.write(script)
                    os.fchmod(out.fileno(), 0o755)
                    out.close()
                    backup = None
                    if ours:
                        backup = hook.with_name(f'{hook.name}.tether-old.{os.getpid()}')
                        if os.path.lexists(backup):
                            raise GateError(400, f'Tether cannot update {hook} while {backup} exists.')
                        # A commit between the rename and the link runs without the hook (fail-open).
                        os.rename(hook, backup)
                        if _owned_hook(backup) is None:
                            try:
                                os.link(backup, hook, follow_symlinks=False)
                            except OSError as exc:
                                raise GateError(400, f'Tether could not restore {hook}. The hook is at {backup}.') from exc
                            backup.unlink()
                            raise GateError(400, refusal)
                    try:
                        os.link(temp, hook)
                    except FileExistsError:
                        if backup is not None:
                            backup.unlink()
                        raise GateError(400, refusal) from None
                    except OSError as exc:
                        if backup is not None:
                            raise GateError(400, f'Tether could not update {hook}. The hook is at {backup}.') from exc
                        raise
                    if backup is not None:
                        backup.unlink()
                finally:
                    temp.unlink(missing_ok=True)
            return ('Updated' if ours else 'Installed') + ' the Tether commit-msg hook.'
    except TimeoutError:
        raise GateError(400, f'Timed out waiting for {lock}.') from None


# ---- the numbers for Settings ----

def _rows(*, locked=False):
    path = log_path()
    sources = (path.with_name(path.name + '.1'), path)

    def snapshot(*, strict=False):
        files = []
        for source in sources:
            try:
                files.append(source.read_bytes())
            except FileNotFoundError:
                continue
            except OSError:
                if strict:
                    raise
        return files

    def marks():
        found = []
        for source in sources:
            try:
                info = source.stat()
            except FileNotFoundError:
                found.append(None)
            else:
                found.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        return found

    try:
        with nullcontext() if locked else _Lock():
            return _parse_rows(snapshot())
    except TimeoutError:
        pass

    # Rotation can move the current file over .1 between the reads. Check the whole pair before parsing it.
    for _ in range(3):
        try:
            before = marks()
            files = snapshot(strict=True)
            if before == marks():
                return _parse_rows(files)
        except OSError:
            break
    return None


def _parse_rows(files):
    for content in files:
        for line in content.splitlines():
            try:
                row = json.loads(line.decode('utf-8', errors='replace'))
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get('at'), (int, float)):
                yield row


def stats(pid=None, days=DAYS, now=None):
    """The last `days` days of the log, for one project or for all. None when no consistent snapshot is available.

    The counts cover final-message checks and Bash commit calls. Skip rows are excluded. Final-message checks
    count passes as stamped and would_block or let_through as unstamped. They need no landed row.
    For Bash calls, the rate uses only pass, would_block and let_through with one commit in the command,
    one commit landed and no other call of the session in that repository at the same time.
    false_blocks: 'would_block' and 'let_through' verdicts whose commit landed with a valid line.
    rate: false_blocks / stamped (such commits with a valid line), or None with no such commit yet."""
    now = time.time() if now is None else now
    since = now - days * 86400
    rows = _rows()
    if rows is None:
        return None
    checks, results = [], {}
    for row in rows:
        if row['at'] < since or (pid and row.get('project') != pid):
            continue
        if row.get('event') == 'check' and isinstance(row.get('verdict'), str):
            checks.append(row)
        elif row.get('event') == 'landed' and isinstance(row.get('id'), str):
            results[row['id']] = row
    counts = {name: 0 for name in ('pass', 'would_block', 'block', 'let_through', 'unknown')}
    stamped = false = unstamped = 0
    for row in checks:
        if row['verdict'] not in counts:
            continue
        counts[row['verdict']] += 1
        if row.get('via') in ('commit-msg', 'landed'):
            stamped += row['verdict'] == 'pass'
            unstamped += row['verdict'] in ('would_block', 'let_through')
            continue
        result = results.get(row.get('id'))
        if (not result or row['verdict'] not in ('pass', 'would_block', 'let_through') or row.get('count') != 1
                or result.get('count') != 1 or result.get('shared')):
            continue
        if result.get('landed') in ('verified', 'none'):
            stamped += 1
            false += row['verdict'] != 'pass'
        elif result.get('landed') == 'missing':
            unstamped += 1
    return {'days': days, 'commits': sum(counts.values()), 'passed': counts['pass'], 'would_block': counts['would_block'],
            'blocked': counts['block'], 'let_through': counts['let_through'], 'unknown': counts['unknown'],
            'stamped': stamped, 'unstamped': unstamped, 'false_blocks': false,
            'rate': round(false / stamped, 4) if stamped else None}


_stats_cache = {}
_stats_lock = threading.Lock()


def cached_stats(pid=None):
    """stats(pid) for the page, read again only when a log file changed or a minute passed."""
    path, marks = log_path(), []
    for source in (path, path.with_name(path.name + '.1')):
        try:
            found = source.stat()
            marks.append((found.st_size, found.st_mtime_ns))
        except OSError:
            marks.append(None)
    key, clock = (str(path), pid), time.monotonic()
    with _stats_lock:
        kept = _stats_cache.get(key)
        if kept and kept[0] == marks and clock - kept[1] < 60:
            return kept[2]
        value = stats(pid)
        if value is None:
            return None
        if len(_stats_cache) > 64:
            _stats_cache.clear()
        _stats_cache[key] = (marks, clock, value)
        return value


# ---- HTTP (/mobile/api/commit-gate). server.py checks sign-in, Origin and X-Codex-Mobile first. ----

PATH = '/mobile/api/commit-gate'


def handles(path):
    return path.split('?', 1)[0] == PATH


def state(pid=None):
    resolved = resolve(pid)
    return {'modes': [{'mode': mode, 'name': name, 'summary': summary} for mode, name, summary in MODES],
            'global': resolved['global'], 'mode': resolved['mode'], 'source': resolved['source'],
            'project': resolved['project'], 'stats': cached_stats(pid), 'max_blocks': MAX_BLOCKS}


def get(handler):
    from urllib.parse import parse_qs, urlsplit
    pid = parse_qs(urlsplit(handler.path).query).get('project', [None])[0]
    try:
        if pid is not None:
            project_path(pid)
        handler.respond(200, state(pid))
    except GateError as exc:
        handler.respond(exc.status, {'error': str(exc)})


def post(handler):
    """{mode} sets the global mode. {project, override, mode} sets a project override."""
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 4096:
            raise GateError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise GateError(400, 'Invalid request.')
        pid = body.get('project')
        if pid is None:
            set_global(body.get('mode'))
        else:
            set_project(pid, body.get('override'), body.get('mode'))
        handler.respond(200, state(pid))
    except GateError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not save the commit gate mode.'})


def _main(args):
    if args[:1] == ['commit-msg']:
        try:
            if hasattr(signal, 'SIGALRM') and hasattr(signal, 'alarm'):
                signal.signal(signal.SIGALRM, lambda *a: os._exit(0))
                signal.alarm(5)
            if len(args) == 2:
                code, message = commit_msg(args[1], cwd=os.getcwd())
                if message:
                    print(message, file=sys.stderr)
                return code
        except Exception:
            pass
        return 0
    if args[:1] == ['install-hook']:
        import argparse
        parser = argparse.ArgumentParser(prog='commit_gate.py install-hook')
        parser.add_argument('--repo', default='.')
        parser.add_argument('--uninstall', action='store_true')
        options = parser.parse_args(args[1:])
        try:
            print(install_hook(options.repo, uninstall=options.uninstall))
        except (GateError, OSError) as exc:
            print(f'Tether commit gate: {exc}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
