"""Open a file an agent names in chat (a screenshot, a screen recording, a PDF, a source or log file) on the phone.

Read-only: images, video, audio, PDF and text open in Tether (text is sent as text/plain, never rendered); any other
file (a spreadsheet, a document, an archive) is sent as a download; files whose names look like secrets (.env, keys, tokens) are refused; under the home folder only; never inside folders
that hold keys or tokens. A relative path is read from the tab's folder, as the agent would mean it."""
import os
import re
from pathlib import Path

TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
         '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.m4v': 'video/mp4',
         '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.wav': 'audio/wav', '.ogg': 'audio/ogg', '.pdf': 'application/pdf'}
TEXT = 'text/plain; charset=utf-8'
DOWNLOAD = 'application/octet-stream'  # a type Tether cannot show: the browser saves it
TEXT_LIMIT = 2 * 1024 * 1024
TEXT_SUFFIXES = {'.txt', '.md', '.markdown', '.log', '.json', '.jsonl', '.csv', '.tsv', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.conf',
                 '.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.css', '.scss', '.html', '.htm', '.xml', '.svg', '.sh', '.zsh', '.bash',
                 '.sql', '.go', '.rs', '.java', '.kt', '.swift', '.c', '.h', '.cc', '.cpp', '.hpp', '.rb', '.php', '.lua', '.diff', '.patch',
                 '.tf', '.graphql', '.proto', '.vue', '.svelte', '.ipynb', '.lock', '.gitignore', '.dockerignore', '.editorconfig'}
TEXT_NAMES = {'Makefile', 'Dockerfile', 'README', 'LICENSE', 'Procfile', 'Gemfile', 'Justfile', 'CODEOWNERS'}
SECRET = re.compile(r'^\.env(\..*)?$|\.(pem|key|p12|pfx|keystore|jks|kdbx|gpg|asc)$|^id_(rsa|dsa|ecdsa|ed25519)|secret|token|credential|password|passwd|\.netrc$|\.pgpass$|\.npmrc$|\.pypirc$', re.I)
# Hidden folders that hold credentials or tool state. Other hidden folders (.work-notes, .release-work) stay readable.
PRIVATE = {'.ssh', '.gnupg', '.config', '.aws', '.azure', '.claude', '.codex', '.local', '.docker', '.kube', '.password-store',
           '.pki', '.mozilla', '.git', '.npm', '.cargo', '.gemini', '.vscode-server'}


def resolve(name, folder, home=None, text_limit=TEXT_LIMIT):
    """The real file for `name`, or ValueError with a message for the phone. text_limit None: no size limit for text."""
    home = Path(home or os.path.expanduser('~')).resolve()
    name = (name or '').strip()
    if not name or '\0' in name or len(name) > 1024:
        raise ValueError('Choose a file.')
    path = Path(os.path.expanduser(name))
    if not path.is_absolute():
        path = Path(folder or home) / path
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('That file is not on the server.') from exc
    if home not in path.parents:
        raise ValueError('Tether opens files in your home folder only.')
    if any(part in PRIVATE for part in path.relative_to(home).parts[:-1]):
        raise ValueError('Tether does not open files in that folder.')
    if SECRET.search(path.name):
        raise ValueError('Tether does not open files that may hold secrets.')
    kind = TYPES.get(path.suffix.lower()) or (TEXT if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES else DOWNLOAD)
    if not path.is_file():
        raise ValueError('That is not a file.')
    if kind == TEXT and text_limit is not None and path.stat().st_size > text_limit:
        raise ValueError('That file is too large to show (2 MB at most).')
    return path, kind


def openable(names, folder, home=None):
    """Check file names without reading their contents."""
    results = {}
    for name in names:
        if not isinstance(name, str):
            continue
        try:
            resolve(name, folder, home)
            results[name] = True
        except ValueError:
            results[name] = False
    return results


def byte_range(header, size):
    """(start, end) for a single 'bytes=a-b' Range header, None for the whole file. ValueError when unsatisfiable."""
    if not header or not header.startswith('bytes=') or ',' in header:
        return None
    start, _, end = header[6:].strip().partition('-')
    if not start:  # the last N bytes
        count = int(end)
        if count <= 0:
            raise ValueError
        return max(0, size - count), size - 1
    first, last = int(start), int(end) if end else size - 1
    if first >= size or last < first:
        raise ValueError
    return first, min(last, size - 1)
