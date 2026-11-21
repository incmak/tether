"""Read Tether's audit log directly, without contacting the server."""
import argparse
from datetime import date
import json
import sys

import audit
import tether_zone


def local_day(value):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return tether_zone.parse(value, '%Y-%m-%d')
    except (ValueError, OverflowError, OSError):
        pass
    raise argparse.ArgumentTypeError('Use a local date in YYYY-MM-DD format.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='show recent changes')
    listing.add_argument('--kind', help='a kind or group, or a comma-separated list')
    listing.add_argument('--project')
    listing.add_argument('--actor')
    listing.add_argument('--since', type=local_day)
    listing.add_argument('--until', type=local_day)
    listing.add_argument('--text')
    listing.add_argument('--limit', type=int, default=30)
    listing.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = audit.query(text=args.text, kinds=[kind.strip() for kind in (args.kind or '').split(',') if kind.strip()],
                             project=args.project, actor=args.actor, since=args.since, until=args.until, limit=args.limit)
    except ValueError as exc:
        listing.error(str(exc))
    except OSError:
        print('Tether could not read the audit log.', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif not result['entries']:
        print('Nothing recorded.')
    else:
        for entry in result['entries']:
            when = tether_zone.stamp(entry['at'], '%Y-%m-%d %H:%M:%S')
            actor = entry['actor']
            source, name = actor['source'], audit.actor_name(actor)
            label = f'{name} ({source})' if name != source else source
            line = f'{when}  {entry["kind"]}  {entry["summary"]}  · {label}'
            print(''.join(char if char.isprintable() else ' ' for char in line))
    return 0


if __name__ == '__main__':
    sys.exit(main())
