"""Pure notification classification from a bounded transcript and final reply."""
import re
import codex_messages

from agents import task_notice


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


def turn_source(agent, records, *, identify=None):
    """Find the delivered prompt before the latest assistant record; absent evidence means user-started.

    Tool results and debug copies are not prompts. Claude sidechains belong to another agent.
    Codex and Antigravity currently expose no reliable background-origin flag.
    """
    def prompt_source(text, background=False):
        return 'background' if background else (identify(text) if identify else None) or 'user'

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
        if agent == 'claude':
            if record.get('isSidechain'):
                continue
            if kind == 'assistant' and not record.get('isMeta'):
                assistant_seen = True
                continue
            if not assistant_seen:
                continue
            if kind == 'user':
                if record.get('isAutoCompactSummary'):
                    continue  # A compact summary does not start a new turn.
                content = text_content(message.get('content'))
                # Tool-result-only user records are continuations of the same turn.
                if not content.strip():
                    continue
                return prompt_source(content, claude_background(record, content))
            if kind == 'attachment':
                attachment = record.get('attachment') or {}
                if not isinstance(attachment, dict):
                    continue
                if attachment.get('type') == 'queued_command':
                    prompt = attachment.get('prompt')
                    if not isinstance(prompt, str) or not prompt.strip():
                        continue
                    return prompt_source(prompt, claude_background(record, prompt))
                if attachment.get('type') == 'hook_blocking_error' and identify is not None:
                    hook_blocked = True  # a blocked hook continues the same turn: find the prompt that started it
                    continue
                if attachment.get('type') in ('hook_blocking_error', 'hook', 'scheduled_task', 'cron', 'loop', 'monitor_event'):
                    return 'background'
            if kind == 'system' and record.get('subtype') in ('hook', 'hook_response', 'scheduled_task', 'scheduled_wakeup', 'cron', 'loop', 'monitor_event'):
                return 'background'
        elif agent == 'codex':
            if kind == 'response_item' and payload.get('type') == 'message':
                if payload.get('role') == 'assistant':
                    assistant_seen = True
                elif assistant_seen and payload.get('role') == 'user':
                    content = text_content(payload.get('content'))
                    if any(part.get('_user') for part in codex_messages.parts(payload)):
                        return prompt_source(content)
                    # AGENTS instructions are injected context, not a fresh prompt.
                    if content.lstrip().startswith('# AGENTS.md instructions'):
                        continue
                    return prompt_source(content, codex_messages.background(payload))
            elif assistant_seen and kind == 'event_msg' and payload.get('type') == 'user_message':
                return prompt_source(text_content(payload.get('message')))
        elif agent == 'antigravity':
            if kind in ('AGENT_RESPONSE', 'PLANNER_RESPONSE'):
                assistant_seen = True
            elif assistant_seen and kind == 'USER_INPUT':
                return 'user'
    return 'background' if hook_blocked else 'user'  # the prompt left the tail: keep the hook's answer


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
