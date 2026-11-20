"""Reply to a helper's current question from its task's chat."""

import json
import os
import re
import unicodedata

import attention
import helper_tabs
import terminal_input

DIGEST = re.compile(r'[a-f0-9]{64}')
CHANGED = 'The helper’s question changed. Reload the task and review the question again.'


def _menu(info):
    prompt = info.get('attention') or {}
    if (info.get('state') != 'Input needed' or prompt.get('reason') != 'menu'
            or prompt.get('askBridge') or (prompt.get('ask_current') or {}).get('multi')
            or any(re.match(r'^\[(?: |✔|✓|x)\]', option['text'], re.I) for option in prompt.get('options', []))):
        return None
    return prompt


def _transcript_version(entry):
    try:
        stat = os.stat(entry['transcript'])
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return stat.st_size, stat.st_mtime_ns


def _menu_digest(entry, menu, content):
    if content:
        current = menu.get('ask_current') or {}
        return helper_tabs.digest(json.dumps(['menu', entry['id'], entry['thread'], content,
                                              current.get('index'), current.get('header'),
                                              _transcript_version(entry)]))
    return None


def _reply_digest(entry, reply_turn, listed=None):
    thread = helper_tabs.verified_thread(entry)
    ask, turn = attention.STORE.reply_identity(entry['id'], thread)
    if ask is None or listed is not None and (listed.get('since'), listed.get('next')) != (ask['since'], ask['next']):
        return None
    version = _transcript_version(entry)
    if version is None:
        return None
    reply, current_turn = reply_turn(entry)
    if current_turn != turn or attention.reply_ask(reply)[0] != 'yes':
        return None
    return helper_tabs.digest(json.dumps(['reply', entry['id'], thread, turn, ask['since'], ask['next'], version]))


def prompt_digest(entry, row, *, reply_turn):
    """Issue a digest only when a card can safely submit a choice or text."""
    if not entry.get('writable') or not helper_tabs.verified_thread(entry):
        return None
    menu = _menu(row)
    if menu is not None:
        return _menu_digest(entry, menu, terminal_input.menu_digest(menu))
    if row.get('state') == 'Idle' and (row.get('ask') or {}).get('kind') == 'reply':
        return _reply_digest(entry, reply_turn, row['ask'])
    return None


def answer(body, entry, run, status, send, *, reply_turn):
    """The caller verifies that this pane still holds the child conversation and holds the send lock."""
    digest = body.get('digest')
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError('Reload the task to get the current question.')
    text, choice = body.get('text'), body.get('choice')
    if (text is None) == (choice is None):
        raise ValueError('Enter an answer or choose one option.')
    if text is not None:
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 20000:
            raise ValueError('Enter an answer with 1 to 20000 characters.')
        if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in text):
            raise ValueError('The answer contains terminal control characters.')
    if choice is not None and type(choice) is not int:
        raise ValueError('Choose one of the displayed options.')
    if not entry.get('writable'):
        raise helper_tabs.Stale('The helper can no longer accept an answer. Open it to check.')

    current = status(entry)
    menu = _menu(current)
    if menu is not None:
        content = terminal_input.menu_digest(menu)
        if digest != _menu_digest(entry, menu, content):
            raise helper_tabs.Stale(CHANGED)
        if text is not None:
            options = [option for option in menu['options'] if option.get('free')]
            if len(options) != 1:
                raise ValueError('This question takes a choice. Open the helper to answer in the terminal.')
            choice = options[0]['id']
        else:
            options = [option for option in menu['options'] if option['id'] == choice and not option.get('free')
                       and option['text'] != 'Chat about this']
            if not options:
                raise ValueError('Choose one of the displayed options.')
        try:
            terminal_input.menu_choose(entry, choice, label=options[0]['text'], answer=text, run=run, digest=content)
        except terminal_input.MenuChanged as exc:
            raise helper_tabs.Stale(CHANGED) from exc
        except terminal_input.ConversationChanged as exc:
            raise helper_tabs.Stale(str(exc)) from exc
    else:
        if current.get('state') != 'Idle' or digest != _reply_digest(entry, reply_turn):
            raise helper_tabs.Stale(CHANGED)
        if text is None:
            raise ValueError('Enter an answer in the message box.')
        try:
            send(entry, text, 'Idle')
        except terminal_input.ConversationChanged as exc:
            raise helper_tabs.Stale(str(exc)) from exc
    return {'ok': True}
