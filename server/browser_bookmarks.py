"""Private bookmark lists for the two server browsers."""
import builtins
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import urlsplit

import paths

BROWSERS = ('test', 'signed')
LIMIT = 500
LOCK = threading.Lock()


def _check_browser(browser):
    if browser not in BROWSERS:
        raise ValueError('Bookmarks are for the Test and Signed-in browsers.')


def _host(url):
    invalid = 'Only http and https pages can be bookmarked.'
    if not isinstance(url, str):
        raise ValueError(invalid)
    if len(url) > 2048:
        raise ValueError('This address is too long to bookmark.')
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        raise ValueError(invalid) from None
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or not host:
        raise ValueError(invalid)
    return host


def _valid(data):
    if not isinstance(data, dict) or data.keys() != set(BROWSERS):
        return False
    for rows in data.values():
        if not isinstance(rows, builtins.list) or len(rows) > LIMIT:
            return False
        for row in rows:
            if (not isinstance(row, dict) or row.keys() != {'id', 'url', 'title', 'added'}
                    or not isinstance(row['id'], str) or not isinstance(row['title'], str)
                    or type(row['added']) is not int):
                return False
            _host(row['url'])
    return True


def _read(file):
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
        if _valid(data):
            return data
    except FileNotFoundError:
        return {browser: [] for browser in BROWSERS}
    except (ValueError, RecursionError):
        pass
    try:
        os.replace(file, file.with_name(file.name + '.bad'))
    except OSError:
        pass  # A corrupt store still reads as empty if the backup cannot be made.
    return {browser: [] for browser in BROWSERS}


def _write(file, data):
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=file.parent, prefix='.browser-bookmarks-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, separators=(',', ':'))
        os.replace(temporary, file)
    finally:
        Path(temporary).unlink(missing_ok=True)


def list(browser):
    _check_browser(browser)
    with LOCK:
        return _read(paths.private_file('browser-bookmarks.json'))[browser]


def add(browser, url, title=None):
    _check_browser(browser)
    host = _host(url)
    if title is not None and not isinstance(title, str):
        raise ValueError('Bookmark title must be text.')
    title = (' '.join((title or '').split()) or host)[:300].rstrip()
    with LOCK:
        file = paths.private_file('browser-bookmarks.json')
        data = _read(file)
        rows = data[browser]
        for row in rows:
            if row['url'] == url:
                row['title'] = title
                break
        else:
            if len(rows) >= LIMIT:
                raise ValueError('You have 500 bookmarks. Remove one first.')
            rows.insert(0, {'id': secrets.token_hex(8), 'url': url, 'title': title, 'added': int(time.time())})
        _write(file, data)
        return rows


def remove(browser, ident):
    _check_browser(browser)
    with LOCK:
        file = paths.private_file('browser-bookmarks.json')
        data = _read(file)
        rows = data[browser]
        kept = [row for row in rows if row['id'] != ident]
        if len(kept) != len(rows):
            data[browser] = kept
            _write(file, data)
        return kept
