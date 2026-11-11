#!/usr/bin/env python3
"""Local photograph catalogue, Takeout importer and slideshow."""
import argparse
import logging
import random
import shutil
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from catalogue.database import connect
from catalogue.locking import writer_lock
from catalogue.metadata import ExifTool
from catalogue.scanner import scan, validate_root


def interval(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.isoformat(sep=' ', timespec='microseconds')
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use an ISO date/time such as 2026-10-27 14:00:00') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('photo_catalogue.sqlite3'), help='Database outside the photo root')
    parser.add_argument('--log', type=Path, default=Path('photo_catalogue.log'))
    commands = parser.add_subparsers(dest='command', required=True)
    scanning = commands.add_parser('scan')
    scanning.add_argument('root', type=Path)
    scanning.add_argument('--mount-point', type=Path, help='Require this exact volume mount (recommended for USB disks)')
    scanning.add_argument('--batch-size', type=int, default=64)
    scanning.add_argument('--exiftool', default='exiftool')
    scanning.add_argument('--no-progress', action='store_true')
    importing = commands.add_parser('import-takeout', help='Merge an extracted Google Takeout photo folder')
    importing.add_argument('source', type=Path)
    importing.add_argument('root', type=Path, nargs='?', default=Path('/run/media/martin/Popeye'), help='Destination archive root')
    importing.add_argument('--mount-point', type=Path)
    importing.add_argument('--dry-run', action='store_true', help='Preview using a temporary catalogue snapshot')
    importing.add_argument('--report', type=Path, default=None, help='New CSV report outside the photo trees')
    importing.add_argument('--batch-size', type=int, default=64)
    importing.add_argument('--exiftool', default='exiftool')
    importing.add_argument('--no-progress', action='store_true')
    commands.add_parser('stats')
    commands.add_parser('random')
    viewing = commands.add_parser('slideshow', help='Serve a browser slideshow alongside an active scan')
    viewing.add_argument('--host', default='127.0.0.1')
    viewing.add_argument('--port', type=int, default=8765)
    viewing.add_argument('--interval', type=int, default=10, help='Seconds per photograph (1–3600)')
    listing = commands.add_parser('list')
    listing.add_argument('--from', dest='start', type=interval, required=True)
    listing.add_argument('--to', dest='end', type=interval, required=True)
    listing.add_argument('--time-basis', choices=['chronological', 'local', 'utc'], default='chronological')
    args = parser.parse_args(argv)
    db = None
    resources = ExitStack()
    try:
        if args.command in ('scan', 'import-takeout'):
            root = validate_root(args.root, args.mount_point)
            source_root = validate_root(args.source) if args.command == 'import-takeout' else None
            for output in (args.db, args.log):
                if output.resolve().is_relative_to(root) or (source_root and output.resolve().is_relative_to(source_root)):
                    raise ValueError('Database and log must be outside the source and destination photo trees')
            if not shutil.which(args.exiftool):
                raise ValueError('ExifTool is required: sudo apt install libimage-exiftool-perl')
        if args.command != 'scan' and not args.db.exists():
            raise ValueError('No catalogue found; run scan first or select --db')
        logging.basicConfig(filename=args.log, level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
        if args.command == 'slideshow':
            from catalogue.slideshow import serve
            serve(args.db, args.host, args.port, args.interval)
            return 0
        if args.command in ('scan', 'import-takeout'):
            resources.enter_context(writer_lock(args.db))
        if args.command == 'import-takeout' and args.dry_run:
            db = sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
        else:
            db = connect(args.db)
        if args.command == 'import-takeout':
            from catalogue.importer import import_takeout
            report = args.report or Path('takeout-import-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.csv')
            counts = import_takeout(db, args.source, root, ExifTool(args.exiftool), report,
                                    args.dry_run, args.batch_size, not args.no_progress, args.mount_point)
            return 1 if counts.errors else 0
        if args.command == 'scan':
            c = scan(db, root, ExifTool(args.exiftool), args.batch_size, not args.no_progress, args.mount_point)
            print(f'Directories: {c.directories:,} | Discovered: {c.discovered:,} | Catalogued: {c.catalogued:,} | '
                  f'Unchanged: {c.skipped:,} | Errors: {c.errors:,}')
            return 1 if c.errors else 0
        if args.command == 'stats':
            row = db.execute('SELECT COUNT(*) AS photos, COALESCE(SUM(file_size),0) AS bytes, '
                             'COUNT(captured_local) AS dated, MIN(chronological_at) AS earliest, '
                             'MAX(chronological_at) AS latest FROM photos').fetchone()
            print(f"Archives: {db.execute('SELECT COUNT(*) FROM archives').fetchone()[0]:,}\n"
                  f"Photographs: {row['photos']:,}\nSource bytes: {row['bytes']:,}\n"
                  f"With capture time: {row['dated']:,}\nEarliest: {row['earliest'] or '—'}\nLatest: {row['latest'] or '—'}")
        elif args.command == 'random':
            # A uniform random offset handles sparse/deleted IDs without bias.
            count = db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
            if not count:
                print('Catalogue is empty.', file=sys.stderr)
                return 1
            print(db.execute('SELECT full_path FROM photos ORDER BY id LIMIT 1 OFFSET ?',
                             (random.randrange(count),)).fetchone()[0])
        elif args.command == 'list':
            if args.start > args.end:
                raise ValueError('--from must be no later than --to')
            column = {'chronological': 'chronological_at', 'local': 'captured_local', 'utc': 'captured_utc'}[args.time_basis]
            for row in db.execute(f'SELECT full_path,{column} FROM photos WHERE {column}>=? AND {column}<=? '
                                  f'ORDER BY {column},id', (args.start, args.end)):
                print(f'{row[1]}\t{row[0]}')
        return 0
    except KeyboardInterrupt:
        if db:
            db.commit()
        print('\nInterrupted. Completed work is saved; rerun scan to resume.', file=sys.stderr)
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    finally:
        if db:
            db.close()
        resources.close()


if __name__ == '__main__':
    sys.exit(main())
