"""The backlog on the work board. BACKLOG.md stays the record (backlog.py): the board reads its items as cards and
moves them between Ready, Building and Done through backlog.set_stage. Nothing goes to the work item store."""
import re

import backlog
from projects import ProjectError

PROJECT = 'Backlog'
REF = re.compile(r'[Bb]-?[0-9]{1,6}')


def is_ref(item_id):
    """True for a backlog ID like B12 or B-12. Work item prefixes never use B (work_items.RESERVED)."""
    return isinstance(item_id, str) and bool(REF.fullmatch(item_id.strip()))


def _card(card):
    return {**card, 'project': PROJECT}


def cards():
    """{'items', 'error'} for the board. A backlog that cannot be read leaves the rest of the board as it is."""
    try:
        return {'items': [_card(c) for c in backlog.board_items()], 'error': ''}
    except ProjectError as exc:
        return {'items': [], 'error': str(exc)}
    except (OSError, UnicodeError):
        return {'items': [], 'error': 'Tether could not read the backlog.'}


def stage(item_id, to, version, who='Mak'):
    return _card(backlog.set_stage(item_id.strip(), to, version, who))
