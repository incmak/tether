"""Session cron jobs (Claude Code CronCreate): a short name, the schedule in words, and the next run.

Claude Code reads a job's five cron fields in the local time zone of the machine it runs on, which is this server's
machine. It fires a recurring job up to a few minutes late (jitter), so the next run is approximate.
"""
import datetime
import re

FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # minute, hour, day of month, month, day of week (0 and 7 = Sunday)
DAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')


def parse_field(text, low, high):
    """The set of values one cron field allows, or None when the field is not plain cron syntax."""
    values = set()
    for part in text.split(','):
        match = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', part)
        if not match:
            return None
        if match[1] == '*':
            start, end = low, high
        else:
            start, _, end = match[1].partition('-')
            start = int(start)
            end = int(end) if end else (high if match[2] else start)
        step = int(match[2]) if match[2] else 1
        if not (low <= start <= end <= high) or step < 1:
            return None
        values.update(range(start, end + 1, step))
    return values


def parse(schedule):
    """[(minutes, hours, days, months, weekdays, day_any, weekday_any)] or None for anything but five plain fields."""
    fields = schedule.split() if isinstance(schedule, str) else []
    if len(fields) != 5:
        return None
    sets = [parse_field(field, low, high) for field, (low, high) in zip(fields, FIELDS)]
    if any(values is None for values in sets):
        return None
    sets[4] = {day % 7 for day in sets[4]}
    return (*sets, fields[2] == '*', fields[4] == '*')


def next_run(schedule, now):
    """The next minute the schedule allows after `now` (Unix seconds), in local time; None if none in five years.

    Times are compared as instants, so a clock change is safe: a wall time that happens twice (autumn) counts twice,
    and one the clock skips (spring) does not happen."""
    parsed = parse(schedule)
    if parsed is None:
        return None
    minutes, hours, days, months, weekdays, day_any, weekday_any = parsed
    after = (int(now) // 60 + 1) * 60  # the minute after the current one
    day = datetime.datetime.fromtimestamp(now).date()
    for _ in range(366 * 5):
        weekday = (day.weekday() + 1) % 7  # cron counts from Sunday
        # Standard cron: when both day fields are limited, either one may match.
        if day_any or weekday_any:
            day_ok = day.day in days and weekday in weekdays
        else:
            day_ok = day.day in days or weekday in weekdays
        if day.month in months and day_ok:
            found = [instant for hour in hours for minute in minutes for instant in moments(day, hour, minute) if instant >= after]
            if found:
                return min(found)
        day += datetime.timedelta(days=1)
    return None


def moments(day, hour, minute):
    """The instants a local wall time names on `day`: none when the clock skips it, two when it repeats."""
    wall = (day.year, day.month, day.day, hour, minute)
    instants = set()
    for fold in (0, 1):
        instant = datetime.datetime(*wall, fold=fold).timestamp()
        if datetime.datetime.fromtimestamp(instant).timetuple()[:5] == wall:
            instants.add(instant)
    return instants


def describe(schedule):
    """The schedule in words for the common shapes; the raw fields otherwise."""
    fields = schedule.split() if isinstance(schedule, str) else []
    if len(fields) != 5 or parse(schedule) is None:
        return schedule or ''
    minute, hour, day, month, weekday = fields
    if (day, month) != ('*', '*'):
        return schedule
    step = re.fullmatch(r'\*/(\d+)', minute)
    if hour == '*' and weekday == '*':
        if minute == '*':
            return 'Every minute'
        if step:
            return f'Every {step[1]} minutes'
        if re.fullmatch(r'\d+(,\d+)*', minute):
            marks = [f':{int(value):02d}' for value in minute.split(',')]
            return f"Every hour at {', '.join(marks[:-1]) + ' and ' + marks[-1] if len(marks) > 1 else marks[0]}"
    if re.fullmatch(r'\d+', minute) and re.fullmatch(r'\d+', hour):
        at = f'{int(hour):02d}:{int(minute):02d}'
        if weekday == '*':
            return f'Every day at {at}'
        if weekday == '1-5':
            return f'Weekdays at {at}'
        if re.fullmatch(r'[0-7]', weekday):
            return f'Every {DAYS[int(weekday) % 7]} at {at}'
    hourly = re.fullmatch(r'\*/(\d+)', hour)
    if re.fullmatch(r'\d+', minute) and hourly and weekday == '*':
        return f'Every {hourly[1]} hours at :{int(minute):02d}'
    return schedule


def label(prompt):
    """A short name for a job: the start of its prompt, up to the first bracket, sentence end or line end."""
    if not isinstance(prompt, str):
        return ''
    first = prompt.strip().split('\n', 1)[0]
    first = re.split(r'\s\(|(?<=[a-z0-9])\.\s|:\s', first, maxsplit=1)[0].strip().rstrip('.')
    return first if len(first) <= 60 else first[:59].rstrip() + '…'


def summary(crons, now):
    """The jobs as the UI shows them: id, name, schedule, its words, recurring, and the next run."""
    rows = []
    for item in crons if isinstance(crons, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            continue
        schedule = item.get('schedule', '')
        row = {'id': item['id'], 'label': label(item.get('prompt')) or item['id'], 'schedule': schedule,
               'every': describe(schedule), 'recurring': item.get('recurring', True)}
        upcoming = next_run(schedule, now)
        if upcoming is not None:
            row['next'] = upcoming
        rows.append(row)
    return rows
