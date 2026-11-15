"""Compact tool metadata and bounded, on-demand transcript details."""
import base64
import binascii
import difflib
import json
import re

MAX_LINES = 200
MAX_BYTES = 16384
# Images an agent looked at (Claude's Read of a PNG). Served from the transcript only, never from a client path.
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}


def images(result):
    content = (result or {}).get('content')
    if not isinstance(content, list):
        return []
    return [part['source'] for part in content if isinstance(part, dict) and part.get('type') == 'image' and isinstance(part.get('source'), dict)
            and part['source'].get('type') == 'base64' and part['source'].get('media_type') in IMAGE_TYPES and isinstance(part['source'].get('data'), str)]


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(text(item.get('text', '')) for item in value if isinstance(item, dict))
    return json.dumps(value, ensure_ascii=False) if value is not None else ''


def bounded(value, tail=False, max_lines=MAX_LINES, max_bytes=MAX_BYTES):
    lines = value.splitlines()
    selected = lines[-max_lines:] if tail else lines[:max_lines]
    raw = '\n'.join(selected).encode('utf-8')
    cut = len(lines) > max_lines or len(raw) > max_bytes
    if cut:
        selected = lines[-(max_lines - 1):] if tail else lines[:max_lines - 1]
        raw = '\n'.join(selected).encode('utf-8')
        budget = max_bytes - len('\n[truncated]'.encode())
        raw = raw[-budget:] if tail else raw[:budget]
    value = raw.decode('utf-8', 'ignore')
    if cut:
        value = ('[truncated]\n' + value) if tail else (value + '\n[truncated]')
    return value, cut


def arguments(call):
    raw = call.get('input', call.get('arguments', {}))
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def summary(call):
    name = str(call.get('name') or 'Tool')
    data = arguments(call)
    title = next((text(data[key]) for key in ('command', 'cmd', 'file_path', 'path', 'pattern', 'query', 'url', 'description') if data.get(key)), '') if isinstance(data, dict) else text(data)
    if name.rsplit('__', 1)[-1].rsplit('.', 1)[-1] == 'apply_patch':
        title = ', '.join(re.findall(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', text(data), re.M))
    item = {'id': str(call.get('call_id') or call.get('id') or ''), 'name': name[:160],
            'title': ' '.join(title.split())[:240], 'is_error': False, 'status': 'pending'}
    if isinstance(data, dict) and isinstance(data.get('description'), str) and data['description'].strip():
        item['description'] = ' '.join(data['description'].split())[:160]
    return item


def mark_result(messages, identifier, result, time=None, details=None):
    for message in reversed(messages):
        tool = message.get('tool')
        if tool and tool['id'] == identifier:
            tool['is_error'] = failed(result)
            tool['status'] = 'complete'
            if time:
                tool['ended'] = time
            if count := len(images(result)):
                tool['images'] = count
            if tool['name'] == 'Artifact' and not tool['is_error'] and (found := artifact(details)):
                tool['artifact'] = found
            break


ARTIFACT_URL = re.compile(r'^https://claude\.ai/(?:code/)?artifact/[\w-]{6,80}$')


def artifact(details):
    """A page Claude published with its Artifact tool: the claude.ai link and the local HTML file it came from.
    `details` is the transcript record's toolUseResult. A read, list or quickstart call has no path, so it shows no card."""
    if not isinstance(details, dict):
        return None
    url, path, title = details.get('url'), details.get('path'), details.get('title')
    if not isinstance(url, str) or not ARTIFACT_URL.match(url) or not isinstance(path, str) or not path.lower().endswith(('.html', '.htm')):
        return None
    return {'url': url, 'path': path[:1024], 'title': ' '.join(title.split())[:160] if isinstance(title, str) and title.strip() else path.rsplit('/', 1)[-1]}


def failed(result):
    output = text(result.get('content', result.get('output')))
    return bool(result.get('is_error') or re.search(r'(?im)^(?:Exit code:|Process exited with code)\s*[1-9]\d*', output))


def build(call, result=None):
    complete = result is not None
    result = result or {}
    item = summary(call)
    data = arguments(call)
    output = text(result.get('content', result.get('output')))
    name = item['name'].rsplit('__', 1)[-1].rsplit('.', 1)[-1]
    kind = 'text'
    if name in ('Edit', 'MultiEdit') and isinstance(data, dict):
        path = text(data.get('file_path') or 'file')
        edits = data.get('edits', []) if name == 'MultiEdit' else [data]
        detail = '\n'.join('\n'.join(difflib.unified_diff(text(edit.get('old_string')).splitlines(),
            text(edit.get('new_string')).splitlines(), fromfile='a/' + path, tofile='b/' + path, lineterm='')) for edit in edits if isinstance(edit, dict))
        kind = 'diff'
    elif name == 'apply_patch':
        detail, kind = text(data), 'diff'
    elif name == 'Write' and isinstance(data, dict):
        detail = text(data.get('content'))
    else:
        detail = text(data) + ('\n\nResult:\n' + output if output else '\n\nResult pending or unavailable.')
    # Keep the command even when a long output is cut from the front.
    if isinstance(data, dict) and (name == 'Bash' or data.get('cmd') or data.get('command')):
        command, command_cut = bounded(text(data.get('command') or data.get('cmd')), max_lines=20, max_bytes=2048)
        output, output_cut = bounded(output, tail=True, max_lines=175, max_bytes=MAX_BYTES - len(command.encode('utf-8')) - 100)
        detail = command + '\n\n' + (output or 'Result pending or unavailable.')
        cut = command_cut or output_cut
    else:
        detail, cut = bounded(detail)
    if failed(result) and (kind == 'diff' or name == 'Write'):
        detail, extra_cut = bounded(detail + '\n\nError:\n' + output)
        cut = cut or extra_cut
    return dict(item, detail=detail, format=kind, truncated=cut, is_error=failed(result), status='complete' if complete else 'pending')


def lookup(config, identifier):
    """Use only the selected session's transcript; never accept a client path."""
    call, result = find(config, identifier)
    return dict(build(call, result), images=len(images(result)))


def image(config, identifier, index):
    """The index-th image in one tool result: (media type, bytes)."""
    _, result = find(config, identifier)
    return decode_image(result, index)


def message_image(config, key, index):
    """Read a user image only from the selected Claude session's transcript."""
    if config['agent'] != 'claude' or not isinstance(key, str) or not key:
        raise ValueError('That image is no longer available.')
    with open(config['transcript']) as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get('isSidechain') or record.get('isMeta') or record.get('type') != 'user':
                continue
            if record.get('uuid') == key:
                return decode_image(record.get('message'), index)
    raise ValueError('That image is no longer available.')


def decode_image(result, index):
    found = images(result)
    if not 0 <= index < len(found):
        raise ValueError('That image is no longer available.')
    try:
        return found[index]['media_type'], base64.b64decode(found[index]['data'], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError('That image is no longer available.') from exc


def find(config, identifier, *, allow_sidechain=False):
    if not identifier:
        raise ValueError('Choose a tool call.')
    call, result = None, None
    with open(config['transcript'], 'rb') as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            if config['agent'] == 'claude':
                if (record.get('isSidechain') and not allow_sidechain) or record.get('isMeta') or record.get('type') not in ('assistant', 'user'):
                    continue
                message = record.get('message')
                parts = message.get('content') if isinstance(message, dict) else None
                if not isinstance(parts, list):
                    continue
            elif config['agent'] == 'codex' and record.get('type') == 'response_item':
                parts = [record.get('payload') or {}]
            else:
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get('type') in ('tool_use', 'function_call', 'custom_tool_call') and str(part.get('call_id') or part.get('id') or '') == identifier:
                    call = part
                if part.get('type') in ('tool_result', 'function_call_output', 'custom_tool_call_output') and (part.get('tool_use_id') or part.get('call_id')) == identifier:
                    result = part
            if call is not None and result is not None:
                break
    if call is None:
        raise ValueError('Tool call is no longer available.')
    return call, result
