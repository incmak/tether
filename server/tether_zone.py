"""Use the synced time zone, then the legacy TETHER_REMINDER_ZONE, then Asia/Kolkata. Offset-less stamps are
read in the current zone; after a zone change an old stamp shifts by the zone difference.
A stamp in a repeated hour reads back as the later occurrence. A stamp in a gap shifts forward by the gap.
"""
from datetime import datetime
from functools import lru_cache
import os
import time
from zoneinfo import ZoneInfo, available_timezones

if __package__:
    from . import prefs_sync
else:
    import prefs_sync

DEFAULT = 'Asia/Kolkata'
FORMAT = '%Y-%m-%d %H:%M'


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


def wall_times(wall, tz):
    """Unix seconds for a naive wall time, earliest first. In a gap, shift the time forward."""
    candidates = {wall.replace(tzinfo=tz, fold=fold).timestamp() for fold in (0, 1)}
    matches = sorted(at for at in candidates if datetime.fromtimestamp(at, tz).replace(tzinfo=None) == wall)
    return matches or [max(candidates)]


def parse(text, fmt=FORMAT):
    wall = datetime.strptime(text, fmt).replace(tzinfo=None)
    return wall_times(wall, zone())[-1]
