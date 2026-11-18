"""The verification stamp grammar (B189). One copy for scripts/verification.py and the commit gate (commit_gate.py).

A stamp is the text after "Verified:" on one line:
  tests(server 412 ok, ui 91/93) screenshots(24) review(codex APPROVED)
  none (docs only)
Each item is kind(detail). "none" is allowed, so unverified work stays visible and can be listed."""
import re

ITEM = r'[a-z][a-z-]*\([^()\n]+\)'
STAMP = re.compile(rf'none(?: \([^()\n]+\))?|{ITEM}(?: +{ITEM})*')
FOUND = re.compile(r'\bVerified:[ \t]*([^\n]*)')
LINE = re.compile(r'^Verified:[ \t]*([^\n]*)', re.M)  # in a commit message the line starts with it
HELP = ('Give a verification stamp: kind(detail) items such as '
        '"tests(server 412 ok) screenshots(24) review(codex APPROVED)", or "none (reason)".')


def valid(stamp):
    """The stamp text after "Verified:" with spaces tidied, or raise ValueError."""
    text = re.sub(r'\s+', ' ', str(stamp or '')).strip()
    if text.lower().startswith('verified:'):
        text = text[len('verified:'):].strip()
    if not STAMP.fullmatch(text):
        raise ValueError(HELP)
    return text


def status(text, pattern=FOUND):
    """'verified', 'none' or 'missing' for a DEPLOYED.md line or a commit message. A malformed stamp counts as missing."""
    found = pattern.findall(text or '')
    if not found:
        return 'missing'
    # The text after "Verified:" is the stamp itself. valid() also takes a second "Verified:" (for command-line
    # input); a line must not, or "Verified: Verified: tests(ok)" would count.
    stamp = re.sub(r'\s+', ' ', found[-1]).strip()
    if not STAMP.fullmatch(stamp):
        return 'missing'
    return 'none' if stamp == 'none' or stamp.startswith('none (') else 'verified'  # not the kind none-such(x)


def message_status(text):
    """status() for a commit message: "Verified:" must start a line. "Document Verified: none" as a subject is not
    a stamp."""
    return status(text, LINE)
