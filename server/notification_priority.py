"""Pure notification classification from a bounded transcript and final reply."""
from datetime import datetime
import re
import codex_messages
from antigravity import transcript_message

from agents import task_notice, tidy_user_text


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part.get('text', '') for part in content
                         if isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text')
                         and isinstance(part.get('text'), str))
    return ''


def claude_background(record, content):
    notice = task_notice(content, 0, None)
    return bool(record.get('isMeta') or record.get('isSystemInjected') or
                record.get('source') in ('system', 'hook', 'cron', 'loop', 'monitor', 'scheduled') or
                (notice and notice.get('kind') in ('notice', 'peer')) or
                re.match(r'^\s*<(?:system-reminder|hook-output|scheduled-task|cron-trigger|loop-prompt)\b', content))


def _timestamp(record):
    try:
        stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            return stamp.timestamp()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def turn_source(agent, records, *, identify=None, distinguish_missing=False, with_time=False, latest_turn=False):
    """Find the delivered prompt before the latest assistant record.

    Tool results and debug copies are not prompts. Claude sidechains belong to another agent.
    Codex and Antigravity currently expose no reliable background-origin flag.
    With latest_turn, any prompt after the latest assistant record gives 'unknown': that turn has no
    reply yet, so the prompt before that record belongs to an earlier turn.
    """
    finished_at = None

    def result(source, record=None):
        return (source, _timestamp(record) if record is not None else None, finished_at) if with_time else source

    def prompt_source(record, text, background=False):
        return result('background' if background else (identify(text) if identify else None) or 'user', record)

    if agent == 'antigravity':
        # Exports update old steps out of order. Keep each step's newest version.
        steps = {}
        for index, record in enumerate(records):
            step = record.get('step_index')
            steps[step if isinstance(step, int) else index] = record
        records = [steps[step] for step in sorted(steps)]
    assistant_seen = hook_blocked = False
    for record in reversed(records):
        kind = record.get('type')
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
        is_assistant = False
        if agent == 'claude':
            if record.get('isSidechain'):
                continue
            if kind == 'assistant' and not record.get('isMeta'):
                is_assistant = True
            elif not assistant_seen:
                if latest_turn and _claude_turn_start(record, kind):
                    return result('unknown')
                continue
            if kind == 'user':
                if record.get('isAutoCompactSummary'):
                    continue  # A compact summary does not start a new turn.
                content = text_content(message.get('content'))
                # Tool-result-only user records are continuations of the same turn.
                if not content.strip():
                    continue
                return prompt_source(record, tidy_user_text(content), claude_background(record, content))
            if kind == 'attachment':
                attachment = record.get('attachment') or {}
                if not isinstance(attachment, dict):
                    continue
                if attachment.get('type') == 'queued_command':
                    prompt = attachment.get('prompt')
                    if not isinstance(prompt, str) or not prompt.strip():
                        continue
                    return prompt_source(record, tidy_user_text(prompt), claude_background(record, prompt))
                if attachment.get('type') == 'hook_blocking_error' and identify is not None:
                    hook_blocked = True  # a blocked hook continues the same turn: find the prompt that started it
                    continue
                if attachment.get('type') in CLAUDE_BACKGROUND_ATTACHMENTS:
                    return result('background')
            if kind == 'system' and record.get('subtype') in CLAUDE_BACKGROUND_SYSTEM:
                return result('background')
        elif agent == 'codex':
            if latest_turn and not assistant_seen and _codex_prompt(kind, payload):
                return result('unknown')
            if kind == 'response_item' and payload.get('type') == 'message':
                if payload.get('role') == 'assistant':
                    is_assistant = True
                elif assistant_seen and payload.get('role') == 'user':
                    content = text_content(payload.get('content'))
                    user_parts = [part['text'] for part in codex_messages.parts(payload) if part.get('_user')]
                    if user_parts:
                        return prompt_source(record, '\n'.join(user_parts))
                    # AGENTS instructions are injected context, not a fresh prompt.
                    if content.lstrip().startswith('# AGENTS.md instructions'):
                        continue
                    return prompt_source(record, content, codex_messages.background(payload))
            elif assistant_seen and kind == 'event_msg' and payload.get('type') == 'user_message':
                return prompt_source(record, text_content(payload.get('message')))
            elif with_time and not assistant_seen and finished_at is None and kind == 'event_msg' \
                    and payload.get('type') == 'task_complete':
                finished_at = _timestamp(record)
        elif agent == 'antigravity':
            if kind in ('AGENT_RESPONSE', 'PLANNER_RESPONSE'):
                is_assistant = True
            elif latest_turn and not assistant_seen and kind == 'USER_INPUT':
                return result('unknown')
            elif assistant_seen and kind == 'USER_INPUT':
                if identify is not None and (prompt := transcript_message(record, 0, lambda text: text)):
                    return prompt_source(record, prompt['text'])
                return result('user')
        if is_assistant:
            if with_time and not assistant_seen and finished_at is None:
                finished_at = _timestamp(record)
            assistant_seen = True
    if distinguish_missing:
        return result('unknown-background' if hook_blocked else 'unknown')
    return result('background' if hook_blocked else 'user')


CLAUDE_BACKGROUND_ATTACHMENTS = ('hook_blocking_error', 'hook', 'scheduled_task', 'cron', 'loop', 'monitor_event')
CLAUDE_BACKGROUND_SYSTEM = ('hook', 'hook_response', 'scheduled_task', 'scheduled_wakeup', 'cron', 'loop', 'monitor_event')


def _claude_turn_start(record, kind):
    """A record that starts a turn: a prompt, or a background record other than a blocked hook."""
    if kind == 'system':
        return record.get('subtype') in CLAUDE_BACKGROUND_SYSTEM
    attachment = record.get('attachment') if isinstance(record.get('attachment'), dict) else {}
    if kind == 'attachment' and attachment.get('type') in CLAUDE_BACKGROUND_ATTACHMENTS:
        return attachment.get('type') != 'hook_blocking_error'  # a blocked hook continues the same turn
    return _claude_prompt(record, kind)


def _claude_prompt(record, kind):
    if kind == 'user' and not record.get('isAutoCompactSummary'):
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        content = text_content(message.get('content'))
    elif kind == 'attachment' and isinstance(record.get('attachment'), dict) \
            and record['attachment'].get('type') == 'queued_command':
        content = record['attachment'].get('prompt')
        content = content if isinstance(content, str) else ''
    else:
        return False
    return bool(content.strip())


def _codex_prompt(kind, payload):
    if kind == 'event_msg':
        return payload.get('type') == 'user_message'
    if kind != 'response_item' or payload.get('type') != 'message' or payload.get('role') != 'user':
        return False
    if any(part.get('_user') for part in codex_messages.parts(payload)):
        return True
    content = text_content(payload.get('content'))
    return bool(content.strip()) and not content.lstrip().startswith('# AGENTS.md instructions')


def status_line(reply):
    """('yes' | 'no' | None, the reply's lines without its final "Needs you:" line)."""
    lines = str(reply or '').rstrip().splitlines()
    status = None
    if lines:
        # Markdown may wrap the whole line, the label, or the value.
        plain = re.sub(r'[*`]', '', lines[-1]).strip()
        match = re.fullmatch(r'Needs you:\s*(yes|no)\.?', plain, re.I)
        if match:
            status = match[1].lower()
            lines.pop()
    return status, lines


def classify(kind, agent, records, reply):
    """Return (priority, preview text). Only a final status line overrides text guesses."""
    status, lines = status_line(reply)
    preview = '\n'.join(lines).rstrip()
    if kind in ('approval', 'input') or status == 'yes':
        return 'urgent', preview
    level = 'low' if turn_source(agent, records) == 'background' else 'important'
    if status is None:
        plain = re.sub(r'[*`]', '', preview).strip()
        if plain.endswith('?'):
            level = 'important'
        elif re.search(r'(?:^|[.!?\n])\s*[^.!?\n]*\b(?:nothing needs you|no action needed|no change)\b[^.!?\n]*\.?\s*$', plain, re.I):
            level = 'low'
    return level, preview
