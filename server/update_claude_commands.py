"""Rebuild claude_commands.json from the official command reference.

    python3 update_claude_commands.py            # fetches https://code.claude.com/docs/en/commands.md
    python3 update_claude_commands.py page.md    # or reads a saved copy

The catalog reads the JSON file; nothing fetches the page at run time."""
import json
import re
import sys
import urllib.request
from pathlib import Path

URL = 'https://code.claude.com/docs/en/commands.md'
OUT = Path(__file__).with_name('claude_commands.json')
ROW = re.compile(r'^\| `/([a-z0-9:-]+)([^`]*)` \| (.*) \|\s*$')
ALIASES = re.compile(r'Alias(?:es)?: ((?:`/[a-z0-9-]+`(?:,? (?:and )?)?)+)')


def plain(text):
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)  # links -> their words
    return text.replace('**', '').replace('`', '').replace('\\|', '|')


def first_sentence(text):
    match = re.match(r'(.+?[.!?])(?:\s|$)', text)
    return (match[1] if match else text).strip()


def parse(page):
    commands, seen = [], set()
    for line in page.splitlines():
        row = ROW.match(line)
        if not row or row[1] in seen:
            continue
        seen.add(row[1])
        cell = row[3]
        kind = 'skill' if cell.startswith(('**[Skill]', '**[Workflow]')) else 'builtin'
        text = plain(re.sub(r'^\*\*\[(Skill|Workflow)\]\([^)]*\)\.\*\*\s*', '', cell))
        aliases = re.findall(r'/([a-z0-9-]+)', ALIASES.findall(cell)[-1]) if ALIASES.search(cell) else []
        commands.append({'name': row[1], 'description': first_sentence(text), 'aliases': aliases, 'hint': row[2].replace('\\|', '|').strip(), 'kind': kind})
    return commands


if __name__ == '__main__':
    page = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else urllib.request.urlopen(URL, timeout=20).read().decode()
    commands = parse(page)
    if len(commands) < 50:
        sys.exit('Only %d commands found; the page layout may have changed. Not writing.' % len(commands))
    OUT.write_text(json.dumps({'source': URL, 'commands': commands}, indent=1) + '\n')
    print('%d commands -> %s' % (len(commands), OUT.name))
