"""Replay Friday intent cases without executing any tool calls."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
from threading import Lock
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if __package__:
    from . import friday_agent as friday
else:
    import friday_agent as friday

DEFAULT_CASES = Path(__file__).with_name('fixtures') / 'friday_intent' / 'cases.json'
GROUPS = frozenset('approval backlog_add backlog_list backlog_send cant_do confirm deny event '
                   'list_chats misheard new_chat no_tool outside panel read_reply readback reminder '
                   'send_direct split_turn status switch'.split())
LOOKUPS = {'list_chats', 'backlog_list', 'read_last_reply'}
CLAIM = re.compile(r"(?i)\bI(?:'ve| have)?\s+(?:just\s+)?"
                   r"(?:sent|switched|opened|created|started|approved|denied|added|moved|closed)\b")


ASK = r"(?:^|[.!?]\s+|[\"'])(?:Shall|Should|Can|May) I send (?:(?![.!]\s|\bor\b)[^?])*?\bto (?:the )?"  # One sentence, one target.
STRAY = re.compile('[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]')  # CJK or full-width text in an English voice reply.


def signed(messages):
    outgoing = deepcopy(messages)
    for message in outgoing:
        calls = message.get('tool_calls') if message.get('role') == 'assistant' else None
        if calls and not any('extra_content' in call for call in calls):
            calls[0]['extra_content'] = {'google': {'thought_signature': 'skip_thought_signature_validator'}}
    return outgoing


def _asks_send_to(reply, chat):
    """True when a question in the canonical form "Shall I send <task> to <chat>?" names chat as its one target."""
    return bool(re.search(ASK + re.escape(chat) + r'(?: chat)?(?: now)?\?', reply))


def _score(trace, expect, expected_tools):
    action, reply, proposal = trace['action'], trace['reply'], trace['proposal']
    name, args = (action['name'], action['args']) if action else (None, {})
    readback_chat = expect.get('readback_chat')
    readback_instead = proposal is not None and action is None and None not in expected_tools
    if trace['blocked']:
        yield 'blocked'
    if len(trace['actions']) + (name in LOOKUPS) > 1:  # An expected lookup plus a write is also extra.
        yield 'extra_action'
    if readback_instead:
        yield 'readback_instead'
    elif name not in expected_tools:
        yield 'wrong_tool'
    if proposal and readback_chat is not None:
        args = {'chat': proposal['chat'], 'prompt': proposal['payload']}
    wrong_args = any(key not in args or not re.search(pattern, str(args[key]))
                     for key, pattern in expect.get('args', {}).items())
    forbidden_prompt = 'prompt_not' in expect and re.search(expect['prompt_not'], str(args.get('prompt', '')))
    if not readback_instead and (wrong_args or forbidden_prompt):
        yield 'wrong_args'
    if 'reply' in expect and not re.search(expect['reply'], reply):
        yield 'reply'
    if 'reply_not' in expect and re.search(expect['reply_not'], reply):
        yield 'reply'
    if readback_chat is not None:
        if proposal is None:
            matches = _asks_send_to(reply, readback_chat)
        else:
            matches = (proposal['name'] == expect.get('readback_tool', 'send_to_chat')
                       and proposal['chat'].strip().casefold() == readback_chat.casefold())
        if not matches:
            yield 'readback'
    elif proposal and None in expected_tools:
        yield 'unwanted_readback'
    if proposal is None and action is None:
        if not expect.get('claim_ok') and CLAIM.search(reply):
            yield 'claimed_action'
        if not reply.strip() or STRAY.search(reply):
            yield 'stray_text'


def _tool_args(call):
    try:
        args = json.loads(call['function']['arguments'])
    except (KeyError, TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}


def _run_once(case, fixture, complete, brain, expected_tools, trace):
    prefix = friday.llm_prefix()
    messages = prefix['messages'] + deepcopy(case.get('history', []))
    fragments = [case['user']] if isinstance(case['user'], str) else case['user']
    for fragment in fragments:
        text = friday.EVENT_PREFIX + fragment if case.get('event') else friday.fix_transcript(fragment)
        messages.append({'role': 'user', 'content': text})
    messages, _ = friday.add_state(messages, case.get('state', fixture.get('state', {})), [], None)
    message = messages[-1]
    source = 'event' if message['content'].startswith(friday.EVENT_PREFIX) else 'speech'
    if not friday.is_real_user(message):
        source = 'tool'
    i = next((i for i in range(len(messages) - 2, -1, -1)
              if messages[i].get('role') == 'assistant'), -1)
    previous_assistant = (messages[i].get('content') or '') if i >= 0 else ''
    last_user_text = ''
    if source == 'speech':
        last_user_text = ' '.join(m['content'].strip() for m in messages[i + 1:]
            if friday.is_real_user(m) and isinstance(m.get('content'), str)
            and m['content'].strip() and not m['content'].startswith(friday.EVENT_PREFIX))
    user_turn = 1
    pending = friday.SendProposal(**case['pending'], turn=0) if 'pending' in case else None
    if pending and previous_assistant != friday.send_readback(pending):
        pending = None
    declined = None
    if pending and friday.APPROVAL_NO.search(last_user_text):
        # Clear the pending proposal but keep it for guards in this run.
        declined, pending = pending, None
    used = set()
    results = {**fixture.get('tool_results', {}), **case.get('tool_results', {})}

    for _ in range(4):
        suggestions = friday.suggested_replies(messages) if source == 'speech' else ()
        trace['steps'] += 1
        payload = {'model': brain.model, 'messages': signed(messages), 'tools': prefix['tools'],
                   'max_tokens': brain.max_tokens, **brain.extra}
        message = dict(complete(payload))
        usage = message.pop('_usage', None) or {}
        trace['prompt_tokens'] += usage.get('prompt_tokens') or 0
        if not message.get('tool_calls'):
            trace['reply'] = message.get('content') or ''
            return
        messages.append(message)
        for call in message['tool_calls']:
            name, args = call['function']['name'], _tool_args(call)
            reason = friday.check_tool(name, args, source, last_user_text, used, previous_assistant,
                                       pending=pending or declined, user_turn=user_turn, suggestions=suggestions)
            if reason == friday.READBACK_REQUIRED and name in friday.SEND_TOOLS:
                if trace['proposal'] is None:
                    key = 'prompt' if name == 'send_to_chat' else 'item'
                    proposal = friday.SendProposal(name, str(args.get('chat') or ''), str(args.get(key) or ''),
                                                   user_turn)
                    trace['proposal'] = {'name': name, 'chat': proposal.chat, 'payload': proposal.payload}
                    pending = None
                    trace['reply'] = friday.send_readback(proposal)
                result = {'error': 'Not sent yet. Friday asked the user to confirm.'}
            elif reason:
                trace['blocked'].append({'name': name, 'args': args, 'blocked': reason})
                result = {'error': reason}
            else:
                used.add(name)
                if trace['action'] is None and (name not in LOOKUPS or name in expected_tools):
                    trace['action'] = {'name': name, 'args': args}
                if name not in LOOKUPS:
                    trace['actions'].append({'name': name, 'args': args})
                    continue
                result = results.get(name, {'ok': True})
            messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
        if trace['proposal'] is not None or trace['action'] is not None:
            return


def run_case(case, fixture, complete, runs=1, brain=None):
    """Score repeated runs of one fixture case with an injected completion function."""
    if brain is None:
        brain = SimpleNamespace(model='fake', max_tokens=1024, extra={})
    expect = case['expect']
    expected_tools = expect.get('any_tool', []) + ([expect['tool']] if 'tool' in expect else [])
    fails, samples = Counter(), []
    passed = prompt_tokens = llm_calls = 0
    for _ in range(runs):
        trace = {'steps': 0, 'reply': '', 'action': None, 'actions': [], 'blocked': [],
                 'proposal': None, 'prompt_tokens': 0}
        try:
            _run_once(case, fixture, complete, brain, expected_tools, trace)
        except LiveError as exc:
            print(exc, file=sys.stderr)
            reasons = ['blocked', 'provider_error'] if trace['blocked'] else ['provider_error']
        else:
            reasons = list(_score(trace, expect, expected_tools))
        fails.update(reasons)
        passed += not reasons
        prompt_tokens += trace.pop('prompt_tokens')
        llm_calls += trace['steps']
        # Keep the first two runs, plus up to two failed runs, so a failure can be read.
        if len(samples) < 2 or (reasons and sum(bool(item.get('fails')) for item in samples) < 2):
            samples.append(dict(trace, fails=reasons))
    return dict(id=case['id'], group=case['group'], split=case['split'], runs=runs, passed=passed,
                fails=dict(fails), samples=samples, prompt_tokens=prompt_tokens, llm_calls=llm_calls)


def load_fixture(path):
    with Path(path).open(encoding='utf-8') as stream:
        fixture = json.load(stream)
    if not isinstance(fixture, dict) or type(fixture.get('version')) is not int or fixture['version'] != 1:
        raise ValueError('fixture version must be 1')
    names = {schema['function']['name'] for schema in friday.tool_schemas()}
    ids = set()
    if not isinstance(fixture.get('cases'), list):
        raise ValueError('cases must be a list')
    _validate_context(fixture, 'fixture', names)
    for index, case in enumerate(fixture['cases']):
        label = f'case {index + 1}'
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'].strip():
            raise ValueError(f'{label} must have a case id')
        label = case['id']
        if label in ids:
            raise ValueError(f'duplicate id {label!r}')
        ids.add(label)
        group = case.get('group')
        if not isinstance(group, str) or group not in GROUPS:
            raise ValueError(f'{label}: unknown group {group!r}')
        if case.get('split') not in ('dev', 'holdout') or case.get('source') not in ('log', 'hand'):
            raise ValueError(f'{label}: expected split dev/holdout and source log/hand')
        user = case.get('user')
        if not isinstance(user, str) and not (isinstance(user, list) and user
                                               and all(isinstance(part, str) for part in user)):
            raise ValueError(f'{label}: user must be text or a nonempty list of text fragments')
        if not isinstance(case.get('event', False), bool):
            raise ValueError(f'{label}: event must be a boolean')
        if 'pending' in case:
            pending = case['pending']
            if (not isinstance(pending, dict) or pending.keys() != {'name', 'chat', 'payload'}
                    or pending['name'] not in friday.SEND_TOOLS
                    or not all(isinstance(value, str) for value in pending.values())):
                raise ValueError(f'{label}: pending must specify a send tool name, chat and payload')
        _validate_context(case, label, names)
        _validate_expectation(case.get('expect'), label, names)
    return fixture


def _validate_context(data, label, names):
    if not isinstance(data.get('state', {}), dict) or not isinstance(data.get('tool_results', {}), dict):
        raise ValueError(f'{label}: state and tool_results must be objects')
    for name in data.get('tool_results', {}):
        if name not in names:
            raise ValueError(f'{label}: unknown tool {name!r}')
    if not isinstance(data.get('history', []), list):
        raise ValueError(f'{label}: history must be a list')
    for message in data.get('history', []):
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'tool'):
            raise ValueError(f'{label}: invalid history message role')
        if 'content' in message and message['content'] is not None and not isinstance(message['content'], str):
            raise ValueError(f'{label}: history content must be text or null')
        if not isinstance(message.get('tool_calls', []), list):
            raise ValueError(f'{label}: history tool_calls must be a list')
        for call in message.get('tool_calls', []):
            function = call.get('function') if isinstance(call, dict) else None
            name = function.get('name') if isinstance(function, dict) else None
            if not isinstance(name, str) or name not in names:
                raise ValueError(f'{label}: unknown tool {name!r} in history')


def _validate_expectation(expect, label, names):
    if not isinstance(expect, dict) or not ('tool' in expect or expect.get('any_tool')):
        raise ValueError(f'{label}: expect must specify tool or any_tool')
    tool, alternatives = expect.get('tool'), expect.get('any_tool', [])
    if not isinstance(alternatives, list):
        raise ValueError(f'{label}: any_tool must be a list')
    for name in alternatives if tool is None else [tool, *alternatives]:
        if not isinstance(name, str) or name not in names:
            raise ValueError(f'{label}: unknown tool {name!r}')
    args = expect.get('args', {})
    if not isinstance(args, dict):
        raise ValueError(f'{label}: expect.args must be an object')
    try:
        for pattern in list(args.values()) + [expect[key] for key in ('reply', 'reply_not', 'prompt_not') if key in expect]:
            re.compile(pattern)
    except (TypeError, re.error, OverflowError) as exc:
        raise ValueError(f'{label}: invalid regex ({exc})') from None
    if 'readback_chat' in expect and not isinstance(expect['readback_chat'], str):
        raise ValueError(f'{label}: readback_chat must be text')
    if 'readback_tool' in expect and expect['readback_tool'] not in friday.SEND_TOOLS:
        raise ValueError(f'{label}: readback_tool must be a send tool')


class LiveError(RuntimeError):
    """A provider failure with a diagnostic safe to print."""


def _post_completion(url, payload, token):
    for attempt in range(2):
        try:
            request = Request(url, data=json.dumps(payload).encode('utf-8'), method='POST',
                              headers={'Content-Type': 'application/json'})
            request.add_unredirected_header('Authorization', f'Bearer {token}')
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
            return dict(result['choices'][0]['message'], _usage=result.get('usage'))
        except HTTPError as exc:
            with exc:
                if attempt == 0 and (exc.code == 429 or 500 <= exc.code < 600):
                    continue
                try:
                    body = json.load(exc)
                except (OSError, ValueError, HTTPException):
                    body = None
            detail = friday.provider_error_detail(SimpleNamespace(body=body), token)
            raise LiveError(f'HTTP {exc.code} {detail}'.rstrip()) from None
        except (OSError, HTTPException) as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError)
            if attempt == 0 and timed_out:
                continue
            raise LiveError('Provider request timed out' if timed_out else 'Provider request failed') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise LiveError('Invalid provider request or response') from None


def live_brain():
    kind = os.environ.get('TETHER_FRIDAY_BRAIN', 'local')
    if kind != 'gemini':
        raise LiveError('Live eval needs TETHER_FRIDAY_BRAIN=gemini')
    try:
        import google.auth
        from google.auth.transport.requests import Request as AuthRequest
        credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    except Exception:
        raise LiveError('Google Application Default Credentials unavailable') from None
    try:
        brain = friday.brain_config(os.environ, project)
    except ValueError as exc:
        raise LiveError(str(exc)) from None
    if not brain.base_url.startswith('https://aiplatform.googleapis.com/'):
        raise LiveError('Live eval needs a Vertex AI endpoint')
    lock = Lock()

    def complete(payload):
        with lock:
            try:
                if not credentials.valid:
                    credentials.refresh(AuthRequest())
                token = credentials.token
            except Exception:
                raise LiveError('Google credentials refresh failed') from None
        return _post_completion(brain.base_url + '/chat/completions', payload, token)

    return brain, complete


def _summary(results, field):
    counts = {}
    for result in results:
        row = counts.setdefault(result[field], {'passed': 0, 'total': 0})
        row['passed'] += result['passed']
        row['total'] += result['runs']
    return dict(sorted(counts.items()))


def _print_report(report):
    print(f'{"Scope":<14} {"Name":<24} {"Passed/total":>13} {"Percent":>8}')
    for scope, rows in (('Group', report['groups']), ('Split', report['splits']),
                        ('Overall', {'all': report['overall']})):
        for name, row in rows.items():
            passed, total = row['passed'], row['total']
            percent = 100 * passed / total if total else 0
            print(f'{scope:<14} {name:<24} {passed:>6}/{total:<6} {percent:>7.1f}%')
    for result in report['cases']:
        if result['fails']:
            reasons = ', '.join(f'{reason} ({count})' for reason, count in sorted(result['fails'].items()))
            print(f'{result["id"]}: {reasons}')
    print(f'Mean prompt tokens per LLM call {report["prompt_tokens_mean"]:.1f}')
    print(f'System prompt characters {report["system_prompt_chars"]}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    parser.add_argument('--split', choices=('dev', 'holdout', 'all'), default='all')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--only')
    parser.add_argument('--group')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args(argv)
    if min(args.runs, args.jobs) < 1:
        parser.error('--runs and --jobs must be positive')
    try:
        fixture = load_fixture(args.cases)
        if args.group is not None and args.group not in GROUPS:
            raise ValueError(f'unknown group {args.group!r}')
    except (OSError, ValueError) as exc:
        print(f'Invalid fixture: {exc}', file=sys.stderr)
        return 1
    ids = {item.strip() for item in args.only.split(',')} if args.only is not None else None
    cases = [case for case in fixture['cases'] if (ids is None or case['id'] in ids)
             and (args.group is None or case['group'] == args.group)
             and (args.split == 'all' or case['split'] == args.split)]
    if not args.live:
        report = {'total': len(cases), 'groups': dict(sorted(Counter(c['group'] for c in cases).items())),
                  'splits': dict(sorted(Counter(c['split'] for c in cases).items()))}
        print(f'{report["total"]} cases')
        for label in ('groups', 'splits'):
            print(f'{label.capitalize()} ' + ', '.join(f'{name}={n}' for name, n in report[label].items()))
    else:
        error, results = {}, []
        if cases:
            try:
                brain, complete = live_brain()
            except LiveError as exc:
                error = {'error': str(exc)}
                print(exc, file=sys.stderr)
            else:
                with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                    results = list(pool.map(lambda case: run_case(case, fixture, complete, args.runs, brain), cases))
        calls = sum(result['llm_calls'] for result in results)
        tokens = sum(result['prompt_tokens'] for result in results)
        report = {'cases': results, 'groups': _summary(results, 'group'), 'splits': _summary(results, 'split'),
                  'overall': {'passed': sum(r['passed'] for r in results), 'total': sum(r['runs'] for r in results)},
                  'prompt_tokens': tokens, 'llm_calls': calls, 'prompt_tokens_mean': tokens / calls if calls else 0,
                  'system_prompt_chars': len(friday.SYSTEM_PROMPT), **error}
        _print_report(report)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
