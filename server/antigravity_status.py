"""Capture Antigravity's official status data without accessing credentials."""
import json
import os
import re
import sys
import time

import paths


def capture(data):
    conversation = data.get('conversation_id') or data.get('session_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9-]+', conversation):
        return
    folder = paths.CACHE / 'antigravity'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (conversation + '.json')
    payload = {key: data[key] for key in ('context_window', 'quota', 'model', 'transcript_path', 'agent_state', 'cwd', 'pending_input_count', 'tool_confirmation_pending') if key in data}
    payload['conversation_id'] = conversation
    payload['reported_at'] = time.time()
    temporary = folder / (conversation + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(payload))
    temporary.chmod(0o600)
    temporary.replace(target)


if __name__ == '__main__':
    try:
        data = json.load(sys.stdin)
        capture(data)
    except (OSError, ValueError, TypeError):
        pass
