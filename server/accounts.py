"""Which account each agent is signed in to: email and plan only. Never returns tokens."""
import base64
import json
from pathlib import Path

import paths


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def claude():
    account = read(paths.HOME / '.claude.json').get('oauthAccount') or {}
    oauth = read(paths.CLAUDE / '.credentials.json').get('claudeAiOauth') or {}
    tier = str(oauth.get('rateLimitTier') or '')
    plan = (oauth.get('subscriptionType') or '').capitalize()
    plan += ' 20x' if '20x' in tier else ' 5x' if '5x' in tier else ''
    return {'email': account.get('emailAddress'), 'plan': plan.strip() or None} if account.get('emailAddress') else None


def codex():
    token = ((read(paths.CODEX / 'auth.json').get('tokens') or {}).get('id_token') or '').split('.')
    if len(token) < 2:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(token[1] + '=' * (-len(token[1]) % 4)))
    except ValueError:
        return None
    plan = (claims.get('https://api.openai.com/auth') or {}).get('chatgpt_plan_type')
    return {'email': claims.get('email'), 'plan': plan.capitalize() if plan else None} if claims.get('email') else None


def summary():
    return {'claude': claude(), 'codex': codex()}
