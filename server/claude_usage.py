"""Capture Claude's official status-line data for the mobile rings."""
import json
import os
import re
import subprocess
import sys
import time

import paths


def capture(data):
    session = data.get('session_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9-]+', session):
        return
    folder = paths.CACHE / 'usage'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (session + '.json')
    temporary = folder / (session + '.' + str(os.getpid()) + '.tmp')
    payload = {key: data[key] for key in ('context_window', 'rate_limits', 'model') if key in data}
    payload['reported_at'] = time.time()
    temporary.write_text(json.dumps(payload))
    temporary.chmod(0o600)
    temporary.replace(target)


if __name__ == '__main__':
    raw = sys.stdin.buffer.read()
    try:
        capture(json.loads(raw))
    except Exception:
        pass
    if len(sys.argv) == 3 and sys.argv[1] == '--then':
        try:
            result = subprocess.run(sys.argv[2], shell=True, input=raw,
                                    stdout=subprocess.PIPE, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            sys.exit(1)
        sys.stdout.buffer.write(result.stdout)
        sys.exit(result.returncode)
