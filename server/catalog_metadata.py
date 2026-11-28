"""Metadata made visible in command catalogs."""
import os
import re
import stat
from textwrap import dedent

FRONT = re.compile(r'^---[ \t]*\r?\n(.*?)\n---[ \t]*\r?(?:\n|$)', re.S)


def metadata_text(path, limit):
    """Read bounded metadata from regular files, including symlinked customizations."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as handle:
            if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return handle.read(limit).decode('utf-8', errors='ignore')
    except (OSError, ValueError):
        pass
    return ''


def front_matter(path):
    """Read the scalar fields shown in the catalog, including a block description."""
    match = FRONT.match(metadata_text(path, 4000))
    result = {}
    description = None
    for line in dedent(match[1] if match else '').splitlines():
        if not line or line[0].isspace():
            if description is not None:
                description.append(line.strip())
            continue
        if description is not None:
            result['description'] = ' '.join(description).strip()
            description = None
        key, _, value = line.partition(':')
        key = key.rstrip()
        value = value.strip()
        if key == 'description' and value in ('>', '|', '>-', '|-', '>+', '|+'):
            description = []
        elif key in ('name', 'description', 'argument-hint') and value and value not in ('>', '|', '>-', '|-'):
            result[key] = value.strip('"\'')
    if description is not None:
        result['description'] = ' '.join(description).strip()
    return result


def short(text, limit=140):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'
