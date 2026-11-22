"""Use the synced time zone, then the legacy TETHER_REMINDER_ZONE, then Asia/Kolkata. Exact stamps keep their
written offset. Offset-less stamps are read in the current zone, so a zone change shifts old stamps.
A stamp in a repeated hour reads back as the later occurrence. A stamp in a gap shifts forward by the gap.
"""
from datetime import date, datetime
from functools import lru_cache
import os
import re
import time
from zoneinfo import ZoneInfo, available_timezones

if __package__:
    from . import prefs_sync
else:
    import prefs_sync

DEFAULT = 'Asia/Kolkata'
FORMAT = '%Y-%m-%d %H:%M'
STAMP_FORMAT = '%Y-%m-%d %H:%M %z'
_OFFSET_SUFFIX = r' [+-][0-9]{2}:?[0-9]{2}'
STAMP_PATTERN = rf'[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}} [0-9]{{2}}:[0-9]{{2}}(?:{_OFFSET_SUFFIX})?'


@lru_cache(maxsize=1)
def _available():
    return available_timezones()


def valid(name):
    return isinstance(name, str) and 1 <= len(name) <= 64 and name in _available()


@lru_cache(maxsize=1)
def _preference(file, mtime_ns, size):
    return prefs_sync.load()['prefs'].get('timeZone')


def name():
    file = prefs_sync.FILE
    try:
        info = file.stat()
    except OSError:
        preferred = None
    else:
        preferred = _preference(file, info.st_mtime_ns, info.st_size)
    if valid(preferred):
        return preferred
    legacy = os.environ.get('TETHER_REMINDER_ZONE')
    return legacy if valid(legacy) else DEFAULT


def zone():
    return ZoneInfo(name())


def now(at=None):
    return datetime.fromtimestamp(time.time() if at is None else at, zone())


def stamp(at=None, fmt=FORMAT):
    return now(at).strftime(fmt)


def stamp_exact(at=None):
    return stamp(at, STAMP_FORMAT)


def wall_times(wall, tz):
    """Unix seconds for a naive wall time, earliest first. In a gap, shift the time forward."""
    candidates = {wall.replace(tzinfo=tz, fold=fold).timestamp() for fold in (0, 1)}
    matches = sorted(at for at in candidates if datetime.fromtimestamp(at, tz).replace(tzinfo=None) == wall)
    return matches or [max(candidates)]


def parse(text):
    if re.search(rf'{_OFFSET_SUFFIX}$', text):
        return datetime.strptime(text, STAMP_FORMAT).timestamp()
    wall = datetime.strptime(text, FORMAT)
    return wall_times(wall, zone())[-1]


def day_start(text_or_date, *, tz=None):
    """Unix seconds at the first instant of a local day, or the next instant if the day was skipped."""
    day = date.fromisoformat(text_or_date) if isinstance(text_or_date, str) else text_or_date
    if tz is None:
        tz = zone()
    midnight = datetime(day.year, day.month, day.day)
    at = int(midnight.replace(tzinfo=tz).timestamp())
    if datetime.fromtimestamp(at, tz).replace(tzinfo=None) == midnight:
        return at

    # A gap can start on the previous day. Find the transition between the two offset interpretations.
    before = int(midnight.replace(tzinfo=tz, fold=1).timestamp())
    while at - before > 1:
        mid = (before + at) // 2
        if datetime.fromtimestamp(mid, tz).date() < day:
            before = mid
        else:
            at = mid
    return at
