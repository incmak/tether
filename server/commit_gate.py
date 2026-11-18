#!/usr/bin/env python3
"""Commit gate (B204): does a Claude `git commit` message end with a valid "Verified:" line (stamp.py)?

The person sets the mode in Settings. Nothing else changes it.
  off      no check, no log.
  observe  the default. Each commit is checked and logged; none is blocked.
  enforce  a commit with no valid line is denied, with the reason. Two denials at most in one Claude session:
           the third such commit passes and is logged, so a wrong gate cannot hold a session.

  paths.CONFIG/commit-gate                  the global mode: one word. Missing or bad: observe.
  paths.TETHER/projects/<id>/commit-gate    a project override: {"override": true, "mode": "enforce"}.
  paths.COMMIT_GATE_LOG                     one JSON line per check and per landed commit. No message text.
  paths.CACHE/commit-gate/<session>.json    the session's denial count and its commits that wait for their result.

claude_hooks.py calls hook() for Bash PreToolUse and PostToolUse events whose command names `git` and `commit`.
The gate reads the command, never the screen. It denies only when it read the whole message from the command, that
message has no valid line, and nothing can add one later. A message it cannot read for certain passes with the
verdict "unknown": a shell variable, --amend --no-edit, an editor, --trailer, an option the gate does not know, a
shell comment, a commit hook in the repository. Any error, a busy lock, a failed project lookup and a hook timeout
let the commit through.

After the command, the gate reads the commits that landed. A false block is a "would block" or "let through" verdict
whose commit has a valid line. The rate in Settings is false blocks / landed commits with a valid line, over the
calls where git can say which commit the call made (see stats()). Two sessions that commit in one working tree at
the same moment can still mix their results; sessions use their own worktrees.

Codex commits are not checked: this is a Claude Code hook."""
import fcntl
import json
import math
import os
import re
import stat as stat_module
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import paths  # noqa: E402
import stamp  # noqa: E402

MODES = (
    ('off', 'Off', 'Commits are not checked.'),
    ('observe', 'Observe', 'Each Claude commit is checked and logged. No commit is blocked.'),
    ('enforce', 'Enforce', 'A Claude commit with no valid "Verified:" line is blocked. '
                           'After 2 blocks in one session, the next one passes.'),
)
NAMES = tuple(mode for mode, _, _ in MODES)
DEFAULT = 'observe'
MAX_BLOCKS = 2  # denials in one session; the next commit with no line passes
DAYS = 7
LOG_BYTES = 2_000_000  # the log moves to commit-gate.log.1 above this size
LOCK_SECONDS = 0.3
PENDING_SECONDS = 3600  # a commit command that has not ended after this long gets no result
PENDING_MAX = 50
STATE_DAYS = 45  # cleanup age for sessions with no blocks
PROJECT_ID = re.compile(r'[0-9a-f]{12}')
SESSION = re.compile(r'[a-zA-Z0-9-]{1,80}')


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
REDIRECTION = re.compile(rf'(?:[0-9]{LINE_JOIN})?(?:>{LINE_JOIN}[>&]|<{LINE_JOIN}&|[<>])')
COMPOUND_OPERATOR = re.compile(rf'&{LINE_JOIN}[&>]|\|{LINE_JOIN}[|&]|;{LINE_JOIN}[;&]')
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
WRAPPERS = {'command', 'env', 'time', 'nohup'}
KEYWORDS = set('if then else elif fi for while until do done case esac function select time coproc ! [[ ]]'.split())
KEYWORD_PREFIXES = {'if', 'then', 'else', 'elif', 'while', 'until', 'do', 'time', '!', '{'}
SHELL_COMMANDS = {'eval', 'source', '.', 'exec', 'xargs', 'sh', 'bash', 'zsh'}
GIT_VALUE_OPTIONS = {'-c', '--namespace', '--super-prefix', '--config-env', '--exec-path'}
GIT_PLACE_OPTIONS = {'--git-dir', '--work-tree'}  # they move the repository: the folder is then not known
# `git commit` options that cannot change the message. An option that is not listed here makes the verdict unknown.
COMMIT_VALUE_OPTIONS = {'--author', '--date', '--pathspec-from-file'}
COMMIT_FLAGS = {'--all', '--patch', '--signoff', '--no-signoff', '--no-verify', '--verify', '--allow-empty',
                '--allow-empty-message', '--quiet', '--verbose', '--amend', '--no-edit', '--no-gpg-sign', '--gpg-sign',
                '--include', '--only', '--no-post-rewrite', '--status', '--no-status', '--reset-author',
                '--untracked-files', '--pathspec-file-nul'}
SHORT_FLAGS = set('apsvqnzio')  # single letters of `git commit` that take no value and leave the message alone
FILE_BYTES = 65536
MESSAGE_HOOKS = ('prepare-commit-msg', 'commit-msg')


def _literal(text):
    return not re.search(r'[$`]', text)


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


def _word(command, start, bodies):
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
                expands = char == '$' and re.match(r'[A-Za-z0-9_{(!#?*@$-]', command[index + 1:index + 2] or ' ')
                plain = plain and char != '`' and not expands
            elif (not quote and char == '~' and index == start and 'HOME=' not in command
                  and command[index + 1:index + 2] in ('', '/', ' ', '\t', '\n', ';', '&', '|', ')')):
                parts.append(os.path.expanduser('~'))  # the home folder, as the shell writes it for ~ and ~/x
                index += 1
                continue
            elif not quote:
                plain = plain and char not in SHELL_SPECIAL and char.isprintable() and not (char == '=' and not parts)
                if char == '@' and HEREDOC_FOUND.match(command, index):
                    plain = False
            parts.append(char)
        index += 1
    if quote:
        raise ValueError('unclosed shell quote')
    return _Word(''.join(parts), plain or index == literal_doc, bare, fill), index


def _tokens(command, bodies):
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
            if operator.endswith('<') and (doc := _heredocs(command, start, bodies, cat=False)) is not None:
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
            word, index = _word(command, index, bodies)
            yield word


def _segments(command, bodies):
    """Return simple commands with the preceding operator, stdin and redirections, and flag uncertain syntax."""
    tokens = list(_tokens(command, bodies))
    segments, words, stdin, index, uncertain, redirects_plain = [], [], None, 0, False, True
    preceding, redirected = None, False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if isinstance(token, _Word):
            uncertain = uncertain or (token.bare and token.text in ('{', '}'))
            words.append(token)
        elif REDIRECTION.fullmatch(token):
            redirected = True
            if index >= len(tokens) or not isinstance(tokens[index], _Word):
                uncertain = True
                continue
            target = tokens[index]
            index += 1
            on_stdin = token.startswith('0') or token.startswith('<')
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
    """None when `words` is not `git ... commit`. Else (commit arguments, [-C folders], folder known)."""
    text = [word.text for word in words]
    index, known = 0, True
    while index < len(text) and (ASSIGNMENT.fullmatch(text[index]) or text[index] in WRAPPERS):
        name = text[index].split('=', 1)[0]
        if name.startswith('GIT_CONFIG') or name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
            known = False  # a command-local repository or config is not the one the hook probes
        index += 1
    if index >= len(text) or os.path.basename(text[index]) != 'git':
        return None
    index += 1
    folders = []
    while index < len(text) and text[index].startswith('-'):
        option = text[index]
        if option == '-C':
            if index + 1 >= len(words):
                return None
            folders.append(words[index + 1])
            index += 2
        else:
            name = option.split('=', 1)[0]
            if name in GIT_PLACE_OPTIONS or name in ('-c', '--config-env'):
                known = False
            index += 2 if option in GIT_VALUE_OPTIONS or option in GIT_PLACE_OPTIONS else 1
    if index >= len(text) or text[index] != 'commit':
        return None
    return words[index + 1:], folders, known


def _commit_arguments(args):
    """{'messages': [...], 'files': [...], 'trailers': [...], 'flags': set()} from the words after `git commit`.

    Flags: 'skip' (no commit), 'reuse', 'fixup', 'amend', 'no-edit', and 'open': git or an editor can still change
    the message, or the gate does not know the option."""
    found = {'messages': [], 'files': [], 'trailers': [], 'flags': set()}
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
            elif name in COMMIT_FLAGS:
                if name in ('--amend', '--no-edit'):
                    found['flags'].add(name[2:])
            else:  # --edit, --template, --cleanup, a short form of a long option, a new option
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
                if letter in 'uS':  # an optional value, attached only
                    break
                if letter == 'h':
                    found['flags'].add('skip')
                elif letter not in SHORT_FLAGS:  # -e, -t, an unknown letter
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
    """The text of a message file, or None. Regular files only: a pipe would hold the hook."""
    try:
        path = Path(folder) / name
        with open(path, 'rb', opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK)) as source:
            found = os.fstat(source.fileno())
            if not stat_module.S_ISREG(found.st_mode) or found.st_size > FILE_BYTES:
                return None
            content = source.read(FILE_BYTES + 1)
            return content.decode('utf-8', errors='replace') if len(content) <= FILE_BYTES else None
    except (OSError, ValueError):
        return None


def _one_commit(args, stdin, bodies, folder):
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
        if not all(part.plain or (_literal(part.text) and '\\\n' not in part.text) for part in found['messages']):
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
        elif word.plain and not HEREDOC_FOUND.search(name) and folder is not None:
            text = _read_message_file(folder, name)
        if text is None:
            return 'unknown', 'file'
    elif 'reuse' in flags or ('amend' in flags and 'no-edit' in flags):
        return 'unknown', 'reuse'
    else:
        return 'unknown', 'editor'
    if stamp.message_status(text) != 'missing':
        return 'pass', ''
    if 'open' in flags or found['trailers'] or 'reuse' in flags:
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
        return os.path.realpath(path, strict=True) if os.path.isdir(path) else None
    except OSError:
        return None


def _cd(folder, target):
    """(where a plain `cd target` leads from `folder`, went through a link), or (None, True).

    The shell follows `..` by name and git by the real path. Without `..` the two agree, so a link is fine there;
    a later cd from a linked folder is not followed. With `..`, every step must be a real folder: the shell refuses
    `cd missing/..`."""
    if '..' not in target.split('/'):
        logical = os.path.normpath(os.path.join(folder, target))
        real = _real_directory(logical)
        return real, real != logical
    path = '/' if target.startswith('/') else folder
    for part in target.split('/'):
        if part in ('', '.'):
            continue
        path = os.path.dirname(path) if part == '..' else os.path.join(path, part)
        if _real_directory(path) != path:
            return None, True
    return path, False


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
    linked = folder is not None and folder != os.path.normpath(cwd)
    bodies = []
    try:
        segments, uncertain = _segments(command, bodies)
    except ValueError:
        # Quotes that do not close. Report it only when the text looks like a commit at all.
        if re.search(r'(?:^|[\s;&|(])git\s[^\n;|&]*\bcommit(?:\s|$)', command):
            return {'verdict': 'unknown', 'reason': 'parse', 'count': 1, 'folder': None}
        return None
    # `alone`: only plain `cd` commands ran before. Then no earlier command can have written a message file.
    verdicts, where, alone = [], None, True
    # A cd is followed only when it runs for certain, in the shell itself, to a folder the gate can name:
    # - not when the command has a background `&` (bash can put a whole && list in the background);
    # - not when the command sets HOME (the gate's ~ would be the old one);
    # - not after `x &&` unless x is a cd too (`test -f a && cd b; git commit` can skip the cd);
    # - not after || or in a pipe (`cd a || cd b`, `cd a | x`).
    steady = not any(segment[0] == '&' for segment in segments) and 'HOME=' not in command
    chained = True  # every command so far was a followed cd
    for number, (preceding, words, stdin, redirects_plain, redirected) in enumerate(segments):
        if not words:
            chained = False
            continue
        first = words[0]
        uncertain = uncertain or (first.bare and first.text in KEYWORDS)
        all_plain = redirects_plain and all(word.plain for word in words)
        if first.text == 'cd' and len(words) == 2 and all_plain and not redirected:
            # Only a cd moves the shell. It is followed when it runs for certain and in the shell itself: not after
            # || or in a pipe or in the background (`cd a || cd b`, `cd a | x`, `cd a &`).
            following = segments[number + 1][0] if number + 1 < len(segments) else None
            target = words[1]
            if (folder is not None and not linked and steady and following in FOLDER_SEPARATORS
                    and (preceding in (None, ';', '\n') or (preceding == '&&' and chained))
                    and not target.fill and target.text and not target.text.startswith('-')):
                folder, linked = _cd(folder, target.text)
            else:
                folder = None
            chained = chained and folder is not None
            continue
        chained = False
        while words and words[0].bare and words[0].text in KEYWORD_PREFIXES:
            words = words[1:]
        uncertain = uncertain or _shell_launcher(words)
        commit = _git_commit(words) if words else None
        if commit:
            args, folders, known = commit
            here = folder if known else None
            for item in folders:
                here = (_real_directory(os.path.join(here, item.text))
                        if here is not None and item.plain and not item.fill else None)
            verdict, reason = _one_commit(args, stdin, bodies, here if alone else None)
            if verdict != 'skip':
                if verdict != 'pass' and not all_plain:
                    verdict, reason = 'unknown', 'shell'
                verdicts.append((verdict, reason))
                where = here if len(verdicts) == 1 or where == here else None
        # A simple command cannot move the shell, except these. Any command can write a message file.
        moving = ('cd', 'pushd', 'popd', 'source', '.', 'eval', 'builtin')  # these can move the shell; not followed
        if first.text in moving or (first.text == 'command' and len(words) > 1 and words[1].text in moving):
            folder = None
        alone = False
    if not verdicts:
        return None
    kinds = {verdict for verdict, _ in verdicts}
    result = {'count': len(verdicts), 'folder': where}
    if kinds == {'pass'}:
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


class _Lock:
    """One lock for the session files, their cleanup and the log rotation. It waits LOCK_SECONDS at most; then
    TimeoutError goes up to the hook, which lets the commit through."""

    def __enter__(self):
        folder = _state_folder()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stream = open(folder / '.lock', 'a')
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.stream.close()
                    raise TimeoutError('commit gate lock') from None
                time.sleep(0.005)

    def __exit__(self, kind, error, trace):
        self.stream.close()


def _log(record):
    path = log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = (json.dumps(record, separators=(',', ':')) + '\n').encode()
    with _Lock():
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
    """{'blocks': int, 'pending': {tool id: {...}}}. Call it with the lock held."""
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


def _git(folder, *args, text=True):
    try:
        return subprocess.run(['git', '-C', folder, *args], capture_output=True, text=text, timeout=0.5,
                              errors='replace' if text else None)
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


def _message_hook(folder):
    """True when the repository has a hook that can change the message, False when it has none, None when git
    cannot tell."""
    shown = _git(folder, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks', text=False)
    if shown is None or shown.returncode != 0:
        return None
    hooks = Path(os.fsdecode(shown.stdout).removesuffix('\n'))
    if not hooks.is_absolute():
        return None
    try:
        return any(os.access(hooks / name, os.X_OK) and (hooks / name).is_file() for name in MESSAGE_HOOKS)
    except OSError:
        return None


def _landed(folder, head):
    """[status] for the commits made since `head`, newest first: 'verified', 'none' or 'missing' each. [] when no
    commit landed. None when git cannot tell."""
    shown = _git(folder, 'log', '--first-parent', '-n', '20', '--format=%B%x1e', f'{head}..HEAD' if head else 'HEAD')
    if shown is None or shown.returncode != 0:
        return [] if head == '' else None
    return [stamp.message_status(body) for body in shown.stdout.split('\x1e') if body.strip()]


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
                return None, 'no server running' in shown.stderr or 'No such file or directory' in shown.stderr
            found = {fields[1] for fields in (line.split('\t') for line in shown.stdout.splitlines())
                     if len(fields) == 2 and fields[0] == cwd}
            if len(found) != 1:
                return None, False
            pid = next(iter(found))
    except (OSError, subprocess.SubprocessError):
        return None, False
    if not pid:
        return None, True
    if not PROJECT_ID.fullmatch(pid):
        return None, False
    if project_path(pid).exists() and read_project(pid) is None:
        return pid, False  # a file that the gate cannot read
    return pid, True


def check(data, env=os.environ, now=None):
    """PreToolUse: log the verdict. Returns the hook's stdout: a deny in enforce mode, else ''."""
    now = time.time() if now is None else now
    found = evaluate(data['tool_input']['command'], data.get('cwd'))
    if found is None:
        return ''
    pid, sure = _project(data.get('cwd'), env)
    mode = resolve(pid if sure else None)['mode']
    if mode == 'off':
        return ''
    verdict, reason, folder = found['verdict'], found['reason'], found['folder']
    tree = _tree(folder) if folder is not None else None
    head = _head(folder) if tree is not None else None
    if (verdict == 'missing' and head is None) or (reason == 'file' and folder is None):
        verdict, reason = 'unknown', 'folder'
    elif verdict == 'missing':
        hooked = _message_hook(folder)
        if hooked is not False:
            verdict, reason = 'unknown', 'hook' if hooked else 'folder'
    elif verdict == 'pass' and tree is not None and _message_hook(folder):
        verdict, reason = 'unknown', 'hook'  # the hook can also remove the line
    session = data.get('session_id')
    session = session if isinstance(session, str) and SESSION.fullmatch(session) else None
    tool = data.get('tool_use_id') if isinstance(data.get('tool_use_id'), str) else None
    action, blocks = {'pass': 'pass', 'unknown': 'unknown', 'missing': 'would_block'}[verdict], 0
    if session:
        with _Lock():
            state = _read_session(session, now)
            before = json.dumps(state, sort_keys=True)
            if verdict == 'missing' and mode == 'enforce' and sure:
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
            if json.dumps(state, sort_keys=True) != before:
                _write_session(session, state, now)
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
    try:
        statuses = _landed(waiting['folder'], waiting['head'])
    finally:
        with _Lock():
            state = _read_session(session, now)
            waiting = state['pending'].pop(tool, None)
            if waiting is not None:
                _write_session(session, state, now)
    if statuses is None or waiting is None:
        return
    worst = next((kind for kind in ('missing', 'none', 'verified') if kind in statuses), 'no-commit')
    record = {'at': round(now, 3), 'event': 'landed', 'id': tool, 'session': session, 'project': waiting.get('project'),
              'landed': worst, 'count': len(statuses)}
    if waiting.get('shared') or 'tree' not in waiting:
        record['shared'] = True
    _log(record)


def hook(data, env=os.environ):
    """The text to print for one hook input. claude_hooks.py catches every error: the gate fails open."""
    if data.get('hook_event_name') == 'PreToolUse':
        return check(data, env)
    landed(data)
    return ''


# ---- the numbers for Settings ----

def _rows():
    path = log_path()

    def snapshot():
        files = []
        for source in (path.with_name(path.name + '.1'), path):
            try:
                files.append(source.read_bytes())
            except OSError:
                continue
        return files

    try:
        with _Lock():
            files = snapshot()
    except TimeoutError:
        files = snapshot()
    for content in files:
        for line in content.splitlines():
            try:
                row = json.loads(line.decode('utf-8', errors='replace'))
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get('at'), (int, float)):
                yield row


def stats(pid=None, days=DAYS, now=None):
    """The last `days` days of the log, for one project or for all.

    The counts are Bash calls that ran `git commit`. The rate uses only the calls where the gate read the message
    (pass, would_block, let_through) and git can say which commit the call made: one commit in the command, one commit
    landed, no other call of the session in that repository at the same time.
    false_blocks: 'would_block' and 'let_through' verdicts whose commit landed with a valid line.
    rate: false_blocks / stamped (such commits with a valid line), or None with no such commit yet."""
    now = time.time() if now is None else now
    since = now - days * 86400
    checks, results = [], {}
    for row in _rows():
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
