"""Ask visible, reusable advisor tabs from an agent's own Tether pane."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import advisor
import codex_level
import paths

TAB_CLI = Path(__file__).resolve().with_name('agent_tab_cli.py')
PANE = re.compile(r'%[0-9]+')
PROMPT = '''You are the advisor for an agent in another Tether tab. It asks you because the evidence it found did not settle
this decision. Check the code, docs and project notes in this folder yourself before you answer. Use web search
when market or platform facts matter. Never guess: when the evidence is not enough, or the question needs the
user's taste, business facts or authority, say so. Do not edit files.
Reply in exactly this format:
ANSWER: <the decision, one paragraph>
CONFIDENCE: high | medium | low
EVIDENCE: <files, docs or facts you checked>
NEEDS_USER: yes | no (<why, when yes>)

Question:
'''
CODES = {'decided': 0, 'ask_user': 7, 'timeout': 3, 'error': 1, 'off': 8}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def arguments(argv):
    parser = Parser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    ask = actions.add_parser('ask')
    ask.add_argument('--critical', action='store_true')
    ask.add_argument('--timeout', type=int, default=600)
    ask.add_argument('--stack', metavar='NAME', help="your goal in 1 to 3 words: a new advisor tab joins that stack "
                     "(made if missing) with this tab; without it, it joins this tab's stack or one named after this tab")
    ask.add_argument('question')
    args = parser.parse_args(argv)
    if not 60 <= args.timeout <= 600:
        parser.error('timeout must be 60 to 600 seconds')
    if args.question == '-':
        args.question = sys.stdin.read()
    if not args.question.strip():
        parser.error('a question is required')
    return args


def caller_context(pane):
    shown = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{@project}'],
                           capture_output=True, text=True, timeout=5)
    pid = shown.stdout.strip()
    if shown.returncode == 0 and codex_level.PROJECT_ID.fullmatch(pid):
        return pid, pid
    shown = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{session_id}'],
                           capture_output=True, text=True, timeout=5)
    key = shown.stdout.strip().removeprefix('$')
    if shown.returncode or not re.fullmatch(r'[0-9]+', key):
        raise ValueError('Could not find the caller\'s tmux session.')
    return key, None


def tab(args, timeout, prompt=None):
    done = subprocess.run([sys.executable, str(TAB_CLI), *args], input=prompt,
                          capture_output=True, text=True, timeout=timeout)
    try:
        result = json.loads(done.stdout)
    except ValueError:
        # agent_tab_cli.py prints errors (such as the helper ownership check) on stderr, not as JSON.
        if done.returncode != 0:
            return done.returncode, {'error': done.stderr.strip() or 'The tab command failed.'}
        raise ValueError(done.stderr.strip() or 'The tab command returned invalid JSON.') from None
    if not isinstance(result, dict):
        raise ValueError('The tab command returned invalid JSON.')
    return done.returncode, result


def parse_reply(raw):
    """Use the last occurrence of each label. Incomplete or malformed replies cannot decide."""
    labels = list(re.finditer(r'^\s*(ANSWER|CONFIDENCE|EVIDENCE|NEEDS_USER):[ \t]*', raw, re.I | re.M))
    fields = {}
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(raw)
        fields[label[1].upper()] = raw[label.end():end].strip()
    # Agents may end a reply with a footer of their own (for example a "Needs you:" line), so the one-word fields
    # stop at the first line and NEEDS_USER stops at the first blank line.
    confidence = fields.get('CONFIDENCE', '').split('\n', 1)[0].strip().lower()
    needs = re.split(r'\n\s*\n', fields.get('NEEDS_USER', ''), maxsplit=1)[0].strip()
    complete = (all(fields.get(key) for key in ('ANSWER', 'CONFIDENCE', 'EVIDENCE', 'NEEDS_USER'))
                and confidence in ('high', 'medium', 'low') and re.fullmatch(r'(yes|no)(?:\s*\(.*\))?', needs, re.I | re.S))
    return {'answer': fields.get('ANSWER', ''), 'evidence': fields.get('EVIDENCE', ''),
            'confidence': confidence if complete else 'low',
            'needs_user': not complete or not re.match(r'no\b', needs, re.I)}


def result_for(status, replies):
    result = {'status': status, 'answer': replies[0]['answer'] if replies else '',
              'confidence': min((reply['confidence'] for reply in replies),
                                key=('low', 'medium', 'high').index) if replies else 'low',
              'evidence': '\n\n'.join(reply['evidence'] for reply in replies if reply['evidence']),
              'needs_user': status != 'decided',
              'advisors': [{key: reply[key] for key in ('role', 'agent', 'model', 'pane', 'answer', 'confidence',
                                                      'needs_user', 'raw')} for reply in replies]}
    if len(replies) == 2:
        result['agree'] = None  # The caller compares answers; confidence is not agreement.
    return result


def ask(settings, args, key, replies):
    state_path = paths.CACHE / 'advisor' / f'{key}.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    main = settings['main']
    wanted = [('main', dict(main, model=advisor.resolve(main['agent'], main['model'])))]
    second = advisor.second_opinion(settings) if args.critical else None
    if second:
        wanted.append(('second', second))
    prompt = PROMPT + args.question
    # Submit every question before waiting, so both advisors can work at once.
    for role, choice in wanted:
        stored = state.get(role)
        pane = stored.get('pane') if isinstance(stored, dict) else None
        reuse = False
        if isinstance(pane, str) and PANE.fullmatch(pane) and all(stored.get(k) == v for k, v in choice.items()):
            live = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_id}'],
                                  capture_output=True, text=True, timeout=5)
            reuse = live.returncode == 0
        if reuse:
            # Only the tab that started a helper may send to it (helper_tabs.py). After /clear that check fails,
            # so a failed send starts a fresh advisor tab instead.
            code, response = tab(['send', '--pane', pane, '-'], args.timeout, prompt)
            reuse = code == 0
        if not reuse:
            command = ['start', '--agent', choice['agent']]
            if choice['model'] is not None:
                command += ['--model', choice['model']]
            if choice['effort'] != 'auto':
                command += ['--effort', choice['effort']]
            command += ['--role', 'advisor' if role == 'main' else 'second-advisor', '--cwd', os.getcwd()]
            if args.stack is not None:
                command += ['--stack', args.stack]
            command.append('-')
            code, response = tab(command, args.timeout, prompt)
            pane = response.get('pane')
        if code != 0:
            raise ValueError(response.get('error') or 'Could not ask the advisor tab.')
        if not isinstance(pane, str) or not PANE.fullmatch(pane):
            raise ValueError('The tab command returned an invalid pane.')
        state[role] = dict(choice, pane=pane)
        advisor._write(state_path, json.dumps(state) + '\n')
        replies.append(dict(choice, role=role, pane=pane, answer='', evidence='',
                            confidence='low', needs_user=True, raw=''))
    status = None
    for reply in replies:
        try:
            code, response = tab(['wait', '--pane', reply['pane'], '--timeout', str(args.timeout)], args.timeout + 30)
        except subprocess.TimeoutExpired:
            status = status or 'timeout'
            continue
        raw = response.get('reply', '')
        if not isinstance(raw, str):
            raise ValueError('The tab command returned an invalid reply.')
        reply.update(parse_reply(raw), raw=raw[-4000:])
        if code == 3 or response.get('status') == 'timeout':
            status = status or 'timeout'
        elif code == 5 or response.get('status') == 'needs_input':
            reply.update(confidence='low', needs_user=True)
        elif code != 0 or response.get('status') != 'finished':
            status = 'error'
    if status is None:
        status = 'decided' if all(reply['answer'] and reply['confidence'] == 'high' and not reply['needs_user']
                                  for reply in replies) else 'ask_user'
    return result_for(status, replies)


def log(pid, question, result, settings):
    path = paths.TETHER / 'projects' / pid / 'advisor-log.md'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        lines = []
    question = ' '.join(question.split())[:200]
    answer = ' '.join(result.get('answer', '').split())[:300]
    main = settings['main']
    model = advisor.resolve(main['agent'], main['model']) if main['agent'] != 'none' else 'latest'
    lines.append(f'- {time.strftime("%Y-%m-%d %H:%M")} [{result["status"]}] Q: {question} -> A: {answer} '
                 f'({main["agent"]} {model or "default"})')
    advisor._write(path, '\n'.join(lines[-200:]) + '\n')


def main(argv=None):
    pid, args, settings, replies = None, None, None, []
    try:
        args = arguments(argv)
        settings = advisor.read()
        pane = os.environ.get('TMUX_PANE', '')
        if settings['main']['agent'] == 'none':
            result = {'status': 'off'}
            if PANE.fullmatch(pane):
                try:
                    _, pid = caller_context(pane)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass  # Off stays off even when tmux is unavailable.
        else:
            if not PANE.fullmatch(pane):
                raise ValueError('Run this inside a Tether tab.')
            key, pid = caller_context(pane)
            # One advisor tab set per asking tab: the server lets only the starting tab send to a helper.
            result = ask(settings, args, f'{key}-{pane[1:]}', replies)
    except subprocess.TimeoutExpired:
        result = result_for('timeout', replies)
    except (OSError, ValueError) as exc:
        result = dict(result_for('error', replies), error=str(exc))
    if pid is not None:
        try:
            log(pid, args.question, result, settings)
        except OSError as exc:
            result = dict(result_for('error', replies), error=f'Could not write the advisor log: {exc}')
    print(json.dumps(result))
    return CODES[result['status']]


if __name__ == '__main__':
    sys.exit(main())
